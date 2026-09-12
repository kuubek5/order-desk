"""Скринька невідомих екранів: кадр, якого зчитувач не зрозумів.

**Навіщо.** Читач мовчить при найменшому сумніві — цифра або збігається з
еталоном піксель-у-піксель, або поле порожнє. Правило добре, але мовчання
нікуди не веде: щоб порожнє поле колись заповнилось, треба знати, ЯКИЙ це був
екран, а кадр на диску лежить рівно один на пристрій і перезаписується кожні
кілька секунд (`save_frame`). Рідкісний екран — аварія о третій ночі,
незнайомий діалог — до розбору не доживав ніколи.

**Дублі — умова існування фічі, не оптимізація.** Кадр раз на 6 с = 600 на
годину на пристрій, тобто гігабайти за добу. Тому рядок тут — на ВІДПЕЧАТОК:
хеш зменшеної чорно-білої копії, на якій цифри зникають, а розкладка
лишається. «Той самий екран з іншим відсотком» дублем не вважається помилково
— він і є дубль. Повтор лише збільшує лічильник, і навіть це не частіше раза
на хвилину.

**Захоплення не має гальмувати опитування.** Відпечаток рахується з УЖЕ
розібраного кадру, а диск і база чіпаються лише на НОВОМУ відпечатку. Урок зі
стуку в порти: послідовний прохід у циклі опитування старить решту пристроїв.
З тієї ж причини `note()` не кидає НІКОЛИ: скринька — зручність, а не робота.

**Зберігається двоє:** зменшена копія повного кадру (контекст — що взагалі на
екрані) і виріз зони в РІДНОМУ масштабі (на ньому вчать еталон; зменшена
копія тут була б здогадкою). Стеля — `MAX_PER_DEVICE` відпечатків на пристрій,
витісняється спершу позначене «неважливо», далі найрідше бачене.

**Підпис людини — дані, не правило** (`ScreenPuzzle.label`): зону, еталон чи
правило статусу з нього роблять у репозиторії з тестом на тому ж кадрі. Один
кривий еталон псує читання назовсім, і видно це не одразу.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import log_throttle
from app.config import DATA_DIR
from app.models import ScreenPuzzle

logger = logging.getLogger(__name__)

KIND_FURNACE = "furnace"
KIND_MACHINE = "machine"

# Скільки РІЗНИХ екранів тримаємо на пристрій. Оцінка з брифу: печі ≈2 МБ,
# верстати ≈14 МБ у найгіршому разі — тобто стеля існує, але в неї не впираються.
MAX_PER_DEVICE = 20
# Мініатюра для відпечатка — той самий розмір, що в `machines._frame_signature`
# і `scripts/machine_frame_clusters.py`: цифри на ній бліднуть, розкладка
# лишається.
FINGERPRINT_SIZE = (64, 48)
# Середня різниця яскравості (0..255), нижче якої кадри вважаємо ОДНИМ екраном.
# Число не своє: 6.0 підібрано на 130 бойових кадрах SISMA у відборі
# калібрувальних кадрів (`machines.CALIBRATION_NOVELTY_THRESHOLD`) — простій і
# друк розходяться на 10.2, сусідні кадри одного стану — на частки одиниці.
NOVELTY_THRESHOLD = 6.0
# Як часто повтор відомого екрана чіпає базу. Без цього кожен кадр давав би
# UPDATE на пристрій кожні 6 секунд.
TOUCH_EVERY_SECONDS = 60.0
# Найбільша сторона збереженої копії ПОВНОГО кадру. Виріз зони зберігається в
# рідному масштабі окремо — саме він потрібен для еталона.
STORED_MAX_SIDE = 640

# Причини за пріоритетом: одна загадка на кадр. Кадр із чужого екрана
# провалить усі зони одразу, і чотири рядки на один випадок забили б скриньку
# швидше, ніж вона встигне бути корисною.
REASONS: dict[str, str] = {
    "layout_unknown": "незнайомий розклад екрана",
    "status_split": "сигнали статусу не сходяться",
    "zone_clipped": "зона обрізає символ",
    "glyph_unknown": "немає еталона символу",
    "pattern_mismatch": "прочитане не тієї форми",
    "newgen_unread": "екран JOBS видно, а назву не прочитано",
}
REASON_PRIORITY = tuple(REASONS)

_lock = threading.Lock()
# id рядка → коли востаннє чіпали базу (monotonic).
_touched: dict[int, float] = {}
# Ключ пристрою → {id рядка: підпис}. Тримаємо в памʼяті, щоб не читати теку й
# базу на кожному кадрі; перший доступ підіймає з бази (рядків щонайбільше
# MAX_PER_DEVICE). Той самий прийом, що в `machines._known_signatures`.
_known: dict[str, dict[int, tuple[int, ...]]] = {}
# Скільки відпечатків витіснено з пристрою — для лога, бо стеля не має
# спрацьовувати тихо.
_evicted: dict[str, int] = {}

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def root() -> Path:
    return Path(DATA_DIR) / "screen_puzzles"


def _safe_key(key: str) -> str:
    return _SAFE.sub("_", key or "?")[:64]


def folder(kind: str, key: str) -> Path:
    return root() / (kind if kind in (KIND_FURNACE, KIND_MACHINE) else "other") / _safe_key(key)


def signature(frame) -> tuple[int, ...]:
    """Мініатюра кадру як плаский підпис яскравості."""
    small = frame.convert("L").resize(FINGERPRINT_SIZE, Image.Resampling.BILINEAR)
    return tuple(small.tobytes())


def distance(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    """Середня різниця яскравості двох підписів (0..255)."""
    if len(a) != len(b) or not a:
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def pack(sig: tuple[int, ...]) -> str:
    return base64.b64encode(bytes(sig)).decode("ascii")


def unpack(packed: Optional[str]) -> tuple[int, ...]:
    if not packed:
        return ()
    try:
        return tuple(base64.b64decode(packed))
    except Exception:  # noqa: BLE001 — битий підпис = «такого екрана не памʼятаємо»
        return ()


def fingerprint(frame) -> str:
    """Коротке імʼя екрана — для файлу й для унікальності рядка.

    НЕ ним вирішується «це той самий екран»: sha1 по байтах мініатюри
    ламається від будь-якої зміни в кілька пікселів, а на екрані постійно
    міняються годинник, відсоток і назва програми (виміряно на цехових кадрах
    250i: два кадри ТОГО САМОГО екрана JOBS розходяться в мініатюрі на 73
    рівні). Тобто кожен кадр давав би новий рядок, скринька набивала б стелю
    за хвилини й витісняла сама себе — рівно та поломка, від якої мала
    рятувати. Тотожність екранів визначає `distance` з порогом
    `NOVELTY_THRESHOLD`, як у відборі калібрувальних кадрів верстатів.
    """
    return hashlib.sha1(bytes(signature(frame))).hexdigest()[:32]


def pick_reason(reasons) -> Optional[str]:
    """Найважливіша причина з кількох — за порядком `REASON_PRIORITY`."""
    found = {r for r in reasons if r in REASONS}
    for code in REASON_PRIORITY:
        if code in found:
            return code
    return None


def _shrunk(frame):
    """Копія кадру зі стороною не більшою за `STORED_MAX_SIDE`."""
    width, height = frame.size
    longest = max(width, height)
    if longest <= STORED_MAX_SIDE:
        return frame.convert("RGB")
    scale = STORED_MAX_SIDE / longest
    return frame.convert("RGB").resize((max(1, int(width * scale)), max(1, int(height * scale))))


def _write(path: Path, image) -> None:
    """Запис через тимчасовий файл: екран може читати кадр у ту саму мить."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident():x}.tmp")
    try:
        image.save(tmp, format="PNG")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _load_known(db: Session, key: str) -> dict[int, tuple[int, ...]]:
    """Підписи екранів цього пристрою — з памʼяті, а при першому доступі з бази.

    Читається ОДИН раз на процес: далі словник підтримується записами й
    витісненнями. Рядок без підпису (заведений до цієї версії) пропускаємо — він
    просто не братиме участі в порівнянні, а не змушує перечитувати картинку.
    """
    with _lock:
        cached = _known.get(key)
    if cached is not None:
        return cached
    loaded: dict[int, tuple[int, ...]] = {}
    for row in db.scalars(
        select(ScreenPuzzle).where(ScreenPuzzle.device_key == key)
    ).all():
        sig = unpack(row.signature)
        if sig:
            loaded[row.id] = sig
    with _lock:
        _known[key] = loaded
    return loaded


