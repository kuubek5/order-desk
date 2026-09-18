"""Видача: хто потрапляє на екран, які теки в `export` йому відповідають.

Тут немає ні Request, ні Response — лише правила. Ті самі помічники ділять
екран видачі й фоновий прогрів кешу (`export_warm_once` у web.py): прогрів
МУСИТЬ рахувати корінь, межу за датою й теки клієнтів точно так само, як
екран, — інакше він наповнить кеш під іншим ключем і оператор однаково
чекатиме.
"""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, selectinload

from app.business_day import (
    business_date_of,
    business_today,
    get_rollover,
    utc_to_business,
)
from app.client_matcher import (
    match_client_name,
    match_client_name_cached,
    matcher_cache_key,
)
from app.export_scanner import scan_export_client_cached, scan_export_client_latest_cached
from app.material_match import materials_match
from app.services.clients import quantity_units
from app.services.folder_merge import folder_sibling_map
from app.models import ClientNameAlias, Order, StatusEvent
from app.services.order_dates import parse_sheet_tab
from app.sheet_writer import apply_status_markers
from app.statuses import STATUS_FOUND, STATUS_ISSUED

EXPORT_SCAN_WORKERS = 16
"""Виміряно на бойовому сховищі 27.08.26 (746 тек клієнтів, Synology/SMB):
послідовний обхід 33 мс/запис, у 16 потоків — 8.5 мс/запис, тобто 3.9x.
Ціну диктує затримка кожної ходки, а не процесор, тому потоки й дають
виграш. Більше 16 не ставимо: далі впираємось у сам SMB, а не в очікування."""

HANDOUT_ALL_DAYS = "all"
"""Значення параметра `day`, що просить показати ВСІ дні одразу."""


