"""«📋 Таблиця дня» у боті: лабораторна таблиця дня одним поглядом (власник 23.11.26).

Окремий вид, не частина «Лабораторії»: рядок на роботу, у ТОМУ САМОМУ порядку, що
в Google-таблиці, з її номером рядка. Стани — ті самі предикати, що в картці
«Лабораторія» (`telegram_views.lab_counts`), тож числа двох карток збігаються.

Переробка — окремий рядок під роботою: який раз, хто винен і причина. Причина
береться з колонок «Хто винен» таблиці (туди пишуть або кількість, або текст).
Переробку, яку лаба ніяк не позначила, впізнаємо за ранішим прорахунком того
самого наряду — те саме правило, що у «Виробітку» (`vyrobitok_rows`).

Telegram — зовнішній сервіс, а в довільному тексті таблиці бувають імена
пацієнтів. Тому коментаря CAM («на швидку» — лише знаком) і шляху до роботи тут
НЕМАЄ зовсім, а «Вид» і причина переробки йдуть через `public_text`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.business_day import business_tab_today, prev_tab_day
from app.models import Order

#: Рядків на одній картинці (рядок переробки теж рядок). Вищу Telegram стискає.
LINES_PER_PAGE = 64
#: Запасний текст мусить влізти в повідомлення Telegram (4096) разом із шапкою.
_TEXT_LIMIT = 3400

_NAME_WORD = re.compile(r"^[A-ZА-ЯІЇЄҐ][a-zа-яіїєґ'’ʼ]{2,}")
_INITIAL = re.compile(r"^[А-ЯІЇЄҐ]\.")
_SUM3D_TIME = re.compile(r"\b\d{1,2}-\d{2}-\d{2}\b")


def public_text(text: Optional[str]) -> str:
    """Довільний текст таблиці без хвоста, схожого на ім'я людини.

    Слово з великої літери НЕ на початку («скол Іваненко») або ініціал («І.») —
    ріжемо від нього до кінця. Матеріали й кольори («A3», «ПММА») не зачіпає."""
    words = " ".join((text or "").split()).split(" ")
    kept: list[str] = []
    for index, word in enumerate(words):
        bare = word.strip("(),;:«»\"")
        if _INITIAL.match(bare) or (index and _NAME_WORD.match(bare)):
            break
        kept.append(word)
    else:
        return " ".join(kept).strip()
    # Прізвище перед ініціалом («Іваненко І.») теж ім'я.
    if kept and _INITIAL.match(words[len(kept)].strip("(),;:«»\"")) and _NAME_WORD.match(kept[-1].strip("(),;:«»\"")):
        kept.pop()
    return " ".join(kept).strip(" ,;:(—-")


@dataclass
class Rework:
    times: Optional[int] = None
    blame: str = ""
    reason: str = ""
    quantity: str = ""
    earlier: Optional[date] = None  # впізнана за ранішим прорахунком наряду

    @property
    def text(self) -> str:
        parts = [f"{self.times}-й раз"] if self.times else []
        if self.earlier:
            parts.append(f"уже прораховано {self.earlier:%d.%m}")
        if self.blame and self.reason:
            parts.append(f"{self.blame}: {self.reason}")
        elif self.blame:
            parts.append(f"{self.blame}, причину не вписано")
        else:
            parts.append("причину в таблиці не вписано")
        if self.quantity:
            parts.append(f"{self.quantity} од.")
        return " · ".join(parts)


@dataclass
class TableRow:
    sheet_row: Optional[int]
    number: str
    group: str       # zr/pm/ti/wx або "" — матеріал не фрезерувальний чи невпізнаний
    shade: str
    material: str    # текст таблиці, коли маркера немає
    kind: str
    tech: str
    quantity: str
    sum3d: str
    state: str       # done / take / wait
    rush: bool
    rework: Optional[Rework] = None

    @property
    def lines(self) -> int:
        return 2 if self.rework else 1


@dataclass
class DayTable:
    rows: list[TableRow]
    units: int
    units_left: int
    done: int

    def pages(self) -> list[list[TableRow]]:
        out: list[list[TableRow]] = [[]]
        used = 0
        for row in self.rows:
            if out[-1] and used + row.lines > LINES_PER_PAGE:
                out.append([])
                used = 0
            out[-1].append(row)
            used += row.lines
        return out


def _sum3d_label(raw: Optional[str]) -> str:
    """Останній час із клітинки ID; кілька прорахунків — «12-54-26 +1»."""
    times = _SUM3D_TIME.findall(raw or "")
    if not times:
        return public_text(raw)
    return f"{times[-1]} +{len(times) - 1}" if len(times) > 1 else times[-1]


def _rework(order: Order, earlier: Optional[date]) -> Optional[Rework]:
    from app.stats import parse_int_safe

    record = order.active_rework
    marked = parse_int_safe(order.mill_count)
    times = marked if marked is not None and marked >= 2 else None
    if record is None:
        if times is None and earlier is None and order.status != "переробка":
            return None
        return Rework(times=times, earlier=earlier)
    # У колонках «Хто винен» стоїть або кількість браку, або причина словами.
    cells = [part.strip() for part in (record.blame_quantity or "").split(",")]
    reason = public_text(", ".join(part for part in cells if part and not part.isdigit()))
    counted = next((part for part in cells if part.isdigit()), "")
    redo = (record.redo_quantity or "").strip()
    return Rework(
        times=record.occurrence or times,
        blame=record.blame or "",
        reason=reason,
        quantity=redo if redo.isdigit() else counted,
        earlier=earlier,
    )


def table_day(offset: int) -> date:
    """День вкладки: 0 — сьогоднішня, -1 — попередня. У вихідні — п'ятнична (CLAUDE.md §4)."""
    today = business_tab_today()
    return prev_tab_day(today) if offset else today


