# -*- coding: utf-8 -*-
"""Табло цеху для телевізора — сторінка `/t/<token>/shop` на порту 8010.

Живе поруч із табло пічок і в тому самому окремому застосунку
(`app/routers/furnace_board.py`): той самий секрет у посиланні, той самий
вимикач у налаштуваннях, ті самі «жодних інших маршрутів». Різниця в
аудиторії — логісти дивляться печі, цех дивиться верстати, — тому це
ОКРЕМА адреса, а не заміна: стара сторінка лишається там, де була.

Власних даних сервіс не збирає. Верстати приходять із `machines.snapshot()`
(той самий збирач, що годує екран «Верстати»), печі — з
`furnace_board.board_view()`. Інакше на табло рано чи пізно з'явилось би
друге, розбіжне джерело правди про той самий верстат.

ЧОМУ станів більше, ніж «працює / не працює». `MachineCard.state_key` знає
дев'ять станів, і два з них вимагають людини просто зараз: `done` —
програма завершена, треба зняти, і `fault` — на екрані помилка. Табло, яке
звело б їх до «не фрезерує», мовчало б саме тоді, коли має кричати.
"""

from __future__ import annotations

import functools
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.business_day import business_now

# Стани, які вимагають людини, — і те, як їх бачить око з проходу.
# Ключ — `MachineCard.state_key`, значення — канал кольору на табло.
_TONES = {
    "run": "run",          # фрезерує — бурштин, шестерня крутиться
    "done": "done",        # завершено, зняти — вимагає дії
    "fault": "bad",        # помилка на екрані
    "off": "bad",          # немає зв'язку
    "check": "busy",       # перевіряє програму перед стартом
    "busy": "busy",        # програма пішла, відсотка ще не видно
    "idle": "calm",        # стоїть
    "wait": "calm",        # кадру ще не було
    "unreadable": "calm",  # екран цього верстата читати не навчені
}


# Скільки робіт картка називає ПОІМЕННО. Дві, не одна: один проєкт Sum3D
# часто ділять дві роботи, і «перша + ще 1» лишало другого клієнта безіменним
# саме на екрані, куди дивляться, щоб зрозуміти, чия це заготовка (рішення
# власника 15.09.26). Третя й далі не вміщаються — за ними «ще N».
MAX_NAMED_WORKS = 2

# Як малюється плитка SISMA на телевізорі (власник 12.10.26). Вибір — лише
# картинка ВСЕРЕДИНІ полотна; плитка, футер і правило «рух лише під час
# друку» однакові для всіх. Кожен стан має ВЛАСНЕ слово, порожнього значення
# немає (CLAUDE.md §14 «Порожнє поле = поля не було»): невідоме чи порожнє
# читається як `classic` — той самий розріз камери, що був до вибору.
SISMA_STYLE_KEY = "shop_board_sisma_style"
SISMA_STYLES: tuple[tuple[str, str, str], ...] = (
    ("classic", "Розріз камери (як зараз)",
     "камера збоку: подача порошку, ракель, платформа опускається, лазер"),
    ("melt", "Розпечена ванна",
     "вигляд згори у вікно машини: лазер штрихує перерізи, свіжий слід остигає з бурштину в сталь"),
    ("tower", "Вежа",
     "профіль збоку: коронки й міст виростають рівно на N з M шарів, верхній шар гарячий"),
)
DEFAULT_SISMA_STYLE = "classic"


def normalize_sisma_style(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value in {key for key, _, _ in SISMA_STYLES} else DEFAULT_SISMA_STYLE


def read_sisma_style(db: Optional[Session]) -> str:
    if db is None:
        return DEFAULT_SISMA_STYLE
    from app.settings_store import get_setting

    return normalize_sisma_style(get_setting(db, SISMA_STYLE_KEY))


# ── Диск, що вифрезеровується (ТВ 3, власник 11.10.26) ─────────────────────
# Замість точкової смужки — заготовка, на якій із відсотком проступають
# вирізані одиниці, і дуга прогресу довкола. Число на картці лишається тим
# самим `percent` з екрана верстата; диск лише показує його формою.
#
# Скільки одиниць на диску — СУМА кількостей робіт цього Sum3D (Sum3D ID — час
# диска, не роботи: один проєкт = одна заготовка). Не вгадуємо: якщо хоч одна
# кількість не число або робіт немає, коронок не малюємо зовсім — лише дугу.
# Порожній диск чесніший за диск із вигаданими сімома коронками.
DISC_MAX_UNITS = 36          # більше на 98-мм заготовку не кладуть; більше — лише дуга
_DISC_FIELD = 37.0           # радіус поля коронок у viewBox 0..100 (диск 42, дуга 47)
_DISC_ARC = 2 * math.pi * 47  # довжина дуги прогресу — у `stroke-dasharray`
_GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))


