"""Запобіжник на масові стирання рядків таблиці (B.7).

Звірка позиції (`_resolve_row`) захищає ОДНЕ стирання: цей рядок досі цієї
роботи. Від розгону — циклу, повторного відкату, клацання підряд — вона не
рятує ніяк, бо кожне окреме стирання законне. Тут перевіряється стеля.
"""

from datetime import datetime, timedelta

import pytest

from app import sheet_erase_guard
from app.sheet_erase_guard import SheetEraseBlocked


def test_under_limit_passes():
    for _ in range(sheet_erase_guard.LIMIT - 1):
        sheet_erase_guard.check()
        sheet_erase_guard.record()
    sheet_erase_guard.check()  # не кидає


def test_limit_blocks_further_erases():
    for _ in range(sheet_erase_guard.LIMIT):
        sheet_erase_guard.record()

    with pytest.raises(SheetEraseBlocked) as exc:
        sheet_erase_guard.check()

    assert exc.value.limit == sheet_erase_guard.LIMIT
    assert "стирання зупинено" in str(exc.value)


def test_window_slides_and_releases_after_an_hour():
    long_ago = datetime(2026, 9, 7, 8, 0, 0)
    for _ in range(sheet_erase_guard.LIMIT):
        sheet_erase_guard.record(now=long_ago)

    with pytest.raises(SheetEraseBlocked):
        sheet_erase_guard.check(now=long_ago + timedelta(minutes=59))

    # Година минула — старі стирання вже не рахуються.
    sheet_erase_guard.check(now=long_ago + timedelta(hours=1, seconds=1))
    assert sheet_erase_guard.recent_count(now=long_ago + timedelta(hours=1, seconds=1)) == 0


def test_only_actual_erases_count():
    """Пропущене стирання (рядок не підтверджено) не має їсти стелю."""
    for _ in range(sheet_erase_guard.LIMIT * 2):
        sheet_erase_guard.check()

    assert sheet_erase_guard.recent_count() == 0
