"""Розділи бота зі списками, що розгортаються (власник 08.11.26).

* «📬 Пошта» — листи у «Вхідних», ще не взяті в роботу, за матеріалом
  фрезерування; «не фрезерування» (листи під правилом-винятком) — окремо,
  згорнутою цитатою. «Непрочитаний» = лист ще ніхто не відкрив у KuubMill
  (`seen_at`), а не позначка скриньки ukr.net — рішення власника 08.11.26.
* «🧪 Лабораторія» — лабораторні роботи дня: скільки зайшло, скільки зроблено
  (стоїть Sum3D, рішення власника 08.11.26 — те саме правило, що «В роботі»
  у черзі) і що лишається: «можна брати» і «технік ще не здав».

Кнопка розділу розгортає список у тому самому повідомленні (`e:<вид>:<ключ>`);
вдруге — згортає. Числа беруться тими самими предикатами, що екрани
(`mail_inbox.in_inbox`, `queue_filters`), щоб бот не розходився з CRM.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, selectinload

from app.models import EmailMessage, Order

EXPAND_CALLBACK = "e"
# Межа повідомлення Telegram — 4096 символів; довгий список ріжемо.
MAX_LIST = 15


@dataclass
class ViewParts:
    """Текст розділу і кнопки розгортання над спільними кнопками меню."""

    text: str
    rows: list[list[dict]] = field(default_factory=list)


def _e(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _plural(n: int, one: str, few: str, many: str) -> str:
    tail = n % 100
    if 11 <= tail <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _local(moment: Optional[datetime]) -> Optional[datetime]:
    """Місцевий час без поясу. `received_at` у базі буває і з поясом (лист
    із заголовка Date), і без нього; змішані вони не порівнюються."""
    if moment is None or moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def _when(moment: Optional[datetime], now: datetime) -> str:
    moment, now = _local(moment), _local(now) or now
    if moment is None:
        return ""
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if moment.date() == (now - timedelta(days=1)).date():
        return moment.strftime("вчора %H:%M")
    return moment.strftime("%d.%m %H:%M")


def _span(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} д {hours} год"
    if hours:
        return f"{hours} год {minutes} хв"
    return f"{minutes} хв"


def _bar(done: int, total: int, width: int = 20) -> str:
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def _toggle(label: str, view: str, key: str, opened: Optional[str]) -> dict:
    is_open = opened == key
    data = f"v:{view}" if is_open else f"{EXPAND_CALLBACK}:{view}:{key}"
    return {"text": ("▾ " if is_open else "") + label, "callback_data": data}


# ── Пошта ───────────────────────────────────────────────────────────────────

#: Групи листів за категорією матеріалу. Порядок — як у каталозі.
MAIL_GROUPS = (
    ("zr", "Цирконій", "🦷"),
    ("pm", "ПММА", "◻️"),
    ("ti", "Титан", "🔩"),
    ("wx", "Віск", "🕯"),
    ("slm", "СЛМ", "🖨"),
    ("none", None, "❔"),
)
_GROUP_TITLE = {"none": "Матеріал не розпізнано"}


@dataclass
class Letter:
    name: str
    received_at: Optional[datetime]
    material: str
    units: Optional[int]
    unread: bool
    category: Optional[str] = None


def _letter_units(email: EmailMessage) -> Optional[int]:
    """Одиниці з назв STL (як картка листа), інакше число оператора з тексту."""
    from app.services.stl_units import count_units

    counted = count_units(a.filename for a in (email.attachments or []))
    if counted.units:
        return counted.units
    raw = (email.quantity_guess or "").strip()
    return int(raw) if raw.isdigit() else None


def _letter(email: EmailMessage) -> Letter:
    name = (email.from_name or email.client_name_guess or email.from_address or "—").strip()
    return Letter(
        name=name,
        received_at=_local(email.received_at),
        material=(email.material_color_guess or "").strip(),
        units=_letter_units(email),
        unread=email.seen_at is None,
        category=email.filter_category,
    )


def _not_taken(db: Session) -> list[EmailMessage]:
    """У «Вхідних» і ще не прийняті. Повернутий прийнятий лист (`reopened`)
    роботу вже має, тож сюди не йде."""
    from app.mail_inbox import in_inbox

    return list(db.scalars(
        select(EmailMessage).options(selectinload(EmailMessage.attachments))
        .where(in_inbox, EmailMessage.status == "нове")
        .order_by(EmailMessage.received_at)
    ).all())


def _not_milling(db: Session) -> list[EmailMessage]:
    """Нові листи, які правило-виняток відклало як «не наша робота»."""
    from app.mail_hold import not_on_hold

    return list(db.scalars(
        select(EmailMessage).options(selectinload(EmailMessage.attachments))
        .where(and_(
            EmailMessage.status == "нове",
            EmailMessage.filter_category.is_not(None),
            EmailMessage.mailbox_folder.is_(None),
            EmailMessage.inbox_gone_at.is_(None),
            not_on_hold,
        ))
        .order_by(EmailMessage.received_at)
    ).all())


def mail_groups(db: Session) -> dict[str, list[Letter]]:
    from app.material_classifier import classify_material

    names = {name: key for key, name, _ in MAIL_GROUPS if name}
    groups: dict[str, list[Letter]] = {key: [] for key, _, _ in MAIL_GROUPS}
    for email in _not_taken(db):
        category = classify_material(email.material_color_guess) if email.material_color_guess else None
        groups[names.get(category or "", "none")].append(_letter(email))
    return groups


def _units_sum(letters: list[Letter]) -> int:
    return sum(letter.units or 0 for letter in letters)


def mail_summary(db: Session) -> str:
    groups = mail_groups(db)
    letters = [letter for group in groups.values() for letter in group]
    if not letters:
        return "📬 Пошта: усі листи взято в роботу"
    head = (f"📬 Пошта: <b>{len(letters)}</b> {_plural(len(letters), 'лист', 'листи', 'листів')}"
            f" не взято · {_units_sum(letters)} од.")
    bits = [f"{icon} {(name or 'інше').lower()} {len(groups[key])}"
            for key, name, icon in MAIL_GROUPS if groups[key]]
    unread = sum(1 for letter in letters if letter.unread)
    if unread:
        bits.append(f"🆕 {unread}")
    return head + "\n      " + " · ".join(bits)


def _letter_line(letter: Letter, now: datetime) -> str:
    mark = "🆕 " if letter.unread else ""
    bits = [f"{mark}<b>{_e(letter.name)}</b>", _when(letter.received_at, now)]
    if letter.material:
        bits.append(_e(letter.material))
    if letter.units:
        bits.append(f"{letter.units} од.")
    return " · ".join(b for b in bits if b)


def mail_view(db: Session, opened: Optional[str], now: datetime) -> ViewParts:
    now = _local(now) or now
    groups = mail_groups(db)
    letters = [letter for group in groups.values() for letter in group]
    lines: list[str] = []
    rows: list[list[dict]] = []
    if not letters:
        lines.append("<b>Усі листи взято в роботу</b> ✅")
    else:
        count = len(letters)
        lines.append(f"<b>{count} {_plural(count, 'лист', 'листи', 'листів')} не взято в роботу"
                     f" · {_units_sum(letters)} од.</b>")
        oldest = min((x.received_at for x in letters if x.received_at), default=None)
        sub = []
        if oldest is not None:
            sub.append(f"найстаріший чекає {_span(now - oldest)}")
        unread = sum(1 for x in letters if x.unread)
        if unread:
            sub.append(f"🆕 не відкрито {unread}")
        if sub:
            lines.append(" · ".join(sub))
        lines.append("")
        buttons: list[dict] = []
        for key, name, icon in MAIL_GROUPS:
            group = groups[key]
            if not group:
                continue
            title = name or _GROUP_TITLE[key]
            n = len(group)
            lines.append(f"{icon} {title} — <b>{n}</b> {_plural(n, 'лист', 'листи', 'листів')}"
                         f" · {_units_sum(group)} од.")
            if opened == key:
                shown = [_letter_line(x, now) for x in group[:MAX_LIST]]
                if len(group) > MAX_LIST:
                    shown.append(f"ще {len(group) - MAX_LIST}")
                lines.append("<blockquote>" + "\n".join(shown) + "</blockquote>")
            buttons.append(_toggle(f"{icon} {title if key != 'none' else 'Без матеріалу'} {n}", "mail", key, opened))
        for i in range(0, len(buttons), 2):
            rows.append(buttons[i:i + 2])
    other = [_letter(email) for email in _not_milling(db)]
    if other:
        n = len(other)
        body = "\n".join(
            f"{_e(x.category or 'інше')} · {_e(x.name)} · {_when(x.received_at, now)}" for x in other[:MAX_LIST]
        )
        if n > MAX_LIST:
            body += f"\nще {n - MAX_LIST}"
        lines.append("")
        lines.append(f"<blockquote expandable>▫️ Не фрезерування — {n} "
                     f"{_plural(n, 'лист', 'листи', 'листів')}\n{body}</blockquote>")
    if any(x.unread for x in letters):
        lines.append("<i>🆕 — лист ще ніхто не відкрив у KuubMill</i>")
    return ViewParts("\n".join(lines), rows)


# ── Лабораторія ─────────────────────────────────────────────────────────────


def _lab_orders(db: Session, offset: int) -> list[Order]:
    from app.queue_filters import filter_by_source
    from app.services.telegram_bot import _day_of, _orders_of_day

    # Той самий фільтр, що чіп «Лабораторія» в черзі.
    return filter_by_source(_orders_of_day(db, _day_of(offset)), "lab")


def lab_counts(orders: list[Order]) -> dict[str, list[Order]]:
    from app.queue_filters import order_can_take, order_in_work

    done = [o for o in orders if order_in_work(o)]
    take = [o for o in orders if order_can_take(o)]
    done_ids = {o.id for o in done} | {o.id for o in take}
    wait = [o for o in orders if o.id not in done_ids]
    return {"all": orders, "done": done, "take": take, "wait": wait}


def _rush_first(orders: list[Order]) -> list[Order]:
    from app.services.queue import is_rush_comment

    return sorted(orders, key=lambda o: (not is_rush_comment(o.cam_comment), o.row_number or 10_000))


def _lab_line(order: Order) -> str:
    from app.services.queue import is_rush_comment

    rush = is_rush_comment(order.cam_comment)
    bits = [f"{'🔥 ' if rush else ''}<code>{_e(order.work_order_no or '—')}</code>"]
    if order.material_color:
        bits.append(_e(order.material_color))
    if order.quantity:
        qty = order.quantity.strip()
        bits.append(f"{_e(qty)} од." if qty.isdigit() else _e(qty))
    if order.technician_name:
        bits.append(_e(order.technician_name))
    if rush:
        bits.append("<b>на швидку</b>")
    return " · ".join(bits)


def lab_summary(db: Session) -> str:
    counts = lab_counts(_lab_orders(db, 0))
    total = len(counts["all"])
    if not total:
        return "🧪 Лабораторія: робіт сьогодні ще немає"
    done = len(counts["done"])
    return f"🧪 Лабораторія: зроблено <b>{done}</b> з {total} · лишається {total - done}"


def lab_view(db: Session, offset: int, opened: Optional[str], now: datetime) -> ViewParts:
    from app.services.queue_view import sum_units
    from app.services.telegram_bot import _day_of

    view = "lab" if offset == 0 else "lab_y"
    day = _day_of(offset)
    counts = lab_counts(_lab_orders(db, offset))
    total = len(counts["all"])
    head = f"<b>Робочий день {day.strftime('%d.%m')}</b>"
    if not total:
        return ViewParts(f"{head}\nЛабораторних робіт немає.")
    done = len(counts["done"])
    left = total - done
    units_all, units_done = sum_units(counts["all"]), sum_units(counts["done"])
    lines = [
        head,
        f"<code>{_bar(done, total)}</code>",
        f"Зайшло <b>{total}</b> · зроблено <b>{done}</b> · лишається <b>{left}</b>",
        f"одиниць: {units_all} · зроблено {units_done} · лишається {units_all - units_done}",
        "",
    ]
    buttons: list[dict] = []
    for key, icon, title, short in (
        ("take", "✅", "Можна брати", "Можна брати"),
        ("wait", "⏳", "Технік ще не здав", "Не здано"),
    ):
        group = _rush_first(counts[key])
        if not group:
            continue
        lines.append(f"{icon} {title} — <b>{len(group)}</b>")
        if opened == key:
            shown = [_lab_line(o) for o in group[:MAX_LIST]]
            if len(group) > MAX_LIST:
                shown.append(f"ще {len(group) - MAX_LIST}")
            lines.append("<blockquote>" + "\n".join(shown) + "</blockquote>")
        buttons.append(_toggle(f"{icon} {short} {len(group)}", view, key, opened))
    if not left:
        lines.append("✅ Усе зроблено")
    rows = [buttons] if buttons else []
    return ViewParts("\n".join(lines), rows)
