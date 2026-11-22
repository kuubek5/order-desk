"""Розділ «Виробіток» — місячний облік виготовлених одиниць для нарахування ЗП.

Замінює ручну ODS-таблицю. Один екран рахує сам, оператор може виправити будь-що.
Правила підрахунку відкалібровані на серпні 2026 проти реальної зарплатної
відомості (див. project_vyrobitok_screen / project_slm_tally_rules у пам'яті).

ЩО тут вирішено:

* **одиниці** = сума `Order.quantity` за роботами місяця, не архівними
  (`archived_at IS NULL`), без переробок (`mill_count` ≥ 2 — колонка «Який раз
  фрезерується»); джерело `lab` → колонка «Лабораторія», `email`/`sheet_client`
  → «Пошта»;
* **СЛМ рахує синк, не Orders.** СЛМ не потрапляє в базу Orders
  (`_is_non_queue_row` відкидає ці рядки до збереження), тому `slm` ПОЗА
  `ORDER_MATERIALS`. Замість цього синк тим самим проходом, що читає рядки,
  рахує СЛМ з нижнього блоку вкладки й пише його числа прямо в клітинки
  (`store_slm_totals`); табель читає їх як авто. Правку оператора запис синку
  не чіпає. Класифікатор `slm_totals_from_rows` калібровано проти реальних
  вкладок 27/28/31.08.26;
* **підкови / диски / опаки** — ручні завжди: це не «матеріал у базі», а ознака
  роботи (підкови), витрачений ресурс (диски) або окремий облік по людях
  (опаки);
* **знімок авто.** Лаба чистить старі вкладки → синк архівує роботи того дня →
  рахунок наживо дав би 0 за минулий місяць. Тому щоразу, коли день ще «живий»
  (є роботи в базі), авто-число знімається в `VyrobitokCell.auto_value` і потім
  читається звідти, коли роботи зникли.

Гроші (тарифна сітка з відомості; довідник коефіцієнтів власник дасть повний
пізніше — поки Zn = 0,634, а коефіцієнт поза відомою смугою дає попередження, не
здогад):
* коефіцієнт цирконію = (Zr_лаб + Zr_пошта) ÷ диски → ставка Zn;
* підкови (мости цирконію 11+ од.) оплачуються ВДВІЧІ; їхні одиниці ВХОДЯТЬ у
  цирконій, тому їх віднімають, а не додають;
* одиниці діляться порівну між операторами зміни; опаки НЕ діляться (їх на цьому
  екрані показуємо лише кількістю по людях, без грошей).
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import business_now, business_today, next_tab_day
from app.models import Order, VyrobitokCell, VyrobitokDay, VyrobitokMonth
from app.services import vyrobitok_rows
from app.services.order_dates import order_date_of
from app.settings_store import get_setting, set_setting
from app.stats import parse_int_safe

# Дні, які ЗАРАЗ пересинхронізовує оператор («⟳» у табелі). Фоновий
# заморожувач (freeze_due_days, кожні 10 хв) їх пропускає: інакше між
# unfreeze_day і комітом синку (секунди, на холодному кеші — десятки) він
# встигав перезняти день зі СТАРИХ даних, і свідомий ресинк мовчки губився
# (ревʼю 07.09.26). Памʼять процесу: заморожувач і роут живуть в одному.
_RESYNC_DAYS: set[date] = set()


@contextmanager
def resync_guard(day: date):
    """Тримати `day` поза заморожувачем на час ручного ресинку."""
    _RESYNC_DAYS.add(day)
    try:
        yield
    finally:
        _RESYNC_DAYS.discard(day)

# ── Колонки й родини матеріалів ─────────────────────────────────────────────
# Ключ колонки материалу = коротка форма назви з каталогу (Material.name).
MATERIAL_KEY_BY_NAME = {
    "Цирконій": "zr",
    "ПММА": "pmma",
    "Віск": "wax",
    "СЛМ": "slm",
    "Титан": "ti",
}

# Порядок і підпис колонок матеріалів у групах «Лабораторія» та «Пошта».
MATERIAL_COLS: list[tuple[str, str]] = [
    ("zr", "ZrO"),
    ("pmma", "PMMA"),
    ("wax", "Wax"),
    ("slm", "SLM"),
    ("ti", "Ti"),
]

# Кольори-родини (rgb для CSS --h) — ті самі, що фарбують край рядка в черзі.
HUE = {
    "zr": "143,191,224",
    "pmma": "216,180,106",
    "wax": "63,182,201",
    "slm": "154,167,180",
    "ti": "99,200,139",
    "disks": "168,149,120",
    "pidkovy": "143,191,224",
    "op": "255,180,84",
}

# Опаки — фіксований список людей (рішення власника). Індекс = колонка opakN.
OPAK_PEOPLE = ["Денис", "Костя", "Стас", "Вадим", "Рома"]

# Матеріали, які рахуються з Orders (черга). СЛМ поза списком — його рядки в
# базу Orders не потрапляють (`_is_non_queue_row` викидає до збереження). Його
# ЧИСЛА пише синк прямо у клітинки табеля (auto_value) тим самим проходом, що
# читає рядки, — див. slm_totals_from_rows/store_slm_totals. Тому СЛМ теж
# «знімкова» колонка (читається з auto_value), але НЕ рахується тут із Orders.
ORDER_MATERIALS = ("zr", "pmma", "wax", "ti")
ORDER_COLS = {
    f"{src}_{key}" for src in ("lab", "mail") for key in ORDER_MATERIALS
}
# Усі колонки-матеріали читають auto_value (знімок) — з Orders (ORDER_COLS) або
# від синку (СЛМ).
SNAPSHOT_MATERIALS = ("zr", "pmma", "wax", "ti", "slm")
SNAPSHOT_COLS = {
    f"{src}_{key}" for src in ("lab", "mail") for key in SNAPSHOT_MATERIALS
}

MATERIAL_COL_KEYS = [
    f"{src}_{key}" for src in ("lab", "mail") for key, _ in MATERIAL_COLS
]
OPAK_COL_KEYS = [f"opak{i}" for i in range(len(OPAK_PEOPLE))]
MANUAL_COL_KEYS = ["pidkovy", "disks", *OPAK_COL_KEYS]
# СЛМ теж поки ручний, хоч і стоїть у групах матеріалів.
ALL_COL_KEYS = [*MATERIAL_COL_KEYS, "pidkovy", "disks", *OPAK_COL_KEYS]

# ── Гроші ───────────────────────────────────────────────────────────────────
# Тарифи, які НЕ залежать від коефіцієнта (з відомості). Zn — окремо, за
# коефіцієнтом.
TARIFF_PMMA_WAX = 0.75
TARIFF_TI = 0.75
TARIFF_SLM = 0.8
# Ставка Zn за замовчуванням (коефіцієнт ~24,2 у серпні). Повний довідник
# коефіцієнт→ставка власник дасть пізніше; поки лишається як є.
DEFAULT_RATE_ZN = 0.634
# Межа ВІДОМОГО довідника коефіцієнтів. Вище — CRM не вгадує ставку, а просить
# її (rate_override) і показує попередження.
RATE_BAND_MAX = 26.7

MONTH_NAMES_SHORT = [
    "січ", "лют", "бер", "кві", "тра", "чер",
    "лип", "сер", "вер", "жов", "лис", "гру",
]
MONTH_NAMES_LONG = [
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
]


def parse_decimal(raw: str | None) -> float | None:
    """«52» / «51,5» / «0,634» → float. Кома або крапка. None — порожньо/сміття."""
    if raw is None:
        return None
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_rework(mill_count: str | None) -> bool:
    """Робота фрезерується вдруге і далі — переробка, у виробіток НЕ рахується.
    Саме правило — `vyrobitok_rows._is_rework_mark`; тут лишилось ім'я, на яке
    посилаються `stats_data` і `mail_rework`."""
    return vyrobitok_rows._is_rework_mark(mill_count)


def nf(value: float, decimals: int = 2) -> str:
    """Українське форматування числа: пробіл-роздільник тисяч, кома-десяткова."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", " ").replace(".", ",")


