"""Хто тримає з'єднання з базою, коли їх от-от забракне (28.11.26).

26.11.26 з 21:32 до 21:50 усі 30 з'єднань пулу були зайняті: кожен запит і
кожен воркер падав на «QueuePool limit … reached», а лог не казав, ХТО їх
тримає, — лише що їх нема. Відпустило само, без перезапуску, тож причину
(шара? лок? мережа?) після факту не встановити.

Тому: на кожному видачі з'єднання запамʼятовуємо потік і час; коли зайнятих
стає `ALERT_AT` або більше, пишемо в лог, ЩО ЗАРАЗ РОБЛЯТЬ потоки-тримачі
(їхній поточний стек — саме там видно, на чому висять: `exists()` на UNC,
лок сканера тек, Google). Однакові стеки злиті з лічильником. Не частіше
ніж раз на `REPORT_EVERY_SECONDS` (`log_throttle`).

Поведінки пулу не змінює нічого: лише слухачі `checkout`/`checkin`, без
блокувань поза власним коротким локом. Будь-який збій тут ковтається з
логом — діагностика не сміє ламати видачу з'єднання.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from collections import Counter

from app import log_throttle

logger = logging.getLogger(__name__)

ALERT_AT = 25
REPORT_EVERY_SECONDS = 300.0
# Зʼєднання, взяте щойно, — звичайний запит, не тримач. Звіт про них лише шум.
MIN_HELD_SECONDS = 3.0
_MAX_GROUPS = 12
_STACK_FRAMES = 8

_lock = threading.Lock()
# id(запису пулу) → (ident потоку, назва потоку, коли взяли)
_held: dict[int, tuple[int, str, float]] = {}


def install(engine) -> None:
    from sqlalchemy import event

    def on_checkout(_dbapi_connection, connection_record, _connection_proxy) -> None:
        # `engine.pool` читаємо щоразу: `dispose()` ставить новий пул.
        _note_checkout(connection_record, lambda: engine.pool.checkedout())

    event.listen(engine, "checkout", on_checkout)
    event.listen(engine, "checkin", _on_checkin)


def _note_checkout(connection_record, busy_now) -> None:
    thread = threading.current_thread()
    with _lock:
        _held[id(connection_record)] = (thread.ident or 0, thread.name, time.monotonic())
    try:
        busy = int(busy_now())
    except Exception:  # noqa: BLE001 — діагностика не ламає видачу з'єднання
        logger.debug("pool_watch: не вдалось прочитати стан пулу", exc_info=True)
        return
    if busy >= ALERT_AT:
        _report(busy)


def _on_checkin(_dbapi_connection, connection_record) -> None:
    with _lock:
        _held.pop(id(connection_record), None)


def _short_stack(frame) -> str:
    frames = traceback.extract_stack(frame)
    own = [f for f in frames if _is_app_frame(f.filename)]
    picked = (own or frames)[-_STACK_FRAMES:]
    return " ← ".join(
        f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(picked)
    )


def _is_app_frame(filename: str) -> bool:
    parts = filename.replace("\\", "/").split("/")
    return "app" in parts[:-1] and "site-packages" not in parts


def holders_report(now: float | None = None) -> list[str]:
    """Рядки звіту: «N× потік (тримає до X с): стек», найстаріші першими."""
    now = time.monotonic() if now is None else now
    with _lock:
        held = list(_held.values())
    frames = sys._current_frames()  # noqa: SLF001 — єдиний спосіб побачити чужий стек
    groups: Counter[tuple[str, str]] = Counter()
    oldest: dict[tuple[str, str], float] = {}
    for ident, name, since in held:
        age = now - since
        if age < MIN_HELD_SECONDS:
            continue
        frame = frames.get(ident)
        stack = _short_stack(frame) if frame is not None else "потік уже завершився"
        kind = name.split(" ")[0] if name.startswith("AnyIO") else name
        key = (kind, stack)
        groups[key] += 1
        oldest[key] = max(oldest.get(key, 0.0), age)
    ordered = sorted(groups, key=lambda k: -oldest[k])[:_MAX_GROUPS]
    return [f"{groups[k]}× {k[0]} (до {oldest[k]:.0f} с): {k[1]}" for k in ordered]


def _report(busy: int) -> None:
    skipped = log_throttle.due("pool_watch.busy", every_seconds=REPORT_EVERY_SECONDS)
    if skipped is None:
        return
    try:
        lines = holders_report()
    except Exception:  # noqa: BLE001 — діагностика не ламає видачу з'єднання
        logger.warning("pool_watch: не вдалось зібрати тримачів", exc_info=True)
        return
    tail = f" (ще {skipped} разів відтоді)" if skipped else ""
    logger.warning(
        "Пул з'єднань з базою майже вичерпано: зайнято %d%s. Хто тримає довше %.0f с:\n  %s",
        busy, tail, MIN_HELD_SECONDS, "\n  ".join(lines) or "— (усі взяті щойно)",
    )


def reset_for_tests() -> None:
    with _lock:
        _held.clear()
