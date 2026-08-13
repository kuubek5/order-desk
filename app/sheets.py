import hashlib
import json
import logging
import ssl
import threading
import time
from datetime import date
from typing import Callable, Optional, TypeVar

import gspread
import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter
from sqlalchemy.orm import Session

from app.config import GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_ID
from app.settings_store import get_google_service_account_json, get_google_sheet_id

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# HTTP statuses worth retrying: Google rate-limit (429) and transient
# server-side failures (5xx). A 4xx other than 429 (bad Sheet ID, revoked
# access) is a real error and must fail fast, not spin through retries.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_T = TypeVar("_T")


def is_transient_sheet_error(exc: BaseException) -> bool:
    """True for errors that a short retry might clear: Google 429/5xx, and the
    connection/SSL resets that the TLS-inspecting proxy on the lab PC injects
    mid-request (see _LegacyRenegotiationAdapter). Everything else — auth,
    permission, bad Sheet ID, programmer error — is permanent and returns
    False so callers fail fast instead of retrying a hopeless request."""
    if isinstance(exc, gspread.exceptions.APIError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in _RETRYABLE_STATUS
    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
            requests.exceptions.Timeout,
        ),
    )


def call_with_retry(
    fn: Callable[[], _T],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> _T:
    """Run `fn`, retrying only transient Google Sheets failures with
    exponential backoff (base_delay * 2**i: 1s, 2s, 4s by default). A
    non-transient error is re-raised immediately; the last transient error is
    re-raised after the final attempt. `sleep` is injectable so tests don't
    actually wait."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised unless transient
            if not is_transient_sheet_error(exc) or attempt == attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "Transient Google Sheets error (attempt %d/%d), retrying in %.0fs: %s",
                attempt + 1,
                attempts,
                delay,
                exc,
            )
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


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


# Per-thread cached gspread client. Building one mints an OAuth token (a POST
# to oauth2.googleapis.com) and stands up a fresh requests.Session; doing that
# on every sync and every write-back is wasteful when the token is valid for
# ~1h and the session's connection pool could be reused. The cache is
# THREAD-LOCAL on purpose: a requests.Session is not safe to share across
# threads, and the background sync worker and request-handler write-backs run
# on different threads — each keeps its own client instead of contending over
# one shared session. Keyed by the credentials content, so changing the
# service-account JSON in Settings yields a new key and rebuilds automatically;
# no explicit invalidation call is needed on a config change.
_local = threading.local()


def _credentials_key(db: Optional[Session]) -> tuple[str, Optional[str]]:
    """Cache key for the active service-account config, plus the DB-stored JSON
    when that is the source (so a cache miss can build without a second read).
    Computing the key never mints a token — that's the whole point of caching."""
    if db is not None:
        json_content = get_google_service_account_json(db)
        if json_content is not None:
            digest = hashlib.sha256(json_content.encode("utf-8")).hexdigest()
            return f"db:{digest}", json_content
    return f"file:{GOOGLE_SERVICE_ACCOUNT_JSON}", None


def _build_credentials(json_content: Optional[str]) -> Credentials:
    if json_content is not None:
        return Credentials.from_service_account_info(json.loads(json_content), scopes=_SCOPES)
    return Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=_SCOPES)


def _build_client(creds: Credentials) -> gspread.Client:
    # Token refresh (POST to oauth2.googleapis.com) runs over its own
    # internal session unless we hand it one explicitly, so the adapter
    # has to be mounted on both this session and the API session below.
    token_session = requests.Session()
    token_session.mount("https://", _LegacyRenegotiationAdapter())
    auth_request = Request(session=token_session)

    session = AuthorizedSession(creds, auth_request=auth_request)
    session.mount("https://", _LegacyRenegotiationAdapter())

    return gspread.Client(auth=creds, session=session)


def reset_sheets_cache() -> None:
    """Drop this thread's cached client. Config changes invalidate the cache on
    their own via the content-based key; this exists for tests and for an
    explicit "rebuild now" after a settings change on the same thread."""
    if hasattr(_local, "sheets_client"):
        del _local.sheets_client


def get_client(db: Optional[Session] = None) -> gspread.Client:
    key, json_content = _credentials_key(db)
    cached = getattr(_local, "sheets_client", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    client = _build_client(_build_credentials(json_content))
    _local.sheets_client = (key, client)
    return client


def tab_name_for(d: date) -> str:
    return d.strftime("%d.%m.%y")


def open_spreadsheet(db: Optional[Session] = None) -> gspread.Spreadsheet:
    client = get_client(db)
    if db is not None:
        sheet_id = get_google_sheet_id(db)
    else:
        sheet_id = GOOGLE_SHEET_ID
    # Retry here so every caller (read-sync and operator write-back alike) is
    # shielded from a transient 429/5xx/SSL blip on the open step, not just the
    # ones that remembered to wrap it.
    return call_with_retry(lambda: client.open_by_key(sheet_id))


def get_worksheet_by_date(spreadsheet: gspread.Spreadsheet, d: date) -> gspread.Worksheet | None:
    return get_worksheet_by_name(spreadsheet, tab_name_for(d))


def get_worksheet_by_name(spreadsheet: gspread.Spreadsheet, name: str) -> gspread.Worksheet | None:
    try:
        # WorksheetNotFound is permanent, so call_with_retry re-raises it at
        # once (not transient) and it's handled below; only 429/5xx/SSL retry.
        return call_with_retry(lambda: spreadsheet.worksheet(name))
    except gspread.WorksheetNotFound:
        return None
