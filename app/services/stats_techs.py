"""Вкладка «Техніки» (STATS_BRIEF.md §4.3), лише адмін: обсяг і швидкість на техніка.

Обсяг — виконані роботи лаби за загальним правилом (`stats_data.done_clause`), імена
зведені `st.tech_key` («с» = «c», «Даня» = «ДАня»). Швидкість «наряд → модель» існує
лише з 12.10.26, коли почали записувати `row_seen_at` і `job_code_at`, і лише в робіт,
де ці моменти відомі, тому поруч завжди стоїть кількість вимірів: медіана з трьох
робіт — не висновок.

Брак тут — лише записи з винуватцем «технік», одиницями переробки.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import stats as st
from app.models import Order, ReworkRecord
from app.services import stats_data as data
from app.services import stats_view as view
from app.services.queue import is_rush_comment

# Менше вимірів — медіана на екрані приглушена.
FEW_MEASURES = 10
BLAME_TECH = "технік"
NO_TECH_KEY = ""
NO_TECH_LABEL = "Без техніка"

DEFAULT_SORT = "-units"


def _collect(db: Session, days: list[date], f: data.Filters) -> dict[str, dict]:
    key = ("techs", tuple(days), f)

    def build():
        scope = data.scope_for(db, days)
        idx = data.tab_index(db)
        clauses = [scope.where, data.KIND == "lab", *data._filter_clauses(db, f)]
        out: dict[str, dict] = {}

        def slot(name: str | None) -> dict:
            group = st.tech_key(name) or NO_TECH_KEY
            return out.setdefault(
                group,
                {
                    "units": 0,
                    "works": 0,
                    "rush": 0,
                    "lead": [],
                    "rework_units": 0,
                    "rework_cases": 0,
                    "spelling": {},
                },
            )

        units = data.units_expr(Order.quantity)
        done = db.execute(
            select(Order.sheet_tab, scope.created, Order.technician_name, units, Order.cam_comment).where(
                *clauses, data.done_clause(db)
            )
        ).all()
        for tab, created, name, value, comment in done:
            if data._day_in(scope, data._raw_day(idx, tab, created)) is None:
                continue
            group = slot(name)
            group["works"] += 1
            group["units"] += int(value or 0)
            group["rush"] += 1 if is_rush_comment(comment) else 0
            if name:
                clean = " ".join(name.split())
                group["spelling"][clean] = group["spelling"].get(clean, 0) + 1

        diff = (func.julianday(Order.job_code_at) - func.julianday(Order.row_seen_at)) * 1440.0
        for tab, created, name, minutes in db.execute(
            select(Order.sheet_tab, scope.created, Order.technician_name, diff).where(
                *clauses,
                data.quantity_is_sane(),
                Order.row_seen_at.is_not(None),
                Order.job_code_at.is_not(None),
                Order.job_code_at >= Order.row_seen_at,
            )
        ).all():
            if minutes is None or data._day_in(scope, data._raw_day(idx, tab, created)) is None:
                continue
            slot(name)["lead"].append(float(minutes))

        redo = data.units_expr(ReworkRecord.redo_quantity)
        for tab, created, name, value in db.execute(
            select(Order.sheet_tab, scope.created, Order.technician_name, redo)
            .join(Order, Order.id == ReworkRecord.order_id)
            .where(*clauses, func.lower(func.trim(ReworkRecord.blame)) == BLAME_TECH)
        ).all():
            if data._day_in(scope, data._raw_day(idx, tab, created)) is None:
                continue
            group = slot(name)
            group["rework_cases"] += 1
            group["rework_units"] += int(value or 0)
        return out

    return data._cached(key, build)


def _row(p: view.Params, key: str, raw: dict, was_units: int) -> dict:
    lead = raw["lead"]
    spelling = raw["spelling"]
    label = max(spelling, key=lambda name: (spelling[name], name)) if spelling else (key or NO_TECH_LABEL)
    median_v = st.median(lead)
    p80 = st.percentile(lead, 80)
    works, units = raw["works"], raw["units"]
    return {
        "key": key,
        "label": label if key else NO_TECH_LABEL,
        "units": units,
        "units_text": st.nf(units),
        "works": works,
        "per_work": units / works if works else 0.0,
        "rush_pct": raw["rush"] / works * 100 if works else 0.0,
        "median": median_v,
        "median_text": st.format_minutes(median_v),
        "p80": p80,
        "p80_text": st.format_minutes(p80),
        "n": len(lead),
        "few": len(lead) < FEW_MEASURES,
        "rework_units": raw["rework_units"],
        "rework_cases": raw["rework_cases"],
        "rework_pct": raw["rework_units"] / units * 100 if units else 0.0,
        "delta": st.delta_pct(units, was_units) if was_units else None,
        "prev": was_units,
        "qs": p.qs(tab="overview", filters_over={"tech": key}) if key else None,
    }


def _sort_rows(rows: list[dict], order: str) -> list[dict]:
    desc = order.startswith("-")
    column = order.lstrip("-")

    def value(row: dict):
        item = row[column]
        if column == "label":
            return str(item).casefold()
        return -1.0 if item is None else float(item)

    # Порожні значення завжди внизу: технік без вимірів не «найшвидший».
    filled = [r for r in rows if r[column] is not None]
    empty = [r for r in rows if r[column] is None]
    return sorted(filled, key=value, reverse=desc) + sorted(empty, key=lambda r: str(r["label"]).casefold())


def build(db: Session, p: view.Params) -> dict:
    f = replace(p.filters, src="", client="")
    days = p.period.working_days()
    prev_days = p.period.previous().working_days()
    now = _collect(db, days, f)
    was = _collect(db, prev_days, f)
    rows = [_row(p, key, raw, was.get(key, {}).get("units", 0)) for key, raw in now.items()]
    order = p.ts or DEFAULT_SORT
    rows = _sort_rows(rows, order)
    peak = max((r["units"] for r in rows), default=0) or 1
    for row in rows:
        row["bar"] = round(row["units"] / peak * 100, 1)
    active = order.lstrip("-")
    heads = []
    for column, label, numeric in (
        ("label", "Технік", False),
        ("units", "Одиниці", True),
        ("works", "Роботи", True),
        ("per_work", "Од. на роботу", True),
        ("rush_pct", "На швидку", True),
        ("median", "Наряд → модель, медіана", True),
        ("p80", "80-й перцентиль", True),
        ("n", "Вимірів", True),
        ("rework_units", "Брак, од.", True),
        ("rework_pct", "Брак, %", True),
    ):
        desc_first = numeric and column not in ("median", "p80")
        if active == column:
            target = column if order.startswith("-") else f"-{column}"
        else:
            target = f"-{column}" if desc_first else column
        heads.append(
            {
                "label": label,
                "numeric": numeric,
                "qs": p.qs(ts=target),
                "sort": ("descending" if order.startswith("-") else "ascending") if active == column else None,
            }
        )
    measured = [r for r in rows if r["n"]]
    return {
        "rows": rows,
        "heads": heads,
        "total_units": st.nf(sum(r["units"] for r in rows)),
        "total_works": st.nf(sum(r["works"] for r in rows)),
        "techs_n": sum(1 for r in rows if r["key"]),
        "measured_n": sum(r["n"] for r in measured),
        "few_limit": FEW_MEASURES,
        "lead_ready": data.has_job_code_moments(db),
    }
