from datetime import date, datetime

STATUSES = [
    "нове",
    "прийнято",
    "прораховано",
    "у фрезеруванні",
    "відфрезеровано",
    "знайдено при видачі",
    "видано",
    "проблема",
    "переробка",
]

FINAL_STATUSES = {"відфрезеровано", "знайдено при видачі", "видано"}


def is_overdue(sheet_tab: str | None, status: str) -> bool:
    if not sheet_tab or status in FINAL_STATUSES:
        return False
    try:
        order_date = datetime.strptime(sheet_tab, "%d.%m.%y").date()
    except ValueError:
        return False
    return order_date < date.today()