@dataclass(frozen=True)
class DiscCrown:
    """Одна одиниця на заготовці: місце (viewBox 0..100) і чи вже вирізана."""

    x: float
    y: float
    r: float
    cut: bool


def disc_units(orders) -> Optional[int]:
    """Скільки одиниць фрезерується на цьому диску, або None — не знаємо."""
    total = 0
    for order in orders or ():
        raw = str(getattr(order, "quantity", "") or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            return None
        total += int(raw)
    if not total or total > DISC_MAX_UNITS:
        return None
    return total


@functools.lru_cache(maxsize=64)
def _disc_layout(units: int) -> tuple[float, tuple[tuple[float, float], ...]]:
    """Радіус лунки й центри для `units` одиниць — спіраль Фогеля.

    Радіус береться від НАЙМЕНШОЇ відстані між сусідами саме цієї розкладки
    (а не від середньої щільності): на малих кількостях спіраль нерівна, і
    «середній» радіус давав накладені лунки (вісім одиниць)."""
    if units == 1:
        return 8.0, ((50.0, 50.0),)
    unit = [
        (math.sqrt((i + 0.5) / units) * math.cos(i * _GOLDEN_ANGLE),
         math.sqrt((i + 0.5) / units) * math.sin(i * _GOLDEN_ANGLE))
        for i in range(units)
    ]
    nearest = min(
        math.hypot(a[0] - b[0], a[1] - b[1])
        for i, a in enumerate(unit) for b in unit[i + 1:]
    )
    # Лунка займає 88 % половини найменшої відстані; поле — `_DISC_FIELD`.
    k = nearest * 0.44
    radius = min(8.0, _DISC_FIELD * k / (1 + k))
    reach = _DISC_FIELD - radius
    points = tuple((round(50 + reach * x, 2), round(50 + reach * y, 2)) for x, y in unit)
    return round(radius, 2), points


def disc_crowns(units: Optional[int], percent: Optional[int]) -> list[DiscCrown]:
    """Одиниці на диску: місця (`_disc_layout`) і які вже вирізані.

    Вирізаних — частка, що відповідає відсотку, вниз до цілої: на 99 % остання
    коронка ще не вирізана, і так само її покаже й диск. Розкладка залежить
    лише від кількості, тож між оновленнями коронки не «стрибають» по диску.
    """
    if not units or percent is None:
        return []
    cut = units * max(0, min(100, percent)) // 100
    radius, points = _disc_layout(units)
    return [
        DiscCrown(x=x, y=y, r=radius, cut=index < cut)
        for index, (x, y) in enumerate(points)
    ]


@dataclass
class ShopWork:
    """Одна робота в картці: імʼя (клієнт або наряд) і матеріал.

    Окремий тип, а не два рядки в `ShopMachine`, бо матеріал належить СВОЇЙ
    роботі: показати матеріал першої під іменем другої — та сама вигадка, що
    й спільний клієнт для трьох різних робіт.
    """

    name: str = ""
    material: str = ""
    # Технік — лише в лабораторної роботи (власник 29.09.26): поруч із
    # нарядом, тихіше за нього. У клієнтської лишається порожнім.
    tech: str = ""


@dataclass
class ShopMachine:
    """Один верстат на табло. Усе вже готове до показу — шаблон не рахує."""

    key: str
    name: str
    state: str                       # state_key як є, для налагодження
    tone: str                        # канал кольору (див. _TONES)
    word: str = ""                   # слово стану; для «run» порожнє — там число
    note: str = ""                   # пояснення під словом
    percent: Optional[int] = None
    sum3d: str = ""
    works: list = field(default_factory=list)   # до MAX_NAMED_WORKS штук
    extra: str = ""                  # «ще 2» — роботи, що не вмістились
    # Роботи в `works` — не поточні, а ЩОЙНО ЗНЯТІ. Шаблон мусить це підписати:
    # без підпису завершений верстат виглядав би так само, як працюючий.
    works_are_last: bool = False
    # Підпис над роботою для верстата, який чекає людини. Слово стану стоїть
    # дрібним у кутку плитки, і з проходу його не видно: 17.09.26 власник
    # прочитав три завершені верстати як «різні статуси» саме тому, що в них
    # під назвою стояло імʼя клієнта — те саме, що в машини під фрезою.
    action: str = ""
    portrait: str = ""               # адреса фото верстата, якщо воно є
    # SLM-принтер: шар N з M — числа з екрана машини, не оцінка.
    is_sisma: bool = False
    layer: Optional[int] = None
    layers_total: Optional[int] = None
    # Коли машина обіцяє закінчити — ЇЇ прогноз із екрана, не наша оцінка.
    ends_at: str = ""
    left_text: str = ""
    # Одиниць на диску (див. `disc_units`); None — коронок не малюємо.
    units: Optional[int] = None
    # Назва ПОТОЧНОЇ програми верстата без «.iso», розкладена `program_line`:
    # голова + Sum3D-хвіст. Обидва порожні — назви немає, картка показує
    # просто Sum3D. Шаблон сам обгортає хвіст у `<b>` (екранування тоді його).
    prog_head: str = ""
    prog_tail: str = ""

    @property
    def has_prog(self) -> bool:
        return bool(self.prog_head or self.prog_tail)

    @property
    def crowns(self) -> list[DiscCrown]:
        return disc_crowns(self.units, self.percent)

    @property
    def cut_count(self) -> int:
        return sum(1 for crown in self.crowns if crown.cut)

    @property
    def arc_dash(self) -> float:
        return round(_DISC_ARC, 2)

    @property
    def arc_offset(self) -> float:
        """Незаповнена частина дуги: 0 — повне коло, `arc_dash` — порожнє."""
        share = max(0, min(100, self.percent or 0)) / 100
        return round(_DISC_ARC * (1 - share), 2)

    @property
    def client(self) -> str:
        """Імʼя першої роботи. Лишається заради місць, які знають про одну."""
        return self.works[0].name if self.works else ""

    @property
    def material(self) -> str:
        return self.works[0].material if self.works else ""

    @property
    def sisma_percent(self) -> Optional[int]:
        if not (self.layer and self.layers_total):
            return None
        return round(self.layer / self.layers_total * 100)


# ── Піч: живий жар і крива нагріву (ТВ 5, власник 11.10.26) ────────────────
# Крива — ЛИШЕ з показань у базі (`FurnaceReading`) і тим самим збирачем, що
# графік доби на екрані «Печі» (`furnace.build_day_chart`): розрив лишається
# розривом, проміжних точок ніхто не усереднює. Вікно коротке: біля телевізора
# питають «як іде ця програма», а не «що було вночі».
TV_CURVE_HOURS = 6
# Крива змінюється раз на хвилину (так пишуться показання), а табло питає раз
# на 30 с, і глядачів буває кілька — тож тримаємо готову криву хвилину.
_CURVE_TTL_SECONDS = 55.0
_curve_cache: dict[str, tuple[float, Optional["TvCurve"]]] = {}
_curve_lock = threading.Lock()


@dataclass(frozen=True)
class TvCurve:
    """Готова геометрія кривої для плитки печі. Поле — viewBox `width`×`height`
    графіка доби; «голова» (`head_*`) — у відсотках плитки, бо крива
    розтягується на плитку без збереження пропорцій, і коло там стало б
    еліпсом."""

    lines: list
    dots: list
    gaps: list
    width: int
    height: int
    # Остання прочитана температура, якщо вона свіжа (не старша за дірку
    # графіка). Старої «голови» не малюємо: вона казала б «зараз» про минуле.
    head_x: Optional[float] = None
    head_y: Optional[float] = None


def curve_from_readings(readings, *, started_at: datetime, ended_at: datetime) -> Optional[TvCurve]:
    """Показання → крива. Чиста функція; None — показувати нічого."""
    from app.services.furnace import HISTORY_GAP_SECONDS, build_day_chart

    chart = build_day_chart(readings, started_at=started_at, ended_at=ended_at)
    if not chart.has_data:
        return None
    head_x = head_y = None
    last = next(
        (r for r in reversed(readings) if r.captured_at is not None and r.temp_c is not None),
        None,
    )
    if last is not None and (ended_at - last.captured_at).total_seconds() <= HISTORY_GAP_SECONDS:
        span = max(1.0, (ended_at - started_at).total_seconds())
        share = min(1.0, max(0.0, (last.captured_at - started_at).total_seconds() / span))
        head_x = round(share * 100, 2)
        head_y = round((1 - last.temp_c / chart.top_c) * 100, 2)
    return TvCurve(
        lines=list(chart.lines),
        # Одиночні показання — у відсотках плитки, як і «голова»: коло у
        # розтягнутому SVG стало б еліпсом.
        dots=[
            (round(d.x / chart.width * 100, 2), round(d.y / chart.height * 100, 2))
            for d in chart.dots
        ],
        gaps=list(chart.gaps),
        width=chart.width,
        height=chart.height,
        head_x=head_x,
        head_y=head_y,
    )


def furnace_curve(db: Session, key: str, *, now: Optional[datetime] = None) -> Optional[TvCurve]:
    """Крива печі за останні `TV_CURVE_HOURS` годин.

    Час — наївний локальний, як його пише `FurnaceReading` (годинник цеху), а
    не `business_now()`: інакше вікно зсунулось би на пояс відносно показань.
    """
    from app.services.furnace import day_readings

    ended_at = now or datetime.now()
    started_at = ended_at - timedelta(hours=TV_CURVE_HOURS)
    readings = day_readings(db, key, now=ended_at, hours=TV_CURVE_HOURS)
    return curve_from_readings(readings, started_at=started_at, ended_at=ended_at)


def _cached_curve(db: Session, key: str) -> Optional[TvCurve]:
    stamp = time.monotonic()
    with _curve_lock:
        hit = _curve_cache.get(key)
        if hit is not None and stamp - hit[0] < _CURVE_TTL_SECONDS:
            return hit[1]
    curve = furnace_curve(db, key)
    with _curve_lock:
        _curve_cache[key] = (stamp, curve)
    return curve


def furnace_heat(kind: str, temp: Optional[int]) -> Optional[float]:
    """Сила жару над піччю в роботі (0.25…1) — від прочитаної температури.

    Лише для `run`: жар над вільною піччю казав би «пече» про холодну. Не
    прочитали температуру — тихий жар, а не повний: сила тут теж показання.
    """
    if kind != "run":
        return None
    if temp is None:
        return 0.35
    return round(min(1.0, max(0.25, temp / 1500)), 2)


@dataclass
class ShopFurnace:
    """Плитка печі на табло цеху: картка табло логістів + жар і крива.

    Поля картки ПЕРЕПИСАНІ, а не загорнуті: `BoardCard` — спільний тип двох
    екранів, і додавати в нього криву, якої табло логістів не малює, означало
    б тягнути показання з бази й для них."""

    name: str
    kind: str
    temp: Optional[int] = None
    open_day: str = ""
    open_at: str = ""
    done_iso: str = ""
    note: str = ""
    heat: Optional[float] = None
    curve: Optional[TvCurve] = None


@dataclass
class ShopView:
    now: datetime
    machines: list[ShopMachine] = field(default_factory=list)
    sisma: Optional[ShopMachine] = None
    # Печі приходять готовими картками з табло логістів — один збирач на два
    # екрани, щоб числа на них не могли розійтись (`ShopFurnace`).
    furnaces: list = field(default_factory=list)
    nearest_name: str = ""
    nearest_at: str = ""
    nearest_day: str = ""
    nearest_iso: str = ""
    # Стиль малюнка в полотні SISMA (`SISMA_STYLES`); плитку не змінює.
    sisma_style: str = DEFAULT_SISMA_STYLE

    @property
    def columns(self) -> int:
        """Скільки колонок сітки. Рахується від КІЛЬКОСТІ верстатів, а не
        зашито п'ятіркою: у цеху їх то дев'ять, то десять, і жорстка сітка
        або лишала діру, або давила останній ряд."""
        total = len(self.machines) + (1 if self.sisma else 0)
        if total <= 4:
            return max(total, 1)
        if total <= 6:
            return 3
        if total <= 12:
            return (total + 1) // 2
        return (total + 2) // 3

    @property
    def sisma_span(self) -> int:
        """Скільки клітинок займає картка SISMA. Вона завжди остання в сітці,
        тож забирає вільні клітинки останнього ряду (власник 05.10.26: «якщо
        одна клітинка вільна — нехай сісма займає вільне місце»). Немає
        вільних (додали верстат) — звичайна одна клітинка."""
        if not self.sisma:
            return 1
        total = len(self.machines) + 1
        free = (-total) % self.columns
        return 1 + free


def shop_links(db: Session) -> list[str]:
    """Посилання на табло цеху — ті самі адреси й секрет, що в печей.

    Окрема функція, а не параметр до `furnace_board.board_links`: ту читають
    ще й налаштування з MCP, і зміна її сигнатури зачепила б місця, які до
    телевізора не мають стосунку.
    """
    from app.services import furnace_board

    token = furnace_board.board_token(db)
    if not token:
        return []
    return [
        f"http://{address}:{furnace_board.BOARD_PORT}/t/{token}/shop"
        for address in furnace_board.lan_addresses()
    ]


def _works_of(card, *, last: bool = False) -> tuple[list[ShopWork], str]:
    """Роботи картки: до `MAX_NAMED_WORKS` поіменно і «ще N» за рештою.

    Один проєкт Sum3D може містити кілька робіт (див. `MachineCard.orders`).
    Раніше табло називало ПЕРШУ, а решту рахувало числом — і на екрані, куди
    дивляться, щоб зрозуміти, чия це заготовка, другий клієнт лишався
    безіменним. Тепер називаємо дві; вигадувати спільного клієнта для трьох
    різних робіт так само не можна, тож із третьої йде «ще N».
    """
    field_name = "last_orders" if last else "orders"
    orders = list(getattr(card, field_name, None) or [])
    if not orders:
        return [], ""
    # Порядок ФІКСУЄМО за id. `snapshot()` збирає роботи двома проходами
    # (звичайні й переробки) і без сортування, бо екран «Верстати» показує
    # їх усі — там черговість байдужа. Тут показуються ПЕРШІ дві, і без цього
    # рядка імена на телевізорі могли б мінятись між оновленнями, хоча на
    # верстаті нічого не змінилось.
    orders.sort(key=lambda o: (getattr(o, "id", 0) or 0))
    works: list[ShopWork] = []
    for order in orders:
        # Клієнт є не в кожної роботи: у лабораторних його немає взагалі, там
        # робота впізнається НОМЕРОМ НАРЯДУ. Той самий порядок, що на екрані
        # «Верстати». Без запасного варіанта рядок лишався порожнім, і на
        # телевізорі було видно лише матеріал (скарга з цеху 14.09.26).
        name = (getattr(order, "client_name", "") or "").strip()
        tech = ""
        if not name:
            name = (getattr(order, "work_order_no", "") or "").strip()
            tech = (getattr(order, "technician_name", "") or "").strip()
        bits = [
            (getattr(order, "material_color", "") or "").strip(),
            (getattr(order, "quantity", "") or "").strip(),
            (getattr(order, "kind", "") or "").strip(),
        ]
        material = " · ".join(b for b in bits if b)
        # Робота без імені Й без матеріалу не рядок, а порожнє місце: колись
        # вона робила `has_work` істинним, і картка малювала «—» замість
        # пояснення стану.
        if name or material:
            works.append(ShopWork(name=name, material=material, tech=tech))
    shown = works[:MAX_NAMED_WORKS]
    rest = len(works) - len(shown)
    return shown, (f"ще {rest}" if rest else "")


def _portrait_of(card, token: str) -> str:
    """Адреса картинки верстата: спершу ЙОГО фото, інакше дефолт моделі.

    Той самий порядок, що на екрані «Верстати»: фото, завантажене в
    Налаштуваннях (`app/machine_portraits.py`), має перевагу, бо реальні
    машини виглядають інакше, ніж згенерований портрет. Але коли фото ще
    не завантажили — картка не лишається порожньою: дефолт моделі краще за
    голий фон, і саме так поводиться решта застосунку.

    Адреса фото своя, а не `/machines/portrait/...`: у табло немає маршрутів
    головного застосунку, і бути не повинно.
    """
    from app.machine_portraits import portrait_version
    from app.services.machines import machine_model_key

    machine_id = getattr(card.target, "machine_id", None)
    if machine_id is not None:
        version = portrait_version(machine_id)
        if version is not None:
            return f"/t/{token}/portrait/{machine_id}.jpg?v={version}"
    key = machine_model_key(
        getattr(card.target, "name", "") or "",
        getattr(card.target, "portrait_model", "") or "",
    )
    return f"/static/img/machine-portrait-{key}.jpg"


# Sum3D-хвіст назви програми — час `HH-MM-SS`. `(?<!\d)` обовʼязковий: без нього
# назва, що закінчується ДАТОЮ (`…_2026-10-12`), віддала б хвіст `26-10-12` і
# розрізала рік навпіл.
_PROGRAM_SUM3D_TAIL = re.compile(r"(?P<head>.*?)(?<!\d)(?P<tail>\d{2}-\d{2}-\d{2})", re.S)
_ISO_EXT = re.compile(r"\.iso$", re.I)


def program_line(iso_name) -> tuple[str, str]:
    """Назва програми верстата для картки: (голова, Sum3D-хвіст).

    «2_18-Emotions-a1-x193_2026-10-12_09-09-40.iso» дає
    («2_18-Emotions-a1-x193_2026-10-12_», «09-09-40»). Розширення `.iso`
    (будь-який регістр) відкидається — воно нічого не каже оку з проходу.
    Хвіст — завершальний `HH-MM-SS`: за ним оператор шукає диск і звіряє з
    рядком Sum3D у таблиці, тому шаблон підсвічує його окремо.

    Хвоста немає (інша назва, дата без часу) — (вся назва, ""): краще
    показати назву як є, ніж вигадати межу. Порожнє/не рядок — ("", ""), і
    картка лишається з рядком `sum3d`. Чисте перетворення рядка: екранування
    — справа шаблону, тож Markup звідси не повертаємо.
    """
    if not isinstance(iso_name, str):
        return "", ""
    name = _ISO_EXT.sub("", iso_name.strip()).strip()
    if not name:
        return "", ""
    match = _PROGRAM_SUM3D_TAIL.fullmatch(name)
    if match is None:
        return name, ""
    return match.group("head"), match.group("tail")


def _machine(card, token: str) -> ShopMachine:
    state = card.state_key
    item = ShopMachine(
        key=card.key,
        name=card.target.name,
        state=state,
        tone=_TONES.get(state, "calm"),
        word=card.state_word,
        note=card.state_note,
        percent=card.percent,
        sum3d=card.sum3d_id or "",
        portrait=_portrait_of(card, token),
    )
    item.works, item.extra = _works_of(card)
    # Повна назва програми замість шести цифр Sum3D (власник 12.10.26, макет
    # B). `MachineCard.iso_name` уже None для застарілого кадру й обриву
    # зв'язку, тож стара назва не стане «поточною». `getattr`, бо в тестах
    # картки — SimpleNamespace без цього поля. У стані `done` назва, якщо
    # екран її ще тримає, лишається (вона називає диск, який треба зняти);
    # зникла — картка показує звичайний рядок sum3d.
    item.prog_head, item.prog_tail = program_line(getattr(card, "iso_name", None))
    # Лише роботи, що стоять на шпинделі ЗАРАЗ (`orders`), не щойно зняті.
    item.units = disc_units(getattr(card, "orders", None) or [])
    if not item.works:
        # Верстат щойно доробив, а програма вже зникла з екрана (на новому
        # поколінні SUMMARY назви не несе). Тоді картка називає ОСТАННЮ роботу
        # — інакше два завершені верстати в одному ряду виглядають як різні
        # системи: один із нарядом, другий порожній (скарга з цеху 15.09.26).
        # `last_sum3d_id` сам віддається лише в стані «завершено», тож стара
        # назва не може залипнути на живому верстаті.
        item.works, item.extra = _works_of(card, last=True)
        item.works_are_last = bool(item.works)
    if state == "done":
        # Завершений верстат мусить казати це САМИМ РЯДКОМ під назвою, а не
        # лише словом у кутку: доти фразу «програма завершена · зняти» бачив
        # тільки той верстат, чия робота НЕ зчепилась із чергою, а решта
        # показувала клієнта — і зі сторони виглядала як робоча.
        item.action = "завершено"
        # Заклик до дії їде вниз великим словом (`ЗНЯТИ` у шаблоні), тож у
        # поясненні він зайвий. Ріжемо тут, а не в `machines.state_note`: той
        # самий текст читають екран «Верстати» й смуга черги, де низу немає.
        item.note = item.note.split(" · ")[0]
    return item


def _sisma(card, token: str) -> ShopMachine:
    item = _machine(card, token)
    item.is_sisma = True
    layers = card.layers
    if layers:
        item.layer, item.layers_total = layers
    # Час кінця друку — прогноз САМОЇ машини (`ends_at`), тому й показуємо
    # його як є. Свій ми б не порахували: швидкість шару не стала.
    ends = getattr(card, "ends_at", None)
    if ends is not None:
        item.ends_at = ends.strftime("%H:%M")
    item.left_text = (getattr(card, "left_text", "") or "").strip()
    return item


def shop_view(
    db: Session,
    *,
    token: str = "",
    now: Optional[datetime] = None,
    cards=None,
    furnace_view=None,
    curves: Optional[dict] = None,
    sisma_style: Optional[str] = None,
) -> ShopView:
    """Зібрати все, що показує табло цеху.

    `cards`/`furnace_view`/`curves` — для тестів; `curves` — крива за НАЗВОЮ
    печі (у тестових картках немає ключа печі). `sisma_style` — для тестів;
    інакше стиль читається з налаштувань.
    """
    from app.services import furnace_board
    from app.services.machines import snapshot

    now = now or business_now()
    source = snapshot(db) if cards is None else list(cards)
    view = ShopView(now=now)
    view.sisma_style = (normalize_sisma_style(sisma_style) if sisma_style is not None
                        else read_sisma_style(db))

    for card in source:
        # Верстат, прибраний із табло в Налаштуваннях, сюди не потрапляє —
        # але опитуватись не перестає: `show_on_board` про місце на екрані,
        # а не про те, стежимо ми за ним чи ні.
        if not getattr(card.target, "show_on_board", True):
            continue
        if card.is_sisma_machine:
            # Принтер лишається на табло і тоді, коли зв'язок обірвався:
            # саме тоді він найпотрібніший (та сама причина, що у віджеті).
            view.sisma = _sisma(card, token)
        else:
            view.machines.append(_machine(card, token))

    fv = furnace_board.board_view(db, now=now) if furnace_view is None else furnace_view
    for fc in fv.cards:
        kind = getattr(fc, "kind", "")
        temp = getattr(fc, "temp", None)
        curve = None
        if kind == "run":
            key = getattr(fc, "key", "") or ""
            if curves is not None:
                curve = curves.get(fc.name)
            elif db is not None and key:
                curve = _cached_curve(db, key)
        view.furnaces.append(ShopFurnace(
            name=fc.name,
            kind=kind,
            temp=temp,
            open_day=getattr(fc, "open_day", "") or "",
            open_at=getattr(fc, "open_at", "") or "",
            done_iso=getattr(fc, "done_iso", "") or "",
            note=getattr(fc, "note", "") or "",
            heat=furnace_heat(kind, temp),
            curve=curve,
        ))
    view.nearest_name = fv.nearest_name
    view.nearest_at = fv.nearest_at
    view.nearest_day = fv.nearest_day
    view.nearest_iso = fv.nearest_iso
    return view