@dataclass
class MonthGrid:
    year: int
    month: int
    month_label: str
    rows: list[dict]
    totals: dict[str, int]
    money: dict
    opaks: dict
    months_nav: list[dict]
    warn: dict | None
    # «Не пораховано й чому» (22.11.26): рядки місяця, що не дали одиниць, з
    # причиною — за ПОТОЧНИМ станом таблиці (закритий день число не міняє).
    skipped: list[dict] = field(default_factory=list)
    # Чи діють правила власника від 22.11.26 (типово так; False — аварійний відкат).
    rules_v2: bool = False


def _source_bucket(source: str) -> str:
    """lab → «Лабораторія»; усе клієнтське (email, sheet_client) → «Пошта»."""
    return "lab" if source == "lab" else "mail"


def _load_month_settings(db: Session, year: int, month: int) -> VyrobitokMonth:
    row = db.scalar(
        select(VyrobitokMonth).where(
            VyrobitokMonth.year == year, VyrobitokMonth.month == month
        )
    )
    if row is None:
        # Не зберігаємо порожній рядок на кожен перегляд — віддаємо дефолтний
        # об'єкт у пам'яті. Рядок з'явиться, щойно оператор змінить курс/склад.
        row = VyrobitokMonth(year=year, month=month, kurs="52", people_count=5)
    return row


