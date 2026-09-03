import hashlib
import json
import logging
import ssl
import threading
import time
from collections import deque
from datetime import date, datetime
from typing import Callable, Deque, Optional, TypeVar

import gspread
import requests
from google.auth.credentials import Credentials as BaseCredentials
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from requests.adapters import HTTPAdapter
from sqlalchemy.orm import Session

from app.config import GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_ID
from app.parser import HEADER_ROWS
from app.settings_store import (
    get_google_auth_mode,
    get_google_oauth_client_json,
    get_google_oauth_refresh_token,
    get_google_service_account_json,
    get_google_sheet_id,
)

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


def _is_quota_error(exc: Exception) -> bool:
    """429 з текстом про квоту — окремий випадок: він лікується не повтором,
    а ЧАСОМ, бо ліміт хвилинний."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    return "429" in str(exc) and "Quota exceeded" in str(exc)


GOOGLE_QUOTA_PER_MINUTE = 60
"""Ліміт Google: 60 запитів за хвилину на користувача (для нас «користувач» =
сервісний акаунт). Не наш параметр — довідкове число, з яким порівнюється
лічильник нижче.

ВАЖЛИВО про точність цього порівняння: у Google квоти читань і записів
ОКРЕМІ, по 60/хв кожна, а call_with_retry обгортає і те, і те. Тому лічильник
дає СУМУ, і рядок у логу це чесно каже. Для нашого профілю сума — правильний
орієнтир: читань на два порядки більше за записи (синк читає вкладки кожні
15 с, запис буває лише коли оператор вписує Sum3D чи знімає заливку), тож
сума практично дорівнює читанням. Якщо колись зʼявиться масовий запис,
лічильники доведеться рознести — інакше прилад почне брехати саме тоді, коли
буде найпотрібніший."""

_RATE_WINDOW_SECONDS = 60.0
_rate_lock = threading.Lock()
_rate_calls: Deque[float] = deque()
_rate_reported_at = 0.0


def _record_api_call(now: float | None = None) -> int:
    """Порахувати ОДИН запит до Sheets API і, не частіше ніж раз на хвилину,
    написати в лог, скільки їх було за останні 60 с.

    Навіщо: бойовий лог 03.09.26 показав «429 Quota exceeded ... Read requests
    per minute per user», але жодного способу дізнатись, скільки саме запитів
    ми робимо, не було — лишалось гадати (2 вкладки × 4 тіки × N викликів?).
    Правило власника: спершу поміряти, потім правити. Усе спілкування з
    Google іде через call_with_retry, тож лічильник тут бачить УСЕ: і синк, і
    запис Sum3D, і читання заливок, і діагностику ваги таблиці.

    Ціна — один deque на процес; вікно тримає щонайбільше кілька десятків
    міток. Лог не флудить: рядок виходить раз на хвилину, і лише коли запити
    справді були."""
    global _rate_reported_at
    moment = time.monotonic() if now is None else now
    with _rate_lock:
        _rate_calls.append(moment)
        cutoff = moment - _RATE_WINDOW_SECONDS
        while _rate_calls and _rate_calls[0] < cutoff:
            _rate_calls.popleft()
        count = len(_rate_calls)
        due = moment - _rate_reported_at >= _RATE_WINDOW_SECONDS
        if due:
            _rate_reported_at = moment
    if due:
        # WARNING від 3/4 квоти: саме там 429 стає питанням часу, а не удачі.
        level = (
            logging.WARNING
            if count >= GOOGLE_QUOTA_PER_MINUTE * 0.75
            else logging.INFO
        )
        logger.log(
            level,
            "Sheets API: %d запитів за останні 60 с "
            "(читання+запис; ліміт Google — %d/хв на кожен вид окремо)",
            count,
            GOOGLE_QUOTA_PER_MINUTE,
        )
    return count


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
    actually wait.

    Кожна СПРОБА рахується в _record_api_call — повтор так само їсть квоту,
    як і перший виклик, тож рахувати треба саме спроби."""
    for attempt in range(attempts):
        try:
            _record_api_call()
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised unless transient
            if not is_transient_sheet_error(exc) or attempt == attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            # 429 — квота «читань ЗА ХВИЛИНУ». Бекоф 1-2-4с марний: усі
            # чотири спроби влучають у ту саму хвилину, і синк падає, хоча
            # квота відновиться сама (бойовий лог 30.08.26 — дві невдачі
            # поспіль саме так). Тут чекаємо так, щоб вийти за межу хвилини.
            if _is_quota_error(exc):
                delay = max(delay, 20.0 * (attempt + 1))
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


