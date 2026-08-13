"""app/sheets.py thread-local gspread client cache: the expensive client
(token mint + session) is built once per credentials-content per thread, and
a changed service-account JSON transparently rebuilds it. No real google-auth:
_build_client is patched to hand back unique sentinels and count builds."""

import threading
from unittest.mock import MagicMock

import pytest

import app.sheets as sheets


@pytest.fixture(autouse=True)
def _clear_cache():
    sheets.reset_sheets_cache()
    yield
    sheets.reset_sheets_cache()


@pytest.fixture
def counting_build(monkeypatch):
    """Replace _build_client/_build_credentials so no real auth happens and
    every build returns a distinct object we can identity-compare."""
    builds = {"n": 0}

    def fake_build_client(_creds):
        builds["n"] += 1
        return MagicMock(name=f"client-{builds['n']}")

    monkeypatch.setattr(sheets, "_build_credentials", lambda json_content: object())
    monkeypatch.setattr(sheets, "_build_client", fake_build_client)
    return builds


def _db_with_creds(monkeypatch, json_content):
    monkeypatch.setattr(sheets, "get_google_service_account_json", lambda db: json_content)
    return MagicMock()  # stand-in Session; only identity matters


def test_second_call_same_credentials_reuses_client(counting_build, monkeypatch):
    db = _db_with_creds(monkeypatch, '{"a": 1}')
    first = sheets.get_client(db)
    second = sheets.get_client(db)
    assert first is second
    assert counting_build["n"] == 1  # built once, reused


def test_changed_credentials_rebuild_client(counting_build, monkeypatch):
    db = _db_with_creds(monkeypatch, '{"a": 1}')
    first = sheets.get_client(db)

    # Admin pastes a new service-account JSON: content key changes → rebuild.
    monkeypatch.setattr(sheets, "get_google_service_account_json", lambda db: '{"a": 2}')
    second = sheets.get_client(db)

    assert first is not second
    assert counting_build["n"] == 2


def test_reset_forces_rebuild(counting_build, monkeypatch):
    db = _db_with_creds(monkeypatch, '{"a": 1}')
    first = sheets.get_client(db)
    sheets.reset_sheets_cache()
    second = sheets.get_client(db)
    assert first is not second
    assert counting_build["n"] == 2


def test_open_spreadsheet_is_cached_per_thread(monkeypatch):
    # On the lab PC open_by_key costs ~18s; it must run once, then be reused.
    monkeypatch.setattr(sheets, "get_google_sheet_id", lambda db: "sheet-1")
    opens = {"n": 0}

    def fake_open_by_key(_sid):
        opens["n"] += 1
        return MagicMock(name=f"spreadsheet-{opens['n']}")

    client = MagicMock()
    client.open_by_key.side_effect = fake_open_by_key
    monkeypatch.setattr(sheets, "get_client", lambda db=None: client)
    monkeypatch.setattr(sheets, "_credentials_key", lambda db: ("db:key", None))

    db = MagicMock()
    first = sheets.open_spreadsheet(db)
    second = sheets.open_spreadsheet(db)
    assert first is second
    assert opens["n"] == 1  # opened once, cached


def test_get_worksheet_by_name_is_cached(monkeypatch):
    lookups = {"n": 0}

    def fake_worksheet(_name):
        lookups["n"] += 1
        return MagicMock(name=f"ws-{lookups['n']}")

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = fake_worksheet

    w1 = sheets.get_worksheet_by_name(spreadsheet, "11.08.26")
    w2 = sheets.get_worksheet_by_name(spreadsheet, "11.08.26")
    assert w1 is w2
    assert lookups["n"] == 1
    # a different tab is a separate lookup
    sheets.get_worksheet_by_name(spreadsheet, "12.08.26")
    assert lookups["n"] == 2


def test_cache_is_thread_local(counting_build, monkeypatch):
    db = _db_with_creds(monkeypatch, '{"a": 1}')
    main_client = sheets.get_client(db)

    other_client = {}

    def in_thread():
        other_client["value"] = sheets.get_client(db)

    t = threading.Thread(target=in_thread)
    t.start()
    t.join()

    # Same credentials, but a different thread must get its OWN client (a
    # shared requests.Session across threads is what we're avoiding).
    assert other_client["value"] is not main_client
    assert counting_build["n"] == 2
