"""Сирі знімки вкладок Google-таблиці (app/sheet_backup.py)."""

from datetime import date

import pytest

import app.sheet_backup as sb
from app.parser import HEADER_ROWS


def _work_rows(nums):
    """Побудувати сирий масив вкладки: HEADER_ROWS порожніх шапок + робочі рядки
    (наряд у колонці 1)."""
    rows = [["", "", "", "", ""] for _ in range(HEADER_ROWS)]
    for n in nums:
        rows.append(["", str(n), "1", "цирконій", "анатомія"])
    return rows


class _FakeWS:
    def __init__(self, title, rows):
        self.title = title
        self._rows = rows


class _FakeSpreadsheet:
    def __init__(self, sheets):
        self._sheets = sheets

    def worksheets(self):
        return self._sheets


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Підмінити доступ до Google фейком і скерувати знімки в tmp."""
    state = {"sheets": []}

    def _open(db):
        return _FakeSpreadsheet(state["sheets"])

    def _read(ws, chunk_rows=50):
        return ws._rows

    monkeypatch.setattr("app.sheets.open_spreadsheet", _open, raising=True)
    monkeypatch.setattr("app.sheets.read_all_values", _read, raising=True)
    monkeypatch.setattr("app.sheets.call_with_retry", lambda fn, **k: fn(), raising=True)
    monkeypatch.setattr(sb, "get_google_sheet_id", lambda db: "SHEET123")
    # db_path — саме ФАЙЛ; sheets_backup_dir бере .parent, тож так тека знімків
    # унікальна на кожен тест (інакше .parent = спільний tmp корінь).
    return state, tmp_path / "kuubmill.db"


def test_writes_csv_for_tab_with_data(patched):
    state, dbp = patched
    state["sheets"] = [_FakeWS("22.07.26", _work_rows([24122, 24123]))]

    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 22))

    assert result.ok
    assert result.written == 1
    csv = (sb.sheets_backup_dir(dbp) / "2026-07-22.csv").read_bytes()
    assert b"24122" in csv and b"24123" in csv
    snaps = sb.list_snapshots(dbp)
    assert len(snaps) == 1
    assert snaps[0].tab == "22.07.26"
    assert snaps[0].rows == 2


def test_empty_tab_is_not_stored(patched):
    state, dbp = patched
    state["sheets"] = [_FakeWS("22.07.26", _work_rows([]))]  # лише шапка

    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 22))

    assert result.written == 0
    assert result.skipped_empty == 1
    assert not (sb.sheets_backup_dir(dbp) / "2026-07-22.csv").exists()


def test_no_sheet_id_returns_error(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "get_google_sheet_id", lambda db: "")
    result = sb.snapshot_all_tabs(object(), tmp_path)
    assert not result.ok
    assert "Sheet ID" in result.error


def test_old_tab_not_reread_but_recent_is(patched):
    state, dbp = patched
    # старий день уже знятий: створимо файл заздалегідь
    state["sheets"] = [_FakeWS("01.07.26", _work_rows([1]))]
    sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 1))

    # через два тижні той самий старий день не перечитується (кеш),
    # а свіжий — знімається
    state["sheets"] = [
        _FakeWS("01.07.26", _work_rows([1, 999])),   # змінився, але старий
        _FakeWS("15.07.26", _work_rows([500])),       # свіжий
    ]
    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 16))
    assert result.skipped_cached == 1
    assert result.written == 1
    # старий файл лишився старим (не перечитаний)
    old = (sb.sheets_backup_dir(dbp) / "2026-07-01.csv").read_bytes()
    assert b"999" not in old


def test_force_rereads_old_tab(patched):
    state, dbp = patched
    state["sheets"] = [_FakeWS("01.07.26", _work_rows([1]))]
    sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 1))

    state["sheets"] = [_FakeWS("01.07.26", _work_rows([1, 999]))]
    result = sb.snapshot_all_tabs(object(), dbp, force=True, today=date(2026, 7, 20))
    assert result.written == 1
    old = (sb.sheets_backup_dir(dbp) / "2026-07-01.csv").read_bytes()
    assert b"999" in old


def test_disappeared_tab_is_flagged_but_kept(patched):
    state, dbp = patched
    state["sheets"] = [_FakeWS("22.07.26", _work_rows([24122]))]
    sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 22))

    # наступний прохід — вкладки в Google більше немає
    state["sheets"] = []
    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 8, 1))
    assert result.disappeared == 1
    # копія на місці
    assert (sb.sheets_backup_dir(dbp) / "2026-07-22.csv").exists()
    snap = sb.list_snapshots(dbp)[0]
    assert snap.disappeared_at is not None


def test_truncated_read_does_not_overwrite_full_copy(patched):
    state, dbp = patched
    full = list(range(1, 21))  # 20 робіт
    state["sheets"] = [_FakeWS("15.07.26", _work_rows(full))]
    sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 15))

    # наступний прохід — проксі віддав лише 3 рядки (обрізано)
    state["sheets"] = [_FakeWS("15.07.26", _work_rows([1, 2, 3]))]
    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 16))
    assert result.held_shrink == 1
    assert result.written == 0
    # повна копія на місці — не затерта короткою
    csv = (sb.sheets_backup_dir(dbp) / "2026-07-15.csv").read_bytes()
    assert b"20" in csv


def test_small_drop_overwrites_normally(patched):
    state, dbp = patched
    state["sheets"] = [_FakeWS("15.07.26", _work_rows(list(range(1, 21))))]
    sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 15))
    # прибрали 2 рядки — реальна дрібна зміна, має записатись
    state["sheets"] = [_FakeWS("15.07.26", _work_rows(list(range(1, 19))))]
    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 16))
    assert result.written == 1
    assert result.held_shrink == 0
    assert sb.list_snapshots(dbp)[0].rows == 18


def test_growth_overwrites(patched):
    state, dbp = patched
    state["sheets"] = [_FakeWS("15.07.26", _work_rows([1, 2, 3]))]
    sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 15))
    state["sheets"] = [_FakeWS("15.07.26", _work_rows(list(range(1, 21))))]
    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 16))
    assert result.written == 1
    assert sb.list_snapshots(dbp)[0].rows == 20


def test_download_and_zip(patched):
    state, dbp = patched
    state["sheets"] = [
        _FakeWS("22.07.26", _work_rows([1])),
        _FakeWS("05.08.26", _work_rows([2])),
    ]
    sb.snapshot_all_tabs(object(), dbp, today=date(2026, 8, 5))

    one = sb.read_snapshot_bytes(dbp, "2026-07-22.csv")
    assert one is not None and b"1" in one

    zbytes, count = sb.build_zip(dbp, month="2026-07")
    assert count == 1
    zall, count_all = sb.build_zip(dbp)
    assert count_all == 2


def test_safe_path_rejects_traversal(patched):
    _, dbp = patched
    assert sb._safe_snapshot_path(dbp, "../../etc/passwd") is None
    assert sb._safe_snapshot_path(dbp, "manifest.json") is None
    assert sb._safe_snapshot_path(dbp, "not-a-date.csv") is None


def test_non_dated_tabs_ignored(patched):
    state, dbp = patched
    state["sheets"] = [
        _FakeWS("Легенда", _work_rows([1])),
        _FakeWS("22.07.26", _work_rows([2])),
    ]
    result = sb.snapshot_all_tabs(object(), dbp, today=date(2026, 7, 22))
    assert result.written == 1
    assert {s.tab for s in sb.list_snapshots(dbp)} == {"22.07.26"}
