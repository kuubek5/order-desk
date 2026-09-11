from datetime import datetime

# Іменовані константи для статусів РОБОТИ. Рядки лишаються тими самими (вони
# в базі й у таблиці), але порівняння в коді робляться через ім'я: одруківка
# в літералі «прийнято» не падає, а тихо перестає збігатись — і робота просто
# не змінює стан (аудит 05.09.26, S.6).
#
# УВАГА: у листа (`EmailMessage.status`) свій словник, де слова випадково ті
# самі — «нове», «прийнято», «відхилено». Це РІЗНІ доменні поняття (стан листа
# в тріажі ≠ стан роботи в черзі), тому спільних констант для них свідомо
# немає: злиття словників зробило б помилку типу «поставив листу статус
# роботи» непомітною.
STATUS_NEW = "нове"
STATUS_ACCEPTED = "прийнято"
STATUS_CALCULATED = "прораховано"
STATUS_MILLING = "у фрезеруванні"
STATUS_MILLED = "відфрезеровано"
STATUS_FOUND = "знайдено при видачі"
STATUS_ISSUED = "видано"
STATUS_PROBLEM = "проблема"
STATUS_REWORK = "переробка"

STATUSES = [
    STATUS_NEW,
    STATUS_ACCEPTED,
    STATUS_CALCULATED,
    STATUS_MILLING,
    STATUS_MILLED,
    STATUS_FOUND,
    STATUS_ISSUED,
    STATUS_PROBLEM,
    STATUS_REWORK,
]

FINAL_STATUSES = {STATUS_MILLED, STATUS_FOUND, STATUS_ISSUED}

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
    # РОБОЧА дата, не календарна: о 00:30 нічна зміна ще на вчорашньому дні, і
    # календарна північ помічала б усі вчорашні роботи простроченими просто
    # тому, що годинник цокнув (див. app/business_day.py).
    # І порівняння з ВКЛАДКОЮ, а не з датою: у суботу й неділю роботи пишуть у
    # п'ятничну вкладку (власник 11.09.26) — без цього вся п'ятниця разом із
    # роботами вихідних горіла б «простроченою» два дні поспіль.
    from app.business_day import business_tab_today

    return order_date < business_tab_today()
