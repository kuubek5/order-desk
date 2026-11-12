"""Розділи бота зі списками, що розгортаються (власник 08.11.26).

* «📬 Пошта» — листи у «Вхідних», ще не взяті в роботу, за матеріалом
  фрезерування. Що не фрезерування (правило-виняток: 3D-друк, моделювання;
  СЛМ), не рахується й не показується зовсім (власник 10.11.26). Головний
  вид — картинка (`telegram_mail_picture`, варіант E), текст — «Листи
  списком» і запасний. Одиниць не вигадуємо: лист без числа лишається поза
  сумою, і бот пише, у скількох таких листах числа немає. «Непрочитаний» =
  лист ще ніхто не відкрив у KuubMill (`seen_at`), а не позначка скриньки
  ukr.net — рішення власника 08.11.26.
* «🧪 Лабораторія» — лабораторні роботи дня: скільки зайшло, скільки зроблено
  (стоїть Sum3D, рішення власника 08.11.26 — те саме правило, що «В роботі»
  у черзі) і що лишається: «можна брати» і «технік ще не здав».

Кнопка розділу розгортає список у тому самому повідомленні (`e:<вид>:<ключ>`);
вдруге — згортає. Числа беруться тими самими предикатами, що екрани
(`mail_inbox.in_inbox`, `queue_filters`), щоб бот не розходився з CRM.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import EmailMessage, Order

logger = logging.getLogger(__name__)

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


def _toggle(
    label: str, view: str, key: str, opened: Optional[str], closed: Optional[str] = None
) -> dict:
    """Кнопка розгортання. `closed` — куди веде повторне натискання (згорнути);
    за замовчуванням сам вид."""
    is_open = opened == key
    data = (closed or f"v:{view}") if is_open else f"{EXPAND_CALLBACK}:{view}:{key}"
    return {"text": ("▾ " if is_open else "") + label, "callback_data": data}


# ── Пошта ───────────────────────────────────────────────────────────────────

#: Групи листів за категорією матеріалу. Порядок — як у каталозі. СЛМ тут
#: немає свідомо: це друк металом, не фрезерування (власник 10.11.26).
MAIL_GROUPS = (
    ("zr", "Цирконій", "🦷"),
    ("pm", "ПММА", "◻️"),
    ("ti", "Титан", "🔩"),
    ("wx", "Віск", "🕯"),
    ("none", None, "❔"),
)
_GROUP_TITLE = {"none": "Матеріал не розпізнано"}
#: Категорії, які в «Пошті» не рахуються й не показуються.
_NOT_MILLING = frozenset({"СЛМ"})
#: `opened` текстового виду: весь список одним блоком / нічого не розгорнуто.
MAIL_ALL = "all"
MAIL_LIST = "list"


@dataclass
class Letter:
    name: str
    received_at: Optional[datetime]
    material: str
    units: Optional[int]
    unread: bool
    category: Optional[str] = None
    # Для картинки-списку (як рядок «Нових з пошти» в CRM, 12.11.26):
    group: str = "none"  # ключ MAIL_GROUPS
    shade: str = ""  # код відтінку для маркера («A3», «800»)
    files: int = 0  # файлів на диску
    files_note: str = ""  # «не скачано» / «качаються» замість числа
    ready: bool = False  # готовий до прийняття (той самий бейдж, що в CRM)


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
    letter = Letter(
        name=name,
        received_at=_local(email.received_at),
        material=(email.material_color_guess or "").strip(),
        units=_letter_units(email),
        unread=email.seen_at is None,
        category=email.filter_category,
    )
    _letter_row_state(letter, email)
    return letter


_CYRILLIC_SHADE = str.maketrans({"А": "A", "В": "B", "С": "C", "Д": "D", "а": "A", "в": "B", "с": "C", "д": "D"})


def shade_code(badge: Optional[dict]) -> str:
    """Код відтінку для маркера матеріалу («A3», «800»). Кирилична А/В/С —
    те саме, що латинська (CLAUDE.md §4): люди набирають «А3» на українській
    розкладці."""
    code = ((badge or {}).get("color") or "").rsplit(" ", 1)[-1]
    if not any(ch.isdigit() for ch in code):
        return ""
    return code.translate(_CYRILLIC_SHADE).upper()


def _letter_row_state(letter: Letter, email: EmailMessage) -> None:
    """Те, що рядок пошти в CRM показує поруч з іменем: відтінок, файли,
    готовність. Збій тут не має гасити список — рядок просто лишиться без них."""
    try:
        from app.material_class import mail_material_badge
        from app.triage_status import files_on_disk, triage_readiness

        letter.shade = shade_code(mail_material_badge(email.material_color_guess))
        status = email.attachments_status
        if status == "pending":
            letter.files_note = "качаються"
        elif status == "skipped":
            letter.files_note = "не скачано"
        else:
            letter.files = files_on_disk(email)
        letter.ready = triage_readiness(email).get("state") == "ready"
    except Exception:  # noqa: BLE001 — список важливіший за один значок
        logger.exception("telegram-бот: стан рядка листа %s не зібрано", getattr(email, "id", "?"))


def _not_taken(db: Session) -> list[EmailMessage]:
    """У «Вхідних» і ще не прийняті. Повернутий прийнятий лист (`reopened`)
    роботу вже має, тож сюди не йде. Листи під правилом-винятком («не наша
    робота»: 3D-друк, моделювання) і «на погодженні» `in_inbox` уже відсікає."""
    from app.mail_inbox import in_inbox

    return list(db.scalars(
        select(EmailMessage).options(selectinload(EmailMessage.attachments))
        .where(in_inbox, EmailMessage.status == "нове")
        .order_by(EmailMessage.received_at)
    ).all())


def mail_groups(db: Session) -> dict[str, list[Letter]]:
    from app.material_classifier import classify_material

    names = {name: key for key, name, _ in MAIL_GROUPS if name}
    groups: dict[str, list[Letter]] = {key: [] for key, _, _ in MAIL_GROUPS}
    for email in _not_taken(db):
        category = classify_material(email.material_color_guess) if email.material_color_guess else None
        if category in _NOT_MILLING:
            continue
        key = names.get(category or "", "none")
        letter = _letter(email)
        letter.group = key
        groups[key].append(letter)
    return groups


def mail_letters(db: Session) -> list[Letter]:
    """Усі листи «не взято в роботу» одним списком, найновіші зверху — як у CRM."""
    letters = [letter for group in mail_groups(db).values() for letter in group]
    return sorted(letters, key=lambda x: x.received_at or datetime.min, reverse=True)


def _units_sum(letters: list[Letter]) -> int:
    return sum(letter.units or 0 for letter in letters)


def _unknown(letters: list[Letter]) -> int:
    """Скільки листів без числа одиниць: бот його не вигадує."""
    return sum(1 for letter in letters if not letter.units)


def _oldest(letters: list[Letter]) -> Optional[Letter]:
    dated = [x for x in letters if x.received_at is not None]
    return min(dated, key=lambda x: x.received_at or datetime.min) if dated else None


def letters_word(n: int) -> str:
    return _plural(n, "лист чекає", "листи чекають", "листів чекають")


def no_number_line(unknown: int) -> str:
    return f"у {unknown} {_plural(unknown, 'листі', 'листах', 'листах')} числа одиниць немає"


@dataclass
class MailRow:
    """Рядок картинки: матеріал, листи, одиниці. `units_known` False — у
    жодному листі групи числа немає (смуга штрихована, «? од.»)."""

    title: str
    letters: int
    units: int
    units_known: bool


@dataclass
class MailBoard:
    """Те, що малює картинка «Пошти» (`telegram_mail_picture`)."""

    letters: int
    units: int
    unknown: int
    unread: int
    rows: list[MailRow]
    waited: Optional[timedelta]
    oldest_name: str


def mail_board(db: Session, now: datetime) -> MailBoard:
    now = _local(now) or now
    groups = mail_groups(db)
    letters = [letter for group in groups.values() for letter in group]
    rows = [
        MailRow(name or "Без матеріалу", len(groups[key]), _units_sum(groups[key]),
                _unknown(groups[key]) < len(groups[key]))
        for key, name, _ in MAIL_GROUPS if groups[key]
    ]
    oldest = _oldest(letters)
    waited = (now - oldest.received_at) if oldest and oldest.received_at else None
    return MailBoard(
        letters=len(letters),
        units=_units_sum(letters),
        unknown=_unknown(letters),
        unread=sum(1 for x in letters if x.unread),
        rows=rows,
        waited=waited,
        oldest_name=oldest.name if oldest else "",
    )


def wait_text(delta: timedelta) -> str:
    return _span(delta)


def mail_headline(board: MailBoard) -> str:
    """«12 листів чекають · 35 од.» — у підписі до картинки й у тексті."""
    return f"{board.letters} {letters_word(board.letters)} · {board.units} од."


def mail_summary(db: Session, now: Optional[datetime] = None) -> str:
    board = mail_board(db, now or datetime.now())
    if not board.letters:
        return "📬 Пошта: усі листи взято в роботу"
    line = (f"📬 Пошта: <b>{board.letters}</b> {letters_word(board.letters)}"
            f" · <b>{board.units}</b> од.")
    if board.waited is not None:
        line += f"\n      ⏳ найдовше {_span(board.waited)}"
    return line


def _letter_line(letter: Letter, now: datetime) -> str:
    mark = "🆕 " if letter.unread else ""
    bits = [f"{mark}<b>{_e(letter.name)}</b>", _when(letter.received_at, now)]
    if letter.material:
        bits.append(_e(letter.material))
    bits.append(f"{letter.units} од." if letter.units else "число ?")
    return " · ".join(b for b in bits if b)


def _quote(letters: list[Letter], now: datetime) -> str:
    shown = [_letter_line(x, now) for x in letters[:MAX_LIST]]
    if len(letters) > MAX_LIST:
        shown.append(f"ще {len(letters) - MAX_LIST}")
    return "<blockquote>" + "\n".join(shown) + "</blockquote>"


def mail_view(db: Session, opened: Optional[str], now: datetime) -> ViewParts:
    """Текстовий вид «Пошти»: «Листи списком» з-під картинки (`opened` =
    `MAIL_ALL`) і запасний, коли картинку не намальовано."""
    now = _local(now) or now
    groups = mail_groups(db)
    letters = sorted(
        (letter for group in groups.values() for letter in group),
        key=lambda x: (x.received_at is None, x.received_at or now),
    )
    lines: list[str] = []
    rows: list[list[dict]] = []
    # Згорнути = лишитись у тексті; «v:mail» вже веде на картинку.
    closed = f"{EXPAND_CALLBACK}:mail:{MAIL_LIST}"
    if not letters:
        lines.append("<b>Усі листи взято в роботу</b> ✅")
    else:
        board = mail_board(db, now)
        lines.append(f"<b>{mail_headline(board)}</b>")
        sub = []
        if board.waited is not None:
            sub.append(f"⏳ найдовше чекає {_span(board.waited)} · {_e(board.oldest_name)}")
        if board.unread:
            sub.append(f"🆕 не відкрито {board.unread}")
        if sub:
            lines.append(" · ".join(sub))
        if board.unknown:
            lines.append(no_number_line(board.unknown))
        lines.append("")
        buttons: list[dict] = []
        for key, name, icon in MAIL_GROUPS:
            group = groups[key]
            if not group:
                continue
            title = name or _GROUP_TITLE[key]
            n = len(group)
            units = f" · {_units_sum(group)} од." if _unknown(group) < n else ""
            lines.append(f"{icon} {title} — <b>{n}</b> {_plural(n, 'лист', 'листи', 'листів')}{units}")
            if opened == key:
                lines.append(_quote(group, now))
            label = f"{icon} {title if key != 'none' else 'Без матеріалу'} {n}"
            buttons.append(_toggle(label, "mail", key, opened, closed))
        if opened == MAIL_ALL:
            lines.append("")
            lines.append("<b>Усі листи</b> · найдовше чекає зверху")
            lines.append(_quote(letters, now))
        rows.append([_toggle(f"📋 Усі листи {len(letters)}", "mail", MAIL_ALL, opened, closed)])
        for i in range(0, len(buttons), 2):
            rows.append(buttons[i:i + 2])
    rows.append([{"text": "🖼 Картинкою", "callback_data": "v:mail"}])
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
