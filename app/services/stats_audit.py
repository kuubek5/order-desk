"""Звірка екрана «Статистика» для MCP: що саме лягло в цифри, рядок за рядком.

Статистика групує в SQL і окремих робіт не показує, тому хибне число на екрані
не було чим розібрати: 116 443 одиниці за 30 днів сиділи в трьох рядках, яких ніде
не видно (власник 06.11.26). Тут той самий період і ті самі робочі дні, що на
екрані (`stats_data.scope_for`), але з рядками.

`рядки_тут` мусить збігатися з `екран`: це перевірка самої звірки. Розійшлись —
далі цьому зрізу вірити не можна, і поле `збіг_з_екраном` про це скаже.

Правила підрахунку тут не змінюються: зріз лише показує, що дало б кожне з них.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import utc_to_business
from app.models import Order, ReworkRecord
from app.services import stats_data as data
from app.services.mail_rework import rework_order_ids
from app.stats import parse_int_safe

# Той самий час доби, що в `stats_data._sum3d_parts`, плюс повна форма з датою.
_SUM3D = re.compile(r"(?:\d{4}-\d{2}-\d{2}_)?\d{1,2}-\d{2}-\d{2}.*")
_WHOLE_NUMBER = re.compile(r"[0-9]+")
_DATE_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class AuditRow:
    id: int
    day: date
    tab: str | None
    group: str  # "lab" | "client", як `stats_data.KIND`
    source: str | None
    row_number: int | None
    quantity: str | None
    sum3d: str
    work_order_no: str | None
    client: str | None
    technician: str | None
    material: str | None
    kind: str | None
    job_code: str | None
    mill_count: str | None
    status: str | None
    archived_at: datetime | None
    created_at: datetime | None

    @property
    def units(self) -> int | None:
        """Як `stats_data.units_expr`: лише ціле число, решта — «без кількості»."""
        text = (self.quantity or "").strip()
        return int(text) if _WHOLE_NUMBER.fullmatch(text) else None

    @property
    def sum3d_valid(self) -> bool:
        return bool(_SUM3D.fullmatch(self.sum3d))

    @property
    def is_mill_rework(self) -> bool:
        count = parse_int_safe(self.mill_count)
        return count is not None and count >= 2


def load_rows(db: Session, days: Iterable[date]) -> list[AuditRow]:
    day_list = sorted(set(days))
    scope = data.scope_for(db, day_list)
    idx = data.tab_index(db)
    found = db.execute(
        select(
            Order.id,
            Order.sheet_tab,
            scope.created,
            data.KIND,
            Order.source,
            Order.row_number,
            Order.quantity,
            Order.sum3d_id,
            Order.work_order_no,
            Order.client_name,
            Order.technician_name,
            Order.material_color,
            Order.kind,
            Order.job_code,
            Order.mill_count,
            Order.status,
            Order.archived_at,
            Order.created_at,
        ).where(scope.where)
    ).all()
    rows: list[AuditRow] = []
    for row in found:
        day = data._day_in(scope, data._raw_day(idx, row[1], row[2]))
        if day is None:
            continue
        rows.append(
            AuditRow(
                id=row[0],
                tab=row[1],
                day=day,
                group=row[3],
                source=row[4],
                row_number=row[5],
                quantity=row[6],
                sum3d=(row[7] or "").strip(),
                work_order_no=row[8],
                client=row[9],
                technician=row[10],
                material=row[11],
                kind=row[12],
                job_code=row[13],
                mill_count=row[14],
                status=row[15],
                archived_at=row[16],
                created_at=row[17],
            )
        )
    rows.sort(key=lambda r: (r.day, r.row_number if r.row_number is not None else 10**6, r.id))
    return rows


def _tally(rows: Iterable[AuditRow]) -> dict[str, int]:
    works = units = 0
    for row in rows:
        works += 1
        units += row.units or 0
    return {"роботи": works, "одиниць": units}


def _brief(row: AuditRow) -> dict[str, Any]:
    created = utc_to_business(row.created_at) if row.created_at else None
    return {
        "id": row.id,
        "день": row.day.isoformat(),
        "вкладка": row.tab,
        "рядок": row.row_number,
        "джерело": row.source,
        "наряд": row.work_order_no,
        "клієнт": row.client,
        "технік": row.technician,
        "матеріал": row.material,
        "кількість": row.quantity,
        "вид": row.kind,
        "sum3d_id": row.sum3d or None,
        "номер_роботи": row.job_code,
        "який_раз": row.mill_count,
        "статус": row.status,
        "архівована": row.archived_at.isoformat(timespec="seconds") if row.archived_at else None,
        "створена": created.isoformat(timespec="seconds") if created else None,
    }


def odd_reasons(row: AuditRow) -> list[str]:
    """Ознаки рядка, прочитаного зі зсунутими колонками чи з текстом не на місці."""
    reasons: list[str] = []
    if row.sum3d and not row.sum3d_valid:
        reasons.append("sum3d не схожий на час")
    if _WHOLE_NUMBER.fullmatch((row.material or "").strip()) and (row.material or "").strip() not in (
        "500",
        "800",
        "1000",
    ):
        reasons.append("матеріал лише з цифр")
    if _DATE_IN_TEXT.search(row.technician or ""):
        reasons.append("у техніку дата або номер роботи")
    number = (row.work_order_no or "").strip()
    if number and _WHOLE_NUMBER.fullmatch(number) and len(number) < 4:
        reasons.append("короткий номер наряду")
    return reasons


def _rework_ids(db: Session, rows: list[AuditRow]) -> tuple[set[int], set[int]]:
    ids = [row.id for row in rows]
    records = set(db.scalars(select(ReworkRecord.order_id).where(ReworkRecord.order_id.in_(ids))))
    return records, rework_order_ids(db, ids)


def overview(db: Session, days: list[date], *, min_quantity: int, limit: int) -> dict[str, Any]:
    rows = load_rows(db, days)
    screen = data.summarize(data.facts(db, days, data.Filters()), days, data._material_group_map(db))
    here = _tally(rows)
    records, mail_reworks = _rework_ids(db, rows)
    live = [r for r in rows if r.archived_at is None]
    archived = [r for r in rows if r.archived_at is not None]
    with_sum3d = [r for r in rows if r.sum3d_valid]
    done = [r for r in with_sum3d if not r.is_mill_rework and r.id not in mail_reworks]

    per_day: dict[date, list[AuditRow]] = {}
    for row in rows:
        per_day.setdefault(row.day, []).append(row)

    big = sorted(
        (r for r in rows if (r.units or 0) >= min_quantity),
        key=lambda r: (-(r.units or 0), r.id),
    )
    odd = [(r, odd_reasons(r)) for r in rows]
    odd = [(r, why) for r, why in odd if why]
    return {
        "екран": {"роботи": screen["works"], "одиниць": screen["units"], "без_кількості": screen["noqty"]},
        "рядки_тут": here,
        "збіг_з_екраном": here["роботи"] == screen["works"] and here["одиниць"] == screen["units"],
        "за_правилом": {
            "живі": _tally(live),
            "архівні": _tally(archived),
            "із_sum3d": _tally(with_sum3d),
            "sum3d_порожній": _tally(r for r in rows if not r.sum3d),
            "sum3d_не_час": _tally(r for r in rows if r.sum3d and not r.sum3d_valid),
            "переробка_який_раз_2_і_більше": _tally(r for r in rows if r.is_mill_rework),
            "переробка_з_листа": _tally(r for r in rows if r.id in mail_reworks),
            "є_запис_браку": _tally(r for r in rows if r.id in records),
            "із_sum3d_без_переробок": _tally(done),
        },
        "по_днях": [
            {
                "день": day.isoformat(),
                **_tally(of_day),
                "лаба": _tally(r for r in of_day if r.group == "lab")["одиниць"],
                "клієнти": _tally(r for r in of_day if r.group == "client")["одиниць"],
                "архівні": _tally(r for r in of_day if r.archived_at is not None),
                "із_sum3d_без_переробок": _tally(
                    r
                    for r in of_day
                    if r.sum3d_valid and not r.is_mill_rework and r.id not in mail_reworks
                ),
                "найбільша_кількість": max((r.units or 0 for r in of_day), default=0),
            }
            for day, of_day in sorted(per_day.items())
        ],
        "поріг_кількості": min_quantity,
        "великі_кількості_усього": len(big),
        "великі_кількості": [_brief(r) for r in big[:limit]],
        "дивні_рядки_усього": len(odd),
        "дивні_рядки": [{**_brief(r), "чому": why} for r, why in odd[:limit]],
    }


def carryover(db: Session, days: list[date], *, limit: int) -> dict[str, Any]:
    """Наряди лаби, що стоять більш ніж на одній вкладці періоду: перенесення."""
    groups: dict[str, list[AuditRow]] = {}
    for row in load_rows(db, days):
        number = (row.work_order_no or "").strip()
        if row.group == "lab" and number:
            groups.setdefault(number, []).append(row)
    moved = [rows for rows in groups.values() if len({r.day for r in rows}) > 1]
    moved.sort(key=lambda rows: (-len(rows), rows[0].day))
    twice_done = [rows for rows in moved if sum(1 for r in rows if r.sum3d_valid) > 1]
    return {
        "нарядів_лаби": len(groups),
        "нарядів_на_кількох_вкладках": len(moved),
        "зайвих_рядків": sum(len(rows) - 1 for rows in moved),
        "зайвих_одиниць": sum(
            sum(r.units or 0 for r in rows) - max((r.units or 0 for r in rows), default=0)
            for rows in moved
        ),
        "нарядів_із_sum3d_кілька_разів": len(twice_done),
        "приклади": [
            {"наряд": rows[0].work_order_no, "рядки": [_brief(r) for r in rows]}
            for rows in moved[:limit]
        ],
        "приклади_sum3d_кілька_разів": [
            {"наряд": rows[0].work_order_no, "рядки": [_brief(r) for r in rows]}
            for rows in twice_done[:limit]
        ],
    }


def day_rows(db: Session, day: date, *, only_archived: bool, limit: int) -> dict[str, Any]:
    """Усі рядки одного дня, разом з архівними, яких не показує `kmill_queue`."""
    rows = load_rows(db, [day])
    if only_archived:
        rows = [r for r in rows if r.archived_at is not None]
    return {
        "день": day.isoformat(),
        **_tally(rows),
        "показано": min(limit, len(rows)),
        "рядки": [{**_brief(r), "чому_дивний": odd_reasons(r) or None} for r in rows[:limit]],
    }
