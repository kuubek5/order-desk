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
ні шпильок «мої зараз», ні сканування мережевих тек — лише кілька полів на
компактний рядок.

Скидається щодня (власник 29.09.26): раніше список тримав 30-денне вікно
retention, як черга, і робота лишалась видимою тут навіть ПІСЛЯ видачі —
Юрія Бойка прийняли з пошти 25.09, видали 28.09, а рядок все одно висів
29.09 серед щойно прийнятих листів, до яких оператор більше не мав стосунку.
Дзеркало — не «черга для email», а «що я оприйняв сьогодні»; вчорашнє
(зроблене чи ні) належить самій черзі, не цьому списку.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.business_day import business_tab_today
from app.models import Order, User
from app.queue_filters import READY_FILTERS, count_by_readiness, filter_by_readiness
from app.services.look_prefs import (
    DOCK_HEIGHT,
    MAIL_DOCK_READY_DEFAULT,
    MAIL_DOCK_SCOPES,
    mail_dock_ready_of,
)
from app.services.order_dates import order_date

#: Підпис доку за джерелом. Канон лишається «Прийняте з пошти» — слово, до
#: якого оператори звикли за два тижні; розширений док — уже «Черга».
DOCK_TITLES = {"": "Прийняте з пошти", "lab": "Черга", "all": "Черга"}

#: Порожній стан — свій під кожне джерело: «приймеш лист — зʼявиться» для
#: лабораторного доку брехало б.
DOCK_EMPTY = {
    "": "Прийнятих з пошти робіт поки немає — приймеш лист, робота зʼявиться тут.",
    "lab": "Ні прийнятих з пошти, ні лабораторних робіт під цей фільтр у вкладці дня немає.",
    "all": "У вкладці дня немає робіт під цей фільтр.",
}


def _day_orders(db: Session) -> list[Order]:
    """Живі роботи вкладки дня (у вихідні — пʼятничної, `business_tab_today`,
    той самий годинник, що в черги й видачі), найновіша згори.

    `archived_at IS NULL` стоїть у SQL (архів росте щомісяця); відсів за
    днем — у Python, бо бізнес-дата виводиться з `sheet_tab`, а не зі стовпця
    (як у build_queue_view). `Order.material` тягнемо одразу — його читає
    material_badge для чипа матеріалу в рядку дзеркала.
    """
    today = business_tab_today()
    stmt = (
        select(Order)
        .options(selectinload(Order.material))
        .where(Order.archived_at.is_(None))
        .order_by(Order.id.desc())
    )
    return [o for o in db.scalars(stmt).all() if order_date(o) == today]


def _sheet_order_key(order: Order) -> tuple[int, int, int]:
    """Порядок таблиці: рядок згори вниз (CLAUDE.md §2 — порядок черги = порядок
    таблиці, і полл його не пересортовує). Робота без рядка (поштова, що ще
    чекає запису, `sheet_row_pending`) іде ПЕРШОЮ, найновіша згори: це щойно
    прийняте, і ховати його в кінець списку означало б «зникло»."""
    if order.row_number is None:
        return (0, 0, -order.id)
    return (1, order.row_number, order.id)


def mail_mirror_orders(
    db: Session, *, scope: str = "", ready: str = MAIL_DOCK_READY_DEFAULT
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
    return mail_dock(db, scope=scope, ready=ready).orders


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

    @property
    def title(self) -> str:
        return DOCK_TITLES[self.scope]

    @property
    def empty_text(self) -> str:
        return DOCK_EMPTY[self.scope]

    @property
    def wire_scope(self) -> str:
        """Слово на дроті: канон "" → "mail" (пастка «порожнє поле»)."""
        return self.scope or "mail"


def mail_dock(
    db: Session, *, scope: str = "", ready: str = MAIL_DOCK_READY_DEFAULT, height: int = 0
) -> MailDock:
    """Повний стан доку: роботи, роздільник, лічильники. Один запит вкладки дня;
    усі числа — з нього ж, без додаткових звернень до бази."""
    # Невідоме чи "mail" (слово канону на дроті) → канон. Невідомий `ready`
    # — дефолт, а не помилка: сюди значення приходять з бази, не з форми.
    if scope not in MAIL_DOCK_SCOPES or scope == "mail":
        scope = ""
    if ready not in READY_FILTERS:
        ready = MAIL_DOCK_READY_DEFAULT

    day = _day_orders(db)
    mail = [o for o in day if o.source == "email"]
    lab = [o for o in day if o.source == "lab"]
    others = [o for o in day if o.source != "email"]

    dock = MailDock(scope=scope, ready=ready, height=DOCK_HEIGHT.clamp_or_zero(height))
    dock.mail_count = len(mail)
    dock.all_count = len(day)
    lab_ready = filter_by_readiness(lab, ready)
    dock.lab_count = len(lab_ready)

    if scope == "":
        dock.orders = mail
        dock.ready_counts = count_by_readiness(lab)
        return dock

    if scope == "lab":
        lab_ready.sort(key=_sheet_order_key)
        dock.orders = mail + lab_ready
        dock.sep_index = len(mail) if mail and lab_ready else None
        dock.ready_counts = count_by_readiness(lab)
        return dock

    rows = mail + filter_by_readiness(others, ready)
    rows.sort(key=_sheet_order_key)
    dock.orders = rows
    dock.ready_counts = count_by_readiness(others)
    return dock


def mail_dock_for(db: Session, user: User | None) -> MailDock:
    """Док під збережені налаштування оператора (User.mail_dock_*)."""
    scope = (getattr(user, "mail_dock_scope", "") or "") if user is not None else ""
    height = (getattr(user, "mail_dock_height", 0) or 0) if user is not None else 0
    return mail_dock(db, scope=scope, ready=mail_dock_ready_of(user), height=height)
