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
    _slab_backup,
    _slab_imap,
    _slab_machines,
    _slab_mcp,
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


# ── Резервна копія: тон від ДРУГОЇ копії, не від кількості файлів ──────────
# Аудит 08.09.26. Плита зеленіла від того, що в теці лежать знімки. Але вони
# лежать там і тоді, коли копіювання давно падає, а головне — лежать на тому
# самому диску, що й база: у сценарії «диск помер» їх немає разом із нею.


def _backup_ctx(*, snaps: int = 3, mirror: dict | None = None) -> dict:
    return {
        "monthly_snapshots": [{"name": f"kuubmill-2026-0{i}.db", "size_mb": 1.0} for i in range(1, snaps + 1)],
        "backup_mirror": mirror if mirror is not None else {"dir": "", "last_ok": "", "last_error": ""},
    }


def test_backup_is_not_green_when_every_copy_sits_on_the_db_disk():
    slab = _slab_backup(_backup_ctx())
    assert slab.tone == TONE_WARN


def test_backup_is_not_green_when_the_second_copy_keeps_failing():
    slab = _slab_backup(
        _backup_ctx(mirror={"dir": r"\srvackup", "last_ok": "2026-08-01T10:00:00", "last_error": "OSError: шара недоступна"})
    )
    assert slab.tone == TONE_WARN


def test_backup_is_not_green_before_the_second_copy_ever_ran():
    slab = _slab_backup(_backup_ctx(mirror={"dir": r"\srvackup", "last_ok": "", "last_error": ""}))
    assert slab.tone == TONE_WARN


def test_backup_goes_green_only_with_a_confirmed_off_disk_copy():
    slab = _slab_backup(
        _backup_ctx(mirror={"dir": r"\srvackup", "last_ok": "2026-09-08T07:30:00", "last_error": ""})
    )
    assert slab.tone == TONE_OK


def test_backup_without_any_snapshot_stays_warning():
    slab = _slab_backup(
        _backup_ctx(snaps=0, mirror={"dir": r"\srvackup", "last_ok": "2026-09-08T07:30:00", "last_error": ""})
    )
    assert slab.tone == TONE_WARN


# ── Доступ по мережі (/mcp): тон = слухач, не перемикач і не токен ─────


def _mcp_ctx(*, enabled=False, listening=False, error=None, has_token=False):
    return {
        "mcp_enabled": enabled,
        "mcp_status": SimpleNamespace(listening=listening, error=error, since=None),
        "mcp_has_token": has_token,
        "mcp_port": 8011,
    }


def test_mcp_off_is_grey_even_with_a_token_saved():
    """Вимкнено = сірий — і заданий токен цього не змінює, бо порт закритий."""
    slab = _slab_mcp(_mcp_ctx(enabled=False, has_token=True))
    assert slab.tone == TONE_NONE
    assert slab.label == "вимкнено"


def test_mcp_listening_is_green():
    slab = _slab_mcp(_mcp_ctx(enabled=True, listening=True, has_token=True))
    assert slab.tone == TONE_OK


def test_mcp_enabled_with_error_is_warn_not_alarm():
    """Мовчання порту саме по собі не доводить аварії (урок верстатів,
    CLAUDE.md §14) — тому тут жовте, а не червоне."""
    slab = _slab_mcp(_mcp_ctx(enabled=True, listening=False, error="не вдалось відкрити порт", has_token=True))
    assert slab.tone == TONE_WARN


def test_mcp_enabled_but_not_yet_listening_and_no_error_is_grey():
    """Щойно увімкнули, сторож ще не встиг підняти порт — не зелений і не
    жовтий, бо жодного підтвердженого сигналу (ok чи проблема) ще немає."""
    slab = _slab_mcp(_mcp_ctx(enabled=True, listening=False, error=None, has_token=True))
    assert slab.tone == TONE_NONE
    assert slab.label == "вмикається…"


def test_mcp_token_meter_is_never_green_merely_for_existing():
    """Сам факт задай токена нічого не каже про те, чи слухач працює — тон
    метрики «TOKEN» лишається нейтральним в обох випадках."""
    with_token = _slab_mcp(_mcp_ctx(enabled=True, listening=True, has_token=True))
    without_token = _slab_mcp(_mcp_ctx(enabled=True, listening=True, has_token=False))
    token_meter_with = next(m for m in with_token.meters if m.k == "Токен")
    token_meter_without = next(m for m in without_token.meters if m.k == "Токен")
    assert token_meter_with.tone != TONE_OK
    assert token_meter_without.tone != TONE_OK
    assert token_meter_with.v == "задано"
    assert token_meter_without.v == "не задано"