# ── Заморозка дня ────────────────────────────────────────────────────────────
# Кожна колонка закривається тоді, коли її вже ніхто не дописує (рішення
# власника 06.09.26). Робоча доба кінчається о 07:30, і на цій межі лабораторія
# з поштою вже сталі — нічна зміна дописала «підранок» у ту саму вкладку. СЛМ
# заповнюють руками ще пів дня ПІСЛЯ кінця доби (зазвичай до 12:00, у вкладку
# минулого дня) і там же виправляють описки, тож він синкається далі й
# морозиться увечері.
SLM_FREEZE_HOUR = 18


def _day_rows(db: Session, first: date, last: date) -> dict[date, VyrobitokDay]:
    rows = db.scalars(
        select(VyrobitokDay).where(VyrobitokDay.day.between(first, last))
    ).all()
    return {row.day: row for row in rows}


def orders_frozen_days(db: Session, first: date, last: date) -> set[date]:
    """Дні діапазону, де колонки робіт уже закриті."""
    return {
        day for day, row in _day_rows(db, first, last).items()
        if row.orders_frozen_at is not None
    }


def slm_is_frozen(db: Session, day: date) -> bool:
    row = db.get(VyrobitokDay, day)
    return row is not None and row.slm_frozen_at is not None


def _due_freezes(day: date, today: date, now: datetime) -> tuple[bool, bool]:
    """(роботи, СЛМ) — що з цього для дня `day` вже має бути заморожене.

    Відлік іде від НАСТУПНОЇ ВКЛАДКИ, а не від наступного календарного дня, і
    це не дрібниця. Вкладок за суботу й неділю в таблиці немає: цех у вихідні
    працює, а рядки лягають у п'ятничну вкладку (CLAUDE.md §4). Стара умова
    «день + 1» закривала п'ятницю вже в суботу о 18:00 — усе, що дописували в
    суботу ввечері й у неділю, не рахувалось НІКОЛИ, і не через збій, а за
    побудовою. Бойовий випадок 15.09.26: у вкладці 11.09.26 стоять групи СЛМ
    з підписами «12.09.2026», і в «Виробітку» за 11-те порожньо.

    `next_tab_day` перестрибує вихідні, тож для п'ятниці закриття настає в
    понеділок, а для звичайного дня — наступного ранку, як і було.

    Роботи — щойно вкладка перестала наповнюватись. СЛМ — о 18:00 того ж дня
    (його дописують і правлять ще пів дня після кінця доби). Якщо застосунок
    стояв і день давно минув, обидва просто настали.
    """
    close_day = next_tab_day(day)
    orders_due = today >= close_day
    if today < close_day:
        slm_due = False
    elif today == close_day:
        slm_due = now.hour >= SLM_FREEZE_HOUR
    else:
        slm_due = True
    return orders_due, slm_due


