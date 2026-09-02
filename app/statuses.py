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

# Колір статус-крапки в рядку черги. Друге значення — «порожня» (кільце, не
# заливка): «нове» ще не має тіла роботи, тому лише контур. ЄДИНЕ джерело —
# і рядок (_order_row.html), і легенда (_status_legend.html) читають цю мапу
# через глобал `status_dot`, тож кольори не можуть розійтись.
STATUS_DOT = {
    "нове": ("#b6c6da", True),
    "прийнято": ("var(--accent-d)", False),
    "прораховано": ("var(--accent)", False),
    "у фрезеруванні": ("var(--accent-b)", False),
    "відфрезеровано": ("var(--accent-c)", False),
    "знайдено при видачі": ("var(--accent-e)", False),
    "видано": ("#5c6b80", False),
    "проблема": ("var(--alarm)", False),
    "переробка": ("var(--warn)", False),
}


def status_dot(status: str) -> tuple[str, bool]:
    """(колір, порожня-крапка) для статусу. Невідомий статус — тихий сірий."""
    return STATUS_DOT.get(status, ("#8fa3bb", False))


def is_overdue(sheet_tab: str | None, status: str) -> bool:
    if not sheet_tab or status in FINAL_STATUSES:
        return False
    try:
        order_date = datetime.strptime(sheet_tab, "%d.%m.%y").date()
    except ValueError:
        return False
    return order_date < date.today()
