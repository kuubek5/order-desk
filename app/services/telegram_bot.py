"""Двосторонній Telegram-бот KuubMill: меню стану цеху й сповіщення.

Бриф — TELEGRAM_BOT_BRIEF.md (10.09.26). Бот — окремий бот KuubMill. Ним
користуються власник (`telegram_chat_id`) і учасники, що прийшли за
одноразовим запрошенням (`TelegramMember`). Сюди ж ходить форма
зворотного зв'язку, але в неї своя черга (`Feedback.telegram_*`, бо там
скріншоти); спільний тут ВІДПРАВНИК — один воркер, що говорить з Telegram.

Три частини, і в кожної своє правило:

* **Меню (вхідні).** Long polling `getUpdates` у фоновому потоці — вебхук
  неможливий (127.0.0.1 за TLS-проксі цеху). Оновлення з будь-якого іншого
  чату ігнорується МОВЧКИ: відповідь «не маєте доступу» — уже витік того, що
  бот живий і чий він. Натискання кнопки не шле нове повідомлення, а
  переписує те саме (`editMessageText`), щоб чат не засипало.
  `offset` лежить у базі: після рестарту старі натискання не виконуються
  вдруге.

* **Сповіщення (вихідні).** На ПЕРЕХІД стану, не на стан: «пічка закрилась»,
  «цикл завершено — можна відкривати», «Sisma: друк закінчився». Перехід
  мусить бути побачений на ДВОХ різних кадрах щонайменше за
  `CONFIRM_SECONDS` один від одного — хибне сповіщення гірше за жодне
  (правило печей, CLAUDE.md §14). Статус печі береться лише голосований (RUN
  чи WAIT); «?» і збій зв'язку переходом не є — сумнів = мовчати. Останній
  підтверджений стан пам'ятає `TelegramWatch`, тому рестарт застосунку не
  читає цикл, що йде годину, як новину.

* **Черга відправки.** Подія спершу стає рядком `TelegramOutbox`, відправник
  доносить його з ретраєм і відступом. База — правда, Telegram —
  best-effort; збій мережі не губить повідомлення мовчки, а застаріле
  (`expires_at`) списується, а не шлеться із запізненням на пів дня.

Бот живе, поки працює KuubMill на ПК цеху: вимкнений ПК = тиша. Тому кожна
відповідь меню підписана «станом на HH:MM».
"""

from __future__ import annotations

import html
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app import log_throttle
from app.business_day import business_now, business_today
from app.models import Order, TelegramInvite, TelegramMember, TelegramOutbox, TelegramWatch
from app.services import telegram
from app.services.telegram import KMILL_PREFIX, ApiResult
from app.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

# ── Налаштування ────────────────────────────────────────────────────────────


def bot_enabled(db: Session) -> bool:
    """Вимикач бота + наявність токена й чату. Без будь-чого з трьох — тиша."""
    if (get_setting(db, "telegram_bot_enabled") or "") != "1":
        return False
    return bool(telegram.get_bot_token(db)) and bool(telegram.get_chat_id(db))


def _bot_id(token: str) -> str:
    """Числовий id бота — частина токена ДО двокрапки. Публічна (її видно в
    посиланні на бота), на відміну від решти токена."""
    return token.split(":", 1)[0]


# Ім'я бота (@username) за його id — для посилань-запрошень. Публічне, у
# пам'яті процесу: слухач дізнається його getMe на старті, а екран
# налаштувань не мусить ходити в мережу на кожне відкриття.
_usernames: dict[str, str] = {}


def _remember_username(session, token: str) -> Optional[str]:
    result = telegram.api_call(session, token, "getMe")
    if result.ok and isinstance(result.result, dict) and result.result.get("username"):
        _usernames[_bot_id(token)] = str(result.result["username"])
    return _usernames.get(_bot_id(token))


def bot_username(db: Session, *, fetch: bool = False) -> Optional[str]:
    """@username поточного бота. `fetch=True` — спитати Telegram, якщо в
    пам'яті ще немає (дія адміна «Запросити»); без нього — лише пам'ять."""
    token = telegram.get_bot_token(db)
    if not token:
        return None
    known = _usernames.get(_bot_id(token))
    if known or not fetch:
        return known
    try:
        session = telegram._new_session()
    except Exception:  # noqa: BLE001
        return None
    try:
        return _remember_username(session, token)
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _load_offset(db: Session, token: str) -> Optional[int]:
    """Offset ЦЬОГО бота. Номери оновлень у кожного бота свої: offset,
    успадкований від попереднього токена, або сховав би всі нові
    повідомлення (якщо він більший), або виконав би старі. Тому він
    зберігається як `<id бота>:<offset>`, і чужий = відсутній."""
    raw = (get_setting(db, "telegram_bot_offset") or "").strip()
    owner, _, value = raw.partition(":")
    if not value or owner != _bot_id(token):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _save_offset(db: Session, token: str, offset: int) -> None:
    set_setting(db, "telegram_bot_offset", f"{_bot_id(token)}:{offset}")
    db.commit()


# ── Стан слухача для екрана налаштувань ─────────────────────────────────────
# Живе в пам'яті процесу: це про ЦЕЙ запуск (чи слухає, що сказав Telegram
# востаннє), а не історія. Пише потік слухача, читає HTTP-потік — тому лок.


@dataclass
class BotStatus:
    listening: bool = False
    since: Optional[datetime] = None
    last_ok_at: Optional[datetime] = None
    last_update_at: Optional[datetime] = None
    error: Optional[str] = None
    # Хост вебхука, якщо він стоїть: тоді long polling отримує 409, і
    # KuubMill натискань не бачить. Лише хост — у шляху буває секрет.
    webhook_host: Optional[str] = None
    # Інший процес уже слухає бота (409 без вебхука) — зазвичай друга копія
    # KuubMill (dev і прод з увімкненим ботом одночасно).
    conflict: bool = False
    # Останній приватний чат, що писав боту: «Прив'язати чат» бере його,
    # поки слухач працює (getUpdates із налаштувань тоді або конфліктує,
    # або нічого не бачить — оновлення вже підтвердив слухач).
    last_private_chat: Optional[str] = None


_status = BotStatus()
_status_lock = threading.Lock()


def _set_status(**changes: Any) -> None:
    with _status_lock:
        for name, value in changes.items():
            setattr(_status, name, value)


def status_snapshot() -> BotStatus:
    with _status_lock:
        return BotStatus(**_status.__dict__)


def listener_active() -> bool:
    with _status_lock:
        return _status.listening


def last_private_chat() -> Optional[str]:
    with _status_lock:
        return _status.last_private_chat


def outbox_summary(db: Session) -> dict:
    """Скільки чекає відправки і що сталось з останньою спробою — для
    налаштувань. Невдача, яку видно лише в лозі, для власника не існує."""
    pending = db.scalar(
        select(func.count())
        .select_from(TelegramOutbox)
        .where(TelegramOutbox.sent_at.is_(None), TelegramOutbox.gave_up_at.is_(None))
    ) or 0
    last_sent = db.scalar(select(func.max(TelegramOutbox.sent_at)))
    last_failed = db.scalars(
        select(TelegramOutbox)
        .where(TelegramOutbox.sent_at.is_(None), TelegramOutbox.last_error.is_not(None))
        .order_by(TelegramOutbox.id.desc())
        .limit(1)
    ).first()
    return {
        "pending": int(pending),
        "last_sent_at": last_sent,
        "last_error": last_failed.last_error if last_failed else None,
    }


# ── Текст ──────────────────────────────────────────────────────────────────

# Два меню (рішення власника 10.09.26): власник — адмін і бачить усе;
# учасник — оператор: пічки й Sisma, бо саме про них його будять сповіщення,
# а цифри робіт і видачі — управлінська картина власника. Адмін лише один.
ADMIN_VIEWS = (
    "home", "furnaces", "machines", "sisma",
    "orders", "orders_y", "handout", "handout_y",
)
OPERATOR_VIEWS = ("home", "furnaces", "sisma")
VIEWS = ADMIN_VIEWS  # усі відомі види
_VIEW_TITLE = {
    "home": "",
    "furnaces": "🔥 Пічки",
    "orders": "🧾 Роботи · сьогодні",
    "orders_y": "🧾 Роботи · вчора",
    "handout": "📦 Видача · сьогодні",
    "handout_y": "📦 Видача · вчора",
    "machines": "⚙️ Верстати",
    "sisma": "🖨 Sisma",
}
# Вид із вибором дня → (той самий вид сьогодні, вчора).
_DAY_PAIRS = {
    "orders": ("orders", "orders_y"),
    "orders_y": ("orders", "orders_y"),
    "handout": ("handout", "handout_y"),
    "handout_y": ("handout", "handout_y"),
}


def views_for(admin: bool) -> tuple[str, ...]:
    return ADMIN_VIEWS if admin else OPERATOR_VIEWS


