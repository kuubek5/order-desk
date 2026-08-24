"""Connecting the Google sheet: the two things that actually block an operator.

1. They copy the browser URL, not the id buried inside it.
2. In service-account mode "connecting" IS sharing the sheet with the service
   account's address — which is useless unless that address is on screen and
   the failure message names it.
"""

import json

import gspread
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.settings_store import extract_sheet_id, get_service_account_email, set_setting

SHEET_ID = "1IIEkBnPoDcxgo3-41IdbJu6FZXNawYX9UNdoekFDPbs"


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.mark.parametrize(
    "raw",
    [
        SHEET_ID,
        f"  {SHEET_ID}  ",
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?usp=sharing",
        f"http://docs.google.com/spreadsheets/d/{SHEET_ID}",
    ],
)
def test_extract_sheet_id_accepts_url_or_bare_id(raw):
    assert extract_sheet_id(raw) == SHEET_ID


def test_extract_sheet_id_leaves_unrecognised_input_alone():
    """Not a URL we understand — hand it back rather than silently blanking the
    field, so the existing "bad id" error still explains itself."""
    assert extract_sheet_id("не-посилання") == "не-посилання"
    assert extract_sheet_id("") == ""


def test_service_account_email_read_from_saved_json():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(
            db,
            "google_service_account_json",
            json.dumps({"client_email": "order-desk@proj.iam.gserviceaccount.com"}),
        )
        db.commit()
        assert get_service_account_email(db) == "order-desk@proj.iam.gserviceaccount.com"


def test_service_account_email_is_none_when_unset_or_broken():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        assert get_service_account_email(db) is None
        set_setting(db, "google_service_account_json", "{not json")
        db.commit()
        assert get_service_account_email(db) is None


def _api_error(status: int) -> gspread.exceptions.APIError:
    """A real gspread.APIError built from a minimal fake response.

    gspread parses `response.json()["error"]` and requires `code`/`message`
    there, so the fake has to carry both; our classifier reads the HTTP
    `status_code`, which is what Google actually sets.
    """

    class _Resp:
        status_code = status
        text = "irrelevant"

        def json(self):
            return {"error": {"code": status, "message": "irrelevant", "status": "ERROR"}}

    return gspread.exceptions.APIError(_Resp())


def test_403_names_the_address_to_share_with():
    """The whole point: a 403 means the sheet exists but was never shared, and
    the fix is pasting this address into Google's Share dialog."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(
            db,
            "google_service_account_json",
            json.dumps({"client_email": "order-desk@proj.iam.gserviceaccount.com"}),
        )
        db.commit()
        message = web._sheets_access_error_message(db, _api_error(403))
    assert "order-desk@proj.iam.gserviceaccount.com" in message
    assert "Поділитися" in message


def test_404_points_at_the_id_not_at_permissions():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        message = web._sheets_access_error_message(db, _api_error(404))
    assert "не знайдено" in message
    assert "Поділитися" not in message


def test_oauth_mode_403_talks_about_the_account_not_a_service_address():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(db, "google_auth_mode", "oauth")
        db.commit()
        message = web._sheets_access_error_message(db, _api_error(403))
    assert "Акаунт Google" in message
    assert "gserviceaccount" not in message


def test_unknown_failure_falls_back_without_leaking_raw_google_text():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        message = web._sheets_access_error_message(db, RuntimeError("boom: raw google detail"))
    assert "boom" not in message
    assert message