def new_legacy_session() -> requests.Session:
    """A requests.Session that survives the lab PC's TLS-inspecting proxy — the
    same legacy-renegotiation + Windows-cert-store handling the Sheets client
    uses. Use it for any outbound HTTPS that would otherwise fail with
    SSLEOFError under that proxy (e.g. the GitHub update check), not just
    Sheets."""
    session = requests.Session()
    session.mount("https://", _LegacyRenegotiationAdapter())
    return session


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


_OAUTH_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class OAuthClientConfigError(ValueError):
    """The saved google_oauth_client_json isn't a usable Desktop-client JSON."""


def parse_oauth_client_json(json_content: str) -> dict:
    """Parse the "Desktop app" OAuth client JSON from Google Cloud Console into
    {client_id, client_secret, token_uri}. Shared by app/google_oauth.py (the
    sign-in flow) and the credentials builder below, so both agree on what a
    valid client JSON looks like."""
    try:
        data = json.loads(json_content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OAuthClientConfigError("Не вдалося розібрати JSON OAuth-клієнта") from exc
    block = data.get("installed") or data.get("web")
    if not block or not block.get("client_id") or not block.get("client_secret"):
        raise OAuthClientConfigError(
            "У JSON немає client_id/client_secret у розділі «installed»"
        )
    return {
        "client_id": block["client_id"],
        "client_secret": block["client_secret"],
        "token_uri": block.get("token_uri", _OAUTH_DEFAULT_TOKEN_URI),
    }


# A credentials "spec" the cache key and the builder agree on:
#   ("service_account", json_content_or_None, None)
#   ("oauth", client_json, refresh_token)
_CredsSpec = tuple[str, Optional[str], Optional[str]]


def _credentials_key(db: Optional[Session]) -> tuple[str, _CredsSpec]:
    """Cache key for the active credentials config, plus the spec needed to
    build them (so a cache miss can build without a second DB read). Computing
    the key never mints a token — that's the whole point of caching."""
    if db is not None:
        if get_google_auth_mode(db) == "oauth":
            client_json = get_google_oauth_client_json(db)
            refresh_token = get_google_oauth_refresh_token(db)
            if client_json and refresh_token:
                digest = hashlib.sha256(f"{client_json}:{refresh_token}".encode("utf-8")).hexdigest()
                return f"oauth:{digest}", ("oauth", client_json, refresh_token)
        json_content = get_google_service_account_json(db)
        if json_content is not None:
            digest = hashlib.sha256(json_content.encode("utf-8")).hexdigest()
            return f"db:{digest}", ("service_account", json_content, None)
    return f"file:{GOOGLE_SERVICE_ACCOUNT_JSON}", ("service_account", None, None)


def _build_credentials(spec: _CredsSpec) -> BaseCredentials:
    kind, a, b = spec
    if kind == "oauth":
        client_json, refresh_token = a, b
        cfg = parse_oauth_client_json(client_json)
        # scopes deliberately omitted: the refresh then inherits whatever the
        # user actually granted during sign-in (spreadsheets-only — see
        # app/google_oauth.py SCOPES) instead of re-requesting _SCOPES and
        # failing with invalid_scope on the narrower grant.
        return UserCredentials(
            token=None,
            refresh_token=refresh_token,
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            token_uri=cfg["token_uri"],
        )
    json_content = a
    if json_content is not None:
        return ServiceAccountCredentials.from_service_account_info(json.loads(json_content), scopes=_SCOPES)
    return ServiceAccountCredentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=_SCOPES)


class _LeanHTTPClient(gspread.http_client.HTTPClient):
    """gspread HTTPClient whose default metadata fetch is trimmed with a
    `fields` mask. Spreadsheet.__init__ and Spreadsheet.worksheet() both call
    fetch_sheet_metadata with params=None, which normally returns the FULL
    document metadata — every tab's conditional formats, merges, banding. On a
    daily-tabs document that JSON is huge and each fetch measured ~18s; the
    trimmed mask (doc properties + per-sheet properties, all either needs)
    returns in ~0.2s. Callers that pass their own params (e.g. the row-color
    reader in app/sheet_colors.py) are untouched."""

    def fetch_sheet_metadata(self, id, params=None):
        if params is None:
            params = {
                "includeGridData": "false",
                "fields": "properties,sheets.properties",
            }
        return super().fetch_sheet_metadata(id, params=params)


def _build_client(creds: BaseCredentials) -> gspread.Client:
    # Token refresh (POST to oauth2.googleapis.com) runs over its own
    # internal session unless we hand it one explicitly, so the adapter
    # has to be mounted on both this session and the API session below.
    token_session = requests.Session()
    token_session.mount("https://", _LegacyRenegotiationAdapter())
    auth_request = Request(session=token_session)

    session = AuthorizedSession(creds, auth_request=auth_request)
    session.mount("https://", _LegacyRenegotiationAdapter())
    # The lab's TLS proxy appears to serve a STALE cached copy of the Sheets
    # values response — a read comes back short (missing the tail added since)
    # yet valid and error-free, so every re-read (including «Імпортувати всю
    # історію») sees the same old rows and reports success. Ask any intermediary
    # not to serve or store a cached response. Harmless off-proxy.
    session.headers.update({
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    })

    return gspread.Client(auth=creds, session=session, http_client=_LeanHTTPClient)