def _e(value: Any) -> str:
    """Екранування для parse_mode=HTML. Імена клієнтів і назви пристроїв —
    вільний текст, і `<` у назві інакше ламає розмітку всього повідомлення."""
    return html.escape(str(value), quote=False)


def _hm(moment: Optional[datetime]) -> str:
    return moment.strftime("%H:%M") if moment else ""


def _plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _works(n: int) -> str:
    return f"{n} {_plural(n, 'робота', 'роботи', 'робіт')}"


def _clip(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _footer(now: datetime) -> str:
    return (
        f"<i>станом на {now.strftime('%H:%M')} · бот відповідає, поки KuubMill "
        f"працює на ПК цеху</i>"
    )


# ── Пічки ───────────────────────────────────────────────────────────────────


def _furnace_line(card) -> str:
    name = f"<b>{_e(card.target.name)}</b>"
    state = card.state
    if card.has_problem:
        return f"{name} — ⚠️ {_e(_clip(card.problem_text))}"
    temp = f" · {state.temp_c}°" if state and state.temp_c is not None else ""
    if card.is_running:
        head = f"{name} — 🔥 працює{temp}"
        if state and state.done_at:
            return (
                f"{head}\n   ще {_e(state.remaining_text)} · відкриється "
                f"<b>{_hm(state.done_at)}</b>"
            )
        return head
    if card.is_idle:
        return f"{name} — ✅ вільна{temp}"
    return f"{name} — ❔ табло не читається{temp}"


def furnaces_text(db: Session) -> str:
    from app.services.furnace import configured_targets, strip_cards

    cards = strip_cards(db)
    if not cards:
        if configured_targets(db):
            return "Чекаємо перший кадр із печей…"
        return "Печей не налаштовано (Налаштування → Печі спікання)."
    return "\n".join(_furnace_line(card) for card in cards)


def _furnaces_summary(db: Session) -> str:
    from app.services.furnace import strip_cards, strip_summary

    cards = strip_cards(db)
    if not cards:
        return "🔥 Пічки: даних ще немає"
    summary = strip_summary(cards)
    parts = []
    if summary.running:
        run = f"{summary.running} працює"
        if summary.nearest_text:
            run += f" (найближча відкриється {summary.nearest_text})"
        parts.append(run)
    idle = sum(1 for card in cards if card.is_idle and not card.has_problem)
    if idle:
        parts.append(f"{idle} вільн{'а' if idle == 1 else 'і'}")
    if summary.broken:
        parts.append(f"⚠️ {summary.broken} без зв'язку")
    return "🔥 Пічки: " + (", ".join(parts) or "табло не читаються")


# ── Роботи ──────────────────────────────────────────────────────────────────


def _day_of(offset: int) -> date:
    """Робочий день: 0 — сьогодні, -1 — вчора (межа доби з business_day)."""
    return business_today() + timedelta(days=offset)


def _orders_of_day(db: Session, day: date) -> list[Order]:
    """Вкладка дня черги — той самий набір, що в `build_queue_view`: живі
    роботи з цією робочою датою. Готовність — ті самі предикати
    (`app/queue_filters.py`), тож числа збігаються з чіпами на екрані."""
    from app.services.order_dates import order_date

    rows = db.scalars(
        select(Order)
        .options(selectinload(Order.rework_records))
        .where(Order.archived_at.is_(None))
    ).all()
    return [order for order in rows if order_date(order) == day]


def _today_orders(db: Session) -> list[Order]:
    return _orders_of_day(db, _day_of(0))


def _orders_text_for(db: Session, offset: int) -> str:
    from app.queue_filters import CLIENT_SOURCES, count_by_readiness
    from app.services.queue_view import sum_units

    day = _day_of(offset)
    orders = _orders_of_day(db, day)
    head = f"Робочий день {day.strftime('%d.%m')}"
    if not orders:
        return f"{head}: робіт немає."
    lab = [o for o in orders if o.source == "lab"]
    clients = [o for o in orders if o.source in CLIENT_SOURCES]
    lines = [f"{head} · {_works(len(orders))}, {sum_units(orders)} од."]
    for title, group, with_not_ready in (
        ("Лабораторія", lab, True),
        ("Файли (клієнти)", clients, False),
    ):
        if not group:
            lines.append(f"\n<b>{title}</b> — немає")
            continue
        counts = count_by_readiness(group)
        lines.append(f"\n<b>{title}</b> — {_works(len(group))}, {sum_units(group)} од.")
        lines.append(f"   можна брати: <b>{counts['can_take']}</b>")
        lines.append(f"   в роботі: {counts['in_work']}")
        # Клієнтські роботи «не готовими» не бувають: файли прийшли з листом
        # (`queue_filters._has_path`). Рядок із вічним нулем — шум.
        if with_not_ready:
            lines.append(f"   не готово: {counts['not_ready']}")
    return "\n".join(lines)


def orders_text(db: Session) -> str:
    return _orders_text_for(db, 0)


def orders_yesterday_text(db: Session) -> str:
    return _orders_text_for(db, -1)


def _orders_summary(db: Session) -> str:
    from app.queue_filters import count_by_readiness

    orders = _today_orders(db)
    if not orders:
        return "🧾 Сьогодні: робіт ще немає"
    counts = count_by_readiness(orders)
    return f"🧾 Сьогодні: {_works(len(orders))} · можна брати {counts['can_take']}"


# ── Видача ──────────────────────────────────────────────────────────────────

_ISSUED = ("видано", "знайдено при видачі")


@dataclass(frozen=True)
class HandoutDay:
    clients: int
    clients_done: int
    works: int
    works_done: int
    units: int
    units_done: int


def handout_day(db: Session, offset: int) -> HandoutDay:
    """Видача дня тими самими правилами, що екран «Видача»: множина —
    `handout_eligible_orders` (клієнтські, живі, видані ЛИШАЮТЬСЯ в ній),
    видано — статуси «видано» / «знайдено при видачі», клієнт — за ключем
    групи картки (`handout_group_key`, зокрема «Без імені»). Клієнт
    «готовий», коли видано всі його роботи цього дня.

    Хвоста «з раніших днів не видано» тут свідомо НЕМАЄ: на живій базі він
    дав 453 роботи — старі дні, де видачу не відмічали в CRM. На телефоні
    таке число читається як тривога, якої, можливо, немає (10.09.26)."""
    from app.services.handout import handout_eligible_orders, handout_group_key
    from app.services.order_dates import parse_sheet_tab
    from app.services.clients import quantity_units

    today = _day_of(0)
    day = _day_of(offset)
    eligible = handout_eligible_orders(db, today)
    orders = [o for o in eligible if parse_sheet_tab(o.sheet_tab) == day]
    done = [o for o in orders if o.status in _ISSUED]
    groups: dict[str, list[Order]] = {}
    for order in orders:
        groups.setdefault(handout_group_key(order), []).append(order)
    return HandoutDay(
        clients=len(groups),
        clients_done=sum(1 for g in groups.values() if all(o.status in _ISSUED for o in g)),
        works=len(orders),
        works_done=len(done),
        units=sum(quantity_units(o.quantity) for o in orders),
        units_done=sum(quantity_units(o.quantity) for o in done),
    )


def _handout_text_for(db: Session, offset: int) -> str:
    stats = handout_day(db, offset)
    head = f"Робочий день {_day_of(offset).strftime('%d.%m')}"
    if not stats.works:
        lines = [f"{head}: клієнтських робіт немає."]
    else:
        lines = [
            head,
            "",
            f"Клієнтів видано: <b>{stats.clients_done}</b> з {stats.clients}",
            f"Робіт видано: <b>{stats.works_done}</b> з {stats.works}",
            f"Одиниць видано: {stats.units_done} з {stats.units}",
        ]
        waiting = stats.works - stats.works_done
        lines.append(f"Ще чекає: <b>{_works(waiting)}</b>" if waiting else "Усе видано ✅")
    return "\n".join(lines)


def handout_text(db: Session) -> str:
    return _handout_text_for(db, 0)


def handout_yesterday_text(db: Session) -> str:
    return _handout_text_for(db, -1)


def _handout_summary(db: Session) -> str:
    stats = handout_day(db, -1)
    if not stats.works:
        return ""
    return f"📦 Видача за вчора: {stats.clients_done} з {stats.clients} кл."


# ── Верстати ────────────────────────────────────────────────────────────────


def _order_label(card) -> str:
    """Наряд і клієнт роботи на верстаті (власник дозволив обидва, 10.09.26)."""
    orders = card.orders or []
    if not orders:
        return f"Sum3D {_e(card.sum3d_id)}" if card.sum3d_id else ""
    first = orders[0]
    bits = []
    if first.work_order_no:
        bits.append(f"наряд {_e(first.work_order_no)}")
    if first.client_name:
        bits.append(_e(first.client_name))
    if not bits and card.sum3d_id:
        bits.append(f"Sum3D {_e(card.sum3d_id)}")
    label = " · ".join(bits)
    if len(orders) > 1:
        label += f" (+{len(orders) - 1})"
    return label


def _machine_line(card) -> str:
    name = f"<b>{_e(card.target.name)}</b>"
    key = card.state_key
    if key == "off":
        return f"{name} — ⚠️ {_e(_clip(card.problem_text))}"
    if key == "run":
        head = f"{name} — ⚙️ {card.percent}%"
    elif key == "done":
        head = f"{name} — ✅ завершено · зняти"
    else:
        head = f"{name} — {_e(card.state_word)}"
    label = _order_label(card) if key in ("run", "done", "busy", "check") else ""
    return f"{head}\n   {label}" if label else head


def machines_text(db: Session) -> str:
    from app.services.machines import snapshot

    cards = [card for card in snapshot(db) if not card.is_sisma_machine]
    if not cards:
        return "Верстатів не налаштовано."
    running = sum(1 for card in cards if card.is_running)
    head = f"Фрезерують {running} з {len(cards)}"
    return head + "\n\n" + "\n".join(_machine_line(card) for card in cards)


def _machines_summary(db: Session) -> str:
    from app.services.machines import strip_summary

    summary = strip_summary(db)
    if not summary["total"]:
        return "⚙️ Верстати: не налаштовано"
    text = f"⚙️ Верстати: фрезерують {summary['running']} з {summary['total']}"
    if summary["broken"]:
        text += f" · ⚠️ {summary['broken']} без зв'язку"
    return text


# ── Sisma ───────────────────────────────────────────────────────────────────


def _when(moment: Optional[datetime], now: datetime) -> str:
    if moment is None:
        return ""
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    return moment.strftime("%d.%m %H:%M")


def _sisma_line(card, now: datetime) -> str:
    name = f"<b>{_e(card.target.name)}</b>"
    if card.has_problem:
        return f"{name} — ⚠️ {_e(_clip(card.problem_text))}"
    if card.is_completed:
        return f"{name} — ✅ друк закінчився"
    layers = card.layers
    if layers:
        layer, total = layers
        pct = round(layer * 100 / total) if total else 0
        head = f"{name} — шар <b>{layer}</b> з {total} ({pct}%)"
        if card.phase_text:
            head += f" · {_e(card.phase_text)}"
        detail = []
        if card.started_at:
            detail.append(f"старт {_when(card.started_at, now)}")
        if card.ends_at:
            detail.append(f"кінець ≈ <b>{_when(card.ends_at, now)}</b>")
        if card.left_text:
            detail.append(_e(card.left_text))
        return head + ("\n   " + " · ".join(detail) if detail else "")
    return f"{name} — {_e(card.state_word or 'даних немає')}"


def sisma_text(db: Session) -> str:
    from app.services.machines import snapshot

    cards = [card for card in snapshot(db) if card.is_sisma_machine]
    if not cards:
        return "Принтер Sisma ще не впізнано: жоден верстат не показав його екран."
    now = datetime.now()
    return "\n".join(_sisma_line(card, now) for card in cards)


def _sisma_summary(db: Session) -> str:
    from app.services.machines import snapshot

    cards = [card for card in snapshot(db) if card.is_sisma_machine]
    if not cards:
        return ""
    card = cards[0]
    if card.has_problem:
        return "🖨 Sisma: ⚠️ без зв'язку"
    if card.is_completed:
        return "🖨 Sisma: друк закінчився"
    if card.layers:
        layer, total = card.layers
        text = f"🖨 Sisma: шар {layer}/{total}"
        if card.ends_at:
            text += f" · кінець ≈ {_when(card.ends_at, datetime.now())}"
        return text
    return f"🖨 Sisma: {card.state_word or 'даних немає'}"


# ── Збірка відповіді ────────────────────────────────────────────────────────

_BODIES: dict[str, Callable[[Session], str]] = {
    "furnaces": furnaces_text,
    "orders": orders_text,
    "orders_y": orders_yesterday_text,
    "handout": handout_text,
    "handout_y": handout_yesterday_text,
    "machines": machines_text,
    "sisma": sisma_text,
}


def _safe(fn: Callable[[Session], str], db: Session, what: str) -> str:
    """Одне джерело, що впало, не має гасити все меню: піч без зв'язку чи
    збій запиту — рядок «не вдалось», а решта відповіді на місці."""
    try:
        return fn(db)
    except Exception:  # noqa: BLE001
        logger.exception("telegram-бот: не вдалось зібрати «%s»", what)
        db.rollback()
        return f"{what}: не вдалось прочитати (подробиці в лозі KuubMill)"


def home_text(db: Session, admin: bool = True) -> str:
    if not admin:
        lines = [_safe(_furnaces_summary, db, "Пічки"), _safe(_sisma_summary, db, "Sisma")]
    else:
        lines = [
            _safe(_furnaces_summary, db, "Пічки"),
            _safe(_orders_summary, db, "Роботи"),
            _safe(_handout_summary, db, "Видача"),
            _safe(_machines_summary, db, "Верстати"),
            _safe(_sisma_summary, db, "Sisma"),
        ]
    return "\n".join(line for line in lines if line)


def render(db: Session, view: str, now: Optional[datetime] = None, admin: bool = True) -> str:
    """Текст відповіді меню (parse_mode=HTML), з шапкою й підписом часу.
    Вид, закритий для ролі, стає головним — а не чужим текстом."""
    if view not in views_for(admin):
        view = "home"
    now = now or business_now()
    title = _VIEW_TITLE[view]
    head = f"<b>{KMILL_PREFIX}</b>" + (f" · {title}" if title else "")
    body = home_text(db, admin) if view == "home" else _safe(_BODIES[view], db, title)
    return f"{head}\n\n{body}\n\n{_footer(now)}"


def keyboard(view: str, notify: bool = True, admin: bool = True) -> dict:
    def button(text: str, target: str) -> dict:
        return {"text": text, "callback_data": f"v:{target}"}

    if admin:
        rows = [
            [button("🔥 Пічки", "furnaces"), button("⚙️ Верстати", "machines"), button("🖨 Sisma", "sisma")],
            [button("🧾 Роботи", "orders"), button("📦 Видача", "handout")],
            # Не вид, а дія: звіт збирається у фоні й приходить НОВИМИ
            # повідомленнями (підсумок + файл), меню лишається на місці.
            [{"text": "🩺 Стан системи", "callback_data": REPORT_CALLBACK}],
        ]
    else:
        rows = [[button("🔥 Пічки", "furnaces"), button("🖨 Sisma", "sisma")]]
    pair = _DAY_PAIRS.get(view) if admin else None
    if pair:
        today_view, yesterday_view = pair
        rows.append([
            button(("✓ " if view == today_view else "") + "Сьогодні", today_view),
            button(("✓ " if view == yesterday_view else "") + "Вчора", yesterday_view),
        ])
    last = [button("🔄 Оновити", view)]
    if view != "home":
        last.append(button("🏠 Меню", "home"))
    rows.append(last)
    # Кожен сам вирішує, чи будити його сповіщеннями: логісту пічки о 03:00
    # ні до чого, а просити власника вимкнути — зайвий крок.
    bell = "🔔 Сповіщення: так" if notify else "🔕 Сповіщення: ні"
    rows.append([{"text": bell, "callback_data": f"n:{view}"}])
    return {"inline_keyboard": rows}


# ── Хто користується ботом ──────────────────────────────────────────────────

# «0» — власник вимкнув собі сповіщення кнопкою 🔔; порожньо — увімкнено.
OWNER_NOTIFY_KEY = "telegram_owner_notify"
INVITE_TTL = timedelta(hours=24)


# Склад: учасник, який отримує ЛИШЕ замовлення дисків (екран «Нові диски»).
# Меню пічок, верстатів, Sisma й робіт йому недоступне, сповіщення печей не
# йдуть — рішення власника 10.09.26.
ROLE_WAREHOUSE = "warehouse"
MEMBER_ROLES = ("", ROLE_WAREHOUSE)


@dataclass(frozen=True)
class Recipient:
    chat_id: str
    notify: bool
    member_id: Optional[int] = None  # None — власник
    role: str = ""

    @property
    def is_owner(self) -> bool:
        return self.member_id is None

    @property
    def is_warehouse(self) -> bool:
        return self.role == ROLE_WAREHOUSE


def owner_notify(db: Session) -> bool:
    return (get_setting(db, OWNER_NOTIFY_KEY) or "") != "0"


def recipients(db: Session) -> list[Recipient]:
    """Власник (`telegram_chat_id`) і всі учасники — ті, кому бот відповідає."""
    owner = telegram.get_chat_id(db)
    out = [Recipient(owner, owner_notify(db))] if owner else []
    for member in db.scalars(select(TelegramMember).order_by(TelegramMember.id)):
        if member.chat_id != owner:
            out.append(Recipient(member.chat_id, bool(member.notify), member.id, member.role or ""))
    return out


def warehouse_members(db: Session) -> list[TelegramMember]:
    """Хто отримує замовлення дисків. Власник сюди не входить ніколи."""
    owner = telegram.get_chat_id(db)
    return [
        member
        for member in db.scalars(
            select(TelegramMember)
            .where(TelegramMember.role == ROLE_WAREHOUSE)
            .order_by(TelegramMember.id)
        )
        if member.chat_id != owner
    ]


def warehouse_blocker(db: Session) -> Optional[str]:
    """Чому «Надіслати на склад» зараз неможливе. None — можна.

    Текст іде просто під кнопку: оператор мусить знати, чому кнопка
    сіра, і що «Замовлено без відправки» при цьому працює."""
    if not bot_enabled(db):
        return "Telegram-бот вимкнено або не налаштовано — надіслати на склад не вийде."
    if not warehouse_members(db):
        return "Склад не підключено до бота — адмін запрошує його в Налаштуваннях зворотного зв'язку."
    return None


def find_recipient(db: Session, chat_id: Optional[str]) -> Optional[Recipient]:
    if not chat_id:
        return None
    return next((r for r in recipients(db) if r.chat_id == chat_id), None)


def set_notify(db: Session, recipient: Recipient, notify: bool) -> None:
    """Нічого не комітить."""
    if recipient.is_owner:
        set_setting(db, OWNER_NOTIFY_KEY, "" if notify else "0")
    else:
        member = db.get(TelegramMember, recipient.member_id)
        if member is not None:
            member.notify = notify


def new_invite(
    db: Session,
    *,
    label: str = "",
    created_by_id: Optional[int] = None,
    now: Optional[datetime] = None,
    role: str = "",
) -> TelegramInvite:
    """Одноразове запрошення. 128 біт випадковості: код не вгадати, а
    Telegram пропускає в `start` лише [A-Za-z0-9_-] до 64 символів —
    `token_urlsafe` саме з цього алфавіту. Нічого не комітить."""
    now = now or datetime.now()
    invite = TelegramInvite(
        code=secrets.token_urlsafe(16),
        label=(label or "").strip()[:120],
        role=role if role in MEMBER_ROLES else "",
        created_at=now,
        expires_at=now + INVITE_TTL,
        created_by_id=created_by_id,
    )
    db.add(invite)
    return invite


def active_invites(db: Session, now: Optional[datetime] = None) -> list[TelegramInvite]:
    now = now or datetime.now()
    return list(
        db.scalars(
            select(TelegramInvite)
            .where(
                TelegramInvite.used_at.is_(None),
                TelegramInvite.revoked_at.is_(None),
                TelegramInvite.expires_at > now,
            )
            .order_by(TelegramInvite.id.desc())
        )
    )


def list_members(db: Session) -> list[TelegramMember]:
    return list(db.scalars(select(TelegramMember).order_by(TelegramMember.joined_at)))


def invite_link(username: Optional[str], code: str) -> Optional[str]:
    return f"https://t.me/{username}?start={code}" if username else None


def _display_name(sender: dict) -> str:
    parts = [sender.get("first_name") or "", sender.get("last_name") or ""]
    return " ".join(p for p in parts if p).strip()[:200]


def member_title(member: TelegramMember) -> str:
    """Як назвати людину в списку й у сповіщенні власнику: підпис
    запрошення, інакше ім'я з Telegram, інакше нік."""
    if member.label:
        return member.label
    if member.name:
        return member.name
    return f"@{member.username}" if member.username else member.chat_id


def _start_code(text: str) -> Optional[str]:
    """`/start <код>` (з посилання-запрошення) → код. Інакше None."""
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) == 2 and parts[0].split("@", 1)[0] == "/start":
        return parts[1].strip() or None
    return None


