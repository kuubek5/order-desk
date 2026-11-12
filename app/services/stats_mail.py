"""Вкладка «Пошта» (STATS_BRIEF.md §4.4): коли приходять листи й що з клієнтами.

Карта рахує ЛИСТИ за `received_at`, а не одиниці: кількість стає відомою лише при
прийнятті, тоді як карта відповідає на «коли сідати розбирати». Листи, які відсік
фільтр «не наша робота», сюди не входять.

Клієнти, матеріали й кольори — виконані роботи за тим самим правилом, що й решта
статистики (`stats_data.done_clause`). Вкладка завжди про клієнтів, тому фільтри
«джерело» й «технік» тут не діють.

Тренд дивиться на власний ритм клієнта, а не на спільний поріг: хто шле щодня, той
за два тижні тиші вже «зник», а хто раз на місяць — ні.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app import stats as st
from app.business_day import get_rollover
from app.models import EmailMessage, Order
from app.services import stats_charts as charts
from app.services import stats_data as data
from app.services import stats_view as view

# Скільки днів назад вчити ритм клієнта.
RHYTHM_DAYS = 180
# Менше активних днів — ритму ще немає, і «зник» сказати не можна.
RHYTHM_MIN_DAYS = 4
# «Зник»: мовчить довше за MISSING_GAPS звичних інтервалів і щонайменше MISSING_MIN_DAYS.
MISSING_GAPS = 3
MISSING_MIN_DAYS = 14
# «Зростає / падає»: зміна хоча б на стільки відсотків при обсязі від стількох одиниць.
TREND_PCT = 25
TREND_MIN_UNITS = 5

TREND_LABELS = {
    "up": "зростає",
    "down": "падає",
    "steady": "рівно",
    "new": "новий",
    "gone": "зник",
    "quiet": "мовчить",
}
TOP_CLIENTS = 40


def _letter_filter(days: list[date]) -> list:
    roll = get_rollover()
    first, last = min(days), max(days)
    # П'ятниця несе вихідні: листи суботи й неділі теж її.
    after = last + timedelta(days=3 if last.weekday() == 4 else 1)
    return [
        EmailMessage.received_at >= datetime.combine(first, roll),
        EmailMessage.received_at < datetime.combine(after, roll),
        EmailMessage.filter_category.is_(None),
    ]


def letter_heat(db: Session, days: list[date], client: str = "") -> dict[str, Any]:
    """Листи за днем тижня й годиною."""
    grid = st.empty_grid()
    total = 0
    if not days:
        return {"grid": grid, "n": 0}

    def build():
        dow = cast(func.strftime("%w", EmailMessage.received_at), Integer)
        hour = cast(func.strftime("%H", EmailMessage.received_at), Integer)
        return db.execute(
            select(dow, hour, EmailMessage.from_name, func.count())
            .where(*_letter_filter(days))
            .group_by(dow, hour, EmailMessage.from_name)
        ).all()

    for weekday, clock, name, count in data._cached(("mail_heat", tuple(days)), build):
        if weekday is None or clock is None:
            continue
        if client and st.client_key(name) != client:
            continue
        grid[(int(weekday) + 6) % 7][int(clock)] += int(count)
        total += int(count)
    return {"grid": grid, "n": total}


def letters_in_cell(db: Session, days: list[date], weekday: int, hour: int, client: str = "") -> list[dict]:
    """Листи однієї клітинки карти для бічної панелі."""
    if not days:
        return []
    rows = db.execute(
        select(
            EmailMessage.id,
            EmailMessage.received_at,
            EmailMessage.from_name,
            EmailMessage.from_address,
            EmailMessage.material_color_guess,
            EmailMessage.status,
        ).where(
            *_letter_filter(days),
            cast(func.strftime("%H", EmailMessage.received_at), Integer) == hour,
            cast(func.strftime("%w", EmailMessage.received_at), Integer) == (weekday + 1) % 7,
        )
    ).all()
    out = [
        {
            "id": r[0],
            "at": r[1],
            "who": r[2] or r[3] or "—",
            "guess": r[4],
            "status": r[5],
        }
        for r in rows
        if not client or st.client_key(r[2]) == client
    ]
    # Ключ — сам час листа (тип відомий), а не значення словника змішаного типу:
    # новіший mypy (2.4) не приймає такий ключ сортування.
    out.sort(key=lambda r: r["at"] if isinstance(r["at"], datetime) else datetime.min, reverse=True)
    return out[: data.LIST_LIMIT]


# ── Ритм і тренд ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Rhythm:
    active_days: int
    gap_days: float | None  # звичний інтервал між днями з роботою
    last_day: date | None
    since_days: int | None


def rhythm_of(days: set[date], today: date) -> Rhythm:
    ordered = sorted(days)
    if not ordered:
        return Rhythm(0, None, None, None)
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:])]
    return Rhythm(
        len(ordered),
        float(median(gaps)) if gaps else None,
        ordered[-1],
        (today - ordered[-1]).days,
    )


def trend_of(now_units: int, was_units: int, rhythm: Rhythm) -> str:
    """Одне слово про клієнта."""
    if (
        rhythm.active_days >= RHYTHM_MIN_DAYS
        and rhythm.gap_days is not None
        and rhythm.since_days is not None
        and rhythm.since_days >= max(MISSING_MIN_DAYS, MISSING_GAPS * rhythm.gap_days)
    ):
        return "gone"
    if not now_units:
        return "quiet"
    if not was_units:
        # Без історії взагалі — новий; був раніше й повернувся — це просто зростання.
        return "new" if rhythm.active_days <= RHYTHM_MIN_DAYS else "up"
    if max(now_units, was_units) < TREND_MIN_UNITS:
        return "steady"
    change = st.delta_pct(now_units, was_units) or 0
    if change >= TREND_PCT:
        return "up"
    if change <= -TREND_PCT:
        return "down"
    return "steady"


def client_activity(db: Session, end: date, f: data.Filters) -> dict[str, set[date]]:
    """Дні з виконаною роботою на кожного клієнта за `RHYTHM_DAYS` до кінця періоду."""
    span = st.Period(end - timedelta(days=RHYTHM_DAYS - 1), end).working_days()
    key = ("mail_activity", end, f)

    def build():
        scope = data.scope_for(db, span)
        idx = data.tab_index(db)
        rows = db.execute(
            select(Order.sheet_tab, scope.created, Order.client_name)
            .where(scope.where, data.KIND == "client", data.done_clause(db), *data._filter_clauses(db, f))
            .distinct()
        ).all()
        out: dict[str, set[date]] = {}
        for tab, created, name in rows:
            day = data._day_in(scope, data._raw_day(idx, tab, created))
            client = st.client_key(name)
            if day is not None and client:
                out.setdefault(client, set()).add(day)
        return out

    return data._cached(key, build)


def _client_row(p: view.Params, key: str, now: dict, was: dict, rhythm: Rhythm) -> dict:
    now_units, was_units = int(now.get("units", 0)), int(was.get("units", 0))
    trend = trend_of(now_units, was_units, rhythm)
    gap = rhythm.gap_days
    return {
        "key": key,
        "label": now.get("label") or was.get("label") or key,
        "units": now_units,
        "units_text": st.nf(now_units),
        "works": int(now.get("works", 0)),
        "prev": was_units,
        "delta": st.delta_pct(now_units, was_units) if was_units else None,
        "trend": trend,
        "trend_label": TREND_LABELS[trend],
        "gap": None if gap is None else f"{gap:g} дн.".replace(".", ",", 1),
        "since": rhythm.since_days,
        "last": rhythm.last_day,
        "qs": p.qs(filters_over={"client": key}),
    }


def build(db: Session, p: view.Params) -> dict:
    f = replace(p.filters, src="client", tech="")
    days = p.period.working_days()
    prev_days = p.period.previous().working_days()

    heat = letter_heat(db, days, f.client)
    heat_was = letter_heat(db, prev_days, f.client)
    work = data.work_frame(db)
    total, off = st.off_hours(heat["grid"], work)

    clients_now = data.client_units(db, days, f)
    clients_was = data.client_units(db, prev_days, f)
    activity = client_activity(db, p.period.end, f)
    rows = []
    for key in set(clients_now) | set(clients_was) | set(activity):
        if not key:
            continue
        rhythm = rhythm_of(activity.get(key, set()), p.period.end)
        rows.append(_client_row(p, key, clients_now.get(key, {}), clients_was.get(key, {}), rhythm))
    active = sorted((r for r in rows if r["units"]), key=lambda r: (-r["units"], r["label"]))
    gone = sorted((r for r in rows if r["trend"] == "gone"), key=lambda r: (-r["prev"], r["since"] or 0))
    counts = {name: sum(1 for r in rows if r["trend"] == name) for name in TREND_LABELS}

    group_of = data._material_group_map(db)
    cur = data.summarize(data.facts(db, days, f), days, group_of)
    was = data.summarize(data.facts(db, prev_days, f), prev_days, group_of)
    mat_rows = []
    for group in data.MATERIAL_GROUPS:
        now_units, was_units = cur["materials"][group], was["materials"][group]
        mat_rows.append(
            {
                "group": group,
                "label": data.MATERIAL_GROUP_LABELS[group],
                "units": now_units,
                "units_text": st.nf(now_units),
                "share": round(now_units / cur["units"] * 100) if cur["units"] else 0,
                "prev": was_units,
            }
        )
    colors = data.color_units(db, days, f)
    return {
        "letters": st.nf(heat["n"]),
        "letters_n": heat["n"],
        "letters_was": st.nf(heat_was["n"]),
        "letters_was_n": heat_was["n"],
        "letters_arrow": view._arrow(st.delta_pct(heat["n"], heat_was["n"])),
        "off_pct": round(off / total * 100) if total else None,
        "off_text": st.nf(off),
        "units": st.nf(cur["units"]),
        "units_n": cur["units"],
        "units_was": st.nf(was["units"]),
        "units_was_n": was["units"],
        "units_arrow": view._arrow(st.delta_pct(cur["units"], was["units"])),
        "active_n": len(active),
        "counts": counts,
        "chart": charts.heatmap(heat["grid"], "mail", unit="лист.", frame=work),
        "peak": heat["n"] and max(max(row) for row in heat["grid"]),
        "clients": active[:TOP_CLIENTS],
        "clients_more": max(0, len(active) - TOP_CLIENTS),
        "all_clients": active,
        "gone": gone[:TOP_CLIENTS],
        "gone_all": gone,
        "mat_rows": charts.share_rows(mat_rows),
        "color_rows": charts.share_rows(
            [{"color": c["color"], "group": c["group"], "units": c["units"], "units_text": st.nf(c["units"])} for c in colors[:8]]
        ),
        "colors": colors,
        "grid": heat["grid"],
        "work_frame": work.text,
    }


def side(db: Session, p: view.Params, req: view.ListRequest) -> dict:
    """Бічна панель клітинки карти листів."""
    assert req.cell is not None
    _, weekday, hour = req.cell
    letters = letters_in_cell(db, p.period.working_days(), weekday, hour, p.filters.client)
    return {
        "title": f"{st.DOW[weekday]} · {hour:02d}:00–{(hour + 1) % 24:02d}:00",
        "tag": "листи",
        "letters": letters,
        "letters_n": len(letters),
        "limit_hit": len(letters) >= data.LIST_LIMIT,
    }
