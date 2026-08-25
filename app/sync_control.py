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
