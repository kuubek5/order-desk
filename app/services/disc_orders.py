"""Замовлення нових дисків на склад: запис, скасування, історія, доставка.

Екран — «Нові диски» (`app/routers/discs.py`), бриф — NEW_DISCS_BRIEF.md.
Що замовляти, як виглядає рядок і як розкладено по змінах — у
`app/services/cam_blanks.py`; тут — замовлення як записи
(`CamBlankOrder`) і їхня доля в Telegram.

Три правила власника (10.09.26), на яких усе тримається:

1. **«Надіслати на склад» = відправка в Telegram І одразу «замовлено»** —
   один клік, без підтверджень, зі «Скасувати» під рукою (дух CLAUDE.md §2,
   правило 2). «Замовлено без відправки» — коли замовили телефоном.
2. **Скасувати можна БУДЬ-ЯКЕ замовлення**, не лише останнє. Скасування
   повертає диски в «чекають», а запис лишається в історії закресленим.
   Ризик подвійного замовлення власник прийняв свідомо.
3. **Скасоване в Telegram не шле нового** — бот редагує те саме
   повідомлення складу.

Стан доставки на екрані — лише з підтвердження: «надіслано 17:48» пишеться,
коли Telegram прийняв повідомлення, а не коли оператор натиснув кнопку.
Поки рядок у черзі — «у дорозі».
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import CamBlank, CamBlankOrder, TelegramOutbox, User
from app.services import telegram_bot
from app.services.cam_blanks import (
    MATERIAL_PMMA,
    MATERIAL_ZR,
    WEEKDAYS_UK,
    DiscPos,
    bare_name,
    disc_needs_look,
    disc_position,
    group_meta,
    order_text,
    real_discs,
    shifts_label,
)

VIA_TELEGRAM = "telegram"
VIA_MANUAL = "manual"
VIAS = (VIA_TELEGRAM, VIA_MANUAL)

# Дописане від руки — фрези, полірувальні диски. Межа — щоб випадково
# вставлений у поле абзац не став повідомленням на кілька екранів (і не
# вперся в 4096 символів Telegram).
NOTE_LIMIT = 1500

# Скільки після замовлення під кнопками висить рядок «Надіслано на склад
# HH:MM · N дисків [Скасувати]». Досить, щоб помітити випадковий клік; далі
# скасування живе в історії.
FRESH_WINDOW = timedelta(minutes=30)

# Порожній підпис змін у замовленні лише з дописаного.
NOTE_ONLY_LABEL = "лише дописане"

# Фрези, які замовляють найчастіше, — чипи-шаблон під полем «Дописати».
# Список дав власник 10.09.26 рівно в такому вигляді й з такими групами;
# рядок іде в замовлення дослівно (повторний клік додає «(х2)», «(х3)»…).
# Сенс чисел тут не тлумачимо — склад читає їх так, як пише цех.
BURS_TEMPLATE: tuple[tuple[str, ...], ...] = (
    ("6*2.5 zr", "6*1.0 zr", "6*0.6 zr", "6*0.3 zr", "6*1.5T zr"),
    ("3*2.5 zr", "3*1.0 zr", "3*0.6 zr", "3*0.3 zr", "3*1.5T zr"),
    ("6*3.0 Ti", "6*2.0 Ti", "6*1.0 Ti"),
)

_BUR_RE = re.compile(r"^(?P<head>\d+\*)(?P<size>\S+)\s+(?P<mat>.+)$")


@dataclass(frozen=True)
class BurRow:
    """Рядок шаблону фрез на екрані: підпис групи і короткі чипи.

    13 повних чипів («6*2.5 zr» …) займали пів кошика (власник, 10.09.26).
    Тому група показується одним рядком «6* zr: 2.5 · 1.0 · …», а чип несе
    ПОВНИЙ рядок, який і дописується в замовлення, — склад читає його так
    само, як раніше."""

    label: str
    chips: tuple[tuple[str, str], ...]  # (що на чипі, що дописати)


def burs_rows() -> list[BurRow]:
    rows = []
    for group in BURS_TEMPLATE:
        matches = [_BUR_RE.match(line) for line in group]
        parsed = [m for m in matches if m is not None]
        labels = {f"{m['head']} {m['mat']}" for m in parsed}
        if len(parsed) == len(group) and len(labels) == 1:
            rows.append(BurRow(label=labels.pop(), chips=tuple((m["size"], line) for m, line in zip(parsed, group))))
        else:
            rows.append(BurRow(label="", chips=tuple((line, line) for line in group)))
    return rows


# ── Текст для Telegram ──────────────────────────────────────────────────────


def telegram_text(text: str, when: datetime) -> str:
    """Повідомлення складу — те саме, що в буфері, плюс заголовок із датою.
    Спільне для відправки й для прев'ю «Як побачить склад у Telegram»."""
    return f"<b>Замовлення дисків · {when:%d.%m}</b>\n\n{html.escape(text or '—')}"


