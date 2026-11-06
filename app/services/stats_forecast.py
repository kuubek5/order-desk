"""Прогноз на тиждень наперед (STATS_BRIEF.md §6) і журнал його точності.

Модель навмисно проста й перевірювана: середнє тих самих днів тижня за 8 тижнів без
найбільшого й найменшого, помножене на тренд (останні 2 тижні до попередніх 6). Жодного
«чорного ящика»: кожне число можна перерахувати руками.

Чесність перевірки тримається на двох речах:

* прогноз ЗАПИСУЄТЬСЯ наперед (`ForecastSnapshot`), раз на робочий день. Факт порівнюється
  із записаним, а не з перерахованим заднім числом;
* поруч завжди стоїть наївний прогноз «як тиждень тому». Модель корисна лише тоді, коли
  вона його б'є, і екран каже це прямо, навіть якщо відповідь «не б'є».

Поки журнал порожній, показується розрахунок на минулих днях: кожен день порахований
лише з того, що було відомо до нього. Він підписаний окремо й журналом не вважається.

День — вкладка таблиці: п'ятниця несе й вихідні. Факт — виконані одиниці за правилом
статистики, тому СЛМ сюди не входить. Дні, які адмін позначив нетиповими, модель не вчить
і в підсумок точності не йдуть.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import stats as st
from app.business_day import business_tab_today, business_today, next_tab_day, prev_tab_day, tab_day
from app.models import ForecastSnapshot, StatsAnomalyDay
from app.services import stats_charts as charts
from app.services import stats_data as data

MODEL_VERSION = "seasonal-8w-v1"
SERIES = ("total", "lab", "client", "zr", "pmma", "ti", "oth")
SERIES_LABELS = {
    "total": "Усього",
    "lab": "Лаба",
    "client": "Клієнти",
    "zr": "Цирконій",
    "pmma": "ПММА",
    "ti": "Титан",
    "oth": "Інше",
}
HISTORY_WEEKS = 16
BASE_SAMPLES = 8
MIN_WEEKS = 6  # менше тих самих днів тижня — «замало даних», а не число
TREND_RECENT_DAYS = 14
TREND_OLDER_DAYS = 42
TREND_LIMITS = (0.7, 1.3)  # свято чи один аврал не мусять перекосити весь тиждень
TARGET_ERROR_PCT = 15.0  # ціль власника: ±15 % на день
HINDCAST_DAYS = 30
MIN_ERRORS_FOR_RANGE = 10
HORIZON_DAYS = 5


# ── Дані ───────────────────────────────────────────────────────────────────
def anomaly_days(db: Session) -> set[date]:
    return set(db.scalars(select(StatsAnomalyDay.day)))


def history(db: Session, end: date) -> dict[str, dict[date, int]]:
    """Серія → день → одиниці. Лише дні, де справді була робота: день без вкладки не нуль."""
    days = st.Period(end - timedelta(weeks=HISTORY_WEEKS), end).working_days()
    group_of = data._material_group_map(db)
    out: dict[str, dict[date, int]] = {name: {} for name in SERIES}
    worked: set[date] = set()
    for fact in data.facts(db, days, data.Filters()):
        if fact.works <= 0:
            continue
        worked.add(fact.day)
        group = group_of.get(fact.material_id, "oth")
        for name in ("total", fact.kind, group):
            out[name][fact.day] = out[name].get(fact.day, 0) + fact.units
    # Робочий день без, скажімо, титану — це нуль титану, а не відсутній день.
    for name in SERIES:
        for day in worked:
            out[name].setdefault(day, 0)
    return out


# ── Модель ─────────────────────────────────────────────────────────────────
def predict(series: dict[date, int], target: date, upto: date, skip: set[date]) -> float | None:
    """Прогноз на `target` лише з того, що було відомо до дня `upto`. None — замало даних."""
    known = {day: units for day, units in series.items() if day < upto and day not in skip}
    same = sorted((day for day in known if day.weekday() == target.weekday()), reverse=True)
    if len(same) < MIN_WEEKS:
        return None
    sample = sorted(known[day] for day in same[:BASE_SAMPLES])
    trimmed = sample[1:-1] if len(sample) >= 5 else sample
    base = sum(trimmed) / len(trimmed)

    recent_from = upto - timedelta(days=TREND_RECENT_DAYS)
    older_from = recent_from - timedelta(days=TREND_OLDER_DAYS)
    recent = [units for day, units in known.items() if day >= recent_from]
    older = [units for day, units in known.items() if older_from <= day < recent_from]
    trend = 1.0
    if recent and older and sum(older) > 0:
        low, high = TREND_LIMITS
        ratio = (sum(recent) / len(recent)) / (sum(older) / len(older))
        trend = min(high, max(low, ratio))
    return base * trend


def naive(series: dict[date, int], target: date, skip: set[date]) -> int | None:
    """«Як тиждень тому»: найпростіший суперник, якого модель мусить бити."""
    day = target - timedelta(days=7)
    return None if day in skip else series.get(day)


def _error_pct(forecast: float | None, actual: int) -> float | None:
    if forecast is None or actual <= 0:
        return None
    return (forecast - actual) / actual * 100


def backtest(series: dict[date, int], upto: date, skip: set[date], days: int = HINDCAST_DAYS) -> list[dict]:
    """Кожен із останніх днів, порахований лише з минулого відносно нього."""
    done = sorted((day for day in series if day < upto), reverse=True)[:days]
    rows = []
    for day in sorted(done):
        actual = series[day]
        forecast = predict(series, day, day, skip)
        rows.append(
            {
                "day": day,
                "actual": actual,
                "forecast": forecast,
                "naive": naive(series, day, skip),
                "anomaly": day in skip,
            }
        )
    return rows


def _score(rows: list[dict], key: str) -> dict:
    errors = [
        abs(pct)
        for row in rows
        if not row["anomaly"]
        for pct in [_error_pct(row[key], row["actual"])]
        if pct is not None
    ]
    if not errors:
        return {"n": 0, "mape": None, "within": None}
    return {
        "n": len(errors),
        "mape": sum(errors) / len(errors),
        "within": sum(1 for e in errors if e <= TARGET_ERROR_PCT) / len(errors) * 100,
    }


def error_range(rows: list[dict]) -> tuple[float, float] | None:
    """10-й і 90-й перцентиль відносної похибки: який множник дає діапазон навколо числа."""
    ratios = []
    for row in rows:
        if row["anomaly"] or not row["forecast"] or row["actual"] <= 0:
            continue
        ratios.append(row["actual"] / row["forecast"])
    if len(ratios) < MIN_ERRORS_FOR_RANGE:
        return None
    low, high = st.percentile(ratios, 10), st.percentile(ratios, 90)
    return (low, high) if low is not None and high is not None else None


def targets_from(first: date, count: int = HORIZON_DAYS) -> list[date]:
    out = [first]
    while len(out) < count:
        out.append(next_tab_day(out[-1]))
    return out


# ── Запис наперед ──────────────────────────────────────────────────────────
def write_snapshot(db: Session, today: date | None = None) -> int:
    """Записати прогноз поточного робочого дня. Другий виклик того самого дня — нічого."""
    made_on = today or business_today()
    exists = db.scalar(select(ForecastSnapshot.id).where(ForecastSnapshot.made_on == made_on).limit(1))
    if exists is not None:
        return 0
    first = tab_day(made_on)
    all_series = history(db, first)
    skip = anomaly_days(db)
    written = 0
    for name in SERIES:
        series = all_series[name]
        band = error_range(backtest(series, first, skip))
        for target in targets_from(first):
            value = predict(series, target, first, skip)
            if value is None:
                continue
            db.add(
                ForecastSnapshot(
                    made_on=made_on,
                    target_day=target,
                    series=name,
                    value=value,
                    low=value * band[0] if band else None,
                    high=value * band[1] if band else None,
                    model_version=MODEL_VERSION,
                )
            )
            written += 1
    try:
        db.commit()
    except IntegrityError:
        # Двоє одночасно: перший уже записав той самий день, і це не збій.
        db.rollback()
        return 0
    return written


def snapshots_of(db: Session, series: str, first: date, last: date) -> dict[date, ForecastSnapshot]:
    """Найсвіжіший запис на кожен день, зроблений не пізніше самого дня."""
    out: dict[date, ForecastSnapshot] = {}
    rows = db.scalars(
        select(ForecastSnapshot)
        .where(
            ForecastSnapshot.series == series,
            ForecastSnapshot.target_day >= first,
            ForecastSnapshot.target_day <= last,
        )
        .order_by(ForecastSnapshot.made_on)
    )
    for row in rows:
        if row.made_on <= row.target_day or tab_day(row.made_on) == row.target_day:
            out[row.target_day] = row
    return out


# ── Вид ────────────────────────────────────────────────────────────────────
def _num(value: float | None) -> str:
    return "—" if value is None else st.nf(round(value))


def _pct(value: float | None) -> str:
    # Через round: `+.0f` дав би «-0 %» на похибці в кілька десятих.
    return "—" if value is None else f"{round(value):+d} %".replace("-", "−")


def _pct_abs(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} %"


def build(db: Session, today: date | None = None) -> dict:
    first = today or business_tab_today()
    all_series = history(db, first)
    skip = anomaly_days(db)
    total = all_series["total"]
    weeks_known = max(
        (sum(1 for day in total if day < first and day.weekday() == weekday) for weekday in range(5)),
        default=0,
    )
    enough = predict(total, first, first, skip) is not None

    hindcast = backtest(total, first, skip)
    band = error_range(hindcast)
    plan = []
    for target in targets_from(first):
        cells = {}
        for name in SERIES:
            cells[name] = _num(predict(all_series[name], target, first, skip))
        value = predict(total, target, first, skip)
        plan.append(
            {
                "day": target,
                "value": value,
                "low_v": value * band[0] if value is not None and band else None,
                "high_v": value * band[1] if value is not None and band else None,
                "label": f"{st.DOW[target.weekday()]} {target.day:02d}.{target.month:02d}",
                "sub": "пт + сб + нд" if target.weekday() == 4 else "",
                "cells": cells,
                "low": _num(value * band[0]) if value is not None and band else None,
                "high": _num(value * band[1]) if value is not None and band else None,
                "naive": _num(naive(total, target, skip)),
            }
        )

    written = snapshots_of(db, "total", first - timedelta(days=HINDCAST_DAYS * 2), prev_tab_day(first))
    journal_rows = []
    for row in hindcast:
        record = written.get(row["day"])
        journal_rows.append(
            {
                **row,
                "forecast": record.value if record is not None else None,
                "calc": row["forecast"],
            }
        )
    journal_score = _score(journal_rows, "forecast")
    journal_naive = _score([r for r in journal_rows if r["forecast"] is not None], "naive")
    hind_score = _score(hindcast, "forecast")
    hind_naive = _score([r for r in hindcast if r["forecast"] is not None], "naive")

    def beats(model: dict, rival: dict) -> bool | None:
        if not model["n"] or rival["mape"] is None:
            return None
        return model["mape"] < rival["mape"]

    rows_view = []
    for row in reversed(journal_rows):
        shown = row["forecast"] if row["forecast"] is not None else row["calc"]
        error = _error_pct(shown, row["actual"])
        rows_view.append(
            {
                "day": row["day"],
                "iso": row["day"].isoformat(),
                "label": f"{st.DOW[row['day'].weekday()]} {row['day'].day:02d}.{row['day'].month:02d}",
                "actual": st.nf(row["actual"]),
                "forecast": _num(shown),
                "written": row["forecast"] is not None,
                "error": _pct(error),
                "within": error is not None and abs(error) <= TARGET_ERROR_PCT,
                "naive_error": _pct(_error_pct(row["naive"], row["actual"])),
                "anomaly": row["anomaly"],
            }
        )

    def _past_tip(row: dict, shown: float | None) -> str:
        tip = f"{st.DOW[row['day'].weekday()]} {row['day'].day:02d}.{row['day'].month:02d} · факт {st.nf(row['actual'])} од."
        if shown is not None:
            tip += f" · прогноз {_num(shown)} · похибка {_pct(_error_pct(shown, row['actual']))}"
        if row["anomaly"]:
            tip += " · нетиповий день"
        return tip

    past_points = []
    for row in journal_rows:
        shown = row["forecast"] if row["forecast"] is not None else row["calc"]
        past_points.append({"day": row["day"], "actual": row["actual"], "forecast": shown, "anomaly": row["anomaly"], "tip": _past_tip(row, shown)})
    future_points = []
    for item in plan:
        tip = f"{item['label']}{' (+сб нд)' if item['sub'] else ''} · прогноз {_num(item['value'])} од."
        if item["low"] is not None:
            tip += f" · діапазон {item['low']}–{item['high']}"
        tip += f" · тиждень тому {item['naive']}"
        future_points.append({"day": item["day"], "value": item["value"], "low": item["low_v"], "high": item["high_v"], "tip": tip})

    return {
        "chart": charts.forecast_chart(past_points, future_points) if enough else None,
        "enough": enough,
        "weeks_known": weeks_known,
        "min_weeks": MIN_WEEKS,
        "plan": plan,
        "columns": [(name, SERIES_LABELS[name]) for name in SERIES],
        "has_band": band is not None,
        "journal": {
            "n": journal_score["n"],
            "mape": _pct_abs(journal_score["mape"]),
            "within": _pct_abs(journal_score["within"]),
            "naive_mape": _pct_abs(journal_naive["mape"]),
            "beats": beats(journal_score, journal_naive),
        },
        "hindcast": {
            "n": hind_score["n"],
            "mape": _pct_abs(hind_score["mape"]),
            "mape_v": hind_score["mape"],
            "within": _pct_abs(hind_score["within"]),
            "naive_mape": _pct_abs(hind_naive["mape"]),
            "naive_v": hind_naive["mape"],
            "beats": beats(hind_score, hind_naive),
        },
        "target_pct": round(TARGET_ERROR_PCT),
        "rows": rows_view,
        "model_version": MODEL_VERSION,
    }


def week_brief(db: Session, today: date | None = None) -> dict | None:
    """Для «Огляду»: цей тиждень — факт за минулі дні плюс прогноз на решту."""
    first = today or business_tab_today()
    total = history(db, first)["total"]
    skip = anomaly_days(db)
    monday = first - timedelta(days=first.weekday())
    fact = 0
    expected = 0.0
    for offset in range(5):
        day = monday + timedelta(days=offset)
        if day < first:
            fact += total.get(day, 0)
            continue
        value = predict(total, day, first, skip)
        if value is None:
            return None
        expected += value
    return {"total": st.nf(round(fact + expected)), "fact": st.nf(fact)}


def toggle_anomaly(db: Session, day: date, actor: str | None) -> bool:
    """Позначити день нетиповим або зняти позначку. True — день тепер нетиповий."""
    row = db.scalar(select(StatsAnomalyDay).where(StatsAnomalyDay.day == day))
    if row is not None:
        db.delete(row)
        db.commit()
        return False
    db.add(StatsAnomalyDay(day=day, marked_by=actor, created_at=datetime.now()))
    db.commit()
    return True