def day_table(db: Session, offset: int) -> DayTable:
    from app.material_class import mail_material_badge
    from app.parser import HEADER_ROWS
    from app.services.queue import is_rush_comment
    from app.services.queue_view import sum_units
    from app.queue_filters import filter_by_source
    from app.services.telegram_bot import _orders_of_day
    from app.services.telegram_cards import group_of_name
    from app.services.telegram_views import lab_counts, shade_code
    from app.services.vyrobitok_rows import earlier_calculations

    day = table_day(offset)
    counts = lab_counts(filter_by_source(_orders_of_day(db, day), "lab"))
    states = {o.id: name for name in ("done", "take", "wait") for o in counts[name]}
    orders = sorted(counts["all"], key=lambda o: (o.row_number is None, o.row_number or 0, o.id))
    earlier = earlier_calculations(db, orders, day)

    rows = []
    for order in orders:
        group = group_of_name(order.material.name if order.material else "")
        badge = mail_material_badge(order.material_color) if group else None
        record = order.active_rework
        rows.append(TableRow(
            sheet_row=order.row_number + HEADER_ROWS if order.row_number else None,
            number=(order.work_order_no or "").strip(),
            group=group,
            shade=shade_code(badge),
            material=public_text(order.material_color),
            kind=public_text(order.kind),
            tech=(order.technician_name or "").strip(),
            quantity=(order.quantity or "").strip(),
            sum3d=_sum3d_label(record.sum3d_id if record else order.sum3d_id),
            state=states[order.id],
            rush=is_rush_comment(order.cam_comment),
            rework=_rework(order, earlier.get(order.id)),
        ))
    units = sum_units(counts["all"])
    return DayTable(rows, units, units - sum_units(counts["done"]), len(counts["done"]))


def table_summary(table: DayTable) -> str:
    from app.services.telegram_views import _plural

    total = len(table.rows)
    if not total:
        return "Лабораторних робіт немає"
    text = f"{total} {_plural(total, 'робота', 'роботи', 'робіт')} · {table.units} од. · лишилось {table.units_left}"
    reworks = sum(1 for row in table.rows if row.rework)
    return f"{text} · переробок {reworks}" if reworks else text


def table_text(db: Session, offset: int) -> str:
    """Запасний текстовий вигляд (картинки вимкнено або малювання впало)."""
    from app.services.telegram_views import _e

    table = day_table(db, offset)
    head = f"<b>Робочий день {table_day(offset).strftime('%d.%m')}</b>\n{_e(table_summary(table))}"
    lines: list[str] = []
    size = 0
    for index, row in enumerate(table.rows):
        mark = {"done": "✅", "take": "🟡", "wait": "▫️"}[row.state]
        bits = [f"{mark} <code>{_e(row.number or '—')}</code>"]
        bits += [_e(part) for part in (row.material, row.kind, row.tech) if part]
        bits.append(f"{_e(row.quantity)} од." if row.quantity.isdigit() else _e(row.quantity or "?"))
        if row.sum3d:
            bits.append(f"<code>{_e(row.sum3d)}</code>")
        line = " · ".join(bits) + (" 🔥" if row.rush else "")
        if row.rework:
            line += f"\n   ↺ переробка · {_e(row.rework.text)}"
        if size + len(line) > _TEXT_LIMIT:
            lines.append(f"ще {len(table.rows) - index}")
            break
        lines.append(line)
        size += len(line) + 1
    return head + ("\n\n" + "\n".join(lines) if lines else "")
