"""Сторож чесності плит стану в «Налаштуваннях».

Плита каже кольором «працює / не працює». Правило одне (CLAUDE.md §14, те саме,
що на печах): **хибне число гірше за жодне**, і зелений ставиться ЛИШЕ там, де є
підтвердження роботи. Ці тести ловлять найлегшу помилку такого віджета —
пофарбувати зеленим «поле заповнене» замість «сигнал прийшов».
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.settings_status import (
    TONE_ALARM,
    TONE_NONE,
    TONE_OK,
    TONE_WARN,
    _slab_imap,
    _slab_machines,
    _slab_operators,
    _slab_paths,
    _slab_sheets,
    _slab_state,
)


def _hb(state: str, label: str = "мітка") -> dict:
    return {"state": state, "label": label}


def _ctx(**over) -> dict:
    base = {
        "sync_status": {"mail": _hb("neutral"), "sheet": _hb("neutral")},
        "sync_intervals": {"mail": 2, "sheet": 10},
        "state_nodes": [],
        "values": {},
        "values_set": {},
        "google_configured": False,
        "imap_configured": False,
    }
    base.update(over)
    return base


# ── heartbeat → колір ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hb_state, expected",
    [
        ("success", TONE_OK),
        ("warning", TONE_WARN),
        ("error", TONE_ALARM),
        ("neutral", TONE_NONE),
    ],
)
def test_sheets_tone_follows_the_sync_heartbeat(hb_state, expected, db_session):
    """Колір розділу «Google Таблиця» = стан фонового синку, а не заповненість полів."""
    ctx = _ctx(
        google_configured=True,
        sync_status={"mail": _hb("neutral"), "sheet": _hb(hb_state)},
    )
    assert _slab_sheets(db_session, ctx).tone == expected


def test_sheets_stays_grey_when_not_configured_even_with_a_live_heartbeat(db_session):
    """Не налаштовано — сірий. Зелений тут означав би «працює», а воно й не мало."""
    ctx = _ctx(
        google_configured=False,
        sync_status={"mail": _hb("neutral"), "sheet": _hb("success")},
    )
    slab = _slab_sheets(db_session, ctx)
    assert slab.tone == TONE_NONE
    assert slab.label == "не підключено"


def test_imap_error_is_red_not_yellow():
    ctx = _ctx(imap_configured=True, sync_status={"mail": _hb("error"), "sheet": _hb("neutral")})
    assert _slab_imap(ctx).tone == TONE_ALARM


def test_imap_dead_worker_is_yellow():
    """«Немає відповіді від фонового процесу» — попередження, не помилка."""
    ctx = _ctx(imap_configured=True, sync_status={"mail": _hb("warning"), "sheet": _hb("neutral")})
    assert _slab_imap(ctx).tone == TONE_WARN


# ── стан системи: найгірший із двох синків ──────────────────────────────────


def test_state_takes_the_worst_of_the_two_syncs():
    ctx = _ctx(sync_status={"mail": _hb("error"), "sheet": _hb("success")})
    assert _slab_state(ctx).tone == TONE_ALARM

    ctx = _ctx(sync_status={"mail": _hb("warning"), "sheet": _hb("success")})
    assert _slab_state(ctx).tone == TONE_WARN

    ctx = _ctx(sync_status={"mail": _hb("success"), "sheet": _hb("success")})
    assert _slab_state(ctx).tone == TONE_OK


def test_state_configured_but_silent_is_not_called_unconfigured():
    """Щойно стартували: налаштовано, тіку ще не було. Це «очікує», не «не налаштовано»."""
    ctx = _ctx(google_configured=True, imap_configured=True)
    slab = _slab_state(ctx)
    assert slab.tone == TONE_NONE
    assert slab.label == "очікує першої перевірки"


# ── шляхи: «задано» ніколи не дорівнює «працює» ─────────────────────────────


def test_paths_never_go_green_because_existence_is_not_checked_on_render():
    """Заповнений шлях не доводить, що тека є — похід у мережеву шару робить проба."""
    ctx = _ctx(values={"export_folder_path": "P:\\export", "technician_files_path": "P:\\tech"})
    slab = _slab_paths(ctx)
    assert slab.tone != TONE_OK
    assert all(m.tone != TONE_OK for m in slab.meters)


def test_paths_warn_when_a_required_folder_is_missing():
    ctx = _ctx(values={"export_folder_path": "P:\\export"})
    assert _slab_paths(ctx).tone == TONE_WARN


# ── оператори: втрата доступу — червоне ─────────────────────────────────────


def test_operators_without_an_active_admin_is_red():
    ops = [SimpleNamespace(is_active=True, role="оператор")]
    assert _slab_operators(_ctx(operators=ops)).tone == TONE_ALARM


def test_operators_with_an_active_admin_is_green():
    ops = [SimpleNamespace(is_active=True, role="адмін"), SimpleNamespace(is_active=False, role="оператор")]
    assert _slab_operators(_ctx(operators=ops)).tone == TONE_OK


# ── верстати: обрив звʼязку — червоне, тиша — жовте ─────────────────────────


class _Card:
    def __init__(self, *, problem=False, frame=True, running=False, agent=True):
        self.has_problem = problem
        self.has_frame = frame
        self.is_running = running
        self.target = SimpleNamespace(is_agent=agent)


def test_machines_broken_link_is_red(monkeypatch, db_session):
    monkeypatch.setattr(
        "app.services.settings_status.machines_service.snapshot",
        lambda db: [_Card(), _Card(problem=True, frame=False)],
    )
    assert _slab_machines(db_session, _ctx()).tone == TONE_ALARM


def test_machines_all_silent_is_yellow_not_red(monkeypatch, db_session):
    """Жодного кадру, але й жодної підтвердженої аварії — попередження."""
    monkeypatch.setattr(
        "app.services.settings_status.machines_service.snapshot",
        lambda db: [_Card(frame=False), _Card(frame=False)],
    )
    assert _slab_machines(db_session, _ctx()).tone == TONE_WARN


def test_machines_empty_list_is_grey(monkeypatch, db_session):
    """Верстатів не додано — це не аварія й не успіх."""
    monkeypatch.setattr("app.services.settings_status.machines_service.snapshot", lambda db: [])
    slab = _slab_machines(db_session, _ctx())
    assert slab.tone == TONE_NONE
    assert slab.meters == []