def redeem_invite(
    db: Session, code: str, chat_id: str, sender: dict, now: datetime
) -> Optional[TelegramMember]:
    """Погасити запрошення й записати учасника. None — коду немає, його
    використано, скасовано чи прострочено. Нічого не комітить."""
    invite = db.scalars(select(TelegramInvite).where(TelegramInvite.code == code)).first()
    if (
        invite is None
        or invite.used_at is not None
        or invite.revoked_at is not None
        or invite.expires_at <= now
    ):
        return None
    member = db.scalars(select(TelegramMember).where(TelegramMember.chat_id == chat_id)).first()
    if member is None:
        member = TelegramMember(
            chat_id=chat_id,
            name=_display_name(sender),
            username=(sender.get("username") or None),
            label=invite.label,
            role=invite.role or "",
            notify=True,
            joined_at=now,
            invited_by_id=invite.created_by_id,
            last_seen_at=now,
        )
        db.add(member)
    invite.used_at = now
    invite.used_by_chat = chat_id
    return member


# ── Розбір оновлень ─────────────────────────────────────────────────────────

# Повідомлення, старші за це, не отримують відповіді: ПК цеху стояв уночі,
# Рома тричі писав /start — вранці на нього не має впасти три меню.
STALE_MESSAGE_SECONDS = 10 * 60