def _evict_if_full(db: Session, kind: str, key: str) -> None:
    """Звільнити місце, якщо скринька пристрою повна.

    Порядок жертв: спершу позначене «неважливо» (людина вже сказала, що це не
    цікаво), далі найрідше бачене, далі найдавніше. Вік сам по собі про
    цінність нічого не каже — рідкісний екран тим і цінний, що старий.
    """
    total = db.scalar(
        select(func.count()).select_from(ScreenPuzzle).where(ScreenPuzzle.device_key == key)
    ) or 0
    if total < MAX_PER_DEVICE:
        return
    victims = db.scalars(
        select(ScreenPuzzle)
        .where(ScreenPuzzle.device_key == key)
        .order_by(
            ScreenPuzzle.dismissed.desc(),
            ScreenPuzzle.seen_count.asc(),
            ScreenPuzzle.last_seen_at.asc(),
        )
        .limit(total - MAX_PER_DEVICE + 1)
    ).all()
    for victim in victims:
        _drop_files(victim)
        with _lock:
            _known.get(key, {}).pop(victim.id, None)
            _touched.pop(victim.id, None)
        db.delete(victim)
        _evicted[key] = _evicted.get(key, 0) + 1
    skipped = log_throttle.due(f"screen_inbox.evicted:{key}")
    if skipped is not None:
        logger.info(
            "Скринька екранів %s повна (%s): витіснено %s відпечатків усього%s",
            key, MAX_PER_DEVICE, _evicted.get(key, 0),
            f" (ще {skipped} витіснень відтоді)" if skipped else "",
        )


