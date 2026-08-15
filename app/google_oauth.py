"""Google OAuth "Sign in with Google" flow — an alternative to the
service-account JSON for authenticating to Sheets, for a lab PC where the
Google account that already has access to the sheet is a personal @gmail.com
(not a service account, not shareable via Workspace org policy).

Runs the standard installed-app / loopback flow (RFC 8252): open the system
browser at Google's consent screen, catch the redirect on a throwaway local
HTTP server bound to 127.0.0.1, exchange the returned code for tokens. Only
the refresh token is kept — access tokens are minted on demand by
google.oauth2.credentials.Credentials during normal use (see app/sheets.py).
"""

from __future__ import annotations

import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

from app.sheets import OAuthClientConfigError, new_legacy_session, parse_oauth_client_json

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


@dataclass
class ClientConfig:
    client_id: str
    client_secret: str
    token_uri: str
    auth_uri: str = _DEFAULT_AUTH_URI


class OAuthFlowError(RuntimeError):
    """Raised for any failure in parsing the client JSON or completing the
    browser/token exchange — message is safe to show the admin as-is."""


def parse_client_config(json_content: str) -> ClientConfig:
    """Parse the "Desktop app" OAuth client JSON downloaded from Google Cloud
    Console, via the same parser app/sheets.py uses to build credentials —
    keeps "what counts as a valid client JSON" in one place."""
    try:
        cfg = parse_oauth_client_json(json_content)
    except OAuthClientConfigError as exc:
        raise OAuthFlowError(str(exc)) from exc
    return ClientConfig(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        token_uri=cfg["token_uri"],
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the single GET /?code=...&state=... redirect from Google and
    stores it on the server instance; serves a short human-readable page so
    the admin knows they can close the tab."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        server: _CallbackServer = self.server  # type: ignore[assignment]
        server.query_params = params

        if "error" in params:
            body = "<h3>Помилка авторизації Google.</h3><p>Можна закрити цю вкладку.</p>"
        else:
            body = "<h3>Авторизацію Google завершено.</h3><p>Можна закрити цю вкладку і повернутись у Order Desk.</p>"
        encoded = f"<html><body style='font-family:sans-serif'>{body}</body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the app's request logs free of this throwaway server's noise


class _CallbackServer(HTTPServer):
    query_params: Optional[dict] = None


def _run_local_server(timeout_seconds: float) -> tuple[_CallbackServer, int]:
    """Binds an ephemeral port on 127.0.0.1 and returns (server, port). The
    caller drives one `handle_request()` (with a timeout) to catch the single
    redirect, matching the loopback-IP flow Google's Desktop client type
    expects — any port is accepted for a `http://127.0.0.1:PORT` redirect."""
    server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = timeout_seconds
    port = server.server_address[1]
    return server, port


def run_authorization_flow(config: ClientConfig, *, timeout_seconds: float = 180) -> str:
    """Runs the full loopback OAuth flow and returns a refresh token.

    Opens the admin's system browser (this runs on the same PC as the app) at
    Google's consent screen, waits (up to ``timeout_seconds``) for the single
    redirect carrying the authorization code, then exchanges it for tokens.
    ``access_type=offline`` + ``prompt=consent`` guarantee a refresh token is
    issued even if this Google account already authorized this app before.
    Raises OAuthFlowError on any failure — timeout, user denial, or a token
    exchange error — with a message safe to show the admin."""
    server, port = _run_local_server(timeout_seconds)
    # Google deprecated the "localhost" hostname for the loopback redirect
    # (Feb 2022) — the literal IP 127.0.0.1 is what's accepted now; using the
    # hostname form here produced a generic post-account-picker error.
    redirect_uri = f"http://127.0.0.1:{port}/"
    try:
        auth_url = config.auth_uri + "?" + urlencode({
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        })
        opened = threading.Thread(target=webbrowser.open, args=(auth_url,), daemon=True)
        opened.start()

        server.handle_request()
        params = server.query_params or {}
    finally:
        server.server_close()

    if "error" in params:
        raise OAuthFlowError(f"Google відхилив авторизацію: {params['error'][0]}")
    codes = params.get("code")
    if not codes:
        raise OAuthFlowError("Не отримано код авторизації від Google (тайм-аут або відмова)")

    session = new_legacy_session()
    response = session.post(
        config.token_uri,
        data={
            "code": codes[0],
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise OAuthFlowError(f"Google відхилив обмін токена: {response.text[:200]}")

    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        raise OAuthFlowError(
            "Google не повернув refresh token. Спробуйте ще раз "
            "(можливо, потрібно спершу відкликати доступ додатку в налаштуваннях Google-акаунта)"
        )
    return refresh_token