def freeze_due_days(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Закрити всі дні, чий час настав. Ідемпотентно; викликається за розкладом.

    Догін вбудований: ПК вимикали — на старті цей же прохід закриє всі
    пропущені дні. Дані для цього є, поки роботи живі в базі.

    Заморожуючи роботи, знімок пишемо ЯВНО по всіх колонках дня, включно з
    нулями: інакше колонка, яка спорожніла (роботи зникли з таблиці), лишилась
    би зі старим числом попереднього знімка й закам'яніла б у ньому.
    """
    # Київський час: межа доби й 18:00 — це годинник у цеху, не UTC. У базу
    # кладемо наївний штамп (усі DateTime у схемі наївні — CLAUDE.md §14).
    now = now or business_now()
    today = business_today(now)
    stamp = now.replace(tzinfo=None)

    # Дні-кандидати: усе, де є що морозити — живі роботи або вже знята клітинка.
    days: set[date] = set(
        db.scalars(select(VyrobitokCell.day).distinct()).all()
    )
    # Дві колонки, а не повні рядки ORM: дата роботи рахується лише з
    # `sheet_tab` і `created_at`, а живих робіт у базі тисячі — на кожному
    # проході морозилки це була марна матеріалізація (ревʼю 07.09.26, C.8).
    for sheet_tab, created_at in db.execute(
        select(Order.sheet_tab, Order.created_at).where(Order.archived_at.is_(None))
    ):
        days.add(order_date_of(sheet_tab, created_at))

    rows = _day_rows(db, min(days), max(days)) if days else {}
    result = {"orders": 0, "slm": 0}

    for day in sorted(days):
        if day in _RESYNC_DAYS:
            continue  # оператор саме пересинхронізовує цей день — не чіпати
        if day >= today:
            continue
        orders_due, slm_due = _due_freezes(day, today, now)
        row = rows.get(day)
        need_orders = orders_due and (row is None or row.orders_frozen_at is None)
        need_slm = slm_due and (row is None or row.slm_frozen_at is None)
        if not need_orders and not need_slm:
            continue
        if row is None:
            row = VyrobitokDay(day=day)
            db.add(row)
            rows[day] = row
        if need_orders:
            _snapshot_orders(db, day)
            row.orders_frozen_at = stamp
            result["orders"] += 1
        if need_slm:
            row.slm_frozen_at = stamp
            result["slm"] += 1
    if result["orders"] or result["slm"]:
        db.commit()
    return result


def _snapshot_orders(db: Session, day: date) -> None:
    """Записати знімок колонок робіт за день — по ВСІХ колонках, і нулі теж."""
    auto, _live, _months = _live_units(db, day, day)
    cells = db.scalars(
        select(VyrobitokCell).where(VyrobitokCell.day == day)
    ).all()
    by_key = {c.col_key: c for c in cells}
    for col_key in sorted(ORDER_COLS):
        value = auto.get((day, col_key), 0)
        cell = by_key.get(col_key)
        if cell is None:
            db.add(VyrobitokCell(day=day, col_key=col_key, auto_value=value))
        elif cell.auto_value != value:
            cell.auto_value = value


def unfreeze_day(db: Session, day: date) -> None:
    """Зняти обидві позначки — свідомий «синк цього дня» рахує начисто."""
    row = db.get(VyrobitokDay, day)
    if row is not None:
        db.delete(row)
        db.commit()


RULES_V2_KEY = "vyrobitok_rules_v2"


def rules_v2(db: Session) -> bool:
    """Чи діють правила власника від 22.11.26 (`vyrobitok_rows`): «без Sum3D не
    рахується» і для пошти, переробка наряду — один раз.

    Це НЕ опція, а те, як рахується «Виробіток» (власник: «аксіома, без
    перемикань») — тому вимикача на екрані немає й типово завжди так. Значення
    «0» у налаштуванні лишилось аварійним відкатом до старого рахунку: його
    ставить лише `set_rules_v2` (кодом), екрана для нього немає."""
    return (get_setting(db, RULES_V2_KEY) or "") != "0"


def set_rules_v2(db: Session, enabled: bool) -> None:
    set_setting(db, RULES_V2_KEY, "1" if enabled else "0")


def _judged(
    db: Session, first: date, last: date
) -> tuple[list[vyrobitok_rows.Verdict], set[tuple[int, int]]]:
    return vyrobitok_rows.judge(db, first, last, v2=rules_v2(db))


def _tally(
    verdicts: list[vyrobitok_rows.Verdict],
) -> tuple[dict[tuple[date, str], int], set[date]]:
    """Вердикти → (день,колонка) → сума і «живі» дні.

    День живий, коли в ньому є хоч один рядок без позначки переробки — так
    само, як було: рядки, що не рахуються (технік завів, оператор ще не взяв),
    теж тримають день живим, інакше він показував би старий знімок замість
    чесного нуля."""
    auto: dict[tuple[date, str], int] = defaultdict(int)
    live_days: set[date] = set()
    for v in verdicts:
        if v.reason != vyrobitok_rows.REASON_REWORK_MARK:
            live_days.add(v.day)
        if v.col_key is not None:
            auto[(v.day, v.col_key)] += v.units
    return auto, live_days


def _live_units(
    db: Session, first: date, last: date
) -> tuple[dict[tuple[date, str], int], set[date], set[tuple[int, int]]]:
    """Одиниці з ЖИВИХ Orders: (день,колонка) → сума, живі дні, місяці з даними.

    Спільне для показу й для заморозки — щоб заморожене число рахувалось за
    тими самими правилами, за якими день показувався до неї. Саме рішення
    «рядок рахується чи ні» — у `vyrobitok_rows.judge`.
    """
    verdicts, months_with_orders = _judged(db, first, last)
    auto, live_days = _tally(verdicts)
    return auto, live_days, months_with_orders


def _skipped_view(verdicts: list[vyrobitok_rows.Verdict]) -> list[dict]:
    """Рядки, що не дали одиниць, і перенесені прорахунки — для екрана."""
    out: list[dict] = []
    for v in sorted(verdicts, key=lambda x: (x.day, x.bucket, x.row_number or 0, x.order_id)):
        if v.counted and not v.note:
            continue
        out.append({
            "day": v.day.strftime("%d.%m"),
            "bucket": "лаб" if v.bucket == "lab" else "пошта",
            "row": v.row_number,
            "who": v.work_order_no or v.client_name or "—",
            "material": v.material_color,
            "quantity": v.quantity,
            "counted": v.counted,
            "reason": v.reason_label if not v.counted else v.note,
        })
    return out


def recount_month(db: Session, year: int, month: int) -> dict:
    """Перезняти знімки ЗАКРИТИХ днів місяця з поточних робіт у базі.

    Потрібно, коли змінились правила (22.11.26) чи довідник
    матеріалів (Vivid став цирконієм): закритий день тримає старе число й сам
    не перерахується. Таблицю не читає — рахує з того, що вже є в базі; день,
    у якому живих робіт не лишилось (лаба почистила вкладку), НЕ чіпає, інакше
    його число стало б нулем. Так само не чіпає день, з якого після закриття
    зникла ЧАСТИНА прорахованих рядків (`vyrobitok_rows.vanished_rows`): число
    впало б, і повернути його було б нічим. Такі дні названо у відповіді
    (`partial`) — їх виправляють «⟳» дня або рукою. Позначка закриття лишається."""
    days_in_month = calendar.monthrange(year, month)[1]
    first, last = date(year, month, 1), date(year, month, days_in_month)
    _auto, live_days, _months = _live_units(db, first, last)
    frozen = orders_frozen_days(db, first, last)
    vanished = vyrobitok_rows.vanished_rows(db, first, last, v2=rules_v2(db))
    result: dict = {"days": 0, "skipped_empty": 0, "partial": []}
    for day in sorted(frozen):
        if day not in live_days:
            result["skipped_empty"] += 1
            continue
        if vanished.get(day):
            result["partial"].append(day)
            continue
        _snapshot_orders(db, day)
        result["days"] += 1
    if result["days"]:
        db.commit()
    return result


def compute_month(
    db: Session, year: int, month: int, *, persist: bool = True
) -> MonthGrid:
    days_in_month = calendar.monthrange(year, month)[1]
    first = date(year, month, 1)
    last = date(year, month, days_in_month)
    today = business_today()

    verdicts, months_with_orders = _judged(db, first, last)
    auto, live_days = _tally(verdicts)
    frozen = orders_frozen_days(db, first, last)

    cells = db.scalars(
        select(VyrobitokCell).where(VyrobitokCell.day.between(first, last))
    ).all()
    by_key: dict[tuple[date, str], VyrobitokCell] = {
        (c.day, c.col_key): c for c in cells
    }

    # Знімок авто у сховище для «живих» днів — щоб минулий місяць вижив, коли
    # роботи заархівуються. Пишемо лише те, що змінилось. Заморожений день
    # пропускаємо: його число вже закрите, і перерахунок із почищеної вкладки
    # тільки зіпсував би його (саме заради цього заморозка й існує).
    if persist:
        changed = False
        for (d, col_key), value in auto.items():
            if d in frozen:
                continue
            cell = by_key.get((d, col_key))
            if cell is None:
                cell = VyrobitokCell(day=d, col_key=col_key, auto_value=value)
                db.add(cell)
                by_key[(d, col_key)] = cell
                changed = True
            elif cell.auto_value != value:
                cell.auto_value = value
                changed = True
        if changed:
            db.commit()

    months_with_data = set(months_with_orders)
    for c in cells:
        months_with_data.add((c.day.year, c.day.month))

    def cell_view(d: date, col_key: str) -> dict:
        cell = by_key.get((d, col_key))
        if col_key in SNAPSHOT_COLS:
            # ORDER-колонки на живий день — рахуємо з Orders; СЛМ і мертві дні —
            # зі знімка auto_value (СЛМ туди пише синк, решту — цей же метод).
            if col_key in ORDER_COLS and d in live_days and d not in frozen:
                eff_auto = auto.get((d, col_key), 0)
            elif cell is not None and cell.auto_value is not None:
                eff_auto = cell.auto_value
            else:
                eff_auto = 0
            override = cell.override_value if cell is not None else None
            num = override if override is not None else eff_auto
            return {
                "num": num,
                "auto": eff_auto,
                "edited": override is not None,
                "by_hand": override is not None,
            }
        override = cell.override_value if cell is not None else None
        num = override if override is not None else 0
        # Ручні колонки не мають «авто», тому й не бувають «виправленими» —
        # «edited» означає «CRM порахувала одне, ти вписав інше». Але число
        # тут вписала людина, і на екрані це мусить бути видно так само, як
        # правку (власник 11.09.26: опаки виглядали як пораховані CRM) —
        # для цього окрема ознака `by_hand`.
        return {"num": num, "auto": None, "edited": False, "by_hand": override is not None}

    rows: list[dict] = []
    for dayn in range(1, days_in_month + 1):
        d = date(year, month, dayn)
        weekday = d.weekday()  # 0=пн … 6=нд
        day_cells = {ck: cell_view(d, ck) for ck in ALL_COL_KEYS}
        has_any = d in live_days or any(c["num"] for c in day_cells.values())
        weekend = weekday >= 5
        rows.append(
            {
                "date": d.isoformat(),
                "dayn": dayn,
                "weekend_short": "сб" if weekday == 5 else ("нд" if weekday == 6 else ""),
                # Сьогодні НІКОЛИ не «вихідний»: коли сьогодні випадає на суботу
                # й даних ще нема, порожній off-рядок перебивав би підсвітку
                # today (у шаблоні off має пріоритет). У цей рядок вводять —
                # він мусить лишатись повним і підсвіченим навіть у вихідний.
                "is_off": weekend and not has_any and d != today,
                "is_today": d == today,
                # День закрито: число більше не перераховується. Показуємо, щоб
                # оператор розумів, чому цифра не реагує на правки в таблиці.
                "frozen": d in frozen,
                "cells": day_cells,
            }
        )

    totals = {
        ck: sum(r["cells"][ck]["num"] for r in rows) for ck in ALL_COL_KEYS
    }

    money = _money(db, year, month, totals)
    opaks = {
        "month_label": f"{MONTH_NAMES_LONG[month - 1]} {year}",
        "people": [
            {"name": OPAK_PEOPLE[i], "units": totals[f"opak{i}"]}
            for i in range(len(OPAK_PEOPLE))
        ],
        "total": sum(totals[f"opak{i}"] for i in range(len(OPAK_PEOPLE))),
    }

    months_nav = [
        {
            "num": m,
            "label": MONTH_NAMES_SHORT[m - 1],
            "has_data": (year, m) in months_with_data,
            "on": m == month,
        }
        for m in range(1, 13)
    ]

    warn = None
    if money["rate_out_of_band"]:
        warn = {"coefficient": money["coefficient_str"]}

    return MonthGrid(
        year=year,
        month=month,
        month_label=f"{MONTH_NAMES_LONG[month - 1]} {year}",
        rows=rows,
        totals=totals,
        money=money,
        opaks=opaks,
        months_nav=months_nav,
        warn=warn,
        skipped=_skipped_view(verdicts),
        rules_v2=rules_v2(db),
    )


def _money(db: Session, year: int, month: int, totals: dict[str, int]) -> dict:
    settings = _load_month_settings(db, year, month)
    people = settings.people_count or 5
    kurs = parse_decimal(settings.kurs) or 0.0
    rate_override = parse_decimal(settings.rate_override)

    zr_total = totals["lab_zr"] + totals["mail_zr"]
    pidkovy = totals["pidkovy"]
    disks = totals["disks"]
    pmma_wax = totals["lab_pmma"] + totals["mail_pmma"] + totals["lab_wax"] + totals["mail_wax"]
    ti_total = totals["lab_ti"] + totals["mail_ti"]
    slm_total = totals["lab_slm"] + totals["mail_slm"]

    coefficient = zr_total / disks if disks else None
    rate_out_of_band = (
        coefficient is not None
        and coefficient > RATE_BAND_MAX
        and rate_override is None
    )
    rate_zn = rate_override if rate_override is not None else DEFAULT_RATE_ZN

    zn_plain = zr_total - pidkovy  # цирконій без підков
    types = [
        {"name": "Цирконій", "units": zn_plain, "rate": rate_zn},
        {"name": "Підкови", "units": pidkovy, "rate": rate_zn * 2},
        {"name": "ПММА / воск", "units": pmma_wax, "rate": TARIFF_PMMA_WAX},
        {"name": "Титан", "units": ti_total, "rate": TARIFF_TI},
        {"name": "SLM", "units": slm_total, "rate": TARIFF_SLM},
    ]
    for t in types:
        t["sum"] = t["units"] * t["rate"]
        t["units_str"] = nf(t["units"], 0)
        t["rate_str"] = nf(t["rate"], 3)
        t["sum_str"] = nf(t["sum"], 2)

    total_units = sum(t["units"] for t in types)
    share = total_units / people if people else 0.0
    uo_share = sum((t["units"] / people) * t["rate"] for t in types) if people else 0.0
    grn_share = uo_share * kurs

    return {
        "zr_total": zr_total,
        "disks_total": disks,
        "pidkovy_total": pidkovy,
        "coefficient": coefficient,
        "coefficient_str": nf(coefficient, 1) if coefficient is not None else "—",
        "rate_zn": rate_zn,
        "rate_str": nf(rate_zn, 3),
        "rate_pid_str": nf(rate_zn * 2, 3),
        "rate_override": settings.rate_override or "",
        "rate_out_of_band": rate_out_of_band,
        "types": types,
        "total_units": total_units,
        "total_units_str": nf(total_units, 0),
        "people_count": people,
        "share": share,
        "share_str": nf(share, 2),
        "kurs": settings.kurs or "",
        "uo_share": uo_share,
        "grn_share": grn_share,
        "grn_share_str": nf(grn_share, 0),
    }


# ── СЛМ: підрахунок із рядків вкладки й запис у клітинки ─────────────────────
# СЛМ не потрапляє в Orders (`_is_non_queue_row` викидає його до збереження).
# Тому синк тим самим проходом, що читає рядки, рахує СЛМ і пише його ЧИСЛА
# прямо в клітинки табеля (auto_value). Це не окремий Google-шлях — та сама
# реконсиляція, ті самі рядки, — тому й не розійдеться з чергою.
#
# Правила калібровані проти реальних вкладок 27/28/31.08.26 (лаб 0/2/4,
# файловий 150/341/115 — збіглось до одиниці). Пастка: CADCAM Команда (лаб) і
# CadCam Energy (клієнт) схожі до нерозрізнення, але означають протилежне.
MODELING_KINDS = {"елайнери", "моделі", "сканування", "моделювання"}
CADCAM_LAB_KIND = "cadcam команда"


def _norm_kind(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def slm_totals_from_rows(rows) -> tuple[int, int]:
    """(лаб, пошта) одиниць СЛМ з нижнього блоку вкладки.

    Рахуємо ТІЛЬКИ client-рядки блоку (без наряду й техніка — `is_client_row`).
    наряд-body СЛМ ігноруємо: наряд могли завести, роботу не зробити. CADCAM
    Команда — лабораторний, к-сть навмисно в колонці «Колір роботи»
    (`material_color`), щоб власна сума таблиці її не рахувала. Решта блоку —
    клієнти (файловий СЛМ), к-сть у колонці кількості. Моделі/сканування/
    елайнери — не наша робота й не СЛМ, пропускаємо.

    `rows` — OrderRow-подібні (качине типування: work_order_no, is_client_row,
    kind, material_color, quantity), щоб не тягнути парсер у сервіс.
    """
    lab = 0
    mail = 0
    for row in rows:
        if getattr(row, "work_order_no", ""):  # наряд-body — не рахуємо
            continue
        if not getattr(row, "is_client_row", False):
            continue
        kind = _norm_kind(getattr(row, "kind", ""))
        if kind in MODELING_KINDS:
            continue
        material = (getattr(row, "material_color", "") or "").strip()
        quantity = (getattr(row, "quantity", "") or "").strip()
        # Блок СЛМ = client-рядок без матеріалу АБО з к-стю в колонці кольору
        # (CADCAM). Звичайний клієнт із матеріалом І к-стю — фрезерна робота,
        # сюди не потрапляє (той самий критерій, що у _is_non_queue_row).
        if material and quantity:
            continue
        if kind == CADCAM_LAB_KIND:
            lab += parse_int_safe(material) or 0
        else:
            mail += parse_int_safe(quantity) or 0
    return lab, mail


def store_slm_totals(
    session: Session, day: date, lab_units: int, mail_units: int
) -> None:
    """Записати авто-число СЛМ у клітинки табеля (lab_slm / mail_slm) за день.

    Пише лише `auto_value`; правку оператора (`override_value`) не чіпає — тому
    ручне виправлення переживає синк. Без commit: транзакцією керує викликач
    (sync_tab комітиться поблочно у своєму синк-циклі).

    Заморожений день синк не чіпає: СЛМ дописують і правлять ще пів дня після
    кінця доби, тому він морозиться о 18:00 — а після того почищена або
    перечитана вкладка вже не має права затерти закрите число. Свідомий шлях
    один — «синк цього дня» знімає заморозку ПЕРЕД читанням вкладки."""
    if slm_is_frozen(session, day):
        return
    for col_key, units in (("lab_slm", lab_units), ("mail_slm", mail_units)):
        cell = session.scalar(
            select(VyrobitokCell).where(
                VyrobitokCell.day == day, VyrobitokCell.col_key == col_key
            )
        )
        if cell is None:
            session.add(VyrobitokCell(day=day, col_key=col_key, auto_value=units))
        else:
            cell.auto_value = units


def set_cell(db: Session, day: date, col_key: str, value: int | None) -> None:
    """Зберегти правку оператора. value=None — стерти правку (повернути авто).

    Ручні колонки використовують те саме override_value: там авто немає, тож
    введене число і є значенням клітинки."""
    if col_key not in ALL_COL_KEYS:
        raise ValueError(f"невідома колонка: {col_key}")
    cell = db.scalar(
        select(VyrobitokCell).where(
            VyrobitokCell.day == day, VyrobitokCell.col_key == col_key
        )
    )
    if cell is None:
        cell = VyrobitokCell(day=day, col_key=col_key)
        db.add(cell)
    cell.override_value = value
    db.commit()


def save_month_settings(
    db: Session,
    year: int,
    month: int,
    *,
    kurs: str | None = None,
    people_count: int | None = None,
    rate_override: str | None = None,
) -> VyrobitokMonth:
    row = db.scalar(
        select(VyrobitokMonth).where(
            VyrobitokMonth.year == year, VyrobitokMonth.month == month
        )
    )
    if row is None:
        row = VyrobitokMonth(year=year, month=month)
        db.add(row)
    if kurs is not None:
        row.kurs = kurs.strip() or "52"
    if people_count is not None:
        row.people_count = max(1, people_count)
    if rate_override is not None:
        row.rate_override = rate_override.strip() or None
    db.commit()
    return row


def clear_day_overrides(db: Session, day: date) -> int:
    """Стерти ручні правки авто-колонок дня. Повертає, скільки їх було.

    «Синк цього дня» означає «перерахувати начисто»: він знімає заморозку й
    перечитує вкладку, тож ручне число, яке лишалось поверх auto_value, ховало
    б свіжий результат — оператор тиснув кнопку й бачив те саме число (доти
    єдиним способом побачити авто було стерти клітинку руками).

    Чіпаємо ЛИШЕ SNAPSHOT_COLS: у ручних колонок (підкови, диски, опаки) авто
    немає взагалі, там override — саме значення, і синк його стерти не має
    права.
    """
    cells = db.scalars(
        select(VyrobitokCell).where(
            VyrobitokCell.day == day,
            VyrobitokCell.col_key.in_(sorted(SNAPSHOT_COLS)),
            VyrobitokCell.override_value.is_not(None),
        )
    ).all()
    for cell in cells:
        cell.override_value = None
    if cells:
        db.commit()
    return len(cells)
