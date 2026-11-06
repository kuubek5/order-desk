"""Зрозумілість «Статистики»: речення «Головне», порівняння за днем тижня, міні-графіки.

Числа тут не перевіряються вдруге (це `test_stats_done_rule.py`), лише чи чесно їх
показано: де правило мовчить, де порівняння не бреше зсувом днів, де екран рендериться.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlencode

import pytest
from sqlalchemy.orm import Session

from app.models import Order
from app.services import stats_charts as charts
from app.services import stats_data as data
from app.services import stats_insights as ins
from app.services import stats_view as view
from tests.test_stats import ADMIN, _client, app_db, engine  # noqa: F401 — фікстури

QUERY = {"period": "custom", "from": "2026-10-05", "to": "2026-11-06"}


def _day(lab=0, client=0, works=1) -> dict:
    return {"lab": lab, "client": client, "works": works, "noqty": 0}


# ── Те саме на тому самому дні тижня ─────────────────────────────────────────
def test_previous_day_keeps_the_weekday():
    start = date(2026, 10, 5)
    period = data.st.Period(start, start + timedelta(days=29))
    earlier = period.previous().working_days()
    for day in period.working_days():
        before = view.same_weekday_before(day, earlier)
        assert before is not None, "у 30 днях кожен день тижня є й раніше"
        assert before.weekday() == day.weekday(), "п'ятниця несе вихідні: зсув дав би хибну різницю"
        assert before in earlier


def test_previous_day_is_none_without_history():
    assert view.same_weekday_before(date(2026, 11, 3), []) is None


def test_was_tick_never_leaves_the_plot():
    """Стара висота вища за всі нинішні: шкала мусить її вмістити."""
    days = [date(2026, 11, 2), date(2026, 11, 3)]
    by_day = {d: _day(lab=10) for d in days}
    was = {days[0]: (date(2026, 10, 26), 500)}
    chart = charts.day_chart(days, by_day, [True, True], 10, None, width=1100, height=330, compact=False, was=was)
    bar = chart["bars"][0]
    assert chart["top"] <= bar["was_y"] < chart["base"]
    assert "було Пн 26.10: 500 од." in bar["tip"]
    assert chart["bars"][1]["was_y"] is None, "нема з чим порівняти — нема риски"


def test_close_average_labels_do_not_overlap():
    upper, lower = charts._line_label_ys(100.0, 101.0)
    assert lower - upper >= 15
    assert charts._line_label_ys(100.0, 200.0) == (104.0, 204.0)


def test_first_date_is_dropped_next_to_monday():
    """«27.0831.08»: підпис четверга злипався з понеділком через два стовпчики."""
    days = [date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 31)]
    by_day = {d: _day(lab=1) for d in days}
    chart = charts.day_chart(days, by_day, [True] * 3, 1, None, width=358, height=210, compact=True)
    assert [b["label"] for b in chart["bars"]] == ["", "", "31.08"]


# ── Стрілка не малює руху, якого не видно в числі ────────────────────────────
@pytest.mark.parametrize(
    ("delta", "digits", "bad_up", "sym", "bad", "flat"),
    [
        (0, 0, False, "→", False, True),
        (0.04, 1, True, "→", False, True),
        (5, 0, False, "↑", False, False),
        (-5, 0, False, "↓", True, False),
        (0.3, 1, True, "↑", True, False),
        (-0.3, 1, True, "↓", False, False),
    ],
)
def test_arrow_tone(delta, digits, bad_up, sym, bad, flat):
    arrow = view._arrow(delta, digits=digits, bad_up=bad_up)
    assert (arrow["sym"], arrow["bad"], arrow["flat"]) == (sym, bad, flat)


# ── Міні-графіки ─────────────────────────────────────────────────────────────
def test_sparkline_needs_a_shape():
    assert charts.sparkline([5]) is None
    assert charts.sparkline([0, 0, 0]) is None
    alone = charts.sparkline([1, 2, 3])
    spark = charts.sparkline([1, 2, 3], prev=[6, 6, 6, 6])
    assert spark["d"].startswith("M") and spark["prev_d"] and alone["prev_d"] is None
    assert spark["last"]["y"] > alone["last"]["y"], "спільна шкала: нинішнє нижче за вищий старий підсумок"


def test_running_total():
    assert charts.running_total([1, 0, 2]) == [1, 1, 3]


def test_forecast_chart_splits_past_and_future():
    past = [
        {"day": date(2026, 11, 2), "actual": 100, "forecast": 90, "anomaly": False, "tip": "a"},
        {"day": date(2026, 11, 3), "actual": 120, "forecast": None, "anomaly": True, "tip": "b"},
    ]
    future = [
        {"day": date(2026, 11, 4), "value": 110, "low": 90, "high": 130, "tip": "c"},
        {"day": date(2026, 11, 5), "value": 115, "low": 95, "high": 135, "tip": "d"},
    ]
    chart = charts.forecast_chart(past, future)
    assert chart["actual_d"] and chart["next_d"] and chart["band_d"]
    assert chart["forecast_d"] is None, "одна точка прогнозу — це ще не лінія"
    assert chart["slots"][1]["anomaly"] and chart["slots"][2]["future"]
    assert chart["slots"][2]["ya"] is None, "у майбутньому факту немає"
    assert chart["slots"][0]["x"] < chart["today_x"] <= chart["slots"][2]["x"]


def test_forecast_chart_without_points():
    assert charts.forecast_chart([], []) is None


# ── Головне за період ────────────────────────────────────────────────────────
def test_tempo_line_tone_and_flat():
    assert ins.tempo_line(110, 100, "X")["tone"] == "good"
    low = ins.tempo_line(90, 100, "X")
    assert low["tone"] == "bad" and "на 10 % менше" in low["rest"]
    assert ins.tempo_line(101, 100, "X")["tone"] == "flat", "1 % — не новина"
    assert ins.tempo_line(100, None, "X") is None, "нема з чим порівняти"


def test_peak_needs_several_days_and_a_real_peak():
    friday, monday = date(2026, 11, 6), date(2026, 11, 9)
    line = ins.peak_line({friday: _day(lab=200), monday: _day(lab=100)}, 150)
    assert line["side"] == "day=2026-11-06" and "(пт + сб + нд)" in line["lead"]
    assert ins.peak_line({friday: _day(lab=200)}, 200) is None
    assert ins.peak_line({friday: _day(lab=105), monday: _day(lab=95)}, 100) is None


def test_client_lines_ignore_small_moves_and_nameless():
    qs = lambda key: f"?client={key}"  # noqa: E731
    now = {"a": {"units": 300, "label": "А"}, "b": {"units": 10, "label": "Б"}, "c": {"units": 105, "label": "В"}, "": {"units": 900}}
    was = {"a": {"units": 100, "label": "А"}, "b": {"units": 80, "label": "Б"}, "c": {"units": 100, "label": "В"}}
    up, down = ins.client_lines(now, was, qs)
    assert "А" in up["lead"] and up["qs"] == "?client=a" and up["tone"] == "good"
    assert "Б" in down["lead"] and "−70" in down["rest"] and down["tone"] == "bad"
    assert ins.client_lines({"c": now["c"]}, {"c": was["c"]}, qs) == [], "5 од. і 5 % — не новина"


def test_gone_client_counts_as_a_drop():
    lines = ins.client_lines({}, {"a": {"units": 50, "label": "А"}}, lambda key: "")
    assert len(lines) == 1 and lines[0]["tone"] == "bad"


def test_new_client_says_so():
    line = ins.client_lines({"a": {"units": 50, "label": "А"}}, {}, lambda key: "")[0]
    assert "раніше робіт не було" in line["rest"]


def test_rework_up_is_bad_down_is_good():
    assert ins.rework_line(2.0, 1.0)["tone"] == "bad"
    assert ins.rework_line(1.0, 2.0)["tone"] == "good"
    assert ins.rework_line(1.0, 1.1) is None


def test_gaps_come_first_and_list_is_capped():
    moved = [{"group": "zr", "label": "Цирконій", "share": 60, "dpp": 5.0}]
    now = {"a": {"units": 300, "label": "А"}, "b": {"units": 10, "label": "Б"}}
    was = {"a": {"units": 100, "label": "А"}, "b": {"units": 80, "label": "Б"}}
    lines = ins.overview_lines(
        per_day=200,
        was_per_day=100,
        by_day={date(2026, 11, 6): _day(lab=400), date(2026, 11, 9): _day(lab=100)},
        clients_now=now,
        clients_was=was,
        client_qs=lambda key: "",
        mat_rows=moved,
        rework_pct=5.0,
        rework_was_pct=1.0,
        cover_missing=2,
        noqty=3,
        prev_label="X",
    )
    assert len(lines) == ins.MAX_LINES
    assert "Немає вкладок за 2 робочі дні" in lines[0]["lead"]
    assert "3 роботи без кількості" in lines[1]["lead"]


@pytest.mark.parametrize(("n", "word"), [(1, "день"), (2, "дні"), (5, "днів"), (11, "днів"), (21, "день"), (22, "дні")])
def test_plural(n, word):
    assert ins.plural(n, "день", "дні", "днів") == word


# ── Шапка ────────────────────────────────────────────────────────────────────
def test_presets_are_whole_periods():
    p = view.parse_params(QUERY, date(2026, 11, 6), True)
    presets = {row["label"]: row for row in view._period_presets(p, date(2026, 11, 6))}
    assert "from=2026-10-01" in presets["Минулий місяць"]["qs"] and "to=2026-10-31" in presets["Минулий місяць"]["qs"]
    assert "from=2026-11-02" in presets["Цей тиждень"]["qs"] and "to=2026-11-06" in presets["Цей тиждень"]["qs"]
    assert not presets["Цей місяць"]["on"]
    same = view.parse_params({"period": "custom", "from": "2026-11-01", "to": "2026-11-06"}, date(2026, 11, 6), True)
    assert {row["label"]: row["on"] for row in view._period_presets(same, date(2026, 11, 6))}["Цей місяць"]


# ── Справжній рендер ─────────────────────────────────────────────────────────
def _seed(bind) -> None:
    with Session(bind) as session:
        for tab, sum3d in (("03.11.26", "10-00-00"), ("04.11.26", "11-00-00")):
            session.add(Order(source="lab", sheet_tab=tab, quantity="5", sum3d_id=sum3d, material_color="моно а3"))
        session.commit()
    data.clear_cache()


def test_overview_renders_new_controls(app_db, engine):  # noqa: F811
    _seed(engine)
    status, _, page = _client(ADMIN).get("/stats?" + urlencode(QUERY))
    assert status == 200
    assert 'data-pills' in page and 'class="sx-tools"' in page
    assert "Головне за період" in page and "data-hint=" in page
    assert 'data-view="table"' in page and "sx-daytbl" in page
    assert 'class="sx-spark"' in page
    screen = page.split('id="sx-root"')[1].split('id="sx-tip"')[0]
    assert "✕" not in screen and "↑" not in screen, "значки спільним набором, не шрифтовими символами"


def test_day_table_has_the_same_rows_as_the_chart(app_db, engine):  # noqa: F811
    _seed(engine)
    _, _, page = _client(ADMIN).get("/stats?" + urlencode(QUERY))
    wide = page.split('data-chart="dw"')[1].split("</svg>")[0]
    bars = len(re.findall(r'class="sx-bar', wide))
    rows = len(re.findall(r"<tr>", page.split("sx-daytbl")[1].split("</tbody>")[0])) - 1
    assert bars == rows > 0


def test_filter_reset_only_when_filtered(app_db, engine):  # noqa: F811
    _seed(engine)
    client = _client(ADMIN)
    _, _, plain = client.get("/stats?" + urlencode(QUERY))
    assert "sx-reset" not in plain
    _, _, filtered = client.get("/stats?" + urlencode({**QUERY, "src": "lab"}))
    assert "sx-reset" in filtered


def test_forecast_tab_has_no_controls_that_do_nothing(app_db, engine):  # noqa: F811
    """Прогноз не залежить від періоду й фільтрів: перемикачі там лише вводили б в оману."""
    _seed(engine)
    status, _, page = _client(ADMIN).get("/stats?tab=forecast")
    assert status == 200
    assert 'class="sx-tools"' not in page and 'id="sx-root"' in page


@pytest.mark.parametrize("tab", ["intake", "mail", "techs", "production"])
def test_every_tab_keeps_the_panel(app_db, engine, tab):  # noqa: F811
    _seed(engine)
    status, _, page = _client(ADMIN).get("/stats?" + urlencode({**QUERY, "tab": tab}))
    assert status == 200 and 'class="sx-tools"' in page and "sx-dl" in page
