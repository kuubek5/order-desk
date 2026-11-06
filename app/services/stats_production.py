"""Вкладка «Виробництво» (STATS_BRIEF.md §4.5): де робота чекає найдовше.

Кожен етап — різниця двох моментів ОДНІЄЇ роботи, календарний час (рішення власника).
Момент невідомий — робота в цьому етапі не бере участі, тому біля кожного числа стоїть
кількість вимірів. Рахуємо виконані роботи: незроблена ще не дійшла до кінця.

Звідки моменти:

* поява рядка — `row_seen_at`, лише з 12.10.26; у пошти — час листа;
* модель — `job_code_at`, лише лаба;
* Sum3D — час із самого ID плюс день вкладки (`st.sum3d_moment`): точно до секунди;
* фрезерування — телеметрія верстата, тому лише за 30 днів і лише для робіт, чий диск
  верстат прочитав. Sum3D ID — час ДИСКА, спільний для кількох робіт, і лише час доби:
  запис верстата береться найближчий після Sum3D у межах `MILL_WINDOW`;
* знайдено й видано — перший такий статус. Лабораторні рядки через видачу в CRM не
  проходять, тому в лаби ці етапи майже порожні, і це не вада.

Від'ємний інтервал означає, що момент записаний заднім числом (наприклад, Sum3D
вписали до появи рядка в CRM), і такий вимір відкидається.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import stats as st
from app.business_day import business_date_of, get_rollover, utc_to_business
from app.models import EmailMessage, MachineReading, Order, StatusEvent
from app.services import stats_data as data
from app.services import stats_view as view
from app.statuses import STATUS_FOUND, STATUS_ISSUED

MILL_WINDOW = timedelta(days=3)
# Sum3D ID — момент запису файлу, програму могли запустити одразу: запас на годинники.
MILL_EARLY = timedelta(minutes=5)
MILL_DAYS = 30
BUCKETS = ((60.0, "до 1 год"), (240.0, "1–4 год"), (1440.0, "4–24 год"), (None, "понад добу"))
FEW_MEASURES = 10

STAGES_LAB = (
    ("seen", "model", "Наряд → модель"),
    ("model", "taken", "Модель → Sum3D"),
    ("taken", "mill_start", "Sum3D → старт фрезерування"),
    ("mill_start", "mill_end", "Фрезерування"),
    ("mill_end", "found", "Кінець фрезерування → знайдено"),
    ("found", "issued", "Знайдено → видано"),
)
STAGES_CLIENT = (
    ("seen", "taken", "Лист → Sum3D"),
    ("taken", "mill_start", "Sum3D → старт фрезерування"),
    ("mill_start", "mill_end", "Фрезерування"),
    ("mill_end", "found", "Кінець фрезерування → знайдено"),
    ("found", "issued", "Знайдено → видано"),
)


@dataclass(frozen=True)
class MillRun:
    start: datetime
    end: datetime


def mill_runs(db: Session, first: datetime, last: datetime) -> dict[str, list[MillRun]]:
    """Програми верстатів за період: Sum3D → проміжки справжньої роботи.

    Початок — перший кадр із відсотком понад нуль: програма може годину стояти в черзі
    верстата на 0 %. Кінець — перші 100 %: потім диск просто чекає, поки його знімуть,
    і це вже не фрезерування. Межа програми — її власний `program_at`, бо диск іде через
    північ і поділ за датою кадру розрізав би його навпіл.
    """

    def build():
        program = func.coalesce(MachineReading.program_at, func.date(MachineReading.captured_at))
        started = case((MachineReading.percent > 0, MachineReading.captured_at), else_=None)
        finished = case((MachineReading.percent >= 100, MachineReading.captured_at), else_=None)
        rows = db.execute(
            select(
                MachineReading.sum3d_id,
                program,
                func.min(started),
                func.min(finished),
                func.max(started),
            )
            .where(
                MachineReading.sum3d_id.is_not(None),
                MachineReading.captured_at >= first,
                MachineReading.captured_at < last,
                MachineReading.percent.is_not(None),
            )
            .group_by(MachineReading.sum3d_id, program)
        ).all()
        out: dict[str, list[MillRun]] = defaultdict(list)
        for sum3d, _, start, finish, latest in rows:
            if start is not None:
                out[sum3d].append(MillRun(start, finish or latest))
        for runs in out.values():
            runs.sort(key=lambda run: run.start)
        return dict(out)

    return data._cached(("mill_runs", first, last), build)


def run_after(runs: list[MillRun], taken: datetime) -> MillRun | None:
    """Перша програма цього ID після Sum3D. Запуск через кілька діб — уже чужий диск."""
    for run in runs:
        if run.start >= taken - MILL_EARLY:
            return run if run.start - taken <= MILL_WINDOW else None
    return None


def _moments(db: Session, days: list[date], f: data.Filters) -> list[dict]:
    key = ("production", tuple(days), f)

    def build():
        scope = data.scope_for(db, days)
        idx = data.tab_index(db)
        rollover = get_rollover()
        rows = db.execute(
            select(
                Order.id,
                Order.sheet_tab,
                scope.created,
                data.KIND,
                Order.sum3d_id,
                Order.row_seen_at,
                Order.job_code_at,
                EmailMessage.received_at,
                Order.row_number,
                Order.work_order_no,
                Order.technician_name,
                Order.client_name,
                Order.material_id,
                Order.material_color,
                data.units_expr(Order.quantity),
            )
            .outerjoin(EmailMessage, EmailMessage.id == Order.source_email_id)
            .where(scope.where, data.done_clause(db), *data._filter_clauses(db, f))
        ).all()

        events: dict[tuple[int, str], datetime] = {}
        for order_id, status, moment in db.execute(
            select(StatusEvent.order_id, StatusEvent.status, func.min(StatusEvent.occurred_at))
            .join(Order, Order.id == StatusEvent.order_id)
            .where(scope.where, StatusEvent.status.in_((STATUS_FOUND, STATUS_ISSUED)), StatusEvent.note.is_(None))
            .group_by(StatusEvent.order_id, StatusEvent.status)
        ).all():
            if moment is not None:
                events[(order_id, status)] = utc_to_business(moment)

        out: list[dict] = []
        moments_taken: list[datetime] = []
        parsed: list[tuple] = []
        for row in rows:
            raw = data._raw_day(idx, row[1], row[2])
            if data._day_in(scope, raw) is None or raw is None:
                continue
            clock = st.first_sum3d_time(row[4])
            if clock is None:
                continue
            seen = row[5]
            work_day = st.work_day_for_sum3d(raw, business_date_of(seen) if seen else None)
            taken = st.sum3d_moment(work_day, clock, rollover)
            moments_taken.append(taken)
            parsed.append((row, taken, raw, clock))

        runs: dict[str, list[MillRun]] = {}
        if moments_taken:
            runs = mill_runs(db, min(moments_taken) - MILL_EARLY, max(moments_taken) + MILL_WINDOW)
        for row, taken, raw, clock in parsed:
            order_id, kind = row[0], row[3]
            run = None
            for sum3d in st.sum3d_ids(row[4]):
                found_run = run_after(runs.get(sum3d, []), taken)
                if found_run is not None and (run is None or found_run.start < run.start):
                    run = found_run
            out.append(
                {
                    "id": order_id,
                    "tab": row[1] or "",
                    "tab_date": raw,
                    "row": row[8],
                    "wo": row[9],
                    "tech": row[10],
                    "client": row[11],
                    "material_id": row[12],
                    "color": row[13],
                    "units": row[14],
                    "sum3d": clock.strftime("%H-%M-%S"),
                    "kind": kind,
                    "seen": row[5] if kind == "lab" else row[7],
                    "model": row[6],
                    "taken": taken,
                    "mill_start": run.start if run else None,
                    "mill_end": run.end if run else None,
                    "found": events.get((order_id, STATUS_FOUND)),
                    "issued": events.get((order_id, STATUS_ISSUED)),
                }
            )
        return out

    return data._cached(key, build)


def _interval(row: dict, start: str, end: str) -> float | None:
    """Хвилини етапу однієї роботи; момент невідомий або записаний заднім числом — None."""
    a, b = row.get(start), row.get(end)
    if a is None or b is None or b < a:
        return None
    return (b - a).total_seconds() / 60


def stage_stats(rows: list[dict], start: str, end: str, label: str) -> dict:
    values = [value for value in (_interval(row, start, end) for row in rows) if value is not None]
    counts = [0] * len(BUCKETS)
    for value in values:
        for index, (limit, _) in enumerate(BUCKETS):
            if limit is None or value < limit:
                counts[index] += 1
                break
    total = len(values)
    median_v = st.median(values)
    p80 = st.percentile(values, 80)
    return {
        "label": label,
        "start": start,
        "end": end,
        "n": total,
        "few": total < FEW_MEASURES,
        "median": median_v,
        "median_text": st.format_minutes(median_v),
        "p80": p80,
        "p80_text": st.format_minutes(p80),
        "buckets": [
            {"label": BUCKETS[i][1], "n": counts[i], "pct": round(counts[i] / total * 100, 1) if total else 0.0, "level": i}
            for i in range(len(BUCKETS))
        ],
    }


def _funnel(rows: list[dict], stages: tuple) -> dict:
    items = [stage_stats(rows, start, end, label) for start, end, label in stages]
    # «Найдовше» — лише серед етапів, де вимірів досить: одна робота висновку не робить.
    enough = [item for item in items if not item["few"] and item["median"] is not None]
    longest = max(enough, key=lambda item: item["median"], default=None)
    for item in items:
        item["longest"] = item is longest
    return {"stages": items, "works": len(rows), "longest": longest["label"] if longest else None}


def side(db: Session, p: view.Params, req: view.ListRequest) -> dict:
    """Клік по етапу: ті самі роботи, що дали цю медіану, найдовші зверху.

    Беремо вже пораховані моменти, а не окремий запит: інакше список міг би розійтись
    із числом поруч. Кешовані рядки не змінюємо, кожен список будує свої.
    """
    assert req.stage is not None
    kind, start, end = req.stage
    stages = STAGES_LAB if kind == "lab" else STAGES_CLIENT
    label = next((title for a, b, title in stages if (a, b) == (start, end)), None)
    works: list[dict] = []
    if label is not None:
        group_of = data._material_group_map(db)
        for row in _moments(db, p.period.working_days(), p.filters):
            if (row["kind"] == "lab") != (kind == "lab"):
                continue
            minutes = _interval(row, start, end)
            if minutes is None:
                continue
            works.append(
                {
                    **row,
                    "group": group_of.get(row["material_id"], "oth"),
                    "minutes": minutes,
                    "wait": st.format_minutes(minutes),
                    "from": row[start],
                    "to": row[end],
                }
            )
    works.sort(key=lambda w: (-w["minutes"], w["id"]))
    return {
        "title": label or "—",
        "tag": "лаба" if kind == "lab" else "клієнти",
        "stage_rows": works[: data.LIST_LIMIT],
        "works_n": len(works),
        "units": st.nf(sum(w["units"] or 0 for w in works)),
        "median_text": st.format_minutes(st.median([w["minutes"] for w in works])),
        "export_qs": "?" + urlencode({**p.query(), **req.query()}),
        "limit_hit": len(works) > data.LIST_LIMIT,
    }


def build(db: Session, p: view.Params) -> dict:
    f = p.filters
    days = p.period.working_days()
    rows = _moments(db, days, f)
    lab = [r for r in rows if r["kind"] == "lab"]
    client = [r for r in rows if r["kind"] != "lab"]
    return {
        "lab": _funnel(lab, STAGES_LAB) if f.src != "client" else None,
        "client": _funnel(client, STAGES_CLIENT) if f.src != "lab" else None,
        "buckets": [label for _, label in BUCKETS],
        "few_limit": FEW_MEASURES,
        "mill_days": MILL_DAYS,
        "seen_since": "12.10.26",
    }

