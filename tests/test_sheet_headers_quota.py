"""Дві сторожі синку з кроку S.2 аудиту 05.09.26.

**Заголовки (H-6).** Позиції колонок зашиті числами. Варто комусь вставити
колонку в таблицю — і кожен індекс зсувається мовчки: «Ім'я техніка» читається
як Sum3D, усі роботи дня стають «прийнято», частина зникає з черги як
не-фрезерна, а запис Sum3D лягає в чужу колонку. Один зсув псує і читання, і
запис, тому вкладку зі зсувом краще НЕ імпортувати взагалі.

**Квота (H-7).** «Турбо» (тік 5 с) × до 4 вкладок × 2 виклики ≈ 100 запитів на
хвилину при ліміті Google 60. Це не випадковий 429, а систематичний: на квоті
`call_with_retry` спить 20/40/60 с, тримаючи `_sync_lock`, і ручний синк ці дві
хвилини відповідає «вже виконується».
"""

import csv
import io
from pathlib import Path
from unittest.mock import Mock

from sqlalchemy import select

from app.business_day import business_today
from app.models import SyncLog
from app.parser import HEADER_ANCHORS, header_mismatches
from app.sheet_sync_service import (
    header_mismatch_pending,
    sync_google_sheets,
    sync_hot_tab,
)
from app.sheets import (
    GOOGLE_QUOTA_SOFT_LIMIT,
    _record_api_call,
    api_calls_last_minute,
    quota_is_tight,
    reset_api_call_counter,
)

# Ці ж копії вкладок лежать у backups/sheets — справжні заголовки лабораторії,
# з їхніми одруківками («Вид работи», «Номер работи»).
REAL_HEADER = [
    "№", "Номер наряду", "Кількість", "Колір роботи", "Вид работи", "Здати до",
    "", "", "Номер работи", "Ім'я техніка",
    "Коментар для cam оператора (що саме потрібно фрезереувати та т.і.)",
    "ID", "Прорахував", "Відфрезерував", "БРАК (переробкаи)",
]


def _sheet(header=None, data_rows=1):
    """Аркуш: рядок 1 — підсумки, рядок 2 — заголовки, далі порожні до 7-го."""
    rows = [["", "", "", "", "Всього", "0"]]
    rows.append(list(header if header is not None else REAL_HEADER))
    rows.extend([[] for _ in range(4)])
    rows.extend([["1", "24122", "2", "моно а3", "анатомія"] for _ in range(data_rows)])
    return rows


class TestHeaderAnchors:
    def test_real_backups_all_pass(self):
        """Найважливіше: якорі описують РЕАЛЬНУ таблицю, а не уявну. Якщо цей
        тест червоний — виправляти треба якорі, а не таблицю."""
        backups = sorted((Path(__file__).resolve().parents[1] / "backups" / "sheets").glob("*.csv"))
        if not backups:
            return  # копій у цьому дереві немає — перевіряти нема на чому
        for path in backups:
            with io.open(path, encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
            assert header_mismatches(rows) == [], f"{path.name}: {header_mismatches(rows)}"

    def test_inserted_column_is_detected(self):
        rows = _sheet()
        shifted = [row[:3] + ["Артикул"] + row[3:] for row in rows]
        problems = header_mismatches(shifted)
        assert problems, "вставлену колонку не помічено"
        assert any("колір роботи" in p for p in problems)

    def test_empty_tab_is_not_a_mismatch(self):
        """Порожня вкладка — не зсув: там просто нема чого звіряти."""
        assert header_mismatches([]) == []
        assert header_mismatches([[], [], []]) == []

    def test_typos_and_case_do_not_matter(self):
        header = list(REAL_HEADER)
        header[1] = "  НОМЕР НАРЯДУ  "
        header[9] = "Імʼя техніка"  # інший апостроф
        assert header_mismatches(_sheet(header)) == []

    def test_anchors_cover_the_columns_the_writes_use(self):
        """Sum3D (11) і маркери (12, 13) — те, у що ми ПИШЕМО. Якір на них
        обовʼязковий: помилитись колонкою при записі гірше, ніж при читанні."""
        assert {1, 12, 13} <= set(HEADER_ANCHORS)


class TestSyncRefusesAShiftedTab:
    def _spreadsheet(self, monkeypatch, rows):
        today = business_today()
        worksheet = Mock()
        worksheet.title = today.strftime("%d.%m.%y")
        worksheet.get_all_values.return_value = rows
        spreadsheet = Mock()
        spreadsheet.worksheets.return_value = [worksheet]
        monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)
        monkeypatch.setattr("app.sheet_sync_service.get_google_sheet_id", lambda session: "id")
        monkeypatch.setattr(
            "app.sheet_sync_service.get_google_service_account_json",
            lambda session: '{"type": "service_account"}',
        )
        monkeypatch.setattr("app.sheet_sync_service.fetch_row_fills", lambda ws: {})
        return worksheet, today

    def test_shifted_tab_is_not_imported_and_raises_the_banner(self, db_session, monkeypatch):
        rows = _sheet(data_rows=3)
        shifted = [row[:3] + ["Артикул"] + row[3:] for row in rows]
        worksheet, _ = self._spreadsheet(monkeypatch, shifted)

        result = sync_google_sheets(db_session)

        assert result.created == 0, "зсунуту вкладку не можна імпортувати"
        assert worksheet.title in header_mismatch_pending()
        logs = db_session.scalars(select(SyncLog)).all()
        assert any("структура вкладки не впізнана" in log.message for log in logs)

    def test_a_healthy_tab_clears_the_banner(self, db_session, monkeypatch):
        worksheet, _ = self._spreadsheet(monkeypatch, _sheet(data_rows=2))

        sync_google_sheets(db_session)

        assert worksheet.title not in header_mismatch_pending()


