"""3D-моделі робіт для табло цеху (власник 11–12.11.26).

Плитка верстата на телевізорі покаже справжні STL робіт, що зараз на шпинделі
(мокап `design/tv-board-concept.html`, варіант А «велика й малі»). Цей модуль —
серверна половина: файли роботи → положення → спрощення → готовий шматок у
кеші на диску. Сторінка табло тільки бере готове за ключем.

Правила, звірені на бойових STL 12.11.26 (`kmill_stl_shapes`, ~40 файлів):

* Положення «як у файлі» правильне майже для всього: абатменти, гвинтові
  коронки, мости, каркаси, капи, штифтові вкладки. Поворот за краєм
  (`stl_shapes.upright`) їх перевертав або клав на бік (канал гвинта читався
  як край). Під нахилом у файлі бувала лише ОДИНОЧНА коронка (`crown`/`waxup`)
  — тільки їй і даємо поворот, і тільки коли він робить її помітно нижчою.
  Файли не з Exocad не повертаємо: абатмент там не відрізнити від коронки.
* Мости й капи важать 107–252 тис. трикутників, а телевізору потрібно до
  ~60 тис. для великої моделі й ~10 тис. для малої — спрощуємо злипанням
  вершин по клітинці (те саме, що в мокапі), клітинку підбираємо під ціль.
* Незамкнена модель (`waxup`, 787–1210 ребер без пари) йде з позначкою
  `closed: false`: залити стінку на зрізі трафаретом на ній не вийде.

Що віддаємо назовні: лише ключ (хеш) і форму. Назви файлів, тек і клієнтів —
ні: у них імена пацієнтів. Ключ змінюється разом із файлом (розмір, час
зміни), тож браузер може тримати модель у кеші скільки завгодно.

Важка робота (читання STL, numpy) — лише у фоновому воркері
(`warm_once`); запит табло читає пам'ять (`models_for`), і мережева шара чи
великий міст не затримають сторінку на телевізорі.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePath
from typing import Any, Iterable

import numpy as np

from app import log_throttle

logger = logging.getLogger(__name__)

FORMAT = 1
"""Версія формату шматка. Зміна алгоритму — +1, і старий кеш стає чужим."""

BIG_TRIS = 60_000
"""Велика (поточна) модель — до стількох трикутників (власник 11.11.26)."""
LO_TRIS = 10_000
"""Мала модель у ряду під великою."""
WIRE_EDGES = 12_000
"""Сітка великої моделі (верхня, ще не вирізана частина)."""
SMALL_EDGES = 2_500
"""Рідка сітка малої моделі."""

SINGLE_UNIT_MM = 14.0
"""Найбільший габарит одиночної коронки; міст ширший (звірка 12.11.26)."""
ROTATE_GAIN = 0.85
"""Поворот приймається, лише якщо робить модель нижчою хоча б на 15 %."""
ROTATABLE_TYPES = {"crown", "waxup"}

MAX_MODEL_MM = 80.0
"""Більший габарит — не робота (модель щелепи, сцена). Капа — до ~65 мм."""
MAX_HEIGHT_MM = 40.0
"""Найменший габарит понад це — теж не робота (те саме, що мірка висоти)."""
MAX_FILES_PER_WORK = 48
_NOT_WORK = re.compile(r"model|antag|gingiv|jaw|scan|модел|щелеп", re.IGNORECASE)

PRELOAD_DAYS = 2
"""Наперед готуємо роботи з Sum3D за стільки днів — їх фрезеруватимуть."""
PASS_BUDGET_SECONDS = 10.0
"""Прохід раз на 30 с займає не більше третини: у тому ж процесі опитуються
верстати, а numpy тримає GIL частину часу (0.21.46: потік Tk у процесі сервера
вже гальмував табло). Коронка збирається ~1.5–4 с, міст ~4 с."""
CACHE_KEEP_DAYS = 10
CACHE_MAX_BYTES = 600 * 1_048_576
_KEY = re.compile(r"[0-9a-f]{24}")

# ── Налаштування показу (власник 12.11.26: «меню, що тонко налаштовує
# відображення, швидкість і вмикає/вимикає об'єкти») ──────────────────────
THREE_D_KEY = "shop_board_3d"


@dataclass(frozen=True)
class Field3D:
    key: str
    label: str
    hint: str
    default: float | bool
    low: float = 0.0
    high: float = 0.0
    step: float = 0.0
    unit: str = ""
    group: str = "3d"  # "power" — смуга «Світло», а не 3D (окремий ряд у формі)

    @property
    def is_switch(self) -> bool:
        return isinstance(self.default, bool)


THREE_D_FIELDS: tuple[Field3D, ...] = (
    Field3D("on", "3D на плитках", "Вимкнено — плитки як до 3D, моделі у фоні не готуються.", True),
    Field3D("hero", "Велика модель", "Конструкція, що «фрезерується» зараз, крупно.", True),
    Field3D("small", "Малі моделі", "Усі конструкції програми рядами під великою.", True),
    Field3D("mesh", "Сітка над зрізом", "Ще не вирізана частина великої моделі — сіткою.", True),
    Field3D("spin", "Обертання", "Велика модель повільно обертається.", True),
    Field3D("big", "Розмір великої", "", 1.0, 0.5, 2.0, 0.05, "×"),
    Field3D("smallk", "Розмір малих", "", 1.0, 0.5, 2.0, 0.05, "×"),
    Field3D("zone", "Висота зони 3D", "Більше — зона росте вгору плитки.", 1.0, 0.6, 2.5, 0.05, "×"),
    Field3D("sim", "Секунд на конструкцію", "Скільки триває «фрезерування» однієї.", 5.0, 2.0, 30.0, 0.5, "с"),
    Field3D("hold", "Пауза готовою", "Скільки готова конструкція стоїть цілою.", 1.2, 0.0, 10.0, 0.1, "с"),
    Field3D("spin_s", "Секунд на один оберт", "", 12.0, 4.0, 60.0, 1.0, "с"),
    Field3D("bright", "Яскравість 3D", "Експозиція моделей: менше — темніші, більше — світліші.",
            1.0, 0.4, 2.0, 0.05, "×"),
    # Висота смуги «Світло» в шапці табло (власник 12.11.26): клітинки, цифри
    # годин і сама шапка разом. Не 3D, але той самий запис і те саме прев'ю.
    Field3D("pw_k", "Висота смуги «Світло»", "Клітинки, цифри годин і шапка табло разом.",
            1.0, 0.6, 2.0, 0.05, "×", "power"),
)


def clean_3d(raw: dict[str, Any]) -> dict[str, Any]:
    """Значення з форми чи бази → повний набір у межах. Невідоме — типове.
    Перемикач у формі: присутній = увімкнено (`clean_3d` з форми кличуть із
    `form_keys`, де відсутні галочки вже False)."""
    out: dict[str, Any] = {}
    for f in THREE_D_FIELDS:
        value = raw.get(f.key, f.default)
        if f.is_switch:
            out[f.key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on")
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(f.default)
        if number != number:  # NaN
            number = float(f.default)
        out[f.key] = round(min(f.high, max(f.low, number)), 3)
    return out


def read_3d(db) -> dict[str, Any]:
    """Налаштування 3D з бази; зіпсований запис — типові, а не помилка табло."""
    from app.settings_store import get_setting

    text = get_setting(db, THREE_D_KEY) if db is not None else None
    try:
        stored = json.loads(text) if text else {}
    except ValueError:
        logger.warning("3D табло: налаштування не читаються, беру типові")
        stored = {}
    return clean_3d(stored if isinstance(stored, dict) else {})


AS_FILE = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])
"""Z угору (CAD) → Y угору (табло). Той самий, що `stl_shapes.AS_FILE`."""


def cache_dir() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "board_models"


# ── вибір файлів роботи ────────────────────────────────────────────────────


def work_stl_files(folder: Path) -> list[Path]:
    """STL-файли самої роботи в її теці й на рівень глибше.

    Є експорт Exocad (`…_cad.stl`) — беремо лише його: це і є конструкції, а
    поруч можуть лежати моделі щелеп і скани. Немає — усі STL, крім тих, чия
    назва каже «модель/антагоніст/ясна/скан». Копія того самого файлу (та сама
    назва й розмір в іншій підтеці) не подвоює модель."""
    found: list[Path] = []
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".stl":
                found.append(entry)
            elif entry.is_dir():
                found.extend(s for s in entry.iterdir() if s.is_file() and s.suffix.lower() == ".stl")
    except OSError as exc:
        skipped = log_throttle.due("board_models.folder_unavailable")
        if skipped is not None:
            logger.info("3D табло: тека %s недоступна: %s (ще %d подібних відтоді)", folder, exc, skipped)
        return []
    cad = [p for p in found if p.name.lower().endswith("_cad.stl")]
    chosen = cad or [p for p in found if not _NOT_WORK.search(PurePath(p.name).stem)]
    seen: set[tuple[str, int]] = set()
    unique: list[Path] = []
    for path in sorted(chosen, key=lambda p: p.name.lower()):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        mark = (path.name.lower(), size)
        if mark in seen:
            continue
        seen.add(mark)
        unique.append(path)
    return unique[:MAX_FILES_PER_WORK]


def units_in(filename: str) -> int:
    """Скільки одиниць несе файл: зуби з назви, капа — 14. Без зубів — 1."""
    from app.services.stl_units import count_units

    return max(1, count_units([filename]).units)


# ── геометрія ──────────────────────────────────────────────────────────────


def _weld(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    uniq, inv = np.unique(np.round(tris.reshape(-1, 3), 4), axis=0, return_inverse=True)
    return uniq, inv.reshape(-1, 3)


def open_edges(idx: np.ndarray, n_verts: int) -> int:
    """Ребра без пари в протилежному напрямку (numpy, без множин Python:
    на капі 250 тис. трикутників множина кортежів коштувала б секунди)."""
    a = np.concatenate([idx[:, 0], idx[:, 1], idx[:, 2]]).astype(np.int64)
    b = np.concatenate([idx[:, 1], idx[:, 2], idx[:, 0]]).astype(np.int64)
    fwd = np.unique(a * n_verts + b)
    back = np.unique(b * n_verts + a)
    return int(len(fwd) - np.isin(fwd, back, assume_unique=True).sum())


def pick_pose(tris: np.ndarray, filename: str) -> tuple[np.ndarray, str]:
    """Поворот моделі для табло і його назва («файл» / «поворот»).

    Правило з бойових STL (12.11.26): «як у файлі», крім одиночної коронки
    Exocad, яку поворот за краєм робить нижчою хоча б на 15 %."""
    from app.services import stl_shapes
    from app.services.stl_units import exocad_type

    flat = tris.reshape(-1, 3)
    dims = flat.max(0) - flat.min(0)
    if exocad_type(filename) not in ROTATABLE_TYPES or max(dims[0], dims[1]) >= SINGLE_UNIT_MM:
        return AS_FILE, "файл"
    up = stl_shapes.upright(tris)
    if not up.rim_points:
        return AS_FILE, "файл"
    height = float(np.ptp(flat @ up.rotation[1]))
    if height <= ROTATE_GAIN * float(dims[2]):
        return up.rotation, "поворот"
    return AS_FILE, "файл"


def _cluster(uniq: np.ndarray, idx: np.ndarray, cell: float):
    """Злипання вершин по клітинці `cell` мм: центри клітинок, трикутники між
    різними клітинками і їхні ребра (як у мокапі)."""
    ck = np.floor(uniq / cell).astype(np.int64)
    _, cid = np.unique(ck, axis=0, return_inverse=True)
    cid = cid.reshape(-1)
    cent = np.zeros((int(cid.max()) + 1, 3))
    np.add.at(cent, cid, uniq)
    cent /= np.bincount(cid)[:, None]
    ct = cid[idx]
    ct = ct[(ct[:, 0] != ct[:, 1]) & (ct[:, 1] != ct[:, 2]) & (ct[:, 0] != ct[:, 2])]
    edges = np.unique(np.sort(np.concatenate([ct[:, [0, 1]], ct[:, [1, 2]], ct[:, [0, 2]]]), axis=1), axis=0)
    return cent, ct, edges


def _cluster_to(uniq, idx, start: float, limit: int, what: str):
    """Найдрібніша клітинка, з якою трикутників (`what="tris"`) або ребер
    (`"edges"`) не більше за `limit`."""
    cell = start
    for _ in range(24):
        cent, ct, edges = _cluster(uniq, idx, cell)
        count = len(ct) if what == "tris" else len(edges)
        if count <= limit:
            return cent, ct, edges
        cell *= 1.3
    return cent, ct, edges


def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def build_model(path: Path) -> dict[str, Any] | None:
    """Шматок для табло з одного STL або None (не читається / не робота).

    Формат той самий, що в мокапі (`design/tv-board-concept.js`): координати
    int16 у частках `S`, індекси u2/u4, base64. `pos`/`idx` — велика модель,
    `wire` — її сітка, `lo`/`loi` — мала, `small` — сітка малої."""
    from app.services import stl_shapes

    tris = stl_shapes.read_stl(path)
    if tris is None or not len(tris):
        return None
    flat = tris.reshape(-1, 3)
    dims = np.sort(flat.max(0) - flat.min(0))
    if dims[2] > MAX_MODEL_MM or dims[0] > MAX_HEIGHT_MM or dims[2] <= 0:
        return None
    rotation, pose = pick_pose(tris, path.name)
    v = flat @ rotation.T
    v -= (v.min(0) + v.max(0)) / 2
    uniq, idx = _weld(v.reshape(-1, 3, 3))
    closed = open_edges(idx, len(uniq)) == 0
    if len(idx) > BIG_TRIS:
        big_pos, big_idx, _ = _cluster_to(uniq, idx, 0.05, BIG_TRIS, "tris")
    else:
        big_pos, big_idx = uniq, idx
    wire_pos, _, wire_e = _cluster_to(uniq, idx, 0.55, WIRE_EDGES, "edges")
    lo_pos, lo_idx, _ = _cluster_to(uniq, idx, 0.3, LO_TRIS, "tris")
    sm_pos, _, sm_e = _cluster_to(uniq, idx, 1.0, SMALL_EDGES, "edges")
    scale = float(np.abs(uniq).max()) or 1.0

    def q(p: np.ndarray) -> np.ndarray:
        return np.round(p / scale * 32767).astype("<i2")

    it = "<u2" if len(big_pos) < 65536 else "<u4"
    lo_it = "<u2" if len(lo_pos) < 65536 else "<u4"
    return {
        "v": FORMAT,
        "S": scale,
        "n": int(len(big_pos)),
        "it": it,
        "pos": _b64(q(big_pos)),
        "idx": _b64(big_idx.astype(it)),
        "wire": _b64(q(wire_pos[wire_e].reshape(-1, 3))),
        "lit": lo_it,
        "lo": _b64(q(lo_pos)),
        "loi": _b64(lo_idx.astype(lo_it)),
        "small": _b64(q(sm_pos[sm_e].reshape(-1, 3))),
        "box": [[float(x) for x in uniq.min(0)], [float(x) for x in uniq.max(0)]],
        "closed": closed,
        "pose": pose,
        "tris": int(len(big_idx)),
        "lo_tris": int(len(lo_idx)),
        "units": units_in(path.name),
    }


# ── кеш на диску ───────────────────────────────────────────────────────────


def cache_key(path: Path) -> str | None:
    """Ключ шматка: формат + шлях + розмір + час зміни. Назви не несе."""
    try:
        stat = path.stat()
    except OSError:
        return None
    raw = f"{FORMAT}|{str(path).lower()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def cached_file(key: str) -> Path | None:
    """Готовий шматок за ключем або None. Ключ — лише 24 hex-символи, тож
    вийти за теку кешу ним неможливо."""
    if not _KEY.fullmatch(key or ""):
        return None
    path = cache_dir() / f"{key}.json.gz"
    return path if path.is_file() else None


def _write_cached(key: str, payload: dict[str, Any]) -> None:
    folder = cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{key}.json.gz"
    tmp = folder / f"{key}.{os.getpid()}.tmp"
    tmp.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6))
    os.replace(tmp, target)


def ensure_model(path: Path) -> tuple[str, dict[str, Any]] | None:
    """(ключ, короткий опис) для файлу — з кешу або щойно зібраний."""
    key = cache_key(path)
    if key is None:
        return None
    ready = cached_file(key)
    if ready is not None:
        try:
            os.utime(ready)  # свіжість для прибирання
            meta = _meta_cache.get(key)
            if meta is None:
                payload = json.loads(gzip.decompress(ready.read_bytes()))
                meta = _meta(payload)
                _meta_cache[key] = meta
            return key, meta
        except (OSError, ValueError) as exc:
            logger.info("3D табло: кеш %s не прочитався (%s) — збираю заново", key, exc)
    payload = build_model(path)
    if payload is None:
        return None
    _write_cached(key, payload)
    meta = _meta(payload)
    _meta_cache[key] = meta
    return key, meta


def _meta(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: payload.get(k) for k in ("units", "closed", "pose", "tris", "lo_tris")}


_meta_cache: dict[str, dict[str, Any]] = {}


def prune_cache(*, now: float | None = None) -> int:
    """Прибрати шматки, яких не торкались `CACHE_KEEP_DAYS`, і найстаріші понад
    межу розміру. Повертає, скільки стерто."""
    folder = cache_dir()
    if not folder.is_dir():
        return 0
    moment = now if now is not None else time.time()
    entries = []
    for path in folder.glob("*.json.gz"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    entries.sort(reverse=True)  # найсвіжіші першими
    removed = 0
    total = 0
    for mtime, size, path in entries:
        total += size
        if moment - mtime > CACHE_KEEP_DAYS * 86400 or total > CACHE_MAX_BYTES:
            try:
                path.unlink()
                removed += 1
                _meta_cache.pop(path.name.split(".")[0], None)
            except OSError as exc:
                logger.info("3D табло: не стер %s: %s", path.name, exc)
    return removed


# ── індекс у пам'яті: робота → моделі ──────────────────────────────────────


@dataclass
class WorkModels:
    signature: str
    models: list[dict[str, Any]] = field(default_factory=list)  # key + meta
    checked_at: float = 0.0


RECHECK_SECONDS = 300.0
"""Теку роботи, що ще не на верстаті, переглядаємо не частіше: їх до сотні,
і кожен перегляд — звернення до мережевої шари."""


_index: dict[int, WorkModels] = {}
_index_lock = threading.Lock()


def models_for(orders: Iterable[Any], token: str, base: str | None = None) -> list[dict[str, Any]]:
    """Моделі робіт для плитки — ЛИШЕ з пам'яті, без жодного файлу.

    Порядок фіксований (за id роботи, далі як лежать файли), повтор ключа
    (дві роботи в одній теці) не подвоює модель."""
    with _index_lock:
        index = dict(_index)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order in sorted(orders, key=lambda o: getattr(o, "id", 0) or 0):
        order_id = getattr(order, "id", None)
        work = index.get(order_id) if isinstance(order_id, int) else None
        if work is None:
            continue
        for model in work.models:
            if model["key"] in seen:
                continue
            seen.add(model["key"])
            out.append({
                "url": f"{base or f'/t/{token}/model/'}{model['key']}.json",
                "units": model.get("units") or 1,
                "closed": bool(model.get("closed")),
            })
    return out


def preview_models(base: str, limit: int = 26) -> list[dict[str, Any]]:
    """Моделі для прев'ю в Налаштуваннях, коли жоден верстат зараз не
    фрезерує: робота з найбільшою кількістю готових моделей (з пам'яті)."""
    with _index_lock:
        works = [w for w in _index.values() if w.models]
    if not works:
        return []
    best = max(works, key=lambda w: len(w.models))
    return [{"url": f"{base}{m['key']}.json", "units": m.get("units") or 1,
             "closed": bool(m.get("closed"))} for m in best.models[:limit]]


def _prepare(order, tech_root: str | None, export_root: Path | None, deadline: float,
             *, on_machine: bool = False) -> bool:
    """Підготувати моделі однієї роботи. False — не встигли (час проходу)."""
    from app.services.stl_height import folder_signature, still_settling, work_folder

    with _index_lock:
        known = _index.get(order.id)
    now = time.monotonic()
    if known is not None and not on_machine and now - known.checked_at < RECHECK_SECONDS:
        return True
    folder = work_folder(order, tech_root, export_root)
    if folder is None:
        return True
    files = work_stl_files(folder)
    signature = folder_signature(files)
    if known is not None and known.signature == signature:
        known.checked_at = now
        return True
    if not files:
        with _index_lock:
            _index[order.id] = WorkModels(signature, checked_at=now)
        return True
    if still_settling(files, datetime.now()):
        return True
    models = []
    for path in files:
        if time.monotonic() > deadline:
            return False
        started = time.monotonic()
        try:
            made = ensure_model(path)
        except (OSError, ValueError, MemoryError) as exc:
            logger.warning("3D табло: модель роботи %s не зібрана: %s", order.id, exc)
            made = None
        spent = time.monotonic() - started
        if spent > 5:
            logger.info("3D табло: модель роботи %s збиралась %.1f с", order.id, spent)
        if made is not None:
            key, meta = made
            models.append({"key": key, **meta})
    with _index_lock:
        _index[order.id] = WorkModels(signature, models, time.monotonic())
    return True


def _orders_to_prepare(db) -> tuple[list[Any], set[int]]:
    """Спершу роботи на шпинделях зараз, потім ті, що скоро туди підуть
    (Sum3D стоїть, свіжі, не в архіві). Друге — id робіт на шпинделях."""
    from sqlalchemy import select

    from app.models import Order
    from app.services.machines import snapshot

    first: list[Any] = []
    for card in snapshot(db):
        first.extend(getattr(card, "orders", None) or [])
    since = datetime.now() - timedelta(days=PRELOAD_DAYS)
    later = db.scalars(
        select(Order).where(
            Order.archived_at.is_(None),
            Order.sum3d_id.is_not(None),
            Order.sum3d_id != "",
            Order.created_at >= since,
        ).order_by(Order.created_at.desc())
    ).all()
    seen: set[int] = set()
    ordered = []
    for order in [*first, *later]:
        if order.id in seen:
            continue
        seen.add(order.id)
        ordered.append(order)
    return ordered, {o.id for o in first}


def warm_once(db, tech_root: str | None, export_root: Path | None,
              *, budget: float = PASS_BUDGET_SECONDS) -> int:
    """Один прохід воркера: готує моделі в межах `budget` секунд. Повертає,
    скільки робіт переглянуто. Роботи поза списком забуваються з пам'яті."""
    orders, on_machine = _orders_to_prepare(db)
    wanted = {o.id for o in orders}
    with _index_lock:
        for stale in [k for k in _index if k not in wanted]:
            del _index[stale]
    deadline = time.monotonic() + budget
    done = 0
    for order in orders:
        if not _prepare(order, tech_root, export_root, deadline,
                        on_machine=order.id in on_machine):
            break
        done += 1
    return done
