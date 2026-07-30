"""v0.3 — one-shot sync: reads today's (or the latest) tab and upserts into
the local DB. Run manually for now; no scheduling yet.
"""

import sys
from datetime import date

from app.db import Base, engine, get_session
from app.parser import parse_rows
from app.sheets import get_worksheet_by_date, open_spreadsheet, tab_name_for
from app.sync import sync_tab


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    Base.metadata.create_all(engine)

    spreadsheet = open_spreadsheet()
    today = date.today()
    tab_name = tab_name_for(today)
    worksheet = get_worksheet_by_date(spreadsheet, today)

    if worksheet is None:
        titles = [ws.title for ws in spreadsheet.worksheets()]
        if not titles:
            print("Таблиця порожня.")
            return
        tab_name = titles[-1]
        worksheet = spreadsheet.worksheet(tab_name)
        print(f"Вкладку сьогодні не знайдено, синхронізую останню: '{tab_name}'")

    rows = parse_rows(worksheet.get_all_values())
    with get_session() as session:
        result = sync_tab(session, tab_name, rows)

    print(
        f"Синхронізовано '{tab_name}': нових {result.created}, "
        f"оновлено {result.updated}, без змін {result.unchanged}"
    )


if __name__ == "__main__":
    main()
