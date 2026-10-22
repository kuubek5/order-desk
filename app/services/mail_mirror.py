"""Дзеркало черги внизу екрана пошти — док (MAIL_LAB_DOCK_BRIEF.md, 07.10.26).

Канон (`scope == ""`) — read-only список робіт, ПРИЙНЯТИХ саме з пошти
(source == "email"): оператор прийняв лист — робота одразу видима внизу, без
переходу в чергу. Так було з 23.09.26 (рішення власника: не вся черга).

Опційно (варіант A, власник 07.10.26) оператор розширює той самий док:
`"lab"` — плюс лабораторні рядки вкладки дня, `"all"` — уся вкладка (лаба,
табличні клієнти, пошта). CAM-оператор рахує листи й лабу впереміш і бігав
між «Нові з пошти» і «Чергою»; док дає лабу під рукою, не змінюючи екран для
тих, хто його не вмикав.

Свій легкий прохід, а не build_queue_view: тут не треба ні фільтрів періоду,
ні сканування мережевих тек — лише кілька полів на компактний рядок.

Шпильки «мої зараз» (власник 22.10.26): у блоці ЛАБИ пришпилені оператором
роботи стоять першими — як у черзі, тим самим порядком набору (час
пришпилення, `focus.ranks`), далі решта лаби в порядку таблиці. Блок пошти й
«уся вкладка» шпильки не сортують (рішення власника: поки лише лаба). Набір —
той самий словник, з якого роут бере `focused_ids` для рядків
(`MailDock.focus_rank`), тож підсвітка й порядок не можуть розійтись.

Скидається щодня (власник 29.09.26): раніше список тримав 30-денне вікно
retention, як черга, і робота лишалась видимою тут навіть ПІСЛЯ видачі —
Юрія Бойка прийняли з пошти 25.09, видали 28.09, а рядок все одно висів
29.09 серед щойно прийнятих листів, до яких оператор більше не мав стосунку.
Дзеркало — не «черга для email», а «що я оприйняв сьогодні»; вчорашнє
(зроблене чи ні) належить самій черзі, не цьому списку.

День доку (власник 18.10.26): типово — вкладка сьогодні, як і було; оператор
може перемкнути док на іншу вкладку (Вчора / Завтра / ‹ › по днях з роботами)
і рахувати її лабу тут же — ті самі правила вкладок, що в періодах черги
(`business_day`: у вихідні «Сьогодні» — пʼятниця, «Вчора» в понеділок —
пʼятниця). Обраний день живе на сторінці (кожен запит полла несе його в
`day`), а через перезавантаження /mail — у куці браузера, дійсній лише в той
самий робочий день (`dock_day_cookie` / `dock_day_from_cookie`): наступного
ранку док знову на сьогодні. На акаунті день не зберігається — як і в черзі,
де день живе в адресі, а не в профілі.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.business_day import (
    business_tab_today,
    business_today,
    next_tab_day,
    prev_tab_day,
    tab_day,
)
from app.models import Order, User
from app.queue_filters import READY_FILTERS, count_by_readiness, filter_by_readiness
from app.services.look_prefs import (
    DOCK_HEIGHT,
    MAIL_DOCK_READY_DEFAULT,
    MAIL_DOCK_SCOPES,
    mail_dock_ready_of,
)
from app.services.focus import ranks as focus_ranks
from app.services.order_dates import order_date, parse_sheet_tab
from app.services.queue import RETENTION_DAYS

#: Слово «сьогодні» на дроті. Порожнє `day` означає «поля не було» (CLAUDE.md
#: §14), тож повернення на сьогодні мусить мати ВЛАСНЕ слово.
DOCK_DAY_TODAY = "today"

#: Кука, що переносить обраний день через перезавантаження /mail (прийняття
#: листа з картки, перемикач вигляду — повні переходи, а не свапи).
DOCK_DAY_COOKIE = "kmill_dock_day"

#: Підпис доку за джерелом. Канон лишається «Прийняте з пошти» — слово, до
#: якого оператори звикли за два тижні; розширений док — уже «Черга».
DOCK_TITLES = {"": "Прийняте з пошти", "lab": "Черга", "labonly": "Лабораторія", "all": "Черга"}

#: Порожній стан — свій під кожне джерело: «приймеш лист — зʼявиться» для
#: лабораторного доку брехало б.
DOCK_EMPTY = {
    "": "Прийнятих з пошти робіт поки немає — приймеш лист, робота зʼявиться тут.",
    "lab": "Ні прийнятих з пошти, ні лабораторних робіт під цей фільтр у вкладці дня немає.",
    "labonly": "Лабораторних робіт під цей фільтр у вкладці дня немає.",
    "all": "У вкладці дня немає робіт під цей фільтр.",
}


def _live_orders(db: Session) -> list[Order]:
    """Живі роботи (не в архіві), найновіша згори.

    `archived_at IS NULL` стоїть у SQL (архів росте щомісяця); відсів за
    днем — у Python, бо бізнес-дата виводиться з `sheet_tab`, а не зі стовпця
    (як у build_queue_view). `Order.material` тягнемо одразу — його читає
    material_badge для чипа матеріалу в рядку дзеркала.
    """
    stmt = (
        select(Order)
        .options(selectinload(Order.material))
        .where(Order.archived_at.is_(None))
        .order_by(Order.id.desc())
    )
    return list(db.scalars(stmt).all())


def dock_day_from_wire(value: str | None) -> date | None:
    """День доку зі слова на дроті. `"today"`, порожнє чи нерозібране → None
    (сьогоднішня вкладка); `"дд.мм.рр"` → ВКЛАДКА цього дня (субота й неділя →
    пʼятниця, `tab_day`). Дата — лише через `parse_sheet_tab`, той самий розбір,
    що в смузі днів черги, не свій `strptime`."""
    if not value or value == DOCK_DAY_TODAY:
        return None
    parsed = parse_sheet_tab(value)
    if parsed is None:
        return None
    day = tab_day(parsed)
    return None if day == business_tab_today() else day


def dock_day_cookie(day: date | None) -> str:
    """Значення куки для обраного дня: `<день>_<сьогоднішня вкладка>`.
    Порожній рядок — день сьогоднішній, куку треба стерти."""
    today = business_tab_today()
    if day is None or day == today:
        return ""
    return f"{day.strftime('%d.%m.%y')}_{today.strftime('%d.%m.%y')}"


def dock_day_from_cookie(value: str | None) -> date | None:
    """Обраний день із куки — лише якщо її поставили СЬОГОДНІ (та сама
    вкладка «сьогодні» в другій половині значення). Учорашній вибір уранці не
    діє: док знову на сьогоднішній вкладці, як було до появи вибору дня."""
    if not value:
        return None
    picked, _, picked_on = value.partition("_")
    if parse_sheet_tab(picked_on) != business_tab_today():
        return None
    return dock_day_from_wire(picked)


def _sheet_order_key(order: Order) -> tuple[int, int, int]:
    """Порядок таблиці: рядок згори вниз (CLAUDE.md §2 — порядок черги = порядок
    таблиці, і полл його не пересортовує). Робота без рядка (поштова, що ще
    чекає запису, `sheet_row_pending`) іде ПЕРШОЮ, найновіша згори: це щойно
    прийняте, і ховати його в кінець списку означало б «зникло»."""
    if order.row_number is None:
        return (0, 0, -order.id)
    return (1, order.row_number, order.id)


def _pinned_first(rows: list[Order], focus_rank: Mapping[int, int]) -> list[Order]:
    """Пришпилені оператором — нагору блоку, у порядку НАБОРУ (час
    пришпилення), решта — як стояла (сортування стабільне, порядок таблиці
    серед непришпилених не міняється). Рівно правило черги (`queue_view`,
    «Пришпилені — нагору»): queue.js кладе щойно пришпилений рядок у КІНЕЦЬ
    набору, і полл мусить повторити той самий порядок — за порядком таблиці
    серед пришпилених набір перемішувався б під рукою кожні 15 с."""
    if not focus_rank:
        return rows
    big = len(focus_rank)
    return sorted(rows, key=lambda o: focus_rank.get(o.id, big))


def mail_mirror_orders(
    db: Session, *, scope: str = "", ready: str = MAIL_DOCK_READY_DEFAULT, day: date | None = None
) -> list[Order]:
    """Роботи доку в порядку показу.

    * `scope == ""` — рівно теперішня логіка: пошта дня, `Order.id DESC`
      (порядок СТВОРЕННЯ, найновіша згори; власник 25.09.26: «остання робота
      завжди зверху». Не порядок таблиці: рядки там зсуваються від видалень і
      дописувань — на dev 25.09.26 робота, прийнята о 12:26, стояла в рядку
      нижче за прийняту о 13:59, і дзеркало за рядком ставило б свіжу роботу
      посередині). `ready` тут не діє — листи оператор щойно прийняв сам.
    * `scope == "lab"` — той самий блок пошти, далі лабораторні рядки в
      порядку таблиці, відфільтровані за `ready`. Два порядки — тому між
      блоками шаблон малює роздільник (`MailDock.sep_index`).
    * `scope == "all"` — уся вкладка в порядку таблиці; `ready` діє на все,
      крім пошти.
    """
    return mail_dock(db, scope=scope, ready=ready, day=day).orders


@dataclass
class MailDock:
    scope: str
    ready: str
    height: int
    orders: list[Order] = field(default_factory=list)
    #: Індекс першого лабораторного рядка в `orders` (лише scope="lab" і
    #: лише коли є ОБИДВА блоки) — шаблон ставить перед ним роздільник.
    sep_index: int | None = None
    #: Лічильники сегментів перемикача: пошта дня; лаба під поточний фільтр
    #: готовності; уся вкладка без фільтра.
    mail_count: int = 0
    lab_count: int = 0
    all_count: int = 0
    #: Лічильники чипів готовності — по НЕпоштових рядках обраного джерела
    #: (для канону — по лабі, щоб цифри вже стояли на кнопці «+ Лабораторія»).
    ready_counts: dict[str, int] = field(default_factory=dict)
    #: Вкладка, яку показує док, і сьогоднішня вкладка (у вихідні — пʼятниця).
    day: date = field(default_factory=business_tab_today)
    today: date = field(default_factory=business_tab_today)
    #: Сусідні вкладки — ті самі «Вчора»/«Завтра», що в періодах черги.
    yesterday: date | None = None
    tomorrow: date | None = None
    #: Скільки живих робіт у вкладці (усі джерела, без фільтрів) — числа на
    #: кнопках дня, як лічильники періодів у черзі.
    day_counts: dict[date, int] = field(default_factory=dict)
    #: Найближчі дні з роботами до / після обраного — стрілки ‹ ›.
    prev_day: date | None = None
    next_day: date | None = None
    #: Набір «мої зараз» оператора: order_id → місце за часом пришпилення.
    #: Роут бере звідси ж `focused_ids` для рядків (один запит, одне джерело).
    focus_rank: dict[int, int] = field(default_factory=dict)
    #: Індекс першого рядка блоку ЛАБИ, всередині якого пришпилені йдуть
    #: нагору (scope "lab"/"labonly", коли лаба є); None — такого блоку немає
    #: (канон, «уся вкладка»). Шаблон ставить `data-pin-lab` на tbody, і
    #: queue.js підіймає пришпилений рядок лише в межах цього блоку.
    lab_start: int | None = None
    #: Скільки пришпилених стоїть нагорі блоку лаби — роздільник тоді каже
    #: «мої зараз першими», а не «порядок як у таблиці» (було б неправдою).
    lab_pinned: int = 0

    @property
    def title(self) -> str:
        return DOCK_TITLES[self.scope]

    @property
    def is_today(self) -> bool:
        return self.day == self.today

    @property
    def wire_day(self) -> str:
        """Слово дня на дроті: сьогодні — `"today"`, інакше `"дд.мм.рр"`."""
        return DOCK_DAY_TODAY if self.is_today else self.day.strftime("%d.%m.%y")

    @property
    def other_day(self) -> bool:
        """Обраний день поза «Вчора / Сьогодні / Завтра» — шапка малює його
        окремою кнопкою, інакше вибір стрілкою ніде не підсвічувався б."""
        return self.day not in (self.yesterday, self.today, self.tomorrow)

    @property
    def empty_text(self) -> str:
        # «Приймеш лист — зʼявиться тут» на вчорашній вкладці брехало б: лист
        # ляже в сьогоднішню. Не сьогодні — просто називаємо вкладку.
        if not self.is_today:
            return f"У вкладці {self.day.strftime('%d.%m.%y')} робіт під цей вибір немає."
        return DOCK_EMPTY[self.scope]

    @property
    def wire_scope(self) -> str:
        """Слово на дроті: канон "" → "mail" (пастка «порожнє поле»)."""
        return self.scope or "mail"


def mail_dock(
    db: Session,
    *,
    scope: str = "",
    ready: str = MAIL_DOCK_READY_DEFAULT,
    height: int = 0,
    day: date | None = None,
    focus_rank: Mapping[int, int] | None = None,
) -> MailDock:
    """Повний стан доку: роботи, роздільник, лічильники. Один запит живих
    робіт; вкладка дня, числа на кнопках дня й стрілки — з нього ж, без
    додаткових звернень до бази. `day=None` — сьогоднішня вкладка (як було до
    вибору дня); будь-яка дата зводиться до вкладки (`tab_day`: вихідні →
    пʼятниця)."""
    # Невідоме чи "mail" (слово канону на дроті) → канон. Невідомий `ready`
    # — дефолт, а не помилка: сюди значення приходять з бази, не з форми.
    if scope not in MAIL_DOCK_SCOPES or scope == "mail":
        scope = ""
    if ready not in READY_FILTERS:
        ready = MAIL_DOCK_READY_DEFAULT

    today = business_tab_today()
    shown = today if day is None else tab_day(day)
    live = _live_orders(db)
    by_day: dict[date, int] = {}
    for o in live:
        d = order_date(o)
        by_day[d] = by_day.get(d, 0) + 1
    day_rows = [o for o in live if order_date(o) == shown]
    mail = [o for o in day_rows if o.source == "email"]
    lab = [o for o in day_rows if o.source == "lab"]
    others = [o for o in day_rows if o.source != "email"]

    dock = MailDock(scope=scope, ready=ready, height=DOCK_HEIGHT.clamp_or_zero(height))
    # Набір «мої зараз» (`focus.ranks`) — лише в блоці лаби пришпилені першими.
    dock.focus_rank = dict(focus_rank or {})
    dock.day, dock.today = shown, today
    dock.yesterday, dock.tomorrow = prev_tab_day(today), next_tab_day(today)
    # Стрілки ходять по днях, де є живі роботи, у вікні черги (старіше —
    # Архів), як смуга «Дні» черги. Сьогоднішня вкладка — завжди серед них:
    # це дефолт доку, і дорога назад потрібна навіть у день без жодної роботи.
    floor = business_today() - timedelta(days=RETENTION_DAYS)
    known = sorted({d for d in by_day if d >= floor} | {today})
    dock.day_counts = by_day
    dock.prev_day = next((d for d in reversed(known) if d < shown), None)
    dock.next_day = next((d for d in known if d > shown), None)
    dock.mail_count = len(mail)
    dock.all_count = len(day_rows)
    lab_ready = filter_by_readiness(lab, ready)
    dock.lab_count = len(lab_ready)
    dock.lab_pinned = sum(1 for o in lab_ready if o.id in dock.focus_rank)

    if scope == "":
        dock.orders = mail
        dock.ready_counts = count_by_readiness(lab)
        return dock

    if scope == "labonly":
        # Лише лаба, у порядку таблиці, під фільтр готовності — без пошти
        # (власник 08.10.26: 50 листів дня ховали 7 лабораторних).
        lab_ready.sort(key=_sheet_order_key)
        dock.orders = _pinned_first(lab_ready, dock.focus_rank)
        dock.lab_start = 0 if lab_ready else None
        dock.ready_counts = count_by_readiness(lab)
        return dock

    if scope == "lab":
        lab_ready.sort(key=_sheet_order_key)
        dock.orders = mail + _pinned_first(lab_ready, dock.focus_rank)
        dock.sep_index = len(mail) if mail and lab_ready else None
        dock.lab_start = len(mail) if lab_ready else None
        dock.ready_counts = count_by_readiness(lab)
        return dock

    rows = mail + filter_by_readiness(others, ready)
    rows.sort(key=_sheet_order_key)
    dock.orders = rows
    dock.ready_counts = count_by_readiness(others)
    return dock


def mail_dock_for(db: Session, user: User | None, day: date | None = None) -> MailDock:
    """Док під збережені налаштування оператора (User.mail_dock_*) на вкладку
    `day` (None — сьогодні). День на акаунті не зберігається — див. докстрінг
    модуля. Набір «мої зараз» — цього оператора (`focus.ranks`, один запит);
    роут бере `focused_ids` з `dock.focus_rank`, а не другим запитом."""
    scope = (getattr(user, "mail_dock_scope", "") or "") if user is not None else ""
    height = (getattr(user, "mail_dock_height", 0) or 0) if user is not None else 0
    return mail_dock(
        db, scope=scope, ready=mail_dock_ready_of(user), height=height, day=day,
        focus_rank=focus_ranks(db, user),
    )