def _drop_files(puzzle: ScreenPuzzle) -> None:
    where = folder(puzzle.kind, puzzle.device_key)
    for name in (puzzle.frame_file, puzzle.zone_file):
        if not name:
            continue
        try:
            (where / name).unlink(missing_ok=True)
        except OSError:
            logger.debug("Кадр загадки %s не видалився", name, exc_info=True)


def note(
    db: Session,
    *,
    kind: str,
    key: str,
    name: str,
    frame,
    reason: str,
    detail: str = "",
    zone_crop=None,
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Відкласти кадр, якого читач не зрозумів. Повертає id рядка або None.

    None означає «нічого не робили»: такий екран уже є й лічильник чіпали
    щойно, або захоплення впало — і в жодному з випадків це не привід зупиняти
    опитування.
    """
    try:
        if reason not in REASONS or frame is None:
            return None
        now = now or datetime.now()
        sig = signature(frame)
        moment = time.monotonic()

        known = _load_known(db, key)
        nearest_id, nearest = None, None
        for row_id, other in known.items():
            gap = distance(sig, other)
            if nearest is None or gap < nearest:
                nearest_id, nearest = row_id, gap

        if nearest_id is not None and nearest is not None and nearest <= NOVELTY_THRESHOLD:
            # Такий екран уже є. Лічильник чіпаємо не частіше раза на хвилину:
            # кадр знімається раз на 6 с, і без цього кожен пристрій давав би
            # UPDATE десять разів на хвилину до кінця дня.
            with _lock:
                last = _touched.get(nearest_id)
                if last is not None and moment - last < TOUCH_EVERY_SECONDS:
                    return None
                _touched[nearest_id] = moment
            existing = db.get(ScreenPuzzle, nearest_id)
            if existing is None:
                # Рядок прибрали з екрана між двома кадрами — забуваємо його й
                # наступний кадр заведе новий.
                with _lock:
                    _known.get(key, {}).pop(nearest_id, None)
                return None
            existing.seen_count += 1
            existing.last_seen_at = now
            # Назва пристрою могла змінитись — показувати стару немає сенсу.
            existing.device_name = name or existing.device_name
            db.commit()
            return existing.id

        mark = fingerprint(frame)
        _evict_if_full(db, kind, key)
        where = folder(kind, key)
        frame_file = f"{mark}.png"
        zone_file: Optional[str] = f"{mark}-zone.png" if zone_crop is not None else None
        _write(where / frame_file, _shrunk(frame))
        if zone_crop is not None and zone_file is not None:
            # Виріз — у РІДНОМУ масштабі: на ньому вчать еталон, а зменшена
            # копія растрового шрифту перетворює навчання на вгадування.
            _write(where / zone_file, zone_crop.convert("RGB"))

        puzzle = ScreenPuzzle(
            kind=kind,
            device_key=key,
            device_name=name or "",
            fingerprint=mark,
            reason=reason,
            detail=detail or "",
            frame_file=frame_file,
            zone_file=zone_file,
            signature=pack(sig),
            seen_count=1,
            first_seen_at=now,
            last_seen_at=now,
            label="",
            dismissed=False,
        )
        db.add(puzzle)
        db.commit()
        with _lock:
            _known.setdefault(key, {})[puzzle.id] = sig
            _touched[puzzle.id] = moment
        logger.info(
            "Новий незрозумілий екран %s (%s): %s — %s",
            name or key, kind, REASONS[reason], detail or "без подробиць",
        )
        return puzzle.id
    except Exception:  # noqa: BLE001 — скринька не має права валити опитування
        logger.debug("Кадр загадки не відкладено (%s)", key, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def image_path(puzzle: ScreenPuzzle, which: str = "frame") -> Optional[Path]:
    """Шлях до збереженої картинки загадки або None, якщо файлу немає."""
    name = puzzle.zone_file if which == "zone" else puzzle.frame_file
    if not name:
        return None
    path = folder(puzzle.kind, puzzle.device_key) / name
    return path if path.exists() else None


def listing(
    db: Session, *, include_dismissed: bool = False, limit: int = 200
) -> list[ScreenPuzzle]:
    """Загадки для екрана: найсвіжіші згори, підписані лишаються видимими.

    Підписані не ховаються: підпис — це відповідь МЕНІ, а не закриття справи, і
    поки правило не приїхало релізом, рядок має лишатись на видноті.
    """
    query = select(ScreenPuzzle).order_by(ScreenPuzzle.last_seen_at.desc()).limit(limit)
    if not include_dismissed:
        query = query.where(ScreenPuzzle.dismissed.is_(False))
    return list(db.scalars(query).all())


def counts(db: Session) -> dict[str, int]:
    """Скільки загадок: усього, без підпису, відкладених."""
    total = db.scalar(select(func.count()).select_from(ScreenPuzzle)) or 0
    dismissed = db.scalar(
        select(func.count()).select_from(ScreenPuzzle).where(ScreenPuzzle.dismissed.is_(True))
    ) or 0
    unlabeled = db.scalar(
        select(func.count())
        .select_from(ScreenPuzzle)
        .where(ScreenPuzzle.dismissed.is_(False), ScreenPuzzle.label == "")
    ) or 0
    return {"всього": total, "без_підпису": unlabeled, "неважливих": dismissed}


def set_label(
    db: Session, puzzle_id: int, text: str, user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[ScreenPuzzle]:
    puzzle = db.get(ScreenPuzzle, puzzle_id)
    if puzzle is None:
        return None
    puzzle.label = (text or "").strip()
    puzzle.labeled_at = (now or datetime.now()) if puzzle.label else None
    puzzle.labeled_by_id = user_id if puzzle.label else None
    db.commit()
    return puzzle


def set_dismissed(
    db: Session, puzzle_id: int, dismissed: bool = True
) -> Optional[ScreenPuzzle]:
    """«Це неважливо — не питай більше».

    Рядок лишається з лічильником: він відповідає на «як часто таке буває», і
    саме частота колись може перетворити «неважливо» на «розберись». Але при
    переповненні скриньки він іде першим.
    """
    puzzle = db.get(ScreenPuzzle, puzzle_id)
    if puzzle is None:
        return None
    puzzle.dismissed = bool(dismissed)
    db.commit()
    return puzzle


def forget(db: Session, puzzle_id: int) -> bool:
    """Прибрати загадку зовсім — разом із файлами."""
    puzzle = db.get(ScreenPuzzle, puzzle_id)
    if puzzle is None:
        return False
    _drop_files(puzzle)
    with _lock:
        _touched.pop(puzzle.id, None)
        _known.get(puzzle.device_key, {}).pop(puzzle.id, None)
    db.delete(puzzle)
    db.commit()
    return True


def as_dict(puzzle: ScreenPuzzle) -> dict[str, Any]:
    """Загадка для MCP і для API екрана."""
    return {
        "id": puzzle.id,
        "вид": "піч" if puzzle.kind == KIND_FURNACE else "верстат",
        "пристрій": puzzle.device_name or puzzle.device_key,
        "ключ": puzzle.device_key,
        "причина": REASONS.get(puzzle.reason, puzzle.reason),
        "причина_код": puzzle.reason,
        "подробиці": puzzle.detail,
        "бачено": puzzle.seen_count,
        "уперше": puzzle.first_seen_at.strftime("%Y-%m-%d %H:%M:%S"),
        "востаннє": puzzle.last_seen_at.strftime("%Y-%m-%d %H:%M:%S"),
        "підпис": puzzle.label,
        "неважливо": puzzle.dismissed,
        "є_виріз": bool(puzzle.zone_file),
    }


def reset_state_for_tests() -> None:
    with _lock:
        _touched.clear()
        _evicted.clear()
        _known.clear()
