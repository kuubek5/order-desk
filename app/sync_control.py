"""Process-wide pause switch for all Google Sheet traffic.

One in-memory flag, shared by everything in the process: the background sheet
and mail sync workers (which read the sheet) and the operator write-backs (Sum3D,
status, delete, manual add — which write to it). Paused means the system touches
the sheet in NEITHER direction, so admins can bulk-edit the table without the
app reading a half-finished edit or writing over their work. On resume the next
sync tick reads the current table fresh — the table stays the source of truth.

Deliberately in-memory, defaulting to RUNNING on every process start: a pause
that survived a restart (or an auto-update) could silently freeze the queue for
everyone with nobody remembering why. A forgotten pause is worse than a pause
that clears itself, so restart always resumes. The web UI and tray both show the
live state prominently, so an active pause is never invisible.

Toggled from two places (the tray menu and the web queue), both in this one
process, so a plain threading.Event is enough — no cross-process coordination.
"""

from __future__ import annotations

from datetime import date
import threading
from time import monotonic

# set() = paused. Chosen so the default (clear) is the running state.
_paused = threading.Event()


def is_paused() -> bool:
    return _paused.is_set()


def pause() -> None:
    _paused.set()


def resume() -> None:
    _paused.clear()


def set_paused(value: bool) -> None:
    if value:
        _paused.set()
    else:
        _paused.clear()


# Sync-speed presets (side-panel segmented switch on the queue screen).
# "hot"    — seconds between hot-tab reads (one ~3s API call per tab through
#            the lab proxy, so 5s is the physical floor — see the sizing
#            discussion in CLAUDE.md's proxy notes);
# "screen" — seconds between the queue's local partial=rows polls;
# "full"   — seconds between expensive full syncs (listing + 3-day window).
#            Turbo stretches it: the hot lane already covers the tabs being
#            worked, and a ~15s full pass every minute would starve 5s ticks.
# Held in process memory only: an operational knob, not a credential — after a
# restart the app wakes up in "normal", which is the right default.
SYNC_SPEED_PRESETS = {
    "turbo": {"hot": 5, "screen": 5, "full": 120, "label": "Турбо", "hint": "5с"},
    "normal": {"hot": 15, "screen": 15, "full": 60, "label": "Звичайно", "hint": "15с"},
    "eco": {"hot": 60, "screen": 30, "full": 60, "label": "Економ", "hint": "60с"},
}

_speed_preset = "normal"


def get_speed_preset() -> str:
    return _speed_preset


def set_speed_preset(preset: str) -> bool:
    """Switch the preset. Returns False (and changes nothing) for an unknown
    name — the value comes off a form, so it is never trusted."""
    global _speed_preset
    if preset not in SYNC_SPEED_PRESETS:
        return False
    _speed_preset = preset
    return True


def get_sync_speed() -> dict:
    return SYNC_SPEED_PRESETS[_speed_preset]


# ── Розклад фонових синхронізацій ────────────────────────────────────────
MAIL_SYNC_INTERVAL_SECONDS = 2 * 60
MAIL_SYNC_INITIAL_DELAY_SECONDS = 10
# One sync cycle costs ~4 Google Sheets API calls (spreadsheet.worksheets() +
# get_all_values() per relevant tab, typically 3 tabs) — Google's quota is
# hundreds of reads/minute, far above that. The old 2-minute value was never
# based on a real technical constraint, it was just copied from
# MAIL_SYNC_INTERVAL_SECONDS above; confirmed safe to halve so the queue
# reflects sheet edits sooner.
SHEET_SYNC_INTERVAL_SECONDS = 1 * 60
SHEET_SYNC_INITIAL_DELAY_SECONDS = 10
# Fast lane: between full syncs, re-read ONLY the current day's tab this often.
# With the worker thread's spreadsheet/worksheet cache warm that's a single
# ~3s API call, so today's technician edits reach the CRM within ~15-20s while
# the expensive 3-tab full sync stays at the interval above. See
# app/sheet_sync_service.py::sync_hot_tab.
SHEET_SYNC_HOT_INTERVAL_SECONDS = 15

# Days operators are actually looking at right now (queue partial=rows polls
# record them). The hot lane unions these with today/yesterday so "the open
# tab in the CRM" is always among the fast-synced ones, whatever day it is.
_viewed_days: dict[date, float] = {}
_VIEWED_DAY_TTL_SECONDS = 120.0
_VIEWED_DAYS_CAP = 2
# `_viewed_days` має ДВОХ письменників у різних потоках: request-хендлери
# (get_queue) вставляють переглянутий день, а фоновий воркер таблиці читає й
# чистить його в hot_extra_days. Без локу воркер міг упасти на
# «dictionary changed size during iteration», коли оператор відкриває день
# саме під час тіку. Лок дешевий (дві короткі критичні секції), а гонка
# рідкісна й невідтворювана — рівно той баг, який інакше ловиться раз на
# місяць. (Пор. app/sync_heartbeat.py — там лок НЕ потрібен: один письменник
# на ключ і атомарна заміна незмінного значення.)
_viewed_days_lock = threading.Lock()


def record_viewed_day(day: date | None) -> None:
    if day is None:
        return
    with _viewed_days_lock:
        _viewed_days[day] = monotonic()


def hot_extra_days() -> set[date]:
    """Recently-viewed days still worth fast-syncing, freshest first, capped so
    a filter-hopping operator can't balloon the 5s tick into a full sync."""
    now = monotonic()
    with _viewed_days_lock:
        # Знімок під локом — далі сортуємо/фільтруємо вже свою копію, не чіпаючи
        # живий словник під час ітерації.
        items = list(_viewed_days.items())
        for day, ts in items:
            if now - ts >= _VIEWED_DAY_TTL_SECONDS:
                _viewed_days.pop(day, None)
    fresh = sorted(
        ((day, ts) for day, ts in items if now - ts < _VIEWED_DAY_TTL_SECONDS),
        key=lambda item: item[1],
        reverse=True,
    )
    return {day for day, _ in fresh[:_VIEWED_DAYS_CAP]}
