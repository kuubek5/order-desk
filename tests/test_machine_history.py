"""Історія стану верстата за добу — стрічка «працює/стоїть».

Показання верстатів у базу не пишуться взагалі, тому стрічка живе в пам'яті
процесу (як `outages`) і починається зі старту застосунку. Саме тому головна
властивість, яку тут стережемо, — чесність про те, чого ми НЕ бачили:

— поки застосунок не працював, це «немає даних», а не «верстат стояв»;
— дірка між спостереженнями не подовжує сусідній стан;
— відсутність спостережень взагалі дає порожній стан, а не порожню смугу.
"""

from datetime import datetime, timedelta

import pytest

from app.routers.deps import templates
from app.services import machines as service

KEY = "10.0.0.1"
NOW = datetime(2026, 9, 6, 12, 0, 0)


@pytest.fixture(autouse=True)
def _clean_state():
    service.reset_state_for_tests()
    yield
    service.reset_state_for_tests()


def _at(minutes_ago: float) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


def _observe(kind: str, since_minutes: float, until_minutes: float, step_seconds: float = 30) -> None:
    """Проімітувати опитування: кадр знімається раз на кілька секунд, і саме
    так стрічка й наповнюється. Крок мусить бути меншим за TIMELINE_GAP_SECONDS
    — інакше ми самі створюємо дірку, а не безперервне спостереження."""
    minutes = since_minutes
    while minutes >= until_minutes:
        service.record_state(KEY, kind, _at(minutes))
        minutes -= step_seconds / 60


def _timeline():
    return service.day_timeline(KEY, now=NOW)


# ── Що саме записуємо ───────────────────────────────────────────────────────


def test_observed_kind_maps_one_frame_to_one_state():
    assert service.observed_kind(percent=42, completed=False, error=False) == "run"
    assert service.observed_kind(percent=100, completed=False, error=False) == "done"
    assert service.observed_kind(percent=None, completed=True, error=False) == "done"
    assert service.observed_kind(percent=None, completed=False, error=False) == "idle"
    assert service.observed_kind(percent=42, completed=False, error=True) == "off"


def test_same_state_extends_one_span_instead_of_multiplying_rows():
    """Кадр знімається раз на 5 с. Якби кожне спостереження ставало окремим
    відрізком, доба одного верстата — це 17 тисяч прямокутників."""
    _observe("run", 30, 0)
    spans = [s for s in _timeline().spans if s.kind != "gap"]
    assert len(spans) == 1


def test_state_change_closes_the_previous_span_without_a_hole():
    _observe("run", 30, 25)
    _observe("idle", 24.5, 20)
    kinds = [s.kind for s in _timeline().spans]
    # Дірка лишається лише зліва (до першого спостереження) і справа (після
    # останнього): між станами верстата порожнечі бути не має.
    assert kinds == ["gap", "run", "idle", "gap"]


# ── Чого ми не бачили ───────────────────────────────────────────────────────


def test_downtime_of_the_app_is_a_hole_not_three_hours_of_milling():
    """Застосунок стояв три години. Розтягнути через них попередній стан
    означало б дорисувати верстату фрезерування, якого ніхто не бачив."""
    _observe("run", 300, 299)
    _observe("run", 120, 119)  # той самий стан, але через три години

    spans = _timeline().spans
    runs = [s for s in spans if s.kind == "run"]
    assert len(runs) == 2, "після дірки починається НОВИЙ відрізок"
    holes = [s for s in spans if s.kind == "gap"]
    assert any("немає даних 07:01–10:00" in s.title for s in holes)


def test_before_the_first_observation_it_is_unknown_not_idle():
    _observe("run", 10, 9)
    timeline = _timeline()
    assert timeline.spans[0].kind == "gap"
    assert timeline.spans[0].x == 0.0
    assert timeline.since == _at(10)
    assert "немає даних" in timeline.spans[0].title


def test_no_observations_at_all_is_an_empty_state():
    timeline = _timeline()
    assert timeline.has_data is False
    assert timeline.since is None


def test_totals_count_only_what_was_seen():
    _observe("run", 60, 30)
    _observe("idle", 29.5, 20)
    timeline = _timeline()
    assert timeline.totals["run"] == pytest.approx(30 * 60, abs=61)
    assert timeline.totals["idle"] == pytest.approx(9 * 60, abs=61)
    # «Немає даних» стоїть у підсумку нарівні зі станами — і мусить: без нього
    # рядок «фрезерує 30 хв» мовчки видавав би півгодини за цілу добу.
    assert [kind for _, _, kind in timeline.summary] == ["run", "idle", "gap"]


def test_history_is_trimmed_to_the_day():
    _observe("run", 30 * 60, 30 * 60 - 5)      # 30 годин тому
    _observe("idle", 29 * 60, 29 * 60 - 5)
    _observe("run", 10, 5)
    timeline = _timeline()
    assert [s.kind for s in timeline.spans if s.kind != "gap"] == ["run"]


# ── Розмітка ────────────────────────────────────────────────────────────────


def _render(timeline) -> str:
    tpl = templates.env.get_template("_machine_history.html")
    return tpl.render(timeline=timeline, machine_name="350i Loader")


def test_markup_names_every_span_and_admits_where_data_is_missing():
    _observe("run", 60, 30)
    _observe("off", 29.5, 20)
    html = _render(_timeline())
    assert 'class="fh-span is-run"' in html
    assert 'class="fh-span is-off"' in html
    assert 'class="fh-span is-gap"' in html
    assert "немає даних" in html
    # Історія з пам'яті процесу — це сказано на екрані, а не лише в коді.
    assert "починається з її запуску" in html
    assert "<script" not in html


def test_markup_of_empty_history_says_so():
    html = _render(_timeline())
    assert "Даних ще немає" in html
    assert "<svg" not in html
