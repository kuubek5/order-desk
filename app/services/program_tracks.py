"""Слід різання програми верстата: де саме на диску ріже ця `.iso`.

Власник 14.11.26: на табло підсвічувати обриси, що фрезеруються ЗАРАЗ, і
«механізм має визначати точно». Найточніше джерело — сама програма: у G-коді
записані координати руху фрези в мм від центру диска (звірено на двох `.iso`
у теці Sum3D на проді). Проєкт `.cam` для цього ненадійний (`disc_link`, три
верстати — три різні відповіді).

Звідки програми. Теку виводу кожного верстата Sum3D тримає в
`<тека Sum3D>\\Machines.arl` (`DirPostProcess`). Старі верстати: `.iso` лягає в
`C:\\CAM-out\\…` на ПК із Sum3D ненадовго — потім верстат ПЕРЕНОСИТЬ її до себе
(власник 14.11.26). Тому слід знімається одразу після появи файлу й лежить у
власному кеші KuubMill (`DATA_DIR/program_tracks`), а не читається з теки під
час різання. Нові верстати пишуть у свою мережеву теку `…\\SmartControl\\Jobs`.

Лише читання, і через `shared_read.open_shared`: верстат мусить мати змогу
забрати чи стерти файл, поки ми його читаємо. У теки Sum3D і верстатів нічого
не пишеться. Назв об'єктів (`;Object:` — STL, там бувають прізвища) не
зберігаємо — лише їх кількість.

Що зберігаємо: клітинки 0,5 мм, де фреза різала прямо згори чи знизу
(B = 0, A = 0 або 180) на висоті диска. Нахилені проходи 5 осей у площину
диска напряму не лягають — їх пропускаємо: кожну роботу все одно обдирають з
обох боків без нахилу.

Звірка з диском (`match`) сама себе перевіряє: частка клітинок сліду, що
лягла на обриси `.blk`. Знак осей підбирається з чотирьох варіантів на бік,
але для обох боків РАЗОМ: роботу ріжуть згори й знизу в одному місці диска,
тож правильна пара знаків кладе обидва боки на ті самі обриси й накриває їх
цілком. Окремо кожен бік вибирати не можна (350i Loader 15.11.26): на щільному
диску задній бік «лягав на обриси» за будь-якого знака й лишався на чужій
роботі. Мало лягло або боки розійшлись — «ненадійно», і табло тоді нічого не
підсвічує: хибна підсвітка гірша за жодну.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from app import log_throttle
from app.shared_read import open_shared

logger = logging.getLogger(__name__)

CELL_MM = 0.5
# Вище/нижче цього фреза не ріже диск (найвищий диск 25 мм → ±12,5).
MAX_CUT_Z = 14.0
MAX_FILE_BYTES = 60 * 1024 * 1024
# Файл молодший за це ще може дописуватись.
SETTLE_SECONDS = 2.0
# Скільки днів назад дивитись у теку і скільки тримати слід у кеші.
RECENT_DAYS = 3
KEEP_DAYS = 14
MAX_FILES_PER_PASS = 4
FOLDER_BACKOFF_SECONDS = 600.0
FOLDERS_TTL_SECONDS = 300.0
PROGRAM_EXTS = (".iso", ".nc")
# Обрис «свій», коли слід покрив хоча б таку його частку (і не менше клітинок).
OWN_SHARE = 0.15
OWN_MIN_CELLS = 8
# Фреза обходить роботу ззовні на свій радіус і припуск.
NEAR_MM = 3.0
# Слід вважаємо надійним, коли стільки його лягло на обриси.
TRUST_SHARE = 0.85

_WORD = re.compile(rb"([GXYZAB])\s*(-?\d+(?:\.\d*)?|-?\.\d+)")
_TAIL = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$")
_FLIPS = ((1, 1), (1, -1), (-1, 1), (-1, -1))


# ── Теки програм із Sum3D ──────────────────────────────────────────────────


@dataclass(frozen=True)
class MachineFolder:
    machine: str
    path: str
    ext: str

    @property
    def is_network(self) -> bool:
        return self.path.startswith("\\\\")


def parse_machines(text: str) -> list[MachineFolder]:
    """Верстати з `Machines.arl`: назва, тека виводу, розширення програми."""
    out: list[MachineFolder] = []
    name = path = ext = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line == "<MACHINESTART>":
            name = path = ext = ""
        elif line.startswith("Machine="):
            name = line[8:].strip()
        elif line.startswith("DirPostProcess="):
            path = line[15:].strip().rstrip("\\")
        elif line.startswith("ExtPostProcess="):
            ext = line[15:].strip().lower()
        elif line == "<MACHINEEND>":
            if name and path:
                out.append(MachineFolder(name, path, ext or ".iso"))
            name = path = ext = ""
    return out


def machine_folders(install_paths: Iterable[str]) -> list[MachineFolder]:
    """Теки виводу з усіх `Machines.arl` встановлень Sum3D. Не знайдено — []."""
    out: list[MachineFolder] = []
    for root in install_paths:
        target = os.path.join(root, "Machines.arl")
        try:
            with open_shared(target) as handle:
                text = handle.read(2 * 1024 * 1024).decode("latin-1", "replace")
        except OSError as exc:
            skipped = log_throttle.due(f"program-tracks-arl:{target}")
            if skipped is not None:
                logger.warning("Слід програми: %s не прочитано (%s; ще %d разів відтоді)",
                               target, exc.__class__.__name__, skipped)
            continue
        out.extend(parse_machines(text))
    return out


# ── Розбір програми ────────────────────────────────────────────────────────


@dataclass
class Track:
    name: str
    blank: str = ""
    objects: int = 0
    lines: int = 0
    # Клітинки (x, y) у пів-міліметрах для кожного боку: «0» і «180».
    sides: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    skipped_tilted: int = 0
    source: str = ""
    file_mtime: float = 0.0
    file_size: int = 0
    captured_at: str = ""

    @property
    def cells(self) -> int:
        return sum(len(v) for v in self.sides.values())


def parse_program(data: bytes, name: str = "") -> Track:
    """G-код → слід. Модальні G/X/Y/Z/A/B; береться лише різання (G1) без
    нахилу на висоті диска."""
    track = Track(name=name)
    x = y = z = None
    a = b = 0.0
    cutting = False
    cells: dict[str, set[tuple[int, int]]] = {"0": set(), "180": set()}
    for line in data.splitlines():
        track.lines += 1
        if not line:
            continue
        head = line[:1]
        if head == b";":
            low = line[:40].lower()
            if low.startswith(b";blank:"):
                track.blank = line[7:].strip().decode("latin-1", "replace")[:120]
            elif low.startswith(b";object:"):
                track.objects += 1
            continue
        if head not in b"GXYZAB":
            continue
        moved = False
        for word, value in _WORD.findall(line):
            number = float(value)
            if word == b"G":
                if number in (0.0, 1.0):
                    cutting = number == 1.0
            elif word == b"X":
                x, moved = number, True
            elif word == b"Y":
                y, moved = number, True
            elif word == b"Z":
                z, moved = number, True
            elif word == b"A":
                a = number
            elif word == b"B":
                b = number
        if not (moved and cutting) or x is None or y is None or z is None or abs(z) > MAX_CUT_Z:
            continue
        turn = a % 360.0
        if abs(b) > 0.01 or min(abs(turn), abs(turn - 180.0), abs(turn - 360.0)) > 0.01:
            track.skipped_tilted += 1
            continue
        side = "180" if abs(turn - 180.0) <= 0.01 else "0"
        cells[side].add((int(round(x / CELL_MM)), int(round(y / CELL_MM))))
    track.sides = {k: sorted(v) for k, v in cells.items() if v}
    return track


# ── Кеш слідів ─────────────────────────────────────────────────────────────


def cache_dir() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "program_tracks"


def track_key(program: Optional[str]) -> str:
    """Назва програми без розширення, малими — ключ кешу й пошуку."""
    stem = (program or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(iso|nc)$", "", stem, flags=re.I)
    return re.sub(r"[^a-z0-9._+()\-]", "_", stem.lower())[:180]


def save_track(track: Track, folder: Optional[Path] = None) -> Path:
    folder = folder or cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{track_key(track.name)}.json"
    payload = {
        "name": track.name, "blank": track.blank, "objects": track.objects, "lines": track.lines,
        "sides": track.sides, "skipped_tilted": track.skipped_tilted, "source": track.source,
        "file_mtime": track.file_mtime, "file_size": track.file_size, "captured_at": track.captured_at,
    }
    part = target.with_suffix(".part")
    part.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(part, target)
    return target


def _load(path: Path) -> Optional[Track]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    sides = {str(k): [(int(p[0]), int(p[1])) for p in v] for k, v in (raw.get("sides") or {}).items()}
    return Track(
        name=str(raw.get("name") or ""), blank=str(raw.get("blank") or ""),
        objects=int(raw.get("objects") or 0), lines=int(raw.get("lines") or 0), sides=sides,
        skipped_tilted=int(raw.get("skipped_tilted") or 0), source=str(raw.get("source") or ""),
        file_mtime=float(raw.get("file_mtime") or 0), file_size=int(raw.get("file_size") or 0),
        captured_at=str(raw.get("captured_at") or ""),
    )


def _track_paths(program: Optional[str], folder: Path) -> list[Path]:
    """Точна назва — один файл; гола `дата_час` — усі сліди з тим самим хвостом
    (250i-Tolik 16.11.26: той самий проєкт виведено двічі, `3_…` і `dd_…`)."""
    key = track_key(program)
    if not key:
        return []
    exact = folder / f"{key}.json"
    if exact.is_file():
        return [exact]
    tail = _TAIL.search(key.rstrip("_"))
    if tail is None:
        return []
    try:
        return sorted(folder.glob(f"*{tail.group(1)}*.json"))
    except OSError:
        return []


def _track_path(program: Optional[str], folder: Path) -> Optional[Path]:
    same = _track_paths(program, folder)
    return same[0] if len(same) == 1 else None


def find_track(program: Optional[str], folder: Optional[Path] = None) -> Optional[Track]:
    """Слід програми за назвою з екрана верстата. Точна назва, інакше — єдиний
    файл із тим самим хвостом «дата_час» (екран нового верстата ріже початок)."""
    path = _track_path(program, folder or cache_dir())
    return _load(path) if path is not None else None


# Назва й диск програми за голою `дата_час.iso` (250i-Tolik 15.11.26: літеру
# назви на екрані не впізнано, у проєкті Sum3D ні назви, ні диска — табло диск
# не малювало). Слід знято із САМОГО файлу програми: ім'я файлу і є повна
# назва, шапка `;Blank:` називає диск. Табло питає це на кожен рендер, тому
# відповідь тримаємо в пам'яті: знайдене — довго, «ще немає» — коротко (слід
# з'являється за секунди після того, як Sum3D дописав програму).
NAME_HIT_TTL_SECONDS = 600.0
NAME_MISS_TTL_SECONDS = 20.0
_names: dict[str, tuple[float, Optional[tuple[str, str]]]] = {}


def program_of(iso_name: Optional[str], folder: Optional[Path] = None) -> Optional[tuple[str, str]]:
    """(повна назва програми, диск із шапки) для назви з екрана, якщо слід цієї
    програми знято; інакше None. Кілька слідів з тим самим «дата_час» — None."""
    key = track_key(iso_name)
    if not key:
        return None
    folder = folder or cache_dir()
    slot = f"{folder}|{key}"
    now = time.monotonic()
    with _lock:
        cached = _names.get(slot)
    if cached is not None and now < cached[0]:
        return cached[1]
    found: Optional[tuple[str, str]] = None
    heads: list[tuple[str, str]] = []
    for path in _track_paths(iso_name, folder)[:4]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict) and str(raw.get("name") or "").strip():
            heads.append((str(raw["name"]).strip(), str(raw.get("blank") or "").strip()))
    if len(heads) == 1:
        found = heads[0]
    elif len(heads) > 1 and heads[0][1] and all(h[1].lower() == heads[0][1].lower() for h in heads):
        # Кілька слідів з тим самим «дата_час», але диск у шапці той самий
        # (Tolik 16.11.26: проєкт виведено двічі з різною позначкою). Диск
        # відомий; назву лишаємо голою — позначка на диску в них різна, і
        # вгадана була б хибною.
        found = (str(iso_name).strip(), heads[0][1])
    with _lock:
        if len(_names) > 300:
            _names.clear()
        _names[slot] = (now + (NAME_HIT_TTL_SECONDS if found else NAME_MISS_TTL_SECONDS), found)
    return found


# Підсвітка на табло: які обриси диска ріже програма (власник 14.11.26, вигляд
# A «Бурштин»). Звірка дорога (заливка обрисів у сітку), а табло питає на кожен
# рендер — відповідь пам'ятаємо за файлами сліду й диска з часом зміни: Sum3D
# зберіг диск або знято новий слід — рахуємо наново.
_cuts: dict[tuple, frozenset[int]] = {}


def cutting_contours(names: Iterable[Optional[str]], disc_path: Optional[Path],
                     folder: Optional[Path] = None) -> frozenset[int]:
    """Номери (з нуля, у порядку обрисів диска) обрисів, що ріже програма.

    Порожньо — не знаємо, і табло нічого не підсвічує: сліду немає, шапка
    програми називає ІНШИЙ диск, звірка ненадійна (`match`), або кілька слідів
    із тим самим «дата_час» показують різні обриси. Хибна підсвітка гірша
    за жодну."""
    from app.services import sum3d_disc

    if disc_path is None:
        return frozenset()
    folder = folder or cache_dir()
    paths: list[Path] = []
    for name in names:
        paths = _track_paths(name, folder)[:4]
        if paths:
            break
    if not paths:
        return frozenset()
    try:
        marks = tuple((str(p), p.stat().st_mtime_ns) for p in paths) + (
            (str(disc_path), disc_path.stat().st_mtime_ns),)
    except OSError:
        return frozenset()
    with _lock:
        cached = _cuts.get(marks)
    if cached is not None:
        return cached
    result: frozenset[int] = frozenset()
    shape = sum3d_disc.shape_of(disc_path)
    if shape is not None and shape.contours:
        disc_name = disc_path.stem.lower()
        answers: list[frozenset[int]] = []
        for path in paths:
            track = _load(path)
            if track is None or not track.sides:
                answers = []
                break
            blank = re.sub(r"\.blk$", "", track.blank.strip(), flags=re.I).lower()
            if blank and blank != disc_name:
                answers = []
                break
            report = match(track, shape.radius, shape.contours)
            if not report["надійно"]:
                answers = []
                break
            answers.append(frozenset(i - 1 for i in report["свої_обриси"]))
        if answers and all(a == answers[0] for a in answers):
            result = answers[0]
    with _lock:
        if len(_cuts) > 200:
            _cuts.clear()
        _cuts[marks] = result
    return result


def prune(folder: Optional[Path] = None, *, now: Optional[float] = None) -> int:
    folder = folder or cache_dir()
    limit = (now or time.time()) - KEEP_DAYS * 86400
    removed = 0
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return 0
    for path in entries:
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ── Збирач ─────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_folder_state: dict[str, dict[str, Any]] = {}
_backoff_until: dict[str, float] = {}
_folders_cache: tuple[float, list[MachineFolder]] = (0.0, [])
_last_prune = 0.0


def folder_status() -> dict[str, dict[str, Any]]:
    """Що збирач бачив у теках востаннє (для діагностики)."""
    with _lock:
        return {k: dict(v) for k, v in _folder_state.items()}


def known_folders(db) -> list[MachineFolder]:
    """Теки з `Machines.arl` плюс «Теки програм» з Налаштувань; кеш 5 хв."""
    global _folders_cache
    now = time.monotonic()
    with _lock:
        stamp, cached = _folders_cache
        if cached and now - stamp < FOLDERS_TTL_SECONDS:
            return cached
    from sqlalchemy import select

    from app.models import Machine
    from app.services.sum3d_install import read_installs

    found = machine_folders(i.path for i in read_installs(db))
    for name, path in db.execute(select(Machine.name, Machine.program_folder)).all():
        if (path or "").strip():
            found.append(MachineFolder(name, path.strip().rstrip("\\"), ".iso"))
    with _lock:
        _folders_cache = (now, found)
    return found


def _complete(path: str, size: int) -> bool:
    """Програма дописана: наприкінці є M30 (кінець програми)."""
    try:
        with open_shared(path) as handle:
            handle.seek(max(0, size - 400))
            return b"M30" in handle.read(400).upper()
    except OSError:
        return False


def collect_folder(path: str, *, target: Optional[Path] = None, now: Optional[float] = None,
                   budget: int = MAX_FILES_PER_PASS) -> int:
    """Один прохід теки: зняти слід зі свіжих програм, яких ще немає в кеші.
    Повертає, скільки знято. Теку лише читає."""
    target = target or cache_dir()
    moment = now or time.time()
    state: dict[str, Any] = {"перевірено": datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S")}
    try:
        with os.scandir(path) as it:
            entries = [(e.name, e.stat()) for e in it
                       if e.name.lower().endswith(PROGRAM_EXTS) and e.is_file()]
    except OSError as exc:
        state["помилка"] = exc.__class__.__name__
        with _lock:
            _folder_state[path] = state
            _backoff_until[path] = time.monotonic() + FOLDER_BACKOFF_SECONDS
        return 0
    state["програм"] = len(entries)
    entries.sort(key=lambda item: item[1].st_mtime, reverse=True)
    if entries:
        state["найновіша"] = datetime.fromtimestamp(entries[0][1].st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    taken = 0
    for name, stat in entries:
        if taken >= budget or moment - stat.st_mtime > RECENT_DAYS * 86400:
            break
        if moment - stat.st_mtime < SETTLE_SECONDS or not (0 < stat.st_size <= MAX_FILE_BYTES):
            continue
        cached = target / f"{track_key(name)}.json"
        try:
            if cached.is_file() and cached.stat().st_mtime >= stat.st_mtime:
                continue
        except OSError:
            pass
        full = os.path.join(path, name)
        if not _complete(full, stat.st_size):
            continue
        try:
            with open_shared(full) as handle:
                data = handle.read(MAX_FILE_BYTES)
        except OSError as exc:
            skipped = log_throttle.due(f"program-tracks-read:{path}")
            if skipped is not None:
                logger.warning("Слід програми: файл у %s не прочитано (%s; ще %d разів відтоді)",
                               path, exc.__class__.__name__, skipped)
            continue
        track = parse_program(data, name)
        track.source = path
        track.file_mtime, track.file_size = stat.st_mtime, stat.st_size
        track.captured_at = datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S")
        save_track(track, target)
        taken += 1
        logger.info("Слід програми: %s → %d клітинок (без нахилу), пропущено нахилених %d",
                    name, track.cells, track.skipped_tilted)
    state["знято_за_прохід"] = taken
    with _lock:
        _folder_state[path] = state
    return taken


_in_flight: set[str] = set()


def network_pass(db_factory) -> int:
    """Один прохід мережевих тек: кожна — своїм потоком, тож вимкнений ПК верстата
    (таймаут у десятки секунд) не затримує решти. 23.11.26: раз на 45 с по черзі —
    і SmartControl забирав файл раніше, 14 назв із 36 пропущено. Повертає, скільки
    тек почато перевіряти; попередню перевірку тієї ж теки не дублюємо."""
    with db_factory() as db:
        folders = {item.path.lower(): item.path for item in known_folders(db) if item.is_network}
    started = 0
    for key, path in folders.items():
        with _lock:
            if key in _in_flight or time.monotonic() < _backoff_until.get(path, 0.0):
                continue
            _in_flight.add(key)
        started += 1
        threading.Thread(target=_network_folder, args=(key, path),
                         name="kuubmill-tracks-net-folder", daemon=True).start()
    return started


def _network_folder(key: str, path: str) -> None:
    try:
        collect_folder(path)
    except Exception:  # noqa: BLE001 — одна тека не валить збирач
        if log_throttle.due(f"program-tracks-net:{path}") is not None:
            logger.exception("Слід програми: теку %s не перевірено", path)
    finally:
        with _lock:
            _in_flight.discard(key)


def collect_once(db, *, network: bool) -> int:
    """Прохід опитуванням по мережевих (або, у тестах, локальних) теках.
    Мережеві теки — окремим потоком: мертва висить на таймаутах і не має
    затримувати локальні, звідки верстат забирає файл. Локальні в роботі веде
    швидкий сторож (`watch_once`)."""
    taken = 0
    seen: set[str] = set()
    for item in known_folders(db):
        key = item.path.lower()
        if item.is_network != network or key in seen:
            continue
        seen.add(key)
        with _lock:
            wait = _backoff_until.get(item.path, 0.0)
        if time.monotonic() < wait:
            continue
        taken += collect_folder(item.path)
    return taken


# ── Швидкий сторож локальних тек ───────────────────────────────────────────
#
# Власник 14.11.26: «система може не встигати зчитати програму до того, як
# верстат перенесе файл». Тому локальні теки не опитуються раз на кілька
# секунд, а переглядаються двічі на секунду, і нова програма ОДРАЗУ
# відкривається на читання (ще недописана). Відкритий файл лишається
# читабельним і після того, як верстат його забрав: Windows прибирає ім'я з
# теки, а вміст живе, доки ми не закриємо. `open_shared` дозволяє іншим
# писати, перейменовувати й видаляти — верстату ми не заважаємо. Тримаємо
# рівно доки програма не дописана (M30) або не зникла з теки.

WATCH_MAX_HELD = 6
WATCH_GIVE_UP_SECONDS = 900.0
LOCAL_BACKOFF_SECONDS = 60.0


@dataclass
class _Held:
    handle: Any
    folder: str
    name: str
    first_seen: float
    mtime: float


_held: dict[str, _Held] = {}


def _read_fd(fd: int, start: int, size: int) -> bytes:
    os.lseek(fd, start, os.SEEK_SET)
    chunks: list[bytes] = []
    left = size
    while left > 0:
        chunk = os.read(fd, min(left, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _finish(held: _Held, target: Path, *, gone: bool, moment: float) -> bool:
    """Дочитати з відкритого файлу, зберегти слід, закрити. False — порожньо."""
    fd = held.handle.fileno()
    try:
        size = min(os.fstat(fd).st_size, MAX_FILE_BYTES)
        data = _read_fd(fd, 0, size)
    except OSError as exc:
        logger.warning("Слід програми: %s не дочитано (%s)", held.name, exc.__class__.__name__)
        data = b""
    finally:
        held.handle.close()
    if not data:
        return False
    track = parse_program(data, held.name)
    track.source = held.folder
    track.file_mtime, track.file_size = held.mtime, len(data)
    track.captured_at = datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S")
    save_track(track, target)
    logger.info("Слід програми: %s → %d клітинок%s", held.name, track.cells,
                f" (файл уже забрали з теки, прожив ≈{moment - held.first_seen:.1f} с)" if gone else "")
    return True


def watch_folder(path: str, *, target: Optional[Path] = None, now: Optional[float] = None) -> int:
    """Один перегляд локальної теки (кличеться двічі на секунду). Нові програми
    відкриває одразу; дописані або вже забрані — дочитує з відкритого файлу.
    Повертає, скільки слідів знято. Теку лише читає."""
    target = target or cache_dir()
    moment = now or time.time()
    state: dict[str, Any] = {"перевірено": datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S"),
                             "режим": "швидкий сторож"}
    present: dict[str, float] = {}
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.lower().endswith(PROGRAM_EXTS):
                    try:
                        present[entry.name] = entry.stat().st_mtime
                    except OSError:
                        continue  # файл зник між переліком і запитом — це і є «забрали»
    except OSError as exc:
        state["помилка"] = exc.__class__.__name__
        with _lock:
            previous = _folder_state.get(path) or {}
            state.update({k: v for k, v in previous.items() if k.startswith("останній_")})
            _folder_state[path] = state
            _backoff_until[path] = time.monotonic() + LOCAL_BACKOFF_SECONDS
        return 0
    state["програм"] = len(present)
    # 1. Спершу відкрити ВСЕ нове — це і є гонка з верстатом; розбір потім.
    for name, mtime in sorted(present.items(), key=lambda kv: kv[1], reverse=True):
        full = os.path.join(path, name)
        if full in _held or len(_held) >= WATCH_MAX_HELD or moment - mtime > RECENT_DAYS * 86400:
            continue
        cached = target / f"{track_key(name)}.json"
        try:
            if cached.is_file() and cached.stat().st_mtime >= mtime:
                continue
        except OSError:
            pass
        try:
            _held[full] = _Held(open_shared(full), path, name, moment, mtime)
        except OSError:
            continue  # зник або ще зайнятий — наступний перегляд за пів секунди
    # 2. Дочитати дописані й забрані.
    taken = 0
    for full, held in [(k, v) for k, v in _held.items() if v.folder == path]:
        gone = held.name not in present
        if not gone:
            try:
                os.stat(full)
            except OSError:
                gone = True  # видалення вже триває: ім'я ще в переліку, файл недоступний
        done = False
        if not gone:
            fd = held.handle.fileno()
            try:
                size = os.fstat(fd).st_size
                done = size > 0 and b"M30" in _read_fd(fd, max(0, size - 400), 400).upper()
            except OSError:
                gone = True
        if done or gone:
            del _held[full]
            if _finish(held, target, gone=gone, moment=moment):
                taken += 1
            if gone:
                state["останній_забраний_прожив_с"] = round(moment - held.first_seen, 1)
                state["останній_забраний"] = held.name
        elif moment - held.first_seen > WATCH_GIVE_UP_SECONDS:
            del _held[full]
            held.handle.close()
            logger.warning("Слід програми: %s не дописано за %d хв — відпущено", held.name,
                           int(WATCH_GIVE_UP_SECONDS // 60))
    state["тримаємо_відкритими"] = sum(1 for v in _held.values() if v.folder == path)
    with _lock:
        previous = _folder_state.get(path) or {}
        for key, value in previous.items():
            if key.startswith("останній_") and key not in state:
                state[key] = value
        state["знято_всього"] = int(previous.get("знято_всього") or 0) + taken
        _folder_state[path] = state
    return taken


def release_all() -> None:
    """Закрити все, що тримаємо (зупинка застосунку, тести)."""
    for full in list(_held):
        held = _held.pop(full)
        try:
            held.handle.close()
        except OSError:
            logger.debug("Слід програми: %s уже закрито", held.name)


_local_cache: tuple[float, list[str]] = (0.0, [])


def local_folders(db_factory) -> list[str]:
    """Локальні теки для сторожа; у базу ходимо раз на 5 хв, не двічі на секунду."""
    global _local_cache
    now = time.monotonic()
    stamp, cached = _local_cache
    if stamp and now - stamp < FOLDERS_TTL_SECONDS:
        return cached
    with db_factory() as db:
        found = known_folders(db)
    seen: set[str] = set()
    paths = []
    for item in found:
        if not item.is_network and item.path.lower() not in seen:
            seen.add(item.path.lower())
            paths.append(item.path)
    _local_cache = (now, paths)
    return paths


def watch_once(db_factory) -> int:
    """Один перегляд усіх локальних тек (раз на пів секунди)."""
    global _last_prune
    taken = 0
    for path in local_folders(db_factory):
        with _lock:
            wait = _backoff_until.get(path, 0.0)
        if time.monotonic() >= wait:
            taken += watch_folder(path)
    if time.monotonic() - _last_prune > 3600:
        _last_prune = time.monotonic()
        prune()
    return taken


# ── Звірка з диском ────────────────────────────────────────────────────────


def match(track: Track, radius: float, contours: list[list[tuple[float, float]]]) -> dict[str, Any]:
    """Які обриси диска ріже ця програма і наскільки цьому можна вірити."""
    from app.services.disc_fit import _dilate, _fill_polygon

    n = int(math.ceil(2 * radius / CELL_MM))
    origin = -radius
    label = np.zeros((n, n), dtype=np.int32)
    areas: list[int] = []
    for index, poly in enumerate(contours):
        one = np.zeros((n, n), dtype=bool)
        _fill_polygon(one, poly, origin)
        areas.append(int(one.sum()))
        label[one] = index + 1
    grown = _dilate(label > 0, int(round(NEAR_MM / CELL_MM)))
    pad = (grown.shape[0] - n) // 2
    near = grown[pad: pad + n, pad: pad + n]

    # Кожен бік — чотири варіанти знака осей; для кожного рахуємо, скільки
    # клітинок лягло біля обрисів і в який саме обрис.
    slots = len(contours) + 1
    options: dict[str, list[tuple[tuple[int, int], int, np.ndarray]]] = {}
    for side, cells in track.sides.items():
        pts = np.array(cells, dtype=np.float64) * CELL_MM
        found = []
        for sx, sy in _FLIPS:
            col = np.floor((pts[:, 0] * sx - origin) / CELL_MM).astype(np.int64)
            row = np.floor((pts[:, 1] * sy - origin) / CELL_MM).astype(np.int64)
            ok = (col >= 0) & (col < n) & (row >= 0) & (row < n)
            hits = int(near[row[ok], col[ok]].sum())
            found.append(((sx, sy), hits, np.bincount(label[row[ok], col[ok]], minlength=slots)))
        options[side] = found
    # Пара знаків обирається РАЗОМ: найкраща та, де всі боки накривають ті самі
    # обриси й накривають їх повно (клітинки² / площа обрису — часткове
    # зачеплення кількох чужих обрисів дає мало). За рівності — де більше лягло.
    area = np.array([1.0] + [float(a) or 1.0 for a in areas])
    sides = list(options)
    chosen: tuple[tuple[tuple[int, int], int, np.ndarray], ...] = ()
    best_score: Optional[tuple[float, int]] = None
    for combo in itertools.product(*(options[side] for side in sides)):
        if not combo:
            continue
        shared = np.minimum.reduce([c[2] for c in combo]).astype(np.float64)
        shared[0] = 0.0
        score = (round(float((shared * shared / area).sum()), 6), sum(c[1] for c in combo))
        if best_score is None or score > best_score:
            best_score, chosen = score, combo
    counts = np.zeros(slots, dtype=np.int64)
    total = close = 0
    report_sides: dict[str, Any] = {}
    for side, (flip, hits, by_contour) in zip(sides, chosen):
        cells_n = len(track.sides[side])
        total += cells_n
        close += hits
        counts += by_contour
        report_sides[side] = {"клітинок": cells_n, "знак_xy": list(flip),
                              "лягло_на_обриси": round(hits / cells_n, 3) if cells_n else 0.0}
    # «Свій» обрис — накритий КОЖНИМ боком. Обрис, який зачепив лише один бік,
    # спірний: боки розійшлись, і такому сліду не віримо.
    own: list[int] = []
    disputed: list[int] = []
    for i, contour_area in enumerate(areas):
        need = max(OWN_MIN_CELLS, OWN_SHARE * contour_area)
        covered = [c[2][i + 1] >= need for c in chosen]
        if covered and all(covered):
            own.append(i + 1)
        elif any(covered):
            disputed.append(i + 1)
    share_all = close / total if total else 0.0
    return {
        "обрисів": len(contours),
        "свої_обриси": own,
        "спірні_обриси": disputed,
        "покриття_обрисів": {str(i + 1): round(float(counts[i + 1]) / a, 2) for i, a in enumerate(areas)
                             if a and counts[i + 1]},
        "клітинок_сліду": total,
        "лягло_на_обриси": round(share_all, 3),
        "надійно": bool(own) and not disputed and share_all >= TRUST_SHARE,
        "боки": report_sides,
    }