def reset_sheets_cache() -> None:
    """Drop this thread's cached client. Config changes invalidate the cache on
    their own via the content-based key; this exists for tests and for an
    explicit "rebuild now" after a settings change on the same thread."""
    for attr in ("sheets_client", "spreadsheet_cache", "worksheet_cache"):
        if hasattr(_local, attr):
            delattr(_local, attr)


def get_client(db: Optional[Session] = None) -> gspread.Client:
    key, spec = _credentials_key(db)
    cached = getattr(_local, "sheets_client", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    client = _build_client(_build_credentials(spec))
    _local.sheets_client = (key, client)
    return client


def tab_name_for(d: date) -> str:
    return d.strftime("%d.%m.%y")


def measure_sheet_weight(spreadsheet: gspread.Spreadsheet) -> dict:
    """Count conditional-format rules per tab and weigh the metadata response.

    Read-only diagnostic for the "чому таблиця гальмує" case. A sheet whose
    day-tabs are made by COPYING yesterday's tab accumulates conditional-format
    rules per cell instead of extending the range: the test sheet reached
    105 063 rules / 612 MB of metadata, and every values call paid for it
    (0.3s vs 6.8s measured). Google loads the document as a whole, so the total
    across ALL tabs is what matters — cleaning one tab changes nothing.

    Asks only for `sheets(properties.title,conditionalFormats)`; the response
    size is itself the signal, so it is measured rather than discarded.
    """
    from time import perf_counter

    started = perf_counter()
    # У gspread 6.x Spreadsheet.client — це ВЖЕ сам HTTPClient; зайва ланка
    # .http_client падала AttributeError, і пробник ваги був мертвий з моменту
    # оновлення бібліотеки (спіймано логом бойового ПК 30.08.26).
    payload = spreadsheet.client.fetch_sheet_metadata(
        spreadsheet.id,
        params={
            "includeGridData": "false",
            "fields": "sheets(properties.title,conditionalFormats)",
        },
    )
    elapsed = perf_counter() - started

    tabs = []
    total_rules = 0
    tiny_ranges = 0
    for sheet in payload.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "?")
        rules = sheet.get("conditionalFormats") or []
        for rule in rules:
            for rng in rule.get("ranges", []):
                width = rng.get("endColumnIndex", 0) - rng.get("startColumnIndex", 0)
                height = rng.get("endRowIndex", 0) - rng.get("startRowIndex", 0)
                # 1×1 / 2×1 ranges are the fingerprint of per-cell duplication.
                if 0 < width <= 2 and 0 < height <= 2:
                    tiny_ranges += 1
        total_rules += len(rules)
        tabs.append({"title": title, "rules": len(rules)})

    tabs.sort(key=lambda t: t["rules"], reverse=True)
    payload_bytes = len(json.dumps(payload).encode("utf-8"))
    return {
        "tabs": tabs,
        "tab_count": len(tabs),
        "total_rules": total_rules,
        "tiny_ranges": tiny_ranges,
        "avg_rules": round(total_rules / len(tabs), 1) if tabs else 0,
        "payload_mb": round(payload_bytes / (1024 * 1024), 2),
        "fetch_seconds": round(elapsed, 2),
    }


def open_spreadsheet(db: Optional[Session] = None) -> gspread.Spreadsheet:
    """Open the configured spreadsheet, caching the opened object per thread.

    On the lab PC's proxied link, `client.open_by_key` (a metadata fetch) costs
    ~18s — as much as the write it precedes. Caching the Spreadsheet object per
    thread turns every write-back after the first into just the batch_update
    (~3s) instead of re-opening the whole sheet each time. Keyed by
    (credentials, sheet_id) so a settings change rebuilds it; the worksheet
    cache is cleared whenever the spreadsheet is (re)opened."""
    key, _ = _credentials_key(db)
    sheet_id = get_google_sheet_id(db) if db is not None else GOOGLE_SHEET_ID
    cached = getattr(_local, "spreadsheet_cache", None)
    if cached is not None and cached[0] == (key, sheet_id):
        return cached[1]

    client = get_client(db)
    # Retry here so every caller (read-sync and operator write-back alike) is
    # shielded from a transient 429/5xx/SSL blip on the open step.
    spreadsheet = call_with_retry(lambda: client.open_by_key(sheet_id))
    _local.spreadsheet_cache = ((key, sheet_id), spreadsheet)
    _local.worksheet_cache = {}
    return spreadsheet


