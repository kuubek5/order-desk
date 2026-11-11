"""Графік відключень світла (`power_schedule`) — на СПРАВЖНІХ відповідях джерел.

Зразки зняті 11.11.2026 для Києва (черги 4.1 і 33.1): Yasno й збірник
outage-data-ua. Власник звірив ці години з тим, що бачив сам, — тому числа
тут не вигадані, і зміна розбору, що їх зсуває, мусить впасти.

Стережемо три правила модуля: старий графік не видається за сьогоднішній,
«світла немає» не тривога, а невдале оновлення не стирає ще живий знімок.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.services import power_schedule as ps

FIX = Path(__file__).parent / "fixtures"
YASNO = json.loads((FIX / "power_yasno_kyiv_2026-11-11.json").read_text(encoding="utf-8"))
BACKUP = json.loads((FIX / "power_backup_kyiv_2026-11-11.json").read_text(encoding="utf-8"))
NOON = datetime(2026, 11, 11, 14, 22)

OFF_41 = ((0, 270), (480, 900), (1020, 1440))          # 00:00–04:30, 08:00–15:00, 17:00–24:00
OFF_331 = ((210, 540), (690, 1110), (1230, 1440))      # 03:30–09:00, 11:30–18:30, 20:30–24:00


@pytest.fixture(autouse=True)
def _clean_state():
    ps.reset()
    yield
    ps.reset()


def _snap(now=NOON, **kw):
    return ps.parse_yasno(YASNO, kw.pop("group", "4.1"), now)


def _view(now, snapshot=None, failed=None, group="4.1"):
    return ps.build_view(group, snapshot if snapshot is not None else _snap(now), failed, now)


# ── Розбір ───────────────────────────────────────────────────────────────────

def test_yasno_gives_the_real_hours_of_both_queues():
    today = _snap().day(NOON.date())
    assert today.status == "schedule" and today.off == OFF_41
    assert ps.parse_yasno(YASNO, "33.1", NOON).day(NOON.date()).off == OFF_331
    tomorrow = _snap().day(NOON.date() + timedelta(days=1))
    assert tomorrow.status == "waiting" and tomorrow.off == ()
    # `updatedOn` приходить у UTC (20:00) — показуємо київський час.
    assert _snap().updated_on == datetime(2026, 11, 10, 22, 0, 2)


def test_backup_agrees_with_yasno_minute_for_minute():
    for group, off in (("4.1", OFF_41), ("33.1", OFF_331)):
        snap = ps.parse_backup(BACKUP, group, NOON)
        assert snap.source == "backup"
        assert snap.day(NOON.date()).off == off, group
    assert ps.parse_backup(BACKUP, "4.1", NOON).day(NOON.date() + timedelta(days=1)) is None


@pytest.mark.parametrize("payload", [None, [], {}, {"4.1": "x"}, {"4.1": {}},
                                     {"4.1": {"today": {"date": "2026-11-11", "status": "Щось нове", "slots": []}}},
                                     {"4.1": {"today": {"date": "не дата", "status": "NoOutages"}}}])
def test_unreadable_yasno_is_an_error_not_a_guess(payload):
    with pytest.raises(ps.SourceError):
        ps.parse_yasno(payload, "4.1", NOON)


def test_unknown_group_and_broken_backup_are_errors():
    with pytest.raises(ps.SourceError):
        ps.parse_yasno(YASNO, "59.1", NOON)
    with pytest.raises(ps.SourceError):
        ps.parse_backup({"fact": {}}, "4.1", NOON)
    with pytest.raises(ps.SourceError):
        ps.parse_backup(BACKUP, "59.1", NOON)


def test_only_definite_slots_are_outages():
    payload = {"4.1": {"today": {"date": "2026-11-11", "status": "ScheduleApplies", "slots": [
        {"start": 0, "end": 60, "type": "Definite"}, {"start": 60, "end": 120, "type": "Possible"},
        {"start": 120, "end": 180, "type": "Definite"}, {"start": 180, "end": 240, "type": "Definite"}]}}}
    assert ps.parse_yasno(payload, "4.1", NOON).day(NOON.date()).off == ((0, 60), (120, 240))


@pytest.mark.parametrize("raw,clean", [("4.1", "4.1"), (" 4,1 ", "4.1"), ("04.1", "4.1"), ("33.1", "33.1"),
                                       ("", ""), (None, ""), ("4", ""), ("4.1.2", ""), ("а.1", ""), ("0.1", ""), ("4.10", "")])
def test_group_is_cleaned_or_rejected(raw, clean):
    assert ps.clean_group(raw) == clean


# ── Стан на справжньому дні, хвилина за хвилиною ─────────────────────────────

def test_outage_now_says_when_power_returns():
    v = _view(NOON)
    assert (v.kind, v.say, v.sub, v.left, v.meta) == ("off", "Світла немає", "дадуть о 15:00", "38 хв", "немає до 15:00")
    assert v.change_iso == "2026-11-11T15:00:00" and v.count_label == "до світла"
    assert v.tone == "" and not v.lit, "«немає світла» — спокійний стан, без тону тривоги"
    assert v.today_sum == "без світла 18 год 30 хв" and v.tomorrow_sum == "ще не опубліковано"
    assert v.updated == "оновлено вчора о 22:00"


def test_power_on_says_when_it_goes_and_warns_half_an_hour_before():
    on = _view(datetime(2026, 11, 11, 15, 40))
    assert (on.kind, on.say, on.sub, on.left, on.meta) == ("on", "Світло є", "вимкнуть о 17:00", "1 год 20 хв", "є до 17:00")
    assert on.lit and on.tone == ""
    edge = _view(datetime(2026, 11, 11, 16, 29))
    assert edge.kind == "on", "за 31 хв ще не попередження"
    soon = _view(datetime(2026, 11, 11, 16, 30))
    assert (soon.kind, soon.say, soon.sub, soon.left) == ("soon", "Скоро вимкнуть", "о 17:00", "30 хв")
    assert soon.tone == "is-soon" and soon.back == "до кінця доби" and soon.count_label == "до вимкнення"


def test_transitions_land_on_the_exact_minute():
    assert _view(datetime(2026, 11, 11, 14, 59)).kind == "off"
    assert _view(datetime(2026, 11, 11, 15, 0)).kind == "on"
    assert _view(datetime(2026, 11, 11, 16, 59)).kind == "soon"
    assert _view(datetime(2026, 11, 11, 17, 0)).kind == "off"
    assert _view(datetime(2026, 11, 11, 4, 29)).sub == "дадуть о 04:30"
    assert _view(datetime(2026, 11, 11, 4, 30)).sub == "вимкнуть о 08:00"


def test_evening_outage_without_tomorrow_is_until_end_of_day_not_midnight_promise():
    v = _view(datetime(2026, 11, 11, 21, 30))
    assert (v.kind, v.sub, v.left, v.meta, v.change_iso) == ("off", "до кінця доби", "", "немає до кінця доби", "")


def _with_tomorrow(off, status="schedule", fetched=datetime(2026, 11, 11, 21, 0)):
    snap = _snap(fetched)
    return ps.Snapshot(snap.group, (snap.days[0], ps.Day(NOON.date() + timedelta(days=1), status, off)),
                       snap.fetched_at, snap.source, snap.updated_on)


def test_outage_across_midnight_uses_tomorrow_when_it_is_known():
    v = _view(datetime(2026, 11, 11, 21, 30), _with_tomorrow(((0, 210), (450, 840))))
    assert (v.sub, v.left, v.change_iso) == ("дадуть о 03:30", "6 год", "2026-11-12T03:30:00")
    assert v.tomorrow_sum == "без світла 10 год"
    # Завтра починається зі світла — сьогоднішнє відключення закінчується опівночі.
    v = _view(datetime(2026, 11, 11, 21, 30), _with_tomorrow(((120, 300),)))
    assert (v.sub, v.left) == ("дадуть о 00:00", "2 год 30 хв")


def test_day_without_outages_and_emergency_day():
    calm = ps.Snapshot("4.1", (ps.Day(NOON.date(), "none"),), NOON, "yasno")
    v = ps.build_view("4.1", calm, None, NOON)
    assert (v.kind, v.sub, v.today_sum) == ("on", "сьогодні без відключень", "без відключень")
    alarm = ps.Snapshot("4.1", (ps.Day(NOON.date(), "emergency"),), NOON, "yasno")
    v = ps.build_view("4.1", alarm, None, NOON)
    assert (v.kind, v.say, v.sub, v.tone) == ("alarm", "Аварійні відключення", "графік не діє", "is-alarm")
    assert not v.has_bar and v.now_pct is None, "графік не діє — стрічки з позначкою «зараз» немає"


# ── Сітка годин і стрічка ────────────────────────────────────────────────────

def test_hour_grid_matches_the_real_day():
    v = _view(NOON)
    kinds = [c.kind for c in v.today_cells]
    assert kinds == (["off"] * 4 + ["first"] + ["on"] * 3 + ["off"] * 7 + ["on"] * 2 + ["off"] * 7)
    assert [i for i, c in enumerate(v.today_cells) if c.now] == [14]
    assert all(c.past for c in v.today_cells[:14]) and not any(c.past for c in v.today_cells[14:])
    assert {c.kind for c in v.tomorrow_cells} == {"wait"}
    assert [(s.left, s.width) for s in v.segments] == [(0.0, 18.75), (33.333, 29.167), (70.833, 29.167)]
    assert v.now_pct == 59.861


def test_aware_clock_and_naive_snapshot_mix_without_error():
    """Годинник застосунку — з поясом, знімок і дати джерел — без: різниця
    таких двох падала б TypeError-ом на кожному рендері."""
    from zoneinfo import ZoneInfo

    aware = NOON.replace(tzinfo=ZoneInfo("Europe/Kyiv"))
    assert ps.refresh("4.1", lambda url: YASNO, aware) is True
    snapshot, failed = ps.current()
    assert snapshot.fetched_at.tzinfo is None
    assert ps.build_view("4.1", snapshot, failed, aware).sub == "дадуть о 15:00"
    assert ps.build_view("4.1", _snap(NOON), None, aware).left == "38 хв"


# ── Старий графік гірший за жодного ──────────────────────────────────────────

def test_no_group_means_no_widget():
    assert ps.build_view("", _snap(), None, NOON) is None
    assert ps.build_view("не черга", _snap(), None, NOON) is None


def test_stale_or_foreign_snapshot_is_shown_as_no_schedule():
    fresh = _view(NOON + timedelta(hours=3))
    assert fresh.kind == "off", "рівно три години — ще живий"
    stale = _view(NOON + timedelta(hours=3, minutes=1), _snap(NOON), failed=NOON + timedelta(minutes=10))
    assert (stale.kind, stale.say, stale.sub) == ("void", "Графіка немає", "джерело мовчить з 14:32")
    assert not stale.has_bar and {c.kind for c in stale.today_cells} == {"wait"}
    # Знімок іншої черги (щойно змінили налаштування) — не наш графік.
    assert _view(NOON, ps.parse_yasno(YASNO, "33.1", NOON)).kind == "void"
    # Після півночі у вчорашньому знімку сьогодні — це «завтра», якого ще не публікували.
    after = _view(datetime(2026, 11, 12, 0, 20), _snap(datetime(2026, 11, 11, 23, 50)))
    assert (after.kind, after.sub) == ("void", "на сьогодні ще не опубліковано")


def test_nothing_fetched_yet_is_honest():
    v = ps.build_view("4.1", None, None, NOON)
    assert (v.kind, v.sub) == ("void", "джерело ще не відповіло")
    v = ps.build_view("4.1", None, NOON - timedelta(minutes=30), NOON)
    assert v.sub == "джерело мовчить з 13:52"


# ── Оновлення: головне джерело, запасне, обидва мовчать ──────────────────────

def test_refresh_takes_yasno_then_falls_back_and_keeps_live_snapshot_on_failure():
    calls: list[str] = []

    def both(url):
        calls.append(url)
        return YASNO if url == ps.YASNO_URL else BACKUP

    assert ps.refresh("4.1", both, NOON) is True
    assert calls == [ps.YASNO_URL] and ps.current()[0].source == "yasno"

    def yasno_down(url):
        if url == ps.YASNO_URL:
            raise OSError("timeout")
        return BACKUP

    assert ps.refresh("4,1", yasno_down, NOON + timedelta(minutes=10)) is True
    assert ps.current()[0].source == "backup" and ps.current()[1] is None

    def all_down(url):
        raise OSError("немає мережі")

    later = NOON + timedelta(minutes=20)
    assert ps.refresh("4.1", all_down, later) is False
    snapshot, failed_since = ps.current()
    assert snapshot.source == "backup", "невдача не стирає ще живий знімок"
    assert failed_since == later
    assert ps.refresh("4.1", all_down, later + timedelta(minutes=10)) is False
    assert ps.current()[1] == later, "«мовчить з» — від ПЕРШОЇ невдачі, а не від останньої"
    assert ps.build_view("4.1", snapshot, failed_since, later).kind == "off"
    assert ps.refresh("4.1", both, later + timedelta(minutes=20)) is True
    assert ps.current()[1] is None


def test_refresh_with_garbage_from_yasno_uses_backup_and_empty_group_forgets():
    assert ps.refresh("4.1", lambda url: {"не": "те"} if url == ps.YASNO_URL else BACKUP, NOON) is True
    assert ps.current()[0].source == "backup"
    called: list[str] = []
    assert ps.refresh("", called.append, NOON) is False
    assert called == [] and ps.current() == (None, None), "без черги запити не йдуть"
