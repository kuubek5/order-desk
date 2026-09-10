"""«Де ця робота?» — пройти весь шлях наряду й сказати, де він загубився.

Найчастіше питання цеху: «робота є в таблиці, а в черзі її нема». Відповісти
на нього досі можна було тільки руками — подивитись у таблицю, знайти рядок,
перевірити журнал синку, пошукати в архіві, звірити дати. Розробник витрачав
на це години; власник не міг зробити цього взагалі.

Пошук у застосунку (`/search`) тут не допомагає принципово: він шукає серед
того, що ВЖЕ Є в базі, — а питання ставлять саме тоді, коли роботи в базі
немає.

ЗВІДКИ ДАНІ ПРО ТАБЛИЦЮ. Не з Google, а з локальних CSV-знімків вкладок
(`app/sheet_backup.py`), які застосунок і так знімає щопрохід. Тому
розслідування безкоштовне за квотою, працює без інтернету і не заважає
живому синку. Ціна — знімок може бути на кілька хвилин старіший за таблицю;
про це прямо сказано у відповіді, а не замовчано.

ЧОГО ТУТ НЕМАЄ. Жодного запису й жодної правки: інструмент відповідає на
питання, а рішення лишається за людиною.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import business_today
from app.config import DB_PATH
from app.models import Order
from app.sheet_backup import list_snapshots, read_snapshot_bytes
from app.services.order_dates import order_date
from app.services.queue import RETENTION_DAYS


# Стовпці знімка (нумерація з нуля) — розкладка з CLAUDE.md §3. Дані з рядка 7,
# заголовки вище; сама розкладка стабільна, бо це та сама таблиця цеху.
COL_NUM = 0
COL_WORK_ORDER = 1
COL_QUANTITY = 2
COL_COLOR = 3
COL_KIND = 4
COL_JOB_CODE = 8
COL_TECHNICIAN = 9
COL_COMMENT = 10
COL_SUM3D = 11
FIRST_DATA_ROW = 7          # у нумерації Google (з одиниці)

# Скільки знімків переглядати. Кожен — окремий файл на ~20 КБ; сотня це
# два мегабайти й частки секунди, але стеля потрібна: тека знімків росте
# роками, і розслідування не має ставати повільнішим із часом.
MAX_SNAPSHOTS = 120

_SUM3D_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")


@dataclass
class SheetHit:
    """Рядок, знайдений у знімку вкладки."""

    tab: str
    day: str
    row_number: int
    taken_at: str
    work_order: str
    quantity: str
    color: str
    kind: str
    job_code: str
    technician: str
    sum3d: str
    disappeared_at: Optional[str] = None


@dataclass
class Step:
    """Один крок шляху. `ok`: True — пройдено, False — тут і загубилось,
    None — не перевіряли (і чому)."""

    title: str
    ok: Optional[bool]
    detail: str = ""


@dataclass
class Trace:
    query: str
    orders: list[Order] = field(default_factory=list)
    sheet_hits: list[SheetHit] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    # Головна відповідь одним реченням — те, заради чого екран і відкривали.
    verdict: str = ""
    actions: list[str] = field(default_factory=list)
    # Коли знято найсвіжіший переглянутий знімок: відповідь про таблицю
    # настільки ж свіжа, і мовчати про це не можна.
    snapshot_taken_at: str = ""
    snapshots_seen: int = 0


# ── Пошук у знімках таблиці ─────────────────────────────────────────────────


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def _matches(row: list[str], needle: str) -> bool:
    """Рядок стосується запиту. Порівнюємо ПО ВСІХ клітинках, а не лише по
    колонці наряду: шукають і за Sum3D, і за іменем техніка, і за шляхом."""
    low = needle.lower()
    return any(low in (value or "").lower() for value in row)


def search_snapshots(needle: str, *, limit: int = MAX_SNAPSHOTS) -> tuple[list[SheetHit], str, int]:
    """Знайти рядок у локальних копіях вкладок. Повертає (влучання, коли знято
    найсвіжіший переглянутий знімок, скільки знімків переглянуто)."""
    hits: list[SheetHit] = []
    newest_taken = ""
    seen = 0
    for info in list_snapshots(DB_PATH)[:limit]:
        seen += 1
        if not newest_taken and info.taken_at:
            newest_taken = info.taken_at
        raw = read_snapshot_bytes(DB_PATH, info.filename)
        if raw is None:
            continue
        try:
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
        except (UnicodeDecodeError, csv.Error):
            continue
        for index, row in enumerate(rows, start=1):
            if index < FIRST_DATA_ROW or not _matches(row, needle):
                continue
            hits.append(
                SheetHit(
                    tab=info.tab or info.iso,
                    day=info.day_label or info.iso,
                    row_number=index,
                    taken_at=info.taken_at or "",
                    work_order=_cell(row, COL_WORK_ORDER),
                    quantity=_cell(row, COL_QUANTITY),
                    color=_cell(row, COL_COLOR),
                    kind=_cell(row, COL_KIND),
                    job_code=_cell(row, COL_JOB_CODE),
                    technician=_cell(row, COL_TECHNICIAN),
                    sum3d=_cell(row, COL_SUM3D),
                    disappeared_at=info.disappeared_at,
                )
            )
    return hits, newest_taken, seen


# ── Пошук у базі ────────────────────────────────────────────────────────────


def search_orders(db: Session, needle: str) -> list[Order]:
    """Роботи, що стосуються запиту.

    Порівняння в Python, а не SQL LIKE: `lower()` у SQLite розуміє лише
    латиницю, і «Середюк» не знаходився б з малої — та сама причина, що в
    пошуку черги (§ревʼю 07.09.26).
    """
    low = needle.lower()
    matched = [
        row.id
        for row in db.execute(
            select(
                Order.id, Order.client_name, Order.work_order_no,
                Order.job_code, Order.sum3d_id, Order.technician_name,
            ).order_by(Order.id.desc())
        )
        if any(
            value and low in value.lower()
            for value in (
                row.client_name, row.work_order_no, row.job_code,
                row.sum3d_id, row.technician_name,
            )
        )
    ]
    if not matched:
        return []
    by_id = {
        order.id: order
        for order in db.scalars(select(Order).where(Order.id.in_(matched[:40])))
    }
    return [by_id[i] for i in matched[:40] if i in by_id]


# ── Розбір ──────────────────────────────────────────────────────────────────


def _order_place(order: Order, cutoff: date) -> tuple[str, str]:
    """Де саме зараз ця робота і чому. (коротко, пояснення)."""
    when = order_date(order)
    if order.archived_at is not None:
        return (
            "в архіві",
            f"Прибрана з черги {order.archived_at.strftime('%d.%m.%y %H:%M')}. "
            "Так буває, коли рядок зник із Google Таблиці або роботу видалили "
            "руками. Історія й коментарі збережені.",
        )
    if when < cutoff:
        return (
            "в архіві за віком",
            f"День роботи — {when.strftime('%d.%m.%y')}, а черга показує "
            f"останні {RETENTION_DAYS} днів. Це нормально, не поломка.",
        )
    return ("у черзі", f"День {when.strftime('%d.%m.%y')}, статус «{order.status}».")


def _explain_missing(hits: list[SheetHit]) -> tuple[str, list[str]]:
    """Рядок у таблиці є, роботи в базі немає. Чому — і що робити."""
    hit = hits[0]
    reasons: list[str] = []
    if not hit.color.strip() or not hit.quantity.strip():
        # Найчастіша і найтихіша причина: рядок без матеріалу або без
        # кількості роботою не вважається (§14, `OrderRow.is_work_row`).
        reasons.append(
            "У рядку порожній "
            + ("матеріал/колір" if not hit.color.strip() else "стовпчик кількості")
            + " — застосунок не вважає такий рядок роботою, щоб у чергу не "
            "лізли підсумкові й службові рядки."
        )
    if hit.disappeared_at:
        reasons.append(
            f"Вкладка «{hit.tab}» зникла з таблиці {hit.disappeared_at[:10]} — "
            "рядок лишився тільки в копії."
        )
    if not reasons:
        reasons.append(
            "Рядок виглядає повноцінним. Найімовірніше, синхронізація його ще "
            "не прочитала (вкладка щойно зʼявилась або назва вкладки не "
            "розпізнається) — подивіться «Що не так» і Журнал синку."
        )
    actions = [
        f"Відкрити вкладку «{hit.tab}», рядок {hit.row_number} — і звірити з тим, "
        "що показано нижче.",
    ]
    if not hit.color.strip() or not hit.quantity.strip():
        actions.append("Заповнити порожній стовпчик — далі синк підхопить сам.")
    actions.append("Перевірити екран «Що не так» — можливо, синк узагалі стоїть.")
    return " ".join(reasons), actions


def trace(db: Session, query: str) -> Trace:
    """Пройти шлях наряду й сказати, де він.

    Порядок кроків повторює справжній шлях роботи: рядок у таблиці →
    синхронізація → робота в базі → черга. Так видно не тільки ДЕ вона, а й
    на якому кроці загубилась.
    """
    needle = (query or "").strip()
    result = Trace(query=needle)
    if not needle:
        return result

    result.orders = search_orders(db, needle)
    result.sheet_hits, result.snapshot_taken_at, result.snapshots_seen = search_snapshots(needle)
    cutoff = business_today() - timedelta(days=RETENTION_DAYS)

    # Крок 1 — рядок у таблиці.
    if result.sheet_hits:
        hit = result.sheet_hits[0]
        result.steps.append(
            Step(
                "Рядок у Google Таблиці",
                True,
                f"вкладка «{hit.tab}», рядок {hit.row_number}"
                + (f" · знімок від {hit.taken_at[11:16]}" if len(hit.taken_at) > 15 else ""),
            )
        )
    else:
        result.steps.append(
            Step(
                "Рядок у Google Таблиці",
                None,
                f"не знайдено серед {result.snapshots_seen} збережених копій вкладок. "
                "Це не те саме, що «немає в таблиці»: копія могла бути знята до "
                "того, як рядок додали.",
            )
        )

    # Крок 2 — робота в базі.
    if result.orders:
        first = result.orders[0]
        result.steps.append(
            Step("Робота в застосунку", True, f"№{first.id}, джерело «{first.source}»")
        )
        place, why = _order_place(first, cutoff)
        result.steps.append(Step(f"Зараз — {place}", place == "у черзі", why))
        if place == "у черзі":
            result.verdict = (
                f"Робота в черзі, день {order_date(first).strftime('%d.%m.%y')}, "
                f"статус «{first.status}»."
            )
            result.actions = ["Відкрити її картку — посилання нижче."]
        else:
            result.verdict = f"Робота {place}. {why}"
            result.actions = [
                "Подивитись в «Архіві» — там уся історія.",
                "Якщо рядок у таблиці на місці, синк поверне роботу сам "
                "протягом кількох хвилин.",
            ]
        if len(result.orders) > 1:
            result.steps.append(
                Step(
                    "Схожих робіт",
                    None,
                    f"знайдено ще {len(result.orders) - 1} — перевірте, чи це не "
                    "той самий наряд у різні дні.",
                )
            )
        return result

    result.steps.append(Step("Робота в застосунку", False, "не знайдена"))

    if result.sheet_hits:
        why, actions = _explain_missing(result.sheet_hits)
        result.steps.append(Step("Чому її немає", False, why))
        result.verdict = (
            "Рядок у таблиці є, а роботи в застосунку немає — саме тут вона й "
            "загубилась."
        )
        result.actions = actions
        return result

    result.verdict = (
        "Нічого не знайдено — ні в застосунку, ні в збережених копіях вкладок."
    )
    result.actions = [
        "Перевірити написання: наряд шукається за частиною номера, тож "
        "достатньо кількох цифр.",
        "Якщо робота з пошти — шукати за іменем клієнта.",
        "Якщо рядок додали щойно, копія вкладки могла бути знята раніше: "
        "подивитись у самій таблиці.",
    ]
    return result


def looks_like_sum3d(value: str) -> bool:
    """Хвіст Sum3D ID (`12-01-45`) — за ним теж шукають."""
    return bool(_SUM3D_RE.match((value or "").strip()))


def parse_taken_at(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