# The lab's TLS proxy TRUNCATES a large Google Sheets values response: a single
# worksheet.get_all_values() on a ~120-row tab came back cut at ~100 rows on the
# lab PC (проксі), while the identical call off-proxy returned all 120. The tail
# silently never imported and «Імпортувати всю історію» kept seeing the same
# short read. Reading in SMALL row-chunks keeps every response under the proxy's
# cut point, so the whole tab arrives. Chunk size is intentionally conservative
# and configurable (a smaller value survives a stricter proxy).
DEFAULT_READ_CHUNK_ROWS = 50
# Widest column the parser touches is index 24 (redo_milled) → column Y (25).
# Read a little past it (AB=28) so any trailing operator column is included.
_READ_LAST_COL = "AB"


def read_all_values(worksheet: gspread.Worksheet, chunk_rows: int = DEFAULT_READ_CHUNK_ROWS) -> list[list[str]]:
    """get_all_values, but fetched in bounded row-chunks so a truncating proxy
    can't silently cut off the tail of a big tab.

    Rows are padded back to their absolute grid positions (gspread trims trailing
    empty rows/cells per range), so the returned matrix aligns 1:1 with the sheet
    — parse_rows relies on absolute row_number. Stops after two consecutive
    all-empty chunks past the header so a 1000-row grid holding 120 rows of data
    isn't read to the end."""
    if chunk_rows < 1:
        chunk_rows = DEFAULT_READ_CHUNK_ROWS
    try:
        total = int(getattr(worksheet, "row_count", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        # Unknown grid size (or a test double) — fall back to the plain read.
        return call_with_retry(worksheet.get_all_values)

    def _has_work(batch: list[list[str]]) -> bool:
        # A real work row carries a наряд (col 1) or a вид/client name (col 4).
        # Stray cells further down a tab (a «Всього» formula, leftover notes)
        # must NOT keep us reading to the bottom of a 600-row grid — that
        # multiplies requests and burns the read-quota (429). Bounding to work
        # columns stops a few chunks past the last real row.
        for row in batch:
            if (len(row) > 1 and row[1].strip()) or (len(row) > 4 and row[4].strip()):
                return True
        return False

    out: list[list[str]] = []
    empty_streak = 0
    start = 1
    while start <= total:
        end = min(start + chunk_rows - 1, total)
        rng = f"A{start}:{_READ_LAST_COL}{end}"
        batch = call_with_retry(lambda r=rng: worksheet.get(r)) or []
        for i in range(end - start + 1):
            out.append(list(batch[i]) if i < len(batch) else [])
        if _has_work(batch):
            empty_streak = 0
        elif end > HEADER_ROWS:
            empty_streak += 1
            if empty_streak >= 2:
                break
        start = end + 1
    return out


def get_worksheet_by_date(spreadsheet: gspread.Spreadsheet, d: date) -> gspread.Worksheet | None:
    return get_worksheet_by_name(spreadsheet, tab_name_for(d))


def latest_worksheet_on_or_before(
    spreadsheet: gspread.Spreadsheet, target: date
) -> gspread.Worksheet | None:
    """The dated tab (title `dd.mm.yy`) with the newest date that is still on or
    before ``target`` — the tab an email/manual order should land in when the
    lab hasn't created today's tab yet (they often work a day or two behind).
    Ignores non-dated tabs and any dated tab in the future. None if the document
    has no usable dated tab at all."""
    best_date: date | None = None
    best_ws: gspread.Worksheet | None = None
    for ws in spreadsheet.worksheets():
        try:
            tab_date = datetime.strptime(ws.title, "%d.%m.%y").date()
        except ValueError:
            continue
        if tab_date > target:
            continue
        if best_date is None or tab_date > best_date:
            best_date = tab_date
            best_ws = ws
    return best_ws


def get_worksheet_by_name(spreadsheet: gspread.Spreadsheet, name: str) -> gspread.Worksheet | None:
    """Resolve a worksheet by tab name, caching the object per thread — the
    `spreadsheet.worksheet(name)` metadata fetch is another ~18s on the lab PC's
    link. Cached tabs stay valid for cell reads/writes; the cache is reset each
    time the spreadsheet is reopened (see open_spreadsheet), and a new tab that
    isn't cached is fetched (and then cached) on demand."""
    cache = getattr(_local, "worksheet_cache", None)
    if cache is None:
        cache = {}
        _local.worksheet_cache = cache
    if name in cache:
        return cache[name]
    try:
        # WorksheetNotFound is permanent, so call_with_retry re-raises it at
        # once (not transient) and it's handled below; only 429/5xx/SSL retry.
        worksheet = call_with_retry(lambda: spreadsheet.worksheet(name))
    except gspread.WorksheetNotFound:
        return None
    cache[name] = worksheet
    return worksheet
