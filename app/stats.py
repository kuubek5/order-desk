"""«Статистика» — чисті функції без бази (STATS_BRIEF.md, етап 1).

Тут лише математика й правила, які легко перевірити тестом: період і його
робочі дні, ключ імені техніка й клієнта, нормалізація кольору, час Sum3D з
межею доби, кошики карти «день тижня × година», медіана. SQL-агрегати —
`app/services/stats_data.py`, геометрія графіків — `app/services/stats_charts.py`.

`parse_int_safe` живе тут історично й потрібен черзі та виробітку — не
переносити без Grep по `from app.stats import`.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.business_day import tab_day


def parse_int_safe(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


# Звідки рахуються моменти етапу 0 (`Order.row_seen_at`, `Order.job_code_at`):
# до цієї дати їх не записували, і заднім числом вони не відновлюються.
STATS_COLLECT_SINCE = date(2026, 10, 12)

# Рамка «робочі години» для карти й частки «поза робочим часом». Поки
# константа; STATS_BRIEF §5 — пізніше налаштування адміна (дні й години).
WORK_WEEKDAYS = frozenset(range(5))  # Пн–Пт
WORK_HOURS = (9, 18)  # 9:00–18:00, кінець не включно

# Скільки різних днів «появи рядка» треба, щоб карта за ним мала сенс.
ROW_SEEN_MIN_DAYS = 7

PERIOD_DAYS = {"7": 7, "30": 30, "90": 90}
DEFAULT_PERIOD = "30"
MAX_CUSTOM_DAYS = 400

DOW = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @property
    def length(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> Period:
        """Попередній відрізок тієї самої довжини, впритул до цього."""
        end = self.start - timedelta(days=1)
        return Period(end - timedelta(days=self.length - 1), end)

    def working_days(self) -> list[date]:
        """Дні-вкладки періоду: лише Пн–Пт.

        Вкладок за сб/нд немає (п'ятниця несе й вихідні, `tab_day`), тож
        субота й неділя окремими днями не бувають. П'ятниця входить, лише
        коли вона сама в межах періоду: інакше період, що починається в
        суботу, і попередній до нього рахували б ту саму вкладку двічі.
        """
        out = []
        day = self.start
        while day <= self.end:
            if day.weekday() < 5:
                out.append(day)
            day += timedelta(days=1)
        return out


def resolve_period(
    period: str | None, start: str | None, end: str | None, today: date
) -> tuple[str, Period]:
    """`?period=7|30|90|custom&from=&to=` → (ключ, Period). Хибне → 30 днів.

    `today` — `business_today()`, не календарна дата (межа доби 07:30)."""
    key = period if period in PERIOD_DAYS or period == "custom" else DEFAULT_PERIOD
    if key == "custom":
        try:
            first = date.fromisoformat(start or "")
            last = date.fromisoformat(end or "")
        except ValueError:
            key = DEFAULT_PERIOD
        else:
            if first > last:
                first, last = last, first
            if (last - first).days + 1 > MAX_CUSTOM_DAYS:
                first = last - timedelta(days=MAX_CUSTOM_DAYS - 1)
            return key, Period(first, last)
    days = PERIOD_DAYS[key]
    return key, Period(today - timedelta(days=days - 1), today)


# ── Імена техніків ─────────────────────────────────────────────────────────
# Одне ім'я в таблиці пишуть різними розкладками: «с»/«c»/«С», «Gенис».
# Латинські двійники кириличних літер зводимо до кирилиці — але лише тоді,
# коли ім'я вже кириличне або ЦІЛКОМ складається з таких двійників («c»).
# «dim» лишається «dim»: у ньому немає кирилиці, а «d» двійника не має.
_LAT_TWINS = "aceopxyiIACEOPXKMHTBGgk"
_CYR_TWINS = "асеорхуіІАСЕОРХКМНТВГгк"
_LAT2CYR = str.maketrans(_LAT_TWINS, _CYR_TWINS)
_TWIN_SET = frozenset(_LAT_TWINS)

# Різне НАПИСАННЯ одного техніка, яке двійниками не зводиться. Явний
# список, а не правило: власник 12.10.26 назвав «Kira = Кира = кіра» однією
# людиною. Ключ — уже звичайний ключ (`casefold` після двійників), значення —
# ключ групи. Нову людину сюди додавати лише зі слів власника.
TECH_ALIASES = {
    "kira": "кіра",
    "кира": "кіра",
}


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def tech_key(name: str | None) -> str | None:
    """Ключ групи техніка: регістр і латиниця↔кирилиця не розрізняють."""
    text = " ".join((name or "").split())
    if not text:
        return None
    letters = [ch for ch in text if ch.isalpha()]
    if _has_cyrillic(text) or (letters and all(ch in _TWIN_SET for ch in letters)):
        text = text.translate(_LAT2CYR)
    key = text.casefold()
    return TECH_ALIASES.get(key, key)


def client_key(name: str | None) -> str | None:
    """Ключ клієнта: зайві пробіли й регістр не роблять іншого клієнта."""
    text = " ".join((name or "").split())
    return text.casefold() if text else None


def group_spellings(counts: Iterable[tuple[str | None, int]], key_fn) -> dict[str, dict]:
    """Сирі написання → групи `{ключ: {"label", "spellings", "n"}}`.

    Показуємо НАЙЧАСТІШЕ написання групи (за кількістю робіт), а фільтр
    шукає за всіма написаннями (`spellings`)."""
    groups: dict[str, Counter] = {}
    for raw, n in counts:
        key = key_fn(raw)
        if key is None or raw is None:
            continue
        groups.setdefault(key, Counter())[raw] += n
    return {
        key: {
            "label": " ".join(c.most_common(1)[0][0].split()),
            "spellings": sorted(c),
            "n": sum(c.values()),
        }
        for key, c in groups.items()
    }


# ── Колір роботи ───────────────────────────────────────────────────────────
# Кирилиця → латиниця для зведення написань кольору («моно», «пмма», «титан»).
_COLOR_CYR2LAT = str.maketrans("авсеомрхінпти", "abceompxinpti")


def norm_color(text: str | None) -> str | None:
    """«моно А3» = «mono a3», «a35» = «a3.5» — для зведення в картці «Кольори»."""
    if not text:
        return None
    s = text.lower().strip().translate(_COLOR_CYR2LAT)
    s = s.replace(",", ".").replace(" ", "")
    if not s:
        return None
    s = re.sub(r"([abcd])35\b", r"\g<1>3.5", s)
    s = re.sub(r"^(mono|emo|pmma)([abcd])", r"\1 \2", s)
    return s


# ── Час Sum3D ──────────────────────────────────────────────────────────────
# Той самий шаблон, що `app/services/handout.py::_SUM3D_TIME`: у клітинці
# буває кілька ID через перенос рядка, береться перший (коли роботу взяли).
_SUM3D_TIME = re.compile(r"(\d{1,2})-(\d{2})-(\d{2})")


def first_sum3d_time(text: str | None) -> time | None:
    for h, m, s in _SUM3D_TIME.findall(text or ""):
        try:
            return time(int(h), int(m), int(s))
        except ValueError:
            continue
    return None


def sum3d_moment(work_day: date, taken: time, rollover: time) -> datetime:
    """Справжній момент Sum3D: ID несе лише час доби.

    Як у `handout.night_claims_of`: Sum3D раніше за межу доби (07:30) — це
    нічна зміна ПІСЛЯ робочого дня роботи, тобто наступний календарний день.
    """
    day = work_day + timedelta(days=1) if taken < rollover else work_day
    return datetime.combine(day, taken)


def work_day_for_sum3d(tab_date: date, seen_day: date | None) -> date:
    """Робочий день роботи для карти Sum3D.

    Вкладка п'ятниці несе й суботу з неділею. Якщо відомо, коли рядок
    зʼявився (`row_seen_at`, робоча дата) і це вихідний того самого тижня —
    беремо його: інакше субота лягла б на карту п'ятницею."""
    if seen_day is not None and tab_date.weekday() == 4 and tab_day(seen_day) == tab_date:
        return seen_day
    return tab_date


def is_working_hour(weekday: int, hour: int) -> bool:
    return weekday in WORK_WEEKDAYS and WORK_HOURS[0] <= hour < WORK_HOURS[1]


def empty_grid() -> list[list[int]]:
    return [[0] * 24 for _ in range(7)]


def off_hours(grid: list[list[int]]) -> tuple[int, int]:
    """(усього, з них поза робочими годинами)."""
    total = off = 0
    for dow in range(7):
        for hour in range(24):
            value = grid[dow][hour]
            total += value
            if not is_working_hour(dow, hour):
                off += value
    return total, off


# ── Дрібні числа ───────────────────────────────────────────────────────────
def delta_pct(now: float | None, was: float | None) -> int | None:
    if now is None or not was:
        return None
    return round((now - was) / was * 100)


def median(values: Iterable[float]) -> float | None:
    items = list(values)
    return statistics.median(items) if items else None


def format_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    total = int(round(minutes))
    if total < 60:
        return f"{total} хв"
    hours, mins = divmod(total, 60)
    if hours < 48:
        return f"{hours} год {mins:02d} хв" if mins else f"{hours} год"
    return f"{hours / 24:.1f} дн".replace(".", ",")


def nf(value: float | int | None) -> str:
    """Число з вузьким пробілом між тисячами: 5 704."""
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", " ")


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())
