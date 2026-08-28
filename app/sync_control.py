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

import threading

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