WAREHOUSE_WELCOME = (
    f"{KMILL_PREFIX}: доступ відкрито. Сюди приходитимуть замовлення дисків "
    "на склад — більше нічого."
)
WAREHOUSE_ONLY = f"{KMILL_PREFIX}: цей чат лише для замовлень дисків — меню тут немає."


# ── «🩺 Стан системи»: звіт власнику ───────────────────────────────────────
# Кнопка в меню власника (або текст /report, «звіт»). Звіт — той самий, що
# «Звіт для розробника» в налаштуваннях: самоперевірка, налаштування без
# секретів, база, журнали, помилки й хвіст лога. Приходить двома НОВИМИ
# повідомленнями: короткий підсумок (що не так) і повний звіт файлом.
#
# Збирається у ФОНОВОМУ потоці: проби ходять у мережу й на диск (Google,
# пошта, теки) і можуть тривати десятки секунд, а слухач меню за цей час не
# має замовкати. Один звіт за раз — повторне натискання лише каже «уже
# збираю». Будувати звіт сервіс бота сам не вміє (проби живуть у роутері
# налаштувань), тому його реєструє web.py: `set_report_builder`.

REPORT_CALLBACK = "r:report"
_REPORT_COMMANDS = ("/report", "/zvit", "/звіт", "звіт", "стан", "стан системи")
_report_builder: Optional[Callable[[Session], tuple[str, list]]] = None
_report_lock = threading.Lock()
# Скільки провалених/сумнівних проб і рядків помилок показати в підсумку —
# решта у файлі. Повідомлення має читатись з екрана блокування.
REPORT_SUMMARY_ITEMS = 6


def set_report_builder(builder: Optional[Callable[[Session], tuple[str, list]]]) -> None:
    global _report_builder
    _report_builder = builder


def _is_report_command(text: str) -> bool:
    word = (text or "").strip().lower()
    return word.split("@", 1)[0] in _REPORT_COMMANDS


def _report_ack(started: Optional[bool]) -> str:
    if started is None:
        return "Звіт недоступний у цій збірці"
    if started:
        return "🩺 Збираю звіт — до хвилини, прийде окремими повідомленнями"
    return "Звіт уже збирається — зачекайте"


def start_report(chat_id: str) -> Optional[bool]:
    """Запустити звіт у фоні. True — запущено, False — уже йде, None —
    будувати нема чим (не зареєстровано)."""
    if _report_builder is None:
        return None
    if not _report_lock.acquire(blocking=False):
        return False
    thread = threading.Thread(target=_report_job, args=(chat_id,), name="kuubmill-tg-report", daemon=True)
    try:
        thread.start()
    except Exception:  # noqa: BLE001
        _report_lock.release()
        raise
    return True


def report_summary(results: list, errors: list[str], now: Optional[datetime] = None) -> str:
    """Короткий підсумок звіту для Telegram (parse_mode=HTML)."""
    from app.__version__ import VERSION

    now = now or datetime.now()
    lines = [f"<b>{KMILL_PREFIX}</b> · 🩺 Стан системи · {now:%d.%m %H:%M}", f"Версія {_e(VERSION)}"]
    if results:
        passed = sum(1 for r in results if r.ok)
        bad = [r for r in results if not r.ok]
        warn = [r for r in results if r.ok and r.warn]
        mark = "✅" if not bad else "❌"
        lines.append(f"{mark} Самоперевірка: {passed} з {len(results)}")
        for r in (bad + warn)[:REPORT_SUMMARY_ITEMS]:
            icon = "❌" if not r.ok else "⚠️"
            lines.append(f"{icon} {_e(r.name)} — {_e((r.detail or '')[:160])}")
    if errors:
        lines.append(f"❗ Помилок у лозі за добу: {len(errors)}")
        for line in errors[-3:]:
            lines.append(f"• <code>{_e(line[:200])}</code>")
    else:
        lines.append("✅ Помилок у лозі за добу немає")
    lines.append("Повний звіт — файлом нижче.")
    return "\n".join(lines)