class TestQuotaBrake:
    def test_counter_sees_recent_calls(self):
        reset_api_call_counter()
        for _ in range(5):
            _record_api_call()
        assert api_calls_last_minute() == 5
        assert not quota_is_tight()

    def test_brake_engages_at_the_soft_limit(self):
        reset_api_call_counter()
        for _ in range(GOOGLE_QUOTA_SOFT_LIMIT):
            _record_api_call()
        assert quota_is_tight()

    def test_soft_limit_leaves_headroom_under_googles_60(self):
        """Між нашою перевіркою і самим запитом ще встигає пройти фоновий тік,
        тож гальмувати впритул до 60 марно."""
        assert GOOGLE_QUOTA_SOFT_LIMIT < 60

    def test_old_calls_fall_out_of_the_window(self):
        reset_api_call_counter()
        _record_api_call(now=0.0)
        assert api_calls_last_minute(now=1.0) == 1
        assert api_calls_last_minute(now=120.0) == 0

    def test_hot_tick_skips_itself_when_the_quota_is_tight(self, db_session, monkeypatch):
        """Пропущений цикл дешевший за 429: на квоті call_with_retry спить
        20/40/60 с, тримаючи замок синку."""
        opened = []
        monkeypatch.setattr(
            "app.sheet_sync_service.open_spreadsheet",
            lambda db: opened.append(1),
        )
        monkeypatch.setattr("app.sheet_sync_service.quota_is_tight", lambda: True)

        assert sync_hot_tab(db_session) is None
        assert opened == [], "гарячий тік не мав навіть відкривати таблицю"

    def test_hot_tick_runs_when_there_is_room(self, db_session, monkeypatch):
        today = business_today()
        worksheet = Mock()
        worksheet.title = today.strftime("%d.%m.%y")
        worksheet.get_all_values.return_value = _sheet(data_rows=1)
        spreadsheet = Mock()
        monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)
        monkeypatch.setattr(
            "app.sheet_sync_service.get_worksheet_by_name",
            lambda ss, title: worksheet if title == worksheet.title else None,
        )
        monkeypatch.setattr("app.sheet_sync_service.get_google_sheet_id", lambda session: "id")
        monkeypatch.setattr(
            "app.sheet_sync_service.get_google_service_account_json",
            lambda session: '{"type": "service_account"}',
        )
        monkeypatch.setattr("app.sheet_sync_service.fetch_row_fills", lambda ws: {})

        summary = sync_hot_tab(db_session, today=today)

        assert summary is not None and summary.tabs_processed >= 1
