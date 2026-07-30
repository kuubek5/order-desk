"""v0.1 — read-only sanity check.

Connects to the test spreadsheet, finds today's tab (or falls back), and
prints raw rows so header/data row numbers can be confirmed by eye before
any parsing logic is written.
"""

import sys
from datetime import date

from app.sheets import get_worksheet_by_date, open_spreadsheet, tab_name_for


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    spreadsheet = open_spreadsheet()

    titles = [ws.title for ws in spreadsheet.worksheets()]
    print(f"Вкладки в таблиці ({len(titles)}): {titles}")

    today = date.today()
    wanted = tab_name_for(today)
    worksheet = get_worksheet_by_date(spreadsheet, today)

    if worksheet is None:
        print(f"\nВкладку '{wanted}' (сьогодні) не знайдено.")
        if not titles:
            print("Таблиця порожня.")
            return
        fallback_title = titles[-1]
        print(f"Беру останню вкладку для перевірки: '{fallback_title}'")
        worksheet = spreadsheet.worksheet(fallback_title)
    else:
        print(f"\nЗнайдено вкладку сьогодні: '{wanted}'")

    rows = worksheet.get_all_values()
    print(f"\nВсього рядків: {len(rows)}. Перші 10 рядків (сирі, з номером рядка):\n")
    for i, row in enumerate(rows[:10], start=1):
        print(f"{i:>2}: {row}")


if __name__ == "__main__":
    main()
