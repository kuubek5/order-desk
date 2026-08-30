"""Видача: хто потрапляє на екран, які теки в `export` йому відповідають.

Тут немає ні Request, ні Response — лише правила. Ті самі помічники ділять
екран видачі й фоновий прогрів кешу (`export_warm_once` у web.py): прогрів
МУСИТЬ рахувати корінь, межу за датою й теки клієнтів точно так само, як
екран, — інакше він наповнить кеш під іншим ключем і оператор однаково
чекатиме.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.client_matcher import match_client_name
from app.export_scanner import scan_export_client_cached, scan_export_client_latest_cached
from app.material_match import materials_match
from app.services.clients import quantity_units
from app.models import ClientNameAlias, Order
from app.services.order_dates import parse_sheet_tab

EXPORT_SCAN_WORKERS = 16
"""Виміряно на бойовому сховищі 27.08.26 (746 тек клієнтів, Synology/SMB):
послідовний обхід 33 мс/запис, у 16 потоків — 8.5 мс/запис, тобто 3.9x.
Ціну диктує затримка кожної ходки, а не процесор, тому потоки й дають
виграш. Більше 16 не ставимо: далі впираємось у сам SMB, а не в очікування."""

HANDOUT_ALL_DAYS = "all"
"""Значення параметра `day`, що просить показати ВСІ дні одразу."""


def entries_for_material(material_color: str | None, entries: list, work_day=None) -> list:
    """The export folders under a client whose material matches this work's
    material_color, oldest-first. Empty when the work has no colour or nothing
    lines up — the row then simply shows no folder shortcut. See get_handout for
    why this is an assist, not an exact per-row bind.

    Правила збігу назв — в `app/material_match.py`. Раніше тут стояла рівність
    відсортованих слів, і бойовий випадок 28.08.26 (Pavlenko) показав, чого
    вона варта: у таблиці `emo a3`, на диску `Emotions A3 опаковий всередині`
    — та сама робота, теку видно поруч, а рядок її не знаходив.

    `work_day` відсікає ЧУЖІ партії. Без нього під рядком висіли всі теки
    клієнта з тим самим матеріалом за все вікно сканування — а постійний
    клієнт замовляє `mono a3.5` мало не щодня, тож під однією роботою
    з'являлось по чотири теки з різних днів (скриншот 28.08.26: «робота одна,
    а папок багато»). Прив'язки «рядок ↔ тека» в шляху немає (CLAUDE.md §4:
    ні наряду, ні Sum3D ID), але дата партії є, і робота не могла лежати в
    партії, скачаній ПІСЛЯ неї. Тож беремо одну партію — найближчу з тих, що
    не пізніші за день роботи, а якщо таких немає (файли дозалили наступного
    дня) — найранішу пізнішу. Кілька тек лишається тільки тоді, коли вони
    справді з одного дня; вибір між ними за оператором, як і був."""
    if not material_color or not material_color.strip():
        return []
    matched = [
        e for e in entries
        if materials_match(material_color, e.material_color_folder_name)
    ]
    matched.sort(key=lambda e: e.created_at)
    if work_day is None or not matched:
        return matched

    batch_days = sorted({e.created_at.date() for e in matched})
    not_after = [d for d in batch_days if d <= work_day]
    chosen = not_after[-1] if not_after else batch_days[0]
    return [e for e in matched if e.created_at.date() == chosen]


def handout_eligible_orders(db: Session, today: date) -> list[Order]:
    """Невидані клієнтські роботи за минулі дні — те, що показує видача.

    selectinload(Order.material) обов'язковий: `_matpair.html` викликає
    material_badge(order) на КОЖНІЙ роботі, а без фільтра дня їх ~1800.
    Виміряно (по 3 прогріті запити): без eager 3.8с, з ним 2.7с."""
    candidates = db.scalars(
        select(Order)
        .options(selectinload(Order.material))
        .where(Order.client_name.is_not(None), Order.status != "видано")
    ).all()
    return [
        order
        for order in candidates
        if (order_day := parse_sheet_tab(order.sheet_tab)) is None or order_day < today
    ]


def handout_day_options(eligible: list[Order]) -> list:
    """Минулі дні, де ще лишились невидані клієнтські роботи, за зростанням."""
    return sorted({d for o in eligible if (d := parse_sheet_tab(o.sheet_tab)) is not None})


def handout_select_day(days: list, day: str):
    """Який день показує екран.

    Порожній параметр = НАЙНОВІШИЙ день, а не всі одразу. Так працює сам
    процес: печі відкриваються вранці, і видають те, що вчора відфрезерували
    (CLAUDE.md §9.4). Показ усіх 30 днів разом давав 262 клієнти на екрані —
    звідси й 2525 тек, які треба обійти на мережевому сховищі перед першим
    рядком HTML. Старіші дні нікуди не зникли: вони за чіпами днів і за
    «усі», а скільки там робіт — написано в шапці.

    Невідомий або порожній день повертає до цього ж замовчування, щоб
    зіпсоване посилання не відкривало найважчий можливий екран."""
    if day == HANDOUT_ALL_DAYS:
        return None
    parsed = parse_sheet_tab(day) if day else None
    if parsed is not None and parsed in days:
        return parsed
    return days[-1] if days else None


def handout_not_before(eligible: list[Order]) -> datetime | None:
    """Межа за датою — головний важіль швидкодії обходу. Бойовий лог 27.08.26:
    «262 клієнтів на екрані, 746 тек, 46148 записів, 511.42с» — тобто ~176
    партій на клієнта, роки накопичених тек. Але файли роботи НЕ МОЖУТЬ
    лежати в партії, створеній до появи самої роботи, а на екрані лише
    роботи за останні 30 днів. Тиждень запасу — на розбіжність годинників
    і дозаливки."""
    oldest = min(
        (d for o in eligible if (d := parse_sheet_tab(o.sheet_tab)) is not None),
        default=None,
    )
    if oldest is None:
        return None
    return datetime.combine(oldest, datetime.min.time()) - timedelta(days=7)


def scan_export_for_clients(
    root: Path, folders_by_client: dict[str, str], not_before: datetime | None
) -> dict[str, list]:
    """Обхід сховища для показаних клієнтів, паралельно — див.
    EXPORT_SCAN_WORKERS. Кеш сканера потокобезпечний."""
    if not folders_by_client:
        return {}
    names = list(folders_by_client)
    with ThreadPoolExecutor(max_workers=min(EXPORT_SCAN_WORKERS, len(names))) as pool:
        results = pool.map(
            lambda folder: scan_export_client_cached(root, folder, not_before),
            (folders_by_client[name] for name in names),
        )
        return dict(zip(names, results))


def scan_export_latest_for_clients(
    root: Path, folders_by_client: dict[str, str]
) -> dict[str, list]:
    """Найновіші партії — для клієнтів, у яких вікно за датою дало порожньо."""
    if not folders_by_client:
        return {}
    names = list(folders_by_client)
    with ThreadPoolExecutor(max_workers=min(EXPORT_SCAN_WORKERS, len(names))) as pool:
        results = pool.map(
            lambda folder: scan_export_client_latest_cached(root, folder),
            (folders_by_client[name] for name in names),
        )
        return dict(zip(names, results))


def handout_client_matches(db: Session, client_names, folder_names: list[str]) -> dict:
    """{ім'я клієнта: результат нечіткого зіставлення з текою в export}.

    Чистий CPU — рахується один раз наперед, а не всередині циклу обходу."""
    aliases = {
        a.sheet_name: a.export_folder_name
        for a in db.scalars(select(ClientNameAlias).where(ClientNameAlias.confirmed.is_(True))).all()
    }
    return {name: match_client_name(name, folder_names, aliases) for name in client_names}


def matched_folders(matches: dict) -> dict[str, str]:
    """Лише ті клієнти, кому зіставлення взагалі знайшло теку."""
    return {
        name: match.matched_folder_name
        for name, match in matches.items()
        if match.matched_folder_name
    }


def handout_day_totals(db: Session, day, groups: list[dict]) -> dict:
    """Скільки клієнтів, робіт і одиниць у дні ВСЬОГО, разом із уже виданими.

    Навіщо: лічильник у шапці рахувався по видимих групах, а вони будуються з
    `handout_eligible_orders`, який відкидає видане. Тобто знаменник
    ЗМЕНШУВАВСЯ протягом дня: щойно клієнта видано, «1 / 34 кл.» ставало
    «0 / 33 кл.» — лічильник їхав назад після успішної роботи, а «X од.»
    виглядало як обсяг дня, хоча означало залишок.

    Чисельник і знаменник МУСЯТЬ рахуватись з однієї множини. Тому:
    - беремо ті самі роботи, що вже показані на екрані (`groups`), і додаємо до
      них видані за цей день — а не читаємо таблицю заново з іншими фільтрами.
      Інакше «3 / 11» могло б не зійтись у «11 / 11» ніколи: групи звужені за
      джерелом і за retention-вікном, а окремий запит про це не знав;
    - клієнта рахуємо за тим самим ключем, за яким зібрані картки
      (`group["client_name"]`), а не за сирим `Order.client_name`;
    - архівне не рахуємо взагалі: воно не з'явиться на екрані ніколи, а
      знаменник із ним лишався б недосяжним.
    """
    if day is None:
        return {}

    shown_orders = [order for group in groups for order in group["orders"]]
    shown_ids = {order.id for order in shown_orders}
    shown_clients = {group["client_name"] for group in groups}

    issued_statuses = ("видано", "знайдено при видачі")
    issued = [
        order
        for order in db.scalars(
            select(Order).where(
                Order.client_name.is_not(None),
                Order.status.in_(issued_statuses),
                Order.archived_at.is_(None),
            )
        )
        if order.id not in shown_ids and parse_sheet_tab(order.sheet_tab) == day
    ]
    if not groups and not issued:
        return {}

    # Клієнт «зроблений», коли на екрані його картка зібрана або його роботи
    # цього дня всі вже видані.
    done_clients = {g["client_name"] for g in groups if g["all_found"]}
    issued_clients = {(o.client_name or "").strip() for o in issued} - shown_clients

    return {
        "clients": len(shown_clients | issued_clients),
        "clients_done": len(done_clients | issued_clients),
        "works": len(shown_orders) + len(issued),
        "works_done": sum(
            1 for o in shown_orders if o.status in issued_statuses
        ) + len(issued),
        "units": sum(quantity_units(o.quantity) for o in shown_orders)
        + sum(quantity_units(o.quantity) for o in issued),
    }