def day_fingerprint(orders) -> str:
    """Короткий відбиток стану дня видачі: що змінилось — видно, що ні — ні.

    Потрібен пульсу екрана (`/handout/pulse`): перемальовувати картки щопʼятнадцять
    секунд не можна (обхід export по SMB ~2.7 с), тому спершу питаємо дешеве
    «а чи взагалі щось змінилось».

    Беремо статус І частковий лічильник: «знайдено 3 з 5» міняє саме
    `found_units`, а статус лишається тим самим — на екрані другого оператора
    такий крок інакше не зʼявлявся б зовсім.

    Хеш, а не сам рядок: відбиток їде В ЗАПИТІ браузера (див. роут), а сотня
    робіт дала б кілометровий query string.
    """
    raw = ";".join(
        f"{o.id}:{o.status}:{o.found_units if o.found_units is not None else ''}"
        for o in sorted(orders, key=lambda x: x.id)
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def entries_for_material(
    material_color: str | None,
    entries: list,
    work_day=None,
    night_claims=(),
    order_id: int | None = None,
) -> list:
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
    ні наряду, ні Sum3D ID), але дата партії є.

    Беремо партію ТОГО САМОГО робочого дня, а якщо її немає (файли дозалили
    пізніше) — найранішу пізнішу. СТАРІШІ партії не показуємо зовсім
    (рішення власника 11.09.26). Рядок у таблиці з'являється того дня, коли
    прийняли лист чи технік здав роботу, — тоді ж створюється і партія; тож
    партія, старша за день роботи, майже завжди ЧУЖА, попередня робота того
    самого кольору. Бойовий випадок Oleksandr 10.09.26: під `mono a3` за 10.09
    висіла тека за 09.09, і її відкривали як відповідь. Хибна тека гірша за
    жодну — рядок натомість каже «теки за день немає» (`stale_folder_day`).

    Дні — РОБОЧІ (межа 07:30), з обох боків: тека, створена о 01:00, належить
    нічній зміні попереднього дня, як і вкладка, у яку тоді пишуть рядок.
    Кілька тек лишається тільки тоді, коли вони справді з одного дня; вибір
    між ними за оператором, як і був.

    `night_claims` — нічні прорахунки робіт ЦЬОГО Ж клієнта (`night_claims_of`),
    `order_id` — ця робота. Нічна тека дістається роботі з найближчим до неї
    Sum3D — див. `_entry_belongs`."""
    matched = _material_matches(material_color, entries)
    if work_day is None or not matched:
        return matched

    # Вкладка п'ятниці охоплює й вихідні: у суботу й неділю цех працює, а
    # роботи (і прийняті листи) пишуть у п'ятничну вкладку (власник 11.09.26).
    covered = covered_days(work_day)
    in_tab = [
        e for e in matched if _entry_belongs(e, set(covered), night_claims, order_id)
    ]
    if in_tab:
        return in_tab
    later = sorted({d for d in (_batch_day(e) for e in matched) if d > covered[-1]})
    if not later:
        return []
    return [e for e in matched if _batch_day(e) == later[0]]


def covered_days(work_day) -> list:
    """Робочі дні, що належать вкладці `work_day`: сам день і дні БЕЗ власної
    вкладки поруч із ним — вихідні одразу за ним і одразу перед ним.

    Вкладок за суботу й неділю в таблиці немає, а цех у вихідні працює. Тому
    п'ятниця = пт + сб + нд (власник 11.09.26) — і рівно з тієї ж причини
    понеділок = сб + нд + пн: файли, скачані у вихідні, чекають на вкладку, і
    вона може виявитись як п'ятничною, так і понеділковою — залежно від того,
    коли рядок насправді з'явився.

    Бойовий випадок 14.09.26 (Oleksandr, дві роботи `mono a3.5` і `mono a4`):
    тека `Новая папка (285)` і обидві теки матеріалів створені в НЕДІЛЮ
    13.09 о 16:29, робота стоїть у вкладці понеділка 14.09, а Sum3D ID
    `16-29-49` — через 19 секунд після теки, тобто тека безумовно їхня.
    Правило «старіші партії не показуємо» рахувало неділю старшою за
    понеділок, і обидва рядки казали «за 14.09 теки немає» — прев'ю не було
    що відкривати.

    Назад беремо РІВНО ОДИН день, і лише якщо в нього немає власної вкладки:
    для понеділка це неділя — вечір перед ним. Звичайне «вчора» (пн для вт)
    сюди не потрапляє: у нього є свої рядки, і тека того дня — майже завжди
    чужа попередня робота того самого кольору (рішення 11.09.26).

    Субота для понеділка теж НЕ береться (власник 15.09.26, Кривовид: під
    роботами 14.09 повисли теки за 12.09 — «це давно було»). Субота вже
    покрита вперед пʼятничною вкладкою, а для понеділка вона на два дні
    позаду: у постійного клієнта того самого кольору це майже напевно
    попередня робота, тобто рівно та хибна тека, від якої правило 11.09.26 і
    захищає. Один день назад ловить реальний випадок (недільний вечір), два —
    вже повертають шум."""
    earlier = work_day - timedelta(days=1)
    days = [earlier] if earlier.weekday() >= 5 else []
    days.append(work_day)
    following = work_day + timedelta(days=1)
    while following.weekday() >= 5:
        days.append(following)
        following += timedelta(days=1)
    return days


def stale_folder_day(
    material_color: str | None,
    entries: list,
    work_day=None,
    night_claims=(),
    order_id: int | None = None,
):
    """Робочий день найсвіжішої партії з тим самим матеріалом, якщо ВСІ такі
    партії старші за день роботи (і тому не показуються). None — інакше.

    Для рядка «теки за 10.09 немає»: оператор має бачити, що тека не
    загубилась у програми, а її справді немає за цей день, — і відкрити теку
    клієнта, щоб знайти вручну.

    Питання «чи є своя партія» тут НЕ розвʼязується вдруге: відповідь дає
    `entries_for_material`. Доти правило стояло в двох місцях, і кожне
    уточнення відбору треба було памʼятати переписати в обох — рівно та пара,
    що мовчки розходиться (позначка «немає» під рядком, який теку показує).
    """
    if work_day is None:
        return None
    matched = _material_matches(material_color, entries)
    if not matched:
        return None
    if entries_for_material(material_color, entries, work_day, night_claims, order_id):
        return None
    # Лишились самі старіші (пізніші `entries_for_material` вже віддав би).
    return max(_batch_day(e) for e in matched)


def _material_matches(material_color: str | None, entries: list) -> list:
    if not material_color or not material_color.strip():
        return []
    matched = [
        e for e in entries
        if materials_match(material_color, e.material_color_folder_name)
    ]
    matched.sort(key=lambda e: e.created_at)
    return matched


def _batch_day(entry):
    """Робоча дата партії (межа доби 07:30), як у вкладок таблиці."""
    return business_date_of(entry.created_at)


def _entry_belongs(entry, covered: set, night_claims=(), order_id=None) -> bool:
    """Чи партія належить роботі з днями `covered`.

    День партії — РОБОЧИЙ (межа 07:30) або КАЛЕНДАРНИЙ. Навіщо календарний:
    бойовий випадок 17.09.26 (Кривовид `mono a3.5`, Тертычный `mono a4`) —
    нічний оператор узяв ЗАВТРАШНЮ роботу: рядки у вкладці 17.09 о 01:55 і
    02:07, теки о 04:48–04:55 сімнадцятого, коли робоча доба ще 16.09. Без
    календарної дати рядок казав «за 17.09 теки немає», хоча тека лежала в
    `Новая папка (597)`, секунда в секунду з Sum3D 04-55-31.

    Але та сама ніч — ще й робоча доба ПОПЕРЕДНЬОЇ вкладки, тож нічна тека
    підходить двом дням одразу. Бойовий випадок 18.09.26 (Маріанна Голій,
    `emo a3`): о 00:10 сімнадцятого нічна зміна прорахувала роботу вкладки
    16.09 (Sum3D `00-10-39`), тека о 00:12 — і робота вкладки 17.09 того самого
    кольору (`14-41-08`) через календарну дату теж отримала цю теку: видача
    показувала «00:12», чужу, вже видану коронку.

    Тому нічну теку ділимо за часом: вона дістається роботі клієнта того самого
    кольору, чий нічний прорахунок (`night_claims_of`) найближчий до неї, —
    хоч учорашній, хоч завтрашній. Тека скачується просто перед прорахунком
    (00:12 ↔ 00-10-39, 04:55 ↔ 04-55-31). Тієї ночі ніхто цей колір не
    прораховував — правило як було: тека підходить обом дням.
    """
    if _batch_day(entry) not in covered and entry.created_at.date() not in covered:
        return False
    if order_id is None or entry.created_at.time() >= get_rollover():
        return True
    rivals = [
        claim for claim in night_claims
        if claim.when.date() == entry.created_at.date()
        and materials_match(claim.material, entry.material_color_folder_name)
    ]
    if not rivals:
        return True
    nearest = min(abs(claim.when - entry.created_at) for claim in rivals)
    return any(
        claim.order_id == order_id and abs(claim.when - entry.created_at) == nearest
        for claim in rivals
    )


_SUM3D_TIME = re.compile(r"(\d{1,2})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class NightClaim:
    """Нічний прорахунок роботи: коли саме (дата ночі + час) і якого кольору."""

    order_id: int
    when: datetime
    material: str


def night_claims_of(orders) -> list[NightClaim]:
    """Нічні прорахунки робіт — за ними `_entry_belongs` ділить нічні теки.

    Sum3D ID — лише час доби, тож дату ночі дає момент, коли рядок зʼявився:
    це ніч ПІСЛЯ його робочого дня. Так виходить однаково для обох бойових
    випадків: рядок Голій зʼявився 16.09 удень (ніч — 17.09, 00:10), рядок
    Кривовида — о 01:55 сімнадцятого, коли робоча доба ще 16.09 (ніч — теж
    17.09, 04:55).

    Нічний — Sum3D до межі доби. Якщо Sum3D денний або його ще немає, а рядок
    зʼявився вже після півночі, мірилом береться сама поява рядка: тека
    скачується перед тим, як рядок пишуть. У клітинці Sum3D буває кілька ID
    через перенос рядка — кожен нічний рахується.

    `Order.created_at` — UTC (server_default на SQLite), тому через
    `utc_to_business`."""
    rollover = get_rollover()
    claims: list[NightClaim] = []
    for order in orders:
        if order.created_at is None or not (order.material_color or "").strip():
            continue
        appeared = utc_to_business(order.created_at)
        night = business_date_of(appeared) + timedelta(days=1)
        moments = []
        for h, m, s in _SUM3D_TIME.findall(order.sum3d_id or ""):
            try:
                taken = time(int(h), int(m), int(s))
            except ValueError:
                continue
            if taken < rollover:
                moments.append(datetime.combine(night, taken))
        if not moments and appeared.time() < rollover:
            moments.append(appeared)
        claims.extend(NightClaim(order.id, when, order.material_color) for when in moments)
    return claims


# Ключ групи для робіт БЕЗ імені клієнта.
#
# Навіщо сигнальний рядок, а не порожній/None. Група на цьому екрані
# ідентифікується саме іменем: воно їде у форму, повертається в
# `mark_group_found`/`issue_group` і там перескладає групу запитом. `NULL` у
# SQL не дорівнює нічому, включно з собою, тож `client_name == None` не знайшов
# би жодного рядка — кнопки мовчки нічого не робили б. Тому ключ явний, а
# запити його перекладають назад у `IS NULL` (див. `_group_criterion`).
#
# Самі роботи без імені сюди потрапили свідомо (рішення власника 09.09.26):
# доти вони випадали з видачі зовсім — були в черзі й у виробітку, а на екрані,
# де їх фізично шукають у лотку, не показувались. Ім'я їм не вигадуємо: група
# так і зветься «Без імені», і це підказка оператору дописати клієнта в
# таблицю, а не привід сховати роботу.
NAMELESS_CLIENT_KEY = "__без-імені__"


def handout_group_key(order: Order) -> str:
    """Під яким ключем робота лягає в групу видачі."""
    return (order.client_name or "").strip() or NAMELESS_CLIENT_KEY


def _group_criterion(client_name: str):
    """Умова «роботи цієї групи» — з перекладом сигнального ключа в IS NULL."""
    if client_name == NAMELESS_CLIENT_KEY:
        return or_(Order.client_name.is_(None), Order.client_name == "")
    return Order.client_name == client_name


def handout_eligible_orders(db: Session, today: date) -> list[Order]:
    """Невидані клієнтські роботи — те, що показує видача.

    СЬОГОДНІШНІЙ ДЕНЬ ТЕЖ ТУТ. Довго було `< today`, за логікою «цирконій
    закладають увечері, видають наступного ранку». Але не все йде через піч:
    ПММА, титан і воски готові одразу після фрезерування, і їх видають того
    самого дня — а видача мовчки їх ховала (скарга власника 31.08.26).
    Сьогоднішній день не стає замовчуванням екрана (див. handout_select_day),
    тому ранковий сценарій не змінився: відкрилась видача — там учора.

    selectinload(Order.material) обов'язковий: `_matpair.html` викликає
    material_badge(order) на КОЖНІЙ роботі, а без фільтра дня їх ~1800.
    Виміряно (по 3 прогріті запити): без eager 3.8с, з ним 2.7с."""
    candidates = db.scalars(
        select(Order)
        .options(selectinload(Order.material))
        .where(
            # Клієнтські роботи — і ті, у яких оператор ще не вписав ім'я:
            # доти вони випадали з видачі зовсім (рішення власника 09.09.26).
            # Лабораторні (`source == "lab"`) сюди не входять і не входили:
            # їх не видають клієнтам, вони йдуть назад у лабораторію.
            or_(Order.client_name.is_not(None), Order.source == "sheet_client"),
            # «видано» БІЛЬШЕ НЕ ЗНИМАЄ клієнта зі списку (рішення власника
            # 05.09.26). Раніше тут стояло `Order.status != "видано"`, і через
            # це збій 01.09.26 лишався невидимим: заливка з'їхала разом із
            # рядками, двоє клієнтів стали «видано», і вони просто зникли з
            # екрана — дізнатись можна було хіба від клієнта.
            # Тепер виданий клієнт лишається на місці, згорнутим і з галочкою,
            # тож помилку видно й її можна зняти одним кліком. Перелік довший,
            # але правило «нічого не ховаємо» тут важливіше за короткий список.
            # Архів росте з кожним місяцем, а видати його однаково не можна:
            # без цієї умови кожне відкриття видачі піднімало в памʼять усю
            # історію. Умова та сама, що в черзі (`archived_at IS NULL`), і
            # колонка проіндексована.
            Order.archived_at.is_(None),
        )
    ).all()
    return [
        order
        for order in candidates
        if (order_day := parse_sheet_tab(order.sheet_tab)) is None or order_day <= today
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
    if not days:
        return None
    # Замовчування — найновіший МИНУЛИЙ день, а не сьогоднішній: зранку
    # видають те, що вчора відфрезерували й уночі спекли. Сьогоднішній день
    # доступний чіпом (його роботи без спікання видають того ж дня), але
    # відкривати екран одразу на ньому означало б показувати лоток, якого
    # ще немає.
    today = business_today()
    past = [d for d in days if d < today]
    return past[-1] if past else days[-1]


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
    # Групі без імені зіставляти нема чого: теку в `export` шукають ЗА ІМЕНЕМ
    # клієнта. Порожній результат чесніший за здогад — інакше нечіткий
    # матчер підібрав би їй першу-ліпшу схожу теку.
    #
    # Через кеш, а не напряму: на бойових замірах 09.09.26 саме це зіставлення
    # було найдорожчим на видачі (`match:clients` 0.46-1.08 с у кожному
    # запиті), і платили за нього щоразу заново — кожна галочка «знайдено»
    # перебудовує екран і перезіставляє тих самих клієнтів із тими самими
    # теками. Відбиток входу рахуємо ОДИН раз на виклик, а не на клієнта.
    key = matcher_cache_key(folder_names, aliases)
    siblings = folder_sibling_map(db)
    return {
        name: (
            match_client_name("", [], {})
            if name == NAMELESS_CLIENT_KEY
            else _settle_by_merge(
                match_client_name_cached(name, folder_names, aliases, key), siblings
            )
        )
        for name in client_names
    }


# Ті самі пороги, що за замовчуванням у `match_client_name`.
_AUTO_MATCH_THRESHOLD = 90.0
_AMBIGUOUS_MARGIN = 5.0


def _settle_by_merge(match, siblings: dict[str, list[str]]):
    """Неоднозначність між теками, які власник уже злив, — не неоднозначність.

    Бойовий випадок 18.09.26 (Yatsenko): у `export` дві теки однаково схожі на
    імʼя з таблиці, матчер чесно не обирає жодної — і рядки видачі стоять
    зовсім без тек, навіть без «теки немає». Власник злив ці теки на екрані
    дублікатів, але злиття діяло лише ПІСЛЯ вибору основної теки
    (`folder_sibling_map` у роуті дочитує сестер обраної), тож рівно там, де
    воно й потрібне, не діяло ніколи.

    Якщо ВСІ рівні кандидати — одна група злитих тек, беремо першого: сестер
    роут однаково дочитає, і набір партій той самий, хоч з якої почни. Кеш
    матчера роздає спільні обʼєкти — тому копія, а не правка на місці."""
    if match.matched_folder_name or not siblings or not match.candidates:
        return match
    best_name, best_score = match.candidates[0]
    if best_score < _AUTO_MATCH_THRESHOLD:
        return match
    tied = [n for n, s in match.candidates if best_score - s < _AMBIGUOUS_MARGIN]
    family = {best_name.strip(), *siblings.get(best_name.strip(), [])}
    if len(tied) < 2 or not all(n.strip() in family for n in tied):
        return match
    return replace(match, matched_folder_name=best_name)


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
# Чим скінчилась спроба видати групу. Три різні кінцівки, і роут поводиться з
# кожною по-своєму, тож повертаємо не булеве «вийшло / не вийшло».
ISSUE_GROUP_EMPTY = "empty"
"""У цього клієнта в показаному дні взагалі немає що видавати."""

ISSUE_GROUP_NOTHING_FOUND = "nothing_found"
"""Роботи є, але жодну не позначено «знайдено» — оператору треба сказати чому."""

ISSUE_GROUP_ISSUED = "issued"
"""Статуси проставлено й закомічено; лишається запис у таблицю."""


#: Наскільки схожими мають бути два написання, щоб запропонувати звʼязок.
#: Той самий поріг, що й у зіставленні з теками (`client_matcher`) — це не
#: «схожі люди», а «та сама людина, записана інакше».
SIMILAR_NAME_THRESHOLD = 90.0

#: Найкоротше спільне слово, яке ще щось доводить. Без цієї умови «R» і
#: «Роман Островский» злипались би на самому входженні рядка, а на екрані
#: видачі хибна підказка «це той самий» дорожча за відсутню: за нею йде чужа
#: коронка в чужому пакеті.
SIMILAR_MIN_TOKEN = 4


def _name_tokens(name: str) -> set:
    return {t for t in name.lower().replace(".", " ").replace(",", " ").split() if t}


def similar_group_names(names) -> dict:
    """Пари карток видачі, які, найпевніше, той САМИЙ клієнт.

    Оператор записує клієнта як доведеться: «Ковальчук» і «Яна Ковальчук»,
    «Ритченка» і «Стоматологія Ритченка» — у таблиці це різні рядки, а на
    видачі різні картки, тож роботи однієї людини лежать у двох місцях екрана
    й частина губиться з очей (бойовий випадок 15.09.26, Ковальчук).

    Картки свідомо НЕ зливаються (рішення власника 15.09.26): «Ковальчук» може
    бути й іншою людиною, а зліплені автоматично картки означали б чужу
    коронку в чужому пакеті. Тут лише ЗВʼЯЗОК — кожна картка каже, що поруч є
    схоже імʼя, і веде до нього.

    Умов дві, і потрібні обидві: висока схожість за словами (порядок слів не
    важить — «Яна Ковальчук» проти «Ковальчук») І спільне слово завдовжки хоча
    б `SIMILAR_MIN_TOKEN`. Друга умова відсікає однобуквені й куці імена, які
    «входять» у будь-що."""
    from rapidfuzz import fuzz

    real = [n for n in names if n and n != NAMELESS_CLIENT_KEY]
    tokens = {n: _name_tokens(n) for n in real}
    links: dict = {n: [] for n in real}
    for i, first in enumerate(real):
        for second in real[i + 1:]:
            shared = {
                t for t in tokens[first] & tokens[second] if len(t) >= SIMILAR_MIN_TOKEN
            }
            if not shared:
                continue
            if fuzz.token_set_ratio(first.lower(), second.lower()) < SIMILAR_NAME_THRESHOLD:
                continue
            links[first].append(second)
            links[second].append(first)
    return {name: found for name, found in links.items() if found}


@dataclass
class UnitsResult:
    """Чим скінчився клік по лічильнику одиниць."""

    found: int
    total: int
    #: Знайдено ВСЕ — роботу щойно закрито, заливку в таблиці треба зняти.
    completed: bool = False
    #: Була закрита, стала частковою — заливку треба повернути.
    reopened: bool = False


def work_units(order: Order) -> int:
    """Скільки одиниць у роботі за коміркою кількості (вільний текст)."""
    return quantity_units(order.quantity)


def found_units(order: Order) -> int:
    """Скільки одиниць уже знайдено, з поправкою на статус.

    Поле порожнє у двох РІЗНИХ станах — «ще нічого не знайшли» і «знайшли все
    ще до появи лічильника». Розрізняє їх статус: він лишається джерелом
    правди про повноту, а поле — лише про проміжок."""
    if order.status in (STATUS_FOUND, STATUS_ISSUED):
        return work_units(order)
    value = order.found_units or 0
    return max(0, min(value, work_units(order)))


def is_partly_found(order: Order) -> bool:
    """Знайдено частину одиниць — рядок показує лічильник, а не просто галочку."""
    total = work_units(order)
    return total > 1 and 0 < found_units(order) < total


def bump_found_units(db: Session, user, order: Order, delta: int) -> "UnitsResult | None":
    """Посунути лічильник «знайдено N з M» на одну одиницю.

    Навіщо взагалі. Рядок таблиці несе кілька коронок, а пічки розкладають їх
    по різних закладках, тож «двох знайшов, третьої ще немає» — норма ранкової
    видачі (§2). Досі це не мало де записатись: галочка все-або-нічого, і
    залишок жив у голові оператора.

    Три правила живуть тут, а не в роуті:

    * дійшли до M — робота стає «знайдено при видачі» рівно так, як від
      галочки, і кличний код знімає синю заливку;
    * впали нижче M із закритої — статус вертається (як в `unmark_found`), а
      заливку кличний код малює назад: у таблиці не має лишитись знята
      заливка під ненайденою коронкою;
    * робота «видано» не рухається зовсім — її повертає `unissue`.

    `None` — рахувати нічого (одна одиниця чи порожня кількість) або робота
    вже видана; кличний код тоді не пише в таблицю."""
    total = work_units(order)
    if total <= 1 or order.status == STATUS_ISSUED:
        return None

    was_complete = order.status == STATUS_FOUND

    # Лічильник рухається ОДНИМ атомарним UPDATE, а не читанням-зміною-записом.
    # Двоє операторів шукають коронки в одному лотку й тиснуть свій «+1» в одну
    # мить; на читанні-записі обидва бачили нуль і обидва писали одиницю —
    # знайдена коронка зникала, рядок ніколи не набирав повноти, і синя заливка
    # в таблиці не знімалась, тобто для всієї лабораторії робота лишалась
    # невиданою (відтворено двома сеансами 15.09.26: два «+1» дали found=1).
    # Той самий прийом, що з `rule.hits` — рахує база, не Python.
    #
    # База рахунку СТАТУС-ЗАЛЕЖНА, як і `found_units()`: у завершеної роботи
    # колонка буває порожня (її закрили галочкою до появи лічильника), і голий
    # `coalesce(found_units, 0)` зламав би повернення з повноти — «мінус один»
    # від total пішов би від нуля.
    base = case((Order.status == STATUS_FOUND, total), else_=func.coalesce(Order.found_units, 0))
    moved = func.max(0, func.min(total, base + delta))
    # Межу перевіряє САМ запит: інакше «нічого не змінилось» довелося б
    # визначати за прочитаним раніше числом, тобто знову за застарілим.
    guard = base < total if delta > 0 else base > 0
    row = db.execute(
        update(Order)
        .where(Order.id == order.id, guard)
        .values(found_units=func.nullif(moved, 0))
        .returning(Order.found_units)
        .execution_options(synchronize_session=False)
    ).first()
    # Стан у сесії застарів після прямого UPDATE — інакше наступний flush
    # повернув би на місце старе число.
    db.expire(order, ["found_units", "status"])
    if row is None:
        return UnitsResult(found=found_units(order), total=total)
    new_value = row[0] or 0

    result = UnitsResult(found=new_value, total=total)
    if new_value >= total:
        if not was_complete:
            # Перехід виборює ОДИН запит: без цієї умови двоє операторів
            # дописали б у хронологію дві однакові події «знайдено при видачі».
            # `Session.execute` оголошений як загальний `Result` без `rowcount`;
            # UPDATE завжди віддає `CursorResult` (як у journal_prune).
            if cast("CursorResult[Any]", db.execute(
                update(Order)
                .where(Order.id == order.id, Order.status != STATUS_FOUND)
                .values(status=STATUS_FOUND)
                .execution_options(synchronize_session=False)
            )).rowcount:
                db.expire(order, ["status"])
                db.add(
                    StatusEvent(
                        order_id=order.id,
                        operator_id=user.id,
                        status=STATUS_FOUND,
                        actor=user.username,
                    )
                )
                result.completed = True
    else:
        if was_complete:
            # Той самий поворот, що в `unmark_found`: ПОПЕРЕДНІЙ статус, а не
            # «нове» — робота на видачі за визначенням уже відфрезерована.
            previous = db.scalars(
                select(StatusEvent)
                .where(
                    StatusEvent.order_id == order.id,
                    StatusEvent.status != STATUS_FOUND,
                )
                .order_by(StatusEvent.id.desc())
            ).first()
            back_to = previous.status if previous else "відфрезеровано"
            # Так само один виборений перехід: двоє, що зняли по одиниці з
            # повної роботи, інакше вернули б статус двічі й двічі це записали.
            if cast("CursorResult[Any]", db.execute(
                update(Order)
                .where(Order.id == order.id, Order.status == STATUS_FOUND)
                .values(status=back_to)
                .execution_options(synchronize_session=False)
            )).rowcount:
                db.expire(order, ["status"])
                db.add(
                    StatusEvent(
                        order_id=order.id,
                        operator_id=user.id,
                        status=back_to,
                        actor=user.username,
                    )
                )
                result.reopened = True
    db.commit()
    return result


@dataclass(frozen=True)
class IssueGroupResult:
    """Що зробила видача групи і що лишилось дописати в таблицю.

    `field_map` — {id роботи: імена полів для запису}. Він іде В ОДНЕ завдання
    воркера write-back (`issue_group_warm`), а не по роботі за раз: цикл
    походів у Google прямо з обробника заморожував застосунок на дві хвилини
    на «Видати 8 з 8» (аудит 05.09.26, синк C-2).
    """

    outcome: str
    field_map: dict[int, list[str]]


MARK_GROUP_EMPTY = "empty"
MARK_GROUP_DONE = "done"


@dataclass
class MarkGroupResult:
    """Скільки робіт клієнта щойно стали «знайдено» і які саме."""

    outcome: str
    order_ids: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.order_ids)


def mark_group_found(db: Session, user, client_name: str, day: str) -> MarkGroupResult:
    """Позначити знайденими ВСІ ще не знайдені роботи цього клієнта.

    Навіщо: у клієнта буває 12-50 робіт, і коли оператор дійсно знайшов увесь
    лоток, він ставить 12-50 галочок поспіль. Одна кнопка на картку прибирає цю
    рутину (аудит 05.09.26, крок 3.5).

    Чого це НЕ робить — і це головне: «видано» лишається окремою дією. Правило
    видачі №2 («один клік на дію») означає саме розділення двох рішень —
    «я тримаю коронку в руках» і «я віддав її логісту». Часткова видача — норма
    (§2), тож зливати обидва кроки в одну кнопку не можна: клієнт із трьох
    пічок отримав би «видано» на роботи, які ще в печі.

    Групу перескладаємо на сервері з `client_name`, як і `issue_group`, і
    фільтруємо тим самим днем — списку id з форми не віримо.
    """
    today = business_today()
    candidates = db.scalars(
        select(Order).where(_group_criterion(client_name), Order.status != STATUS_ISSUED)
    ).all()
    group_orders = [
        o for o in candidates
        # `<= today`, як у handout_eligible_orders: ПММА/титан/віск готові в день
        # фрезерування і вже стоять на екрані; `< today` мовчки викидав їх із
        # «Усі знайдено» та «Видати N з M» (ревʼю 07.09.26).
        if (d := parse_sheet_tab(o.sheet_tab)) is not None and d <= today
    ]
    selected_day = parse_sheet_tab(day) if day else None
    if selected_day is not None:
        group_orders = [
            o for o in group_orders if parse_sheet_tab(o.sheet_tab) == selected_day
        ]

    # Уже знайдені пропускаємо: кнопка добирає решту, а не переставляє статус
    # заново (інакше в історії з'явився б другий StatusEvent про те саме).
    pending = [o for o in group_orders if o.status != STATUS_FOUND]
    if not pending:
        return MarkGroupResult(MARK_GROUP_EMPTY)

    for order in pending:
        order.status = STATUS_FOUND
        db.add(StatusEvent(
            order_id=order.id, operator_id=user.id,
            status=order.status, actor=user.username,
        ))
    db.commit()
    return MarkGroupResult(MARK_GROUP_DONE, [o.id for o in pending])


def issue_group(db: Session, user, client_name: str, day: str) -> IssueGroupResult:
    """Видати все ЗНАЙДЕНЕ в цього клієнта: статуси в БД + що писати в таблицю.

    Групу перескладаємо на сервері з `client_name` — списку id з форми не
    віримо ніколи.

    Часткова видача — норма процесу, не виняток (CLAUDE.md §2): цирконій іде
    через три пічки, які відкриваються ~9:00, в обід і під вечір, тож роботи
    одного клієнта фізично виходять у різний час. Тому видаємо рівно ті
    роботи, що вже позначені «знайдено при видачі»/«видано», а решта лишається
    в картці.

    Комітимо ТУТ, до запису в таблицю: БД — джерело правди, і збій проксі не
    має відкочувати рішення оператора. Сам запис у таблицю робить роут (це
    await на HTTP-рівні) з `field_map`, який ми повернули.
    """
    today = business_today()
    candidates = db.scalars(
        select(Order).where(_group_criterion(client_name), Order.status != "видано")
    ).all()
    group_orders = [
        o for o in candidates
        # `<= today`, як у handout_eligible_orders: ПММА/титан/віск готові в день
        # фрезерування і вже стоять на екрані; `< today` мовчки викидав їх із
        # «Усі знайдено» та «Видати N з M» (ревʼю 07.09.26).
        if (d := parse_sheet_tab(o.sheet_tab)) is not None and d <= today
    ]
    # When the handout screen is filtered to one day (day chips), the card
    # the operator sees — and therefore what "Видати" closes — is that day's
    # works only; the client's other days stay open.
    selected_day = parse_sheet_tab(day) if day else None
    if selected_day is not None:
        group_orders = [
            o for o in group_orders if parse_sheet_tab(o.sheet_tab) == selected_day
        ]
    if not group_orders:
        return IssueGroupResult(ISSUE_GROUP_EMPTY, {})

    # Видаємо рівно те, що оператор уже знайшов. Решта лишається в картці.
    group_orders = [
        o for o in group_orders if o.status in ("знайдено при видачі", "видано")
    ]
    if not group_orders:
        return IssueGroupResult(ISSUE_GROUP_NOTHING_FOUND, {})

    actor = user.full_name or user.username
    field_map: dict[int, list[str]] = {}
    for order in group_orders:
        order.status = "видано"
        sheet_fields = apply_status_markers(order, "видано", actor=actor)
        db.add(
            StatusEvent(order_id=order.id, operator_id=user.id, status="видано", actor=user.username)
        )
        field_map[order.id] = sorted(sheet_fields)

    db.commit()
    return IssueGroupResult(ISSUE_GROUP_ISSUED, field_map)