def telegram_cancel_text(text: str, created: datetime, cancelled: datetime) -> str:
    """Те саме повідомлення після скасування: закреслене, з часом скасування
    НАГОРІ — склад бачить його першим, не гортаючи список."""
    return (
        f"❌ <b>Скасовано {cancelled:%H:%M}</b>\n"
        f"<s>Замовлення дисків · {created:%d.%m}</s>\n\n"
        f"<s>{html.escape(text or '—')}</s>"
    )


# ── Запис і скасування ──────────────────────────────────────────────────────


def create_order(
    db: Session,
    *,
    ids: Iterable[int],
    note: str = "",
    via: str = VIA_MANUAL,
    user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[CamBlankOrder]:
    """Замовити позначені диски (+ дописане). None — замовляти нічого.

    Диски забираються АТОМАРНО (`UPDATE … WHERE ordered_at IS NULL`): двоє
    операторів із застарілими сторінками не замовлять той самий диск двічі —
    другий просто не отримає вже забраного. Порожній `ids` — НЕ «усе».

    Нічого не комітить: відправка в Telegram лягає в чергу в тій самій
    транзакції, тож «замовлено» без рядка черги (чи навпаки) не буває.
    """
    now = now or datetime.now()
    if via not in VIAS:
        via = VIA_MANUAL
    wanted = sorted({int(i) for i in ids})
    extra = (note or "").strip()[:NOTE_LIMIT]
    order = CamBlankOrder(created_at=now, created_by_id=user_id, via=via, note=extra)
    db.add(order)
    db.flush()
    if wanted:
        db.execute(
            update(CamBlank)
            .where(CamBlank.id.in_(wanted), CamBlank.ordered_at.is_(None))
            .values(ordered_at=now, order_id=order.id)
        )
    rows = order_discs(db, order.id)
    if not rows and not extra:
        db.delete(order)
        db.flush()
        return None
    order.text = order_text(rows, extra)
    order.disc_count = len(rows)
    order.label = shifts_label(rows) or NOTE_ONLY_LABEL
    if via == VIA_TELEGRAM:
        telegram_bot.queue_disc_order(
            db, order_id=order.id, text=telegram_text(order.text, now), now=now
        )
    return order


def order_discs(db: Session, order_id: int) -> list[CamBlank]:
    """Диски замовлення. Умова `ordered_at != first_seen_at` — захист точки
    відліку: її рядки замовленням не є, і скасування не має вивалити в список
    усю історію теки (CLAUDE.md, бриф §4 п. 3)."""
    return list(
        db.scalars(
            select(CamBlank)
            .where(
                CamBlank.order_id == order_id,
                CamBlank.ordered_at != CamBlank.first_seen_at,
            )
            .order_by(CamBlank.first_seen_at, CamBlank.id)
        ).all()
    )


@dataclass(frozen=True)
class CancelResult:
    order: CamBlankOrder
    returned: int
    # Скільки повідомлень складу буде переписано на «скасовано».
    edits: int


def cancel_order(
    db: Session,
    order_id: int,
    *,
    user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[CancelResult]:
    """Скасувати замовлення: диски знову «чекають», запис лишається в історії
    закресленим, повідомлення складу редагується. None — такого немає або
    вже скасоване (двоє операторів, застаріла сторінка).

    Знімок тексту й підпису фіксується ДО відчеплення дисків: у записів,
    перенесених зі старих пачок, їх ще немає, а після скасування скласти
    вже не буде з чого. Нічого не комітить.
    """
    now = now or datetime.now()
    order = db.get(CamBlankOrder, order_id)
    if order is None or order.cancelled_at is not None:
        return None
    rows = order_discs(db, order.id)
    if not order.text:
        order.text = order_text(rows, order.note)
    if not order.label:
        order.label = shifts_label(rows) or NOTE_ONLY_LABEL
    if not order.disc_count:
        order.disc_count = len(rows)
    for row in rows:
        row.ordered_at = None
        row.order_id = None
    order.cancelled_at = now
    order.cancelled_by_id = user_id
    edits = 0
    if order.via == VIA_TELEGRAM:
        edits = telegram_bot.queue_disc_order_cancel(
            db,
            order_id=order.id,
            text=telegram_cancel_text(order.text, order.created_at, now),
            now=now,
        )
    return CancelResult(order=order, returned=len(rows), edits=edits)


# ── Доставка в Telegram ─────────────────────────────────────────────────────

DELIVERY_MANUAL = "manual"
DELIVERY_QUEUED = "queued"
DELIVERY_SENT = "sent"
DELIVERY_PARTIAL = "partial"
DELIVERY_FAILED = "failed"


@dataclass(frozen=True)
class Delivery:
    state: str
    # Коли Telegram прийняв (для «надіслано HH:MM») — найпізніший із адресатів.
    at: Optional[datetime] = None
    error: str = ""
    # Скасоване замовлення в чаті складу: "edited" — закреслено, "pending" —
    # правка в дорозі, "withdrawn" — оригінал не встиг піти, "failed" — не
    # вдалось. None — скасування не було або без Telegram.
    cancel: Optional[str] = None


def _deliveries(db: Session, orders: list[CamBlankOrder]) -> dict[int, Delivery]:
    tg_ids = [order.id for order in orders if order.via == VIA_TELEGRAM]
    originals = telegram_bot.disc_order_rows(db, tg_ids)
    edits_by_original: dict[int, TelegramOutbox] = {}
    all_original_ids = [row.id for rows in originals.values() for row in rows]
    if all_original_ids:
        for row in db.scalars(
            select(TelegramOutbox).where(TelegramOutbox.edit_of_id.in_(all_original_ids))
        ):
            if row.edit_of_id is not None:
                edits_by_original[row.edit_of_id] = row

    out: dict[int, Delivery] = {}
    for order in orders:
        if order.via != VIA_TELEGRAM:
            out[order.id] = Delivery(DELIVERY_MANUAL)
            continue
        rows = originals.get(order.id, [])
        sent = [row for row in rows if row.sent_at is not None]
        waiting = [row for row in rows if row.sent_at is None and row.gave_up_at is None]
        failed = [row for row in rows if row.sent_at is None and row.gave_up_at is not None]
        if not rows:
            state = DELIVERY_FAILED
        elif waiting:
            state = DELIVERY_QUEUED
        elif sent and failed:
            state = DELIVERY_PARTIAL
        elif sent:
            state = DELIVERY_SENT
        else:
            state = DELIVERY_FAILED
        error = ""
        if not rows:
            error = "у черзі Telegram цього замовлення немає"
        elif failed:
            error = failed[-1].last_error or ""
        cancel = None
        if order.cancelled_at is not None and rows:
            if not sent:
                cancel = "withdrawn"
            else:
                edits = [edits_by_original.get(row.id) for row in sent]
                if all(edit is not None and edit.sent_at is not None for edit in edits):
                    cancel = "edited"
                elif any(edit is None or (edit.sent_at is None and edit.gave_up_at is None) for edit in edits):
                    cancel = "pending"
                else:
                    cancel = "failed"
        out[order.id] = Delivery(
            state=state,
            at=max((row.sent_at for row in sent if row.sent_at is not None), default=None),
            error=error,
            cancel=cancel,
        )
    return out


# ── Історія ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrderView:
    id: int
    created_at: datetime
    count: int
    has_note: bool
    text: str
    label: str
    via: str
    delivery: Delivery
    cancelled_at: Optional[datetime] = None
    by: str = ""
    cancelled_by: str = ""

    @property
    def cancelled(self) -> bool:
        return self.cancelled_at is not None


def _names(db: Session, ids: set[Optional[int]]) -> dict[int, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {
        user.id: (user.full_name or user.username)
        for user in db.scalars(select(User).where(User.id.in_(wanted)))
    }


def _views(db: Session, orders: list[CamBlankOrder]) -> list[OrderView]:
    deliveries = _deliveries(db, orders)
    names = _names(db, {o.created_by_id for o in orders} | {o.cancelled_by_id for o in orders})
    views = []
    for order in orders:
        text, label, count = order.text, order.label, order.disc_count
        if not text or not label:
            # Запис зі старої пачки (міграція 0060): знімка ще немає — складаємо
            # з дисків, що досі до нього прив'язані.
            rows = order_discs(db, order.id)
            text = text or order_text(rows, order.note)
            label = label or shifts_label(rows) or NOTE_ONLY_LABEL
            count = count or len(rows)
        views.append(OrderView(
            id=order.id,
            created_at=order.created_at,
            count=count,
            has_note=bool((order.note or "").strip()),
            text=text,
            label=label,
            via=order.via,
            delivery=deliveries[order.id],
            cancelled_at=order.cancelled_at,
            by=names.get(order.created_by_id, "") if order.created_by_id else "",
            cancelled_by=names.get(order.cancelled_by_id, "") if order.cancelled_by_id else "",
        ))
    return views


HISTORY_PAGE = 30


def history(db: Session, *, limit: int = HISTORY_PAGE) -> list[OrderView]:
    """Замовлення, найновіші згори — і скасовані теж (закресленими)."""
    orders = list(
        db.scalars(
            select(CamBlankOrder)
            .order_by(CamBlankOrder.created_at.desc(), CamBlankOrder.id.desc())
            .limit(max(1, limit))
        ).all()
    )
    return _views(db, orders)


def history_total(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(CamBlankOrder)) or 0)


def order_view(db: Session, order_id: int) -> Optional[OrderView]:
    order = db.get(CamBlankOrder, order_id)
    return _views(db, [order])[0] if order is not None else None


def latest_active(db: Session) -> Optional[CamBlankOrder]:
    """Найсвіжіше НЕскасоване замовлення."""
    return db.scalars(
        select(CamBlankOrder)
        .where(CamBlankOrder.cancelled_at.is_(None))
        .order_by(CamBlankOrder.created_at.desc(), CamBlankOrder.id.desc())
        .limit(1)
    ).first()


def fresh_order(db: Session, *, now: Optional[datetime] = None) -> Optional[OrderView]:
    """Щойно зроблене замовлення для рядка під кнопками — або None."""
    now = now or datetime.now()
    order = latest_active(db)
    if order is None or now - order.created_at > FRESH_WINDOW:
        return None
    return _views(db, [order])[0]


_COUNT_TAIL = re.compile(r"\s*\(\s*[хx]\s*\d+\s*\)\s*$", re.IGNORECASE)


def recent_notes(db: Session, *, limit: int = 5) -> list[str]:
    """Рядки дописаного з недавніх замовлень — чипи «Недавнє» під полем.

    Без кількості в дужках (її додає повторний клік) і без того, що вже є
    в шаблоні фрез: той самий чип двічі — шум."""
    template = {line for group in BURS_TEMPLATE for line in group}
    seen: list[str] = []
    for note in db.scalars(
        select(CamBlankOrder.note)
        .where(CamBlankOrder.note != "")
        .order_by(CamBlankOrder.created_at.desc())
        .limit(20)
    ):
        for line in (note or "").splitlines():
            line = _COUNT_TAIL.sub("", line).strip()
            if line and line not in seen and line not in template:
                seen.append(line)
                if len(seen) >= limit:
                    return seen
    return seen


# ── Усі створені диски ──────────────────────────────────────────────────────
# Журнал кожного диска, який CRM побачила в теці (крім точки відліку): коли
# створено, що це, файл і куди він пішов — «чекає замовлення» чи «замовлено
# <коли>». Графік угорі — скільки дисків за день, по матеріалах.

CHART_DAYS = 30
DAYS_STEP = 3
# Висота стовпчика графіка в пікселях при максимумі шкали.
CHART_PX = 96


@dataclass(frozen=True)
class JournalRow:
    row: CamBlank
    pos: DiscPos
    warn: bool
    ordered_at: Optional[datetime]

    @property
    def file(self) -> str:
        return bare_name(self.row.file_name or self.row.rel_path)


@dataclass(frozen=True)
class JournalDay:
    day: date
    rows: tuple[JournalRow, ...]

    @property
    def key(self) -> str:
        return self.day.isoformat()

    @property
    def weekday(self) -> str:
        return WEEKDAYS_UK[self.day.weekday()]

    @property
    def split(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for item in self.rows:
            counts[item.pos.tag] = counts.get(item.pos.tag, 0) + 1
        return list(counts.items())


@dataclass(frozen=True)
class ChartBar:
    day: date
    zr: int
    pmma: int
    other: int

    @property
    def key(self) -> str:
        return self.day.isoformat()

    @property
    def total(self) -> int:
        return self.zr + self.pmma + self.other

    @property
    def weekday(self) -> str:
        return WEEKDAYS_UK[self.day.weekday()]


@dataclass
class Journal:
    total: int = 0
    waiting: int = 0
    since: Optional[datetime] = None
    bars: list[ChartBar] = field(default_factory=list)
    scale: int = 4
    days: list[JournalDay] = field(default_factory=list)
    more_days: int = 0
    # Фільтр матеріалу: (значення, підпис) — «Усі», цирконій, ПММА, решта тек.
    materials: list[tuple[str, str]] = field(default_factory=list)
    has_other: bool = False
    picked: Optional[ChartBar] = None

    def px(self, count: int) -> int:
        return round(count / self.scale * CHART_PX) if count else 0


def journal(
    db: Session,
    *,
    mat: str = "all",
    st: str = "all",
    q: str = "",
    day: str = "",
    shown: int = DAYS_STEP,
) -> Journal:
    """Вкладка «Усі створені диски»: графік, фільтри, список по днях.

    Фільтр у Python, а не в SQL: група матеріалу виводиться з теки й назви
    (`material_group`), а дисків за рік — кілька тисяч, це мілісекунди.
    """
    rows = real_discs(db)
    items = [
        JournalRow(row=row, pos=disc_position(row), warn=disc_needs_look(row), ordered_at=row.ordered_at)
        for row in rows
    ]
    result = Journal(total=len(items), waiting=sum(1 for item in items if item.ordered_at is None))
    result.since = rows[-1].first_seen_at if rows else None

    groups: dict[str, str] = {}
    for item in items:
        groups.setdefault(item.pos.group, item.pos.title)
    options = [("all", "Усі")]
    if MATERIAL_ZR in groups:
        options.append((MATERIAL_ZR, "Цирконій"))
    if MATERIAL_PMMA in groups:
        options.append((MATERIAL_PMMA, "ПММА"))
    options.extend(
        (key, title) for key, title in sorted(groups.items()) if key not in (MATERIAL_ZR, MATERIAL_PMMA)
    )
    result.materials = options
    result.has_other = any(key not in (MATERIAL_ZR, MATERIAL_PMMA) for key in groups)

    needle = (q or "").strip().casefold()
    base = [
        item for item in items
        if (mat == "all" or item.pos.group == mat)
        and (st == "all" or (st == "wait") == (item.ordered_at is None))
        and (not needle or needle in f"{item.pos.key} {item.file}".casefold())
    ]

    per_day: dict[date, list[JournalRow]] = {}
    for item in base:
        per_day.setdefault(item.row.first_seen_at.date(), []).append(item)

    bars = []
    for bar_day in sorted(per_day)[-CHART_DAYS:]:
        day_items = per_day[bar_day]
        zr = sum(1 for item in day_items if item.pos.group == MATERIAL_ZR)
        pmma = sum(1 for item in day_items if item.pos.group == MATERIAL_PMMA)
        bars.append(ChartBar(day=bar_day, zr=zr, pmma=pmma, other=len(day_items) - zr - pmma))
    result.bars = bars
    peak = max((bar.total for bar in bars), default=1)
    result.scale = max(4, math.ceil(peak / 4) * 4)

    picked = next((bar for bar in bars if bar.key == day), None) if day else None
    result.picked = picked
    ordered_days = sorted(per_day, reverse=True)
    if picked is not None:
        visible = [picked.day]
    elif day:
        # День поза графіком (давніший за 30 днів) — теж показуємо, якщо є.
        visible = [d for d in ordered_days if d.isoformat() == day]
    else:
        step = max(DAYS_STEP, shown)
        visible = ordered_days[:step]
        result.more_days = max(0, len(ordered_days) - step)
    result.days = [JournalDay(day=d, rows=tuple(per_day[d])) for d in visible]
    return result


def group_label(group: str) -> str:
    return group_meta(group)[1]
