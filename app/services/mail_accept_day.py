"""У яку вкладку лягає лист, прийнятий у вихідні (власник 22.11.26).

Правило вихідних (CLAUDE.md §4): вкладок за суботу й неділю немає, тож робота
вихідних і прийнятий тоді лист пишуться у вкладку п'ятниці. Але в неділю
ввечері оператор, закінчивши своє, вантажить верстати роботами НА ПОНЕДІЛОК —
і вони мусять лягти в понеділкову вкладку. Досі це робили руками: рядок у
таблицю, а лист у CRM лишався неприйнятим.

Тут — перемикач «Приймаю на: п'ятницю / понеділок» у шапці пошти:

* видно лише у вихідні (робочий день — субота чи неділя, межа 07:30 та сама,
  що всюди: нічний оператор у понеділок о 03:00 ще на неділі);
* вибір — оператора, не всієї програми: напарник, який доробляє п'ятничне,
  його не помічає;
* у базі лежить НАЗВА цільової вкладки, тож вибір сам втрачає силу в понеділок
  зранку й не переноситься на наступні вихідні;
* понеділок пропонуємо лише коли його вкладку вже видно в таблиці (синк приніс
  з неї хоч одну роботу) — інакше рядок не було б куди писати.

Що вибір НЕ чіпає: теку в `export` (вона за датою скачування, і видача
понеділка сама дивиться на день назад — `handout.covered_days`), переробку з
листа (`mail_rework` — завжди «сьогодні») і зведені листи (другий лист групи
йде до роботи першого разом з її вкладкою).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import business_today, next_tab_day, tab_day
from app.models import Order
from app.settings_store import USER_KEY_PREFIX_ACCEPT_DAY, get_setting, set_setting

TAB_FORMAT = "%d.%m.%y"
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")

CURRENT = "current"
NEXT = "next"


def _key(user: Any) -> str:
    return f"{USER_KEY_PREFIX_ACCEPT_DAY}{int(user.id)}"


def window(now: datetime | None = None) -> Optional[tuple[date, date]]:
    """(вкладка, куди пишуть зараз; наступна вкладка) — лише у вихідні.

    У будень повертає None: вибирати нема з чого, лист лягає у свій день."""
    today = business_today(now)
    current = tab_day(today)
    if current == today:
        return None
    return current, next_tab_day(today)


def tab_known(db: Session, day: date) -> bool:
    """Чи бачив синк таку вкладку в таблиці (є хоч одна робота з неї)."""
    title = day.strftime(TAB_FORMAT)
    return db.scalar(select(Order.id).where(Order.sheet_tab == title).limit(1)) is not None


def chosen(db: Session, user: Any, now: datetime | None = None) -> Optional[date]:
    """Понеділкова вкладка, якщо оператор перемкнув і вибір ще чинний; інакше
    None — тоді прийняття працює як завжди."""
    days = window(now)
    if days is None or user is None or getattr(user, "id", None) is None:
        return None
    upcoming = days[1]
    if get_setting(db, _key(user)) != upcoming.strftime(TAB_FORMAT):
        return None
    return upcoming


def choose(db: Session, user: Any, which: str, now: datetime | None = None) -> bool:
    """Записати вибір оператора. False — вибирати зараз нема з чого (будень)
    або понеділкової вкладки ще немає в таблиці. Коміт — за викликачем."""
    days = window(now)
    if days is None or user is None or getattr(user, "id", None) is None:
        return False
    if which == NEXT:
        if not tab_known(db, days[1]):
            return False
        set_setting(db, _key(user), days[1].strftime(TAB_FORMAT))
        return True
    if which == CURRENT:
        set_setting(db, _key(user), "")
        return True
    return False


def _short(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day:%d.%m}"


@dataclass(frozen=True)
class AcceptDayView:
    current_label: str   # «пт 20.11»
    next_label: str      # «пн 23.11»
    on_next: bool        # оператор приймає на наступну вкладку
    next_known: bool     # наступна вкладка вже є в таблиці

    @property
    def state(self) -> str:
        return NEXT if self.on_next else CURRENT

    @property
    def button_suffix(self) -> str:
        """Хвіст кнопки «Прийняти»: куди ляже рядок, коли це НЕ звичний день."""
        return f" → {self.next_label}" if self.on_next else ""


def view(db: Session, user: Any, now: datetime | None = None) -> Optional[AcceptDayView]:
    """Стан перемикача для шаблонів; None у будень — перемикача немає."""
    days = window(now)
    if days is None or user is None:
        return None
    current, upcoming = days
    return AcceptDayView(
        current_label=_short(current),
        next_label=_short(upcoming),
        on_next=chosen(db, user, now) is not None,
        next_known=tab_known(db, upcoming),
    )
