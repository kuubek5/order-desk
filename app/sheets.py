import ssl
from datetime import date

import gspread
import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter

from app.config import GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_ID

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class _LegacyRenegotiationAdapter(HTTPAdapter):
    """Works around local TLS-inspecting security software.

    The interception proxy triggers a TLS renegotiation that OpenSSL 3.x
    refuses by default (SSLEOFError), while its root CA is only trusted by
    Windows' own certificate store, not by the certifi bundle requests
    normally pins to. Building the context via ssl.create_default_context()
    picks up the Windows store, and cert_verify() is overridden so requests
    doesn't override that trust with the certifi path.
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.options |= 0x00040000  # SSL_OP_ALLOW_UNSAFE_LEGACY_RENEGOTIATION
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def cert_verify(self, conn, url, verify, cert):
        conn.cert_reqs = "CERT_REQUIRED"


def get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=_SCOPES)

    # Token refresh (POST to oauth2.googleapis.com) runs over its own
    # internal session unless we hand it one explicitly, so the adapter
    # has to be mounted on both this session and the API session below.
    token_session = requests.Session()
    token_session.mount("https://", _LegacyRenegotiationAdapter())
    auth_request = Request(session=token_session)

    session = AuthorizedSession(creds, auth_request=auth_request)
    session.mount("https://", _LegacyRenegotiationAdapter())

    return gspread.Client(auth=creds, session=session)


def tab_name_for(d: date) -> str:
    return d.strftime("%d.%m.%y")


def open_spreadsheet() -> gspread.Spreadsheet:
    client = get_client()
    return client.open_by_key(GOOGLE_SHEET_ID)


def get_worksheet_by_date(spreadsheet: gspread.Spreadsheet, d: date) -> gspread.Worksheet | None:
    name = tab_name_for(d)
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        return None