def _report_job(chat_id: str) -> None:
    from app.services.support_report import recent_errors, report_filename

    session = None
    token: Optional[str] = None
    try:
        SessionLocal = _session_factory()
        with SessionLocal() as db:
            token = telegram.get_bot_token(db)
            builder = _report_builder
            if not token or builder is None:
                return
            text, results = builder(db)
        summary = report_summary(results, recent_errors())
        session = telegram._new_session()
        sent = telegram.api_call(
            session, token, "sendMessage",
            {"chat_id": chat_id, "text": summary, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        if not sent.ok:
            _log_throttled("report", "telegram-бот: підсумок звіту не надіслано: %s", sent.error)
        doc = telegram.send_document(
            session, token, chat_id, report_filename(), text.encode("utf-8"),
            caption="Звіт KuubMill — секретів у файлі немає",
        )
        if not doc.ok:
            _log_throttled("report-doc", "telegram-бот: файл звіту не надіслано: %s", doc.error)
    except Exception:  # noqa: BLE001 — фоновий потік не має падати мовчки й валити застосунок
        logger.exception("telegram-бот: не вдалось зібрати звіт стану системи")
        # Мовчазна кнопка гірша за зламану: власник мусить знати, що звіту не буде.
        if token:
            try:
                if session is None:
                    session = telegram._new_session()
                telegram.api_call(session, token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": f"{KMILL_PREFIX}: звіт зібрати не вдалось — причина в лозі KuubMill.",
                })
            except Exception:  # noqa: BLE001
                pass
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        _report_lock.release()


@dataclass
class Action:
    """Один виклик Bot API, який треба зробити у відповідь на оновлення."""

    method: str
    payload: dict = field(default_factory=dict)


def _chat_of(update: dict) -> tuple[Optional[str], Optional[str]]:
    """(chat_id, тип чату) оновлення — з повідомлення або з кнопки під ним."""
    message = update.get("message") or update.get("edited_message")
    if message is None:
        query = update.get("callback_query") or {}
        message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    return (str(chat_id) if chat_id is not None else None, chat.get("type"))


def _menu_payload(
    db: Session,
    chat_id: str,
    view: str,
    notify: bool,
    now: Optional[datetime],
    *,
    admin: bool,
    lead: str = "",
) -> dict:
    if view not in views_for(admin):
        view = "home"
    text = render(db, view, now, admin=admin)
    if lead:
        text = f"{lead}\n\n{text}"
    return {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": keyboard(view, notify, admin=admin),
    }


def handle_update(
    db: Session, update: dict, *, chat_id: Optional[str] = None, now: Optional[datetime] = None
) -> list[Action]:
    """Що відповісти на одне оновлення. Нічого не шле сам — повертає дії.

    Доступ мають власник і учасники (`recipients`) — вирішує база, а не
    параметр `chat_id` (лишився для сумісності викликів і нічого не
    розширює).

    Чужий чат → порожній список, БЕЗ відповіді й без `answerCallbackQuery`:
    мовчання — єдина відповідь, що нічого не розповідає стороннім. Виняток
    один — `/start <код>` із дійсного запрошення."""
    del chat_id
    update_chat, chat_type = _chat_of(update)
    if update_chat is not None and chat_type == "private":
        _set_status(last_private_chat=update_chat)
    if update_chat is None or chat_type != "private":
        return []
    moment = now or business_now()
    local_now = datetime.now()
    who = find_recipient(db, update_chat)

    query = update.get("callback_query")
    if query is not None:
        if who is None:
            return []
        data = str(query.get("data") or "")
        answer = Action("answerCallbackQuery", {"callback_query_id": query.get("id")})
        if who.is_warehouse:
            # Складу кнопок не шлемо; стара кнопка (людина була учасником до
            # того, як їй дали роль складу) нічого не відкриває.
            return [answer]
        if data == REPORT_CALLBACK:
            # Звіт — лише власнику: у ньому стан усього ПК, лог і журнали.
            if who.is_owner:
                answer.payload["text"] = _report_ack(start_report(update_chat))
            return [answer]
        message = query.get("message") or {}
        kind, _, view = data.partition(":")
        # Вид, закритий для ролі, не відкривається навіть підробленою
        # кнопкою: callback_data приходить від клієнта, і вірити їй не можна.
        if (
            kind not in ("v", "n")
            or view not in views_for(who.is_owner)
            or message.get("message_id") is None
        ):
            return [answer]
        notify = who.notify
        if kind == "n":
            notify = not who.notify
            set_notify(db, who, notify)
            db.commit()
            answer.payload["text"] = "Сповіщення увімкнено" if notify else "Сповіщення вимкнено"
        payload = _menu_payload(db, update_chat, view, notify, now, admin=who.is_owner)
        payload["message_id"] = message["message_id"]
        return [answer, Action("editMessageText", payload)]

    message = update.get("message")
    if message is None:
        return []  # редагування старого повідомлення тощо — не запит
    sent = message.get("date")
    if isinstance(sent, (int, float)) and moment.timestamp() - sent > STALE_MESSAGE_SECONDS:
        return []
    sender = message.get("from") or {}

    if who is None:
        code = _start_code(message.get("text") or "")
        if code is None:
            return []
        member = redeem_invite(db, code, update_chat, sender, local_now)
        if member is None:
            return []  # невідомий, використаний чи прострочений код — мовчання
        _notify_owner_of_join(db, member, local_now)
        db.commit()
        if member.role == ROLE_WAREHOUSE:
            return [Action("sendMessage", {"chat_id": update_chat, "text": WAREHOUSE_WELCOME})]
        lead = "Доступ до бота KuubMill відкрито. Кнопки нижче; 🔔 вимикає сповіщення."
        return [
            Action(
                "sendMessage",
                _menu_payload(db, update_chat, "home", True, now, admin=False, lead=lead),
            )
        ]

    if not who.is_owner:
        member = db.get(TelegramMember, who.member_id)
        if member is not None:
            member.last_seen_at = local_now
            member.name = _display_name(sender) or member.name
            member.username = sender.get("username") or member.username
            db.commit()
    if who.is_owner and _is_report_command(message.get("text") or ""):
        return [Action("sendMessage", {"chat_id": update_chat, "text": _report_ack(start_report(update_chat))})]
    if who.is_warehouse:
        # Склад меню не має: на команду — одне коротке пояснення без жодних
        # даних цеху, на звичайний текст («прийнято», «ок» у відповідь на
        # замовлення) — мовчання, щоб бот не відповідав на кожне «ок».
        if (message.get("text") or "").lstrip().startswith("/"):
            return [Action("sendMessage", {"chat_id": update_chat, "text": WAREHOUSE_ONLY})]
        return []
    # Будь-який текст (зокрема /start і /menu) — головне меню новим
    # повідомленням. Окремих команд не заводимо: меню — це і є інтерфейс.
    return [
        Action(
            "sendMessage",
            _menu_payload(db, update_chat, "home", who.notify, now, admin=who.is_owner),
        )
    ]


def _notify_owner_of_join(db: Session, member: TelegramMember, now: datetime) -> None:
    """Власник має знати, хто щойно зайшов: посилання могли переслати далі,
    і перший, хто його відкрив, — не обов'язково той, кому його давали."""
    owner = telegram.get_chat_id(db)
    if not owner:
        return
    who = _e(member_title(member))
    extra = f" (@{_e(member.username)})" if member.username else ""
    if member.role == ROLE_WAREHOUSE:
        extra += " — як склад, отримуватиме замовлення дисків"
    enqueue(
        db,
        dedup_key=f"join:{member.chat_id}:{now:%Y%m%d%H%M%S}",
        kind="member_joined",
        text=f"{KMILL_PREFIX} · 👤 До бота приєднався: <b>{who}</b>{extra}.",
        now=now,
        ttl=timedelta(days=2),
        chat_id=owner,
    )


def _benign_failure(action: Action, result: ApiResult) -> bool:
    """Помилки, які не є збоєм: «Оновити» без змін (той самий текст у ту саму
    хвилину) і прострочена кнопка (натиснули, поки ПК стояв)."""
    text = (result.error or "").lower()
    if action.method == "editMessageText" and "message is not modified" in text:
        return True
    if action.method == "answerCallbackQuery" and "query is too old" in text:
        return True
    return False


# ── Сповіщення: переходи ────────────────────────────────────────────────────

# Новий стан мусить протриматись на двох різних кадрах щонайменше стільки.
CONFIRM_SECONDS = 10
# Між останнім підтвердженим спостереженням і новим минуло більше — перехід
# не сповіщаємо: коли він стався, невідомо, а «закрилась» про цикл, що йде
# годину, — хибне повідомлення. Стан просто беремо за нову точку відліку.
GAP_SECONDS = 30 * 60
SEEN_RESOLUTION_SECONDS = 60

# Стан, побачений один раз і ще не підтверджений: ключ → (стан, момент кадру).
# У пам'яті свідомо: рестарт посеред підтвердження лише відкладає його на
# один тік, а не губить перехід (рядок TelegramWatch лишається старим).
_pending: dict[str, tuple[str, datetime]] = {}
_pending_lock = threading.Lock()


@dataclass(frozen=True)
class Transition:
    key: str
    old: str
    new: str
    at: datetime  # коли новий стан побачили вперше


def observe(db: Session, key: str, state: str, observed_at: datetime) -> Optional[Transition]:
    """Врахувати одне спостереження стану. Повертає перехід, коли він
    ПІДТВЕРДЖЕНИЙ, інакше None. Нічого не комітить."""
    row = db.get(TelegramWatch, key)
    with _pending_lock:
        if row is None:
            db.add(TelegramWatch(key=key, state=state, since_at=observed_at, seen_at=observed_at))
            _pending.pop(key, None)
            return None
        if state == row.state:
            # Раз на хвилину, не щокадру: межа «загубили слід» — пів години,
            # і хвилинна точність їй досить, а UPDATE кожні 15 с на кожну піч
            # — це просто шум у базі.
            if (observed_at - row.seen_at).total_seconds() >= SEEN_RESOLUTION_SECONDS:
                row.seen_at = observed_at
            _pending.pop(key, None)
            return None
        if (observed_at - row.seen_at).total_seconds() > GAP_SECONDS:
            row.state, row.since_at, row.seen_at = state, observed_at, observed_at
            _pending.pop(key, None)
            return None
        pending = _pending.get(key)
        if pending is None or pending[0] != state:
            _pending[key] = (state, observed_at)
            return None
        if (observed_at - pending[1]).total_seconds() < CONFIRM_SECONDS:
            return None
        old = row.state
        row.state, row.since_at, row.seen_at = state, pending[1], observed_at
        del _pending[key]
        return Transition(key=key, old=old, new=state, at=pending[1])


def _furnace_message(card, transition: Transition) -> Optional[tuple[str, str, timedelta]]:
    name = f"<b>{_e(card.target.name)}</b>"
    if transition.old == "WAIT" and transition.new == "RUN":
        text = f"{KMILL_PREFIX} · 🔥 {name} закрилась — цикл пішов."
        state = card.state
        if state and state.done_at:
            text += (
                f"\nВідкриється ≈ <b>{_hm(state.done_at)}</b> "
                f"(ще {_e(state.remaining_text)})."
            )
        return "furnace_closed", text, timedelta(hours=1)
    if transition.old == "RUN" and transition.new == "WAIT":
        text = f"{KMILL_PREFIX} · ✅ {name}: цикл завершено — можна відкривати."
        # Температура — факт з табло поруч із висновком: програму могли й
        # зупинити посеред нагріву, і тоді «можна відкривати» без числа
        # читалось би як дозвіл. Нема числа — нема й рядка, не вгадуємо.
        state = card.state
        if state and state.temp_c is not None:
            text += f"\nНа табло зараз {state.temp_c}°."
        return "furnace_open", text, timedelta(hours=3)
    return None


def _sisma_state(card) -> Optional[str]:
    """Стан принтера для сповіщень. None — не певні, спостереження немає."""
    if card.has_problem or card.stale or not card.has_frame:
        return None
    if card.is_completed:
        return "done"
    if card.layers is not None:
        return "run"
    if card.state_key == "idle":
        return "idle"
    return None


def enqueue(
    db: Session,
    *,
    dedup_key: str,
    kind: str,
    text: str,
    now: datetime,
    ttl: timedelta,
    chat_id: Optional[str] = None,
) -> bool:
    """Поставити повідомлення в чергу. False — таке вже є (той самий ключ).
    `chat_id` None — власнику.

    Перевірка SELECT-ом, а не ловлею IntegrityError: відкат зніс би разом із
    дублем і оновлення TelegramWatch у тій самій транзакції."""
    dedup_key = dedup_key[:200]  # довжина колонки: перевіряємо той ключ, що ляже
    exists = db.scalar(select(TelegramOutbox.id).where(TelegramOutbox.dedup_key == dedup_key))
    if exists is not None:
        return False
    db.add(
        TelegramOutbox(
            dedup_key=dedup_key,
            kind=kind,
            text=text,
            created_at=now,
            expires_at=now + ttl,
            attempts=0,
            chat_id=chat_id,
        )
    )
    return True


def broadcast(
    db: Session, *, event_key: str, kind: str, text: str, now: datetime, ttl: timedelta
) -> int:
    """Подія → по рядку черги кожному, хто не вимкнув собі 🔔. Окремі рядки,
    а не один на всіх: недосяжний учасник не тримає доставку решті, і
    повтор іде лише тому, кому не дійшло. Повертає, скільки рядків лягло."""
    queued = 0
    for recipient in recipients(db):
        # Складу — лише замовлення дисків, сповіщення печей і Sisma не йдуть.
        if not recipient.notify or recipient.is_warehouse:
            continue
        if enqueue(
            db,
            dedup_key=f"{event_key}@{recipient.chat_id}",
            kind=kind,
            text=text,
            now=now,
            ttl=ttl,
            chat_id=recipient.chat_id,
        ):
            queued += 1
    return queued


# ── Замовлення дисків на склад ─────────────────────────────────────────────
# Екран «Нові диски»: «Надіслати на склад» = рядок черги кожному учаснику з
# роллю складу. Скасування замовлення не шле нового повідомлення, а РЕДАГУЄ
# те саме (закреслене + «скасовано HH:MM») — інакше в чаті складу лишилось
# би «замовлення», яке треба пам'ятати, що скасоване (рішення власника).

DISC_ORDER_KIND = "disc_order"
DISC_ORDER_CANCEL_KIND = "disc_order_cancel"
KEEP_KINDS = (DISC_ORDER_KIND, DISC_ORDER_CANCEL_KIND)
# Замовлення, що не дійшло за три доби, уже не новина: склад за цей час
# отримав наступне, а оператор бачить «не дійшло» в історії.
DISC_ORDER_TTL = timedelta(days=3)


def _disc_order_key(order_id: int, chat_id: str) -> str:
    return f"disc-order:{order_id}@{chat_id}"


def queue_disc_order(db: Session, *, order_id: int, text: str, now: datetime) -> int:
    """Поставити замовлення в чергу кожному складу. Повертає скільки рядків.
    Нічого не комітить."""
    queued = 0
    for member in warehouse_members(db):
        if enqueue(
            db,
            dedup_key=_disc_order_key(order_id, member.chat_id),
            kind=DISC_ORDER_KIND,
            text=text,
            now=now,
            ttl=DISC_ORDER_TTL,
            chat_id=member.chat_id,
        ):
            queued += 1
    return queued


def disc_order_rows(db: Session, order_ids: list[int]) -> dict[int, list[TelegramOutbox]]:
    """Рядки черги (оригінали) замовлень — для стану доставки в історії."""
    if not order_ids:
        return {}
    wanted = set(order_ids)
    out: dict[int, list[TelegramOutbox]] = {}
    for row in db.scalars(
        select(TelegramOutbox)
        .where(TelegramOutbox.kind == DISC_ORDER_KIND)
        .order_by(TelegramOutbox.id)
    ):
        head = row.dedup_key.split("@", 1)[0]
        try:
            order_id = int(head.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if order_id in wanted:
            out.setdefault(order_id, []).append(row)
    return out


def queue_disc_order_cancel(
    db: Session, *, order_id: int, text: str, now: datetime
) -> int:
    """Скасування: кожне ВЖЕ надіслане (чи ще в дорозі) повідомлення складу
    редагується на `text`. Недоставлений оригінал списується — інакше склад
    отримав би замовлення вже після скасування. Нічого не комітить.

    Гонка «відправник саме зараз шле оригінал»: списання тоді не встигає, але
    рядок редагування все одно лягає — і відправник виправить повідомлення,
    щойно у оригіналу з'явиться `message_id` (див. `flush_outbox`)."""
    queued = 0
    for original in disc_order_rows(db, [order_id]).get(order_id, []):
        if original.sent_at is None and original.gave_up_at is None:
            _give_up(original, now, "замовлення скасовано до відправки")
        exists = db.scalar(
            select(TelegramOutbox.id).where(TelegramOutbox.edit_of_id == original.id)
        )
        if exists is not None:
            continue
        db.add(
            TelegramOutbox(
                dedup_key=f"disc-order-cancel:{order_id}@{original.chat_id}:{original.id}"[:200],
                kind=DISC_ORDER_CANCEL_KIND,
                text=text,
                created_at=now,
                expires_at=now + DISC_ORDER_TTL,
                attempts=0,
                chat_id=original.chat_id,
                edit_of_id=original.id,
            )
        )
        queued += 1
    return queued


# Відправник спить OUT_TICK_SECONDS між тіками. Замовлення, на яке оператор
# щойно натиснув, не має чекати 15 секунд — кнопка будить відправника.
_outbound_wake = threading.Event()


def wake_outbound() -> None:
    _outbound_wake.set()


def _dedup_key(transition: Transition) -> str:
    return f"{transition.key}:{transition.old}>{transition.new}:{transition.at:%Y%m%d%H%M%S}"


def watch_tick(db: Session, now: Optional[datetime] = None) -> int:
    """Один прохід по печах і принтеру: спостереження → переходи → черга.
    Повертає, скільки ПОДІЙ сталось (не рядків: рядок на кожного адресата).
    Нічого не комітить.

    Перехід фіксується в TelegramWatch навіть тоді, коли слати нікому
    (усі вимкнули 🔔): інакше, увімкнувши дзвіночок, людина отримала б
    новину про давно минуле."""
    from app.services.furnace import STATUS_RUN, STATUS_WAIT, snapshot as furnace_snapshot
    from app.services.machines import snapshot as machine_snapshot

    now = now or datetime.now()
    events = 0

    for card in furnace_snapshot(db):
        state = card.state
        # Лише голосований статус зі свіжого кадру. «?», збій зв'язку чи
        # опитувач, що замовк, — не спостереження.
        if not card.has_data or card.has_problem or state is None or state.captured_at is None:
            continue
        if card.status not in (STATUS_RUN, STATUS_WAIT):
            continue
        transition = observe(db, f"furnace:{card.key}", card.status, state.captured_at)
        if transition is None:
            continue
        message = _furnace_message(card, transition)
        if message is None:
            continue
        kind, text, ttl = message
        events += 1
        broadcast(db, event_key=_dedup_key(transition), kind=kind, text=text, now=now, ttl=ttl)

    for printer in machine_snapshot(db):
        if not printer.is_sisma_machine:
            continue
        observed = _sisma_state(printer)
        if observed is None or printer.frame_at is None:
            continue
        transition = observe(db, f"sisma:{printer.key}", observed, printer.frame_at)
        if transition is None or not (transition.old == "run" and transition.new == "done"):
            continue
        text = f"{KMILL_PREFIX} · 🖨 <b>{_e(printer.target.name)}</b>: друк закінчився."
        events += 1
        broadcast(
            db,
            event_key=_dedup_key(transition),
            kind="sisma_done",
            text=text,
            now=now,
            ttl=timedelta(hours=6),
        )

    return events


# ── Черга відправки ─────────────────────────────────────────────────────────

OUTBOX_BATCH = 20
# Відправлені й списані рядки старші за це прибираються — це журнал
# доставки, а не історія подій (історія — у показаннях печей).
OUTBOX_KEEP_DAYS = 30


def _backoff(attempts: int) -> timedelta:
    """30 с, 1 хв, 2 хв … до 15 хв між спробами."""
    return timedelta(seconds=min(30 * (2 ** max(attempts - 1, 0)), 15 * 60))


def _give_up(row: TelegramOutbox, now: datetime, reason: str) -> None:
    row.gave_up_at = now
    row.last_error = (((row.last_error + " · ") if row.last_error else "") + reason)[:300]


def _hopeless(result: ApiResult) -> bool:
    """Відповіді, після яких повтор нічого не змінить: людина заблокувала
    бота або чату не існує. Повторювати їх годинами — лише шум у черзі."""
    text = (result.error or "").lower()
    if result.status == 403:
        return True
    return result.status == 400 and "chat not found" in text


def _message_id(result: ApiResult) -> Optional[int]:
    """Id повідомлення з відповіді sendMessage — для пізнішого редагування."""
    body = result.result
    if isinstance(body, dict) and isinstance(body.get("message_id"), int):
        return body["message_id"]
    return None


def _unchanged(result: ApiResult) -> bool:
    """editMessageText з тим самим текстом Telegram відхиляє 400-кою
    «message is not modified». Для нас це успіх: повідомлення вже таке."""
    return result.status == 400 and "not modified" in (result.error or "").lower()


# Редагування чекає, поки дійде оригінал, — але не вічно.
EDIT_WAIT = timedelta(seconds=30)


def flush_outbox(
    db: Session,
    send: Callable[[str, str], ApiResult],
    now: Optional[datetime] = None,
    edit: Optional[Callable[[str, int, str], ApiResult]] = None,
) -> int:
    """Донести все, що чекає. `send(chat_id, text)` — один sendMessage;
    `edit(chat_id, message_id, text)` — один editMessageText для рядків, що
    редагують уже надіслане (`edit_of_id`). Без `edit` такі рядки лишаються
    чекати. Комітить після КОЖНОГО рядка: падіння посеред пачки не має
    відправити вже відправлене вдруге. Повертає скільки відправлено."""
    now = now or datetime.now()
    owner = telegram.get_chat_id(db)
    allowed = {r.chat_id for r in recipients(db)}

    # Прострочене списуємо окремо й до вибірки: інакше рядки, що чекають
    # повтору, займали б місця в пачці й тримали чергу всіх інших.
    for row in db.scalars(
        select(TelegramOutbox).where(
            TelegramOutbox.sent_at.is_(None),
            TelegramOutbox.gave_up_at.is_(None),
            TelegramOutbox.expires_at.is_not(None),
            TelegramOutbox.expires_at < now,
        )
    ):
        _give_up(row, now, "прострочено")
    db.commit()

    rows = db.scalars(
        select(TelegramOutbox)
        .where(
            TelegramOutbox.sent_at.is_(None),
            TelegramOutbox.gave_up_at.is_(None),
            (TelegramOutbox.next_attempt_at.is_(None)) | (TelegramOutbox.next_attempt_at <= now),
        )
        .order_by(TelegramOutbox.id)
        .limit(OUTBOX_BATCH)
    ).all()
    sent = 0
    for row in rows:
        chat = row.chat_id or owner
        if not chat or chat not in allowed:
            # Учасника прибрали (або власника перев'язали) — його черга
            # нікому не належить.
            _give_up(row, now, "адресата прибрано")
            db.commit()
            continue
        if row.edit_of_id is not None:
            if edit is None:
                continue
            original = db.get(TelegramOutbox, row.edit_of_id)
            if original is None:
                _give_up(row, now, "оригіналу вже немає в черзі")
                db.commit()
                continue
            if original.message_id is None:
                if original.sent_at is None and original.gave_up_at is None:
                    # Оригінал ще в дорозі — редагувати нема чого. Спробуємо,
                    # щойно дійде; попереду за id він і піде першим.
                    row.next_attempt_at = now + EDIT_WAIT
                    db.commit()
                    continue
                _give_up(row, now, "оригінал не надіслано — редагувати нічого")
                db.commit()
                continue
            result = edit(chat, original.message_id, row.text)
            if _unchanged(result):
                result = ApiResult(True, result.status, None, None)
        else:
            result = send(chat, row.text)
        row.attempts = (row.attempts or 0) + 1
        if result.ok:
            row.sent_at = now
            row.last_error = None
            row.next_attempt_at = None
            if row.edit_of_id is None:
                row.message_id = _message_id(result)
            sent += 1
            db.commit()
            continue
        row.last_error = (result.error or "невідома помилка")[:300]
        # Редагування, на яке Telegram відповів 400 («message to edit not
        # found», «message can't be edited»), повтором не виправиться.
        if _hopeless(result) or (row.edit_of_id is not None and result.status == 400):
            _give_up(row, now, "не доставити")
            db.commit()
            continue
        row.next_attempt_at = now + _backoff(row.attempts)
        db.commit()
        if result.status is None:
            break  # мережа лежить — решту пачки мучити немає сенсу
    return sent


def prune_outbox(db: Session, now: Optional[datetime] = None) -> int:
    """Прибрати старі відправлені й списані рядки.

    Замовлення дисків НЕ прибираються: з них читається стан доставки в
    історії замовлень («надіслано 17:48» / «не дійшло»), а `message_id` —
    потрібен, щоб скасувати БУДЬ-ЯКЕ замовлення, навіть місячної давності,
    редагуванням того самого повідомлення складу. Їх одне-два на день."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=OUTBOX_KEEP_DAYS)
    result = db.execute(
        delete(TelegramOutbox).where(
            (TelegramOutbox.sent_at < cutoff) | (TelegramOutbox.gave_up_at < cutoff),
            TelegramOutbox.kind.not_in(KEEP_KINDS),
        )
    )
    db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


# ── Воркери ─────────────────────────────────────────────────────────────────

POLL_TIMEOUT_SECONDS = 25
# Потік слухача перечитує вимикач і токен між опитуваннями, тож вимкнення в
# налаштуваннях діє не пізніше за цей інтервал + один long poll.
DISABLED_RECHECK_SECONDS = 20
WEBHOOK_RECHECK_SECONDS = 5 * 60
CONFLICT_RECHECK_SECONDS = 60
OUT_TICK_SECONDS = 15
FEEDBACK_FLUSH_SECONDS = 120
FEEDBACK_INITIAL_DELAY_SECONDS = 45


def _session_factory():
    from app.db import SessionLocal

    return SessionLocal


def _read_config(db: Session) -> tuple[bool, Optional[str], Optional[str]]:
    return bot_enabled(db), telegram.get_bot_token(db), telegram.get_chat_id(db)


def _log_throttled(key: str, message: str, *args: Any) -> None:
    skipped = log_throttle.due(f"telegram-bot:{key}")
    if skipped is None:
        return
    suffix = f" (ще {skipped} разів з минулого запису)" if skipped else ""
    logger.warning(message + suffix, *args)


def _webhook_host(session, token: str) -> tuple[Optional[str], Optional[str]]:
    """(хост вебхука або None, помилка). Хост без шляху: у шляху буває секрет."""
    info = telegram.api_call(session, token, "getWebhookInfo")
    if not info.ok:
        return None, info.error
    url = (info.result or {}).get("url") or ""
    if not url:
        return None, None
    return urlparse(url).netloc or "невідомий хост", None


def run_actions(session, token: str, actions: list[Action]) -> None:
    for action in actions:
        result = telegram.api_call(session, token, action.method, action.payload)
        if not result.ok and not _benign_failure(action, result):
            _log_throttled(
                f"action:{action.method}",
                "telegram-бот: %s не вдалось: %s",
                action.method,
                result.error,
            )


def inbound_worker(stop_event: threading.Event) -> None:
    """Слухач меню: long polling getUpdates, поки бот увімкнено."""
    SessionLocal = _session_factory()
    session = None
    # Для якого бота вебхук уже перевірено. Id, а не прапорець: токен міняють
    # у налаштуваннях на ходу, і в нового бота вебхук може стояти свій.
    webhook_checked_for: Optional[str] = None
    if stop_event.wait(5):
        return
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                enabled, token, chat_id = _read_config(db)
                offset = _load_offset(db, token) if token else None
            if not (enabled and token and chat_id):
                _set_status(listening=False, since=None, conflict=False, webhook_host=None, error=None)
                webhook_checked_for = None
                stop_event.wait(DISABLED_RECHECK_SECONDS)
                continue
            if session is None:
                session = telegram._new_session()

            if webhook_checked_for != _bot_id(token):
                host, err = _webhook_host(session, token)
                if host is not None:
                    # Чужий вебхук мовчки НЕ знімаємо: хтось його поставив, і
                    # deleteWebhook зламав би той інший сервіс без жодного сліду.
                    _set_status(listening=False, webhook_host=host, conflict=False, error=None)
                    stop_event.wait(WEBHOOK_RECHECK_SECONDS)
                    continue
                if err is None:
                    webhook_checked_for = _bot_id(token)
                    _set_status(webhook_host=None)
                    _remember_username(session, token)

            if offset is None:
                # Перший запуск цього бота: усе, що накопичилось до нас, — не
                # запит до KuubMill. offset=-1 підтверджує хвіст, нічого не
                # виконуючи. Лише чат запам'ятовуємо — щоб «Прив'язати чат»
                # спрацював із тим /start, яке людина вже надіслала.
                result = telegram.api_call(
                    session, token, "getUpdates", {"offset": -1, "timeout": 0}
                )
                if result.ok:
                    updates = result.result or []
                    for update in updates:
                        update_chat, chat_type = _chat_of(update)
                        if update_chat is not None and chat_type == "private":
                            _set_status(last_private_chat=update_chat)
                    last = max((u.get("update_id", 0) for u in updates), default=-1)
                    with SessionLocal() as db:
                        _save_offset(db, token, last + 1 if last >= 0 else 0)
                    continue

            else:
                payload = {
                    "timeout": POLL_TIMEOUT_SECONDS,
                    "allowed_updates": ["message", "callback_query"],
                }
                if offset:
                    payload["offset"] = offset
                result = telegram.api_call(
                    session, token, "getUpdates", payload,
                    timeout=(10, POLL_TIMEOUT_SECONDS + 15),
                )

            if not result.ok:
                if result.status == 409:
                    webhook_checked_for = None
                    _set_status(listening=False, conflict=True, error=result.error)
                    _log_throttled("409", "telegram-бот: 409 — бота вже слухає інший процес")
                    stop_event.wait(CONFLICT_RECHECK_SECONDS)
                    continue
                _set_status(listening=False, error=result.error)
                _log_throttled("poll", "telegram-бот: getUpdates не вдався: %s", result.error)
                if result.status is None:
                    try:
                        session.close()
                    except Exception:  # noqa: BLE001
                        pass
                    session = None
                stop_event.wait(15)
                continue

            now = datetime.now()
            with _status_lock:
                if not _status.listening:
                    _status.since = now
                _status.listening = True
                _status.last_ok_at = now
                _status.error = None
                _status.conflict = False
            log_throttle.clear("telegram-bot:poll")

            updates = result.result or []
            if not updates:
                continue
            new_offset = max(u.get("update_id", 0) for u in updates) + 1
            # Offset — ДО виконання: якщо відповідь впаде посеред пачки, те
            # саме натискання після рестарту не повториться. Пропущене меню
            # дешевше за подвійне.
            with SessionLocal() as db:
                _save_offset(db, token, new_offset)
                for update in updates:
                    try:
                        actions = handle_update(db, update, chat_id=chat_id)
                    except Exception:  # noqa: BLE001
                        logger.exception("telegram-бот: не вдалось розібрати оновлення")
                        db.rollback()
                        continue
                    if actions:
                        _set_status(last_update_at=datetime.now())
                        run_actions(session, token, actions)
        except Exception:  # noqa: BLE001 — потік слухача не має вмирати
            logger.exception("telegram-бот: збій циклу слухача")
            _set_status(listening=False, error="внутрішня помилка — див. лог")
            stop_event.wait(30)
    _set_status(listening=False)
    if session is not None:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def outbound_tick(db: Session, *, flush_feedback: bool, now: Optional[datetime] = None) -> None:
    """Один тік відправника: переходи → черга → доставка; і звернення
    зворотного зв'язку, що не долетіли. Кожна частина окремо — збій одної
    не зупиняє іншу."""
    now = now or datetime.now()
    if bot_enabled(db):
        try:
            watch_tick(db, now)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("telegram-бот: збій спостереження за пічками/Sisma")

    token = telegram.get_bot_token(db)
    chat_id = telegram.get_chat_id(db)
    if token and chat_id and bot_enabled(db):
        session_box: list = []

        def send(chat: str, text: str) -> ApiResult:
            if not session_box:
                session_box.append(telegram._new_session())
            return telegram.api_call(
                session_box[0],
                token,
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )

        def edit(chat: str, message_id: int, text: str) -> ApiResult:
            if not session_box:
                session_box.append(telegram._new_session())
            return telegram.api_call(
                session_box[0],
                token,
                "editMessageText",
                {
                    "chat_id": chat,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )

        try:
            flush_outbox(db, send, now, edit=edit)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("telegram-бот: збій відправки черги")
        finally:
            for session in session_box:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass

    if flush_feedback:
        from app.services.feedback import flush_pending_pushes

        try:
            flush_pending_pushes(db, now=now)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("feedback push retry tick failed")


def outbound_worker(stop_event: threading.Event) -> None:
    """Єдиний відправник у Telegram: сповіщення бота й ретрай звернень."""
    SessionLocal = _session_factory()
    started = time.monotonic()
    last_feedback: Optional[float] = None
    last_prune: Optional[float] = None
    if stop_event.wait(20):
        return
    while not stop_event.is_set():
        _outbound_wake.clear()
        mono = time.monotonic()
        flush_feedback = mono - started >= FEEDBACK_INITIAL_DELAY_SECONDS and (
            last_feedback is None or mono - last_feedback >= FEEDBACK_FLUSH_SECONDS
        )
        try:
            with SessionLocal() as db:
                outbound_tick(db, flush_feedback=flush_feedback)
                if last_prune is None or mono - last_prune >= 24 * 3600:
                    prune_outbox(db)
                    last_prune = mono
        except Exception:  # noqa: BLE001
            logger.exception("telegram: збій тіку відправника")
        if flush_feedback:
            last_feedback = mono
        # Сон до наступного тіку, але з пробудженням: «Надіслати на склад»
        # будить відправника, і замовлення йде за секунду, а не за 15.
        # Скидається НА ПОЧАТКУ тіку (нижче в циклі): пробудження, що прийшло
        # під час тіку, не губиться — сон одразу закінчиться.
        for _ in range(OUT_TICK_SECONDS):
            if stop_event.wait(1) or _outbound_wake.is_set():
                break


def reset_for_tests() -> None:
    global _status
    _usernames.clear()
    with _pending_lock:
        _pending.clear()
    with _status_lock:
        _status = BotStatus()
