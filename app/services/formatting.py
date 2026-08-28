"""Ukrainian-language formatting helpers.

Pure presentation: no database, no request, no I/O. Kept apart from the HTTP
layer so any screen (queue, archive, settings) can format the same way without
importing the web module.
"""

from datetime import datetime

# Ukrainian month names (nominative) for the Archive screen's month headings.
UK_MONTHS = [
    "", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]


def uk_month_label(year: int, month: int) -> str:
    return f"{UK_MONTHS[month]} {year}"


def pluralize_uk(n: int, one: str, few: str, many: str) -> str:
    """Ukrainian has three plural forms selected by the last one/two digits
    of the count (1 клієнт, 2 клієнти, 5 клієнтів, 11 клієнтів, 21 клієнт…)."""
    n_mod_100 = n % 100
    n_mod_10 = n % 10
    if n_mod_10 == 1 and n_mod_100 != 11:
        return one
    if 2 <= n_mod_10 <= 4 and not (12 <= n_mod_100 <= 14):
        return few
    return many


def relative_time_uk(reference: datetime, now: datetime) -> str:
    """"N хв тому" / "N год тому" — no reusable relative-time helper exists
    elsewhere in this codebase (received_at etc. are all rendered as absolute
    "%d.%m.%y %H:%M" timestamps), so this is a small new one."""
    seconds = max(0, int((now - reference).total_seconds()))
    minutes = seconds // 60
    if minutes < 1:
        return "щойно"
    if minutes < 60:
        unit = pluralize_uk(minutes, "хвилину", "хвилини", "хвилин")
        return f"{minutes} {unit} тому"
    hours = minutes // 60
    unit = pluralize_uk(hours, "годину", "години", "годин")
    return f"{hours} {unit} тому"
