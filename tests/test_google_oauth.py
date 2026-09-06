"""app/google_oauth.py: parsing the Desktop-client JSON and the loopback
authorization flow (no real network — the token exchange and the browser
open are both mocked)."""

import base64
import hashlib
import json
import time
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from app.google_oauth import ClientConfig, OAuthFlowError, parse_client_config, run_authorization_flow
from app.sheets import OAuthClientConfigError, parse_oauth_client_json


class TestParseOAuthClientJson:
    """app.sheets.parse_oauth_client_json — the shared parser both the flow
    and the credentials builder rely on."""

    def test_parses_installed_block(self):
        raw = json.dumps({
            "installed": {
                "client_id": "abc.apps.googleusercontent.com",
                "client_secret": "GOCSPX-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        })
        cfg = parse_oauth_client_json(raw)
        assert cfg["client_id"] == "abc.apps.googleusercontent.com"
        assert cfg["client_secret"] == "GOCSPX-secret"
        assert cfg["token_uri"] == "https://oauth2.googleapis.com/token"

    def test_defaults_token_uri_when_missing(self):
        raw = json.dumps({"installed": {"client_id": "a", "client_secret": "b"}})
        cfg = parse_oauth_client_json(raw)
        assert cfg["token_uri"] == "https://oauth2.googleapis.com/token"

    def test_accepts_web_block_too(self):
        raw = json.dumps({"web": {"client_id": "a", "client_secret": "b"}})
        cfg = parse_oauth_client_json(raw)
        assert cfg["client_id"] == "a"

    def test_invalid_json_raises(self):
        with pytest.raises(OAuthClientConfigError, match="розібрати JSON"):
            parse_oauth_client_json("not json")

    def test_missing_installed_block_raises(self):
        with pytest.raises(OAuthClientConfigError, match="client_id"):
            parse_oauth_client_json(json.dumps({"other": {}}))

    def test_missing_client_secret_raises(self):
        with pytest.raises(OAuthClientConfigError):
            parse_oauth_client_json(json.dumps({"installed": {"client_id": "a"}}))


class TestParseClientConfig:
    """google_oauth.parse_client_config wraps the same parser, surfacing
    OAuthFlowError (the error type this module's callers expect)."""

    def test_parses_valid_config(self):
        raw = json.dumps({"installed": {"client_id": "a", "client_secret": "b"}})
        cfg = parse_client_config(raw)
        assert isinstance(cfg, ClientConfig)
        assert cfg.client_id == "a" and cfg.client_secret == "b"

    def test_invalid_raises_oauth_flow_error(self):
        with pytest.raises(OAuthFlowError):
            parse_client_config("garbage")


_STATE = "state-fixed-for-tests"


def _wire_loopback(monkeypatch, params, *, echo_state=True, opened=None):
    """Fake loopback server + browser for the OAuth flow.

    `params` is what the redirect delivers (None = the socket timed out with
    nothing arriving). The flow now only accepts a request carrying the `state`
    it generated, so the fake echoes it back; `echo_state=False` simulates a
    stray GET from another page probing 127.0.0.1 ports.
    """
    fake_server = MagicMock()
    fake_server.query_params = None

    def handle_request():
        if params is None:
            fake_server.query_params = None
            return
        delivered = {key: list(value) for key, value in params.items()}
        if echo_state:
            delivered["state"] = [_STATE]
        fake_server.query_params = delivered

    fake_server.handle_request.side_effect = handle_request
    monkeypatch.setattr("app.google_oauth._new_state", lambda: _STATE)
    monkeypatch.setattr(
        "app.google_oauth._run_local_server", lambda timeout_seconds: (fake_server, 54321)
    )
    monkeypatch.setattr(
        "app.google_oauth.webbrowser.open",
        lambda url: (opened.append(url) if opened is not None else None),
    )
    return fake_server


class TestRunAuthorizationFlow:
    """The loopback flow: local server catches the redirect, then a token
    exchange POST. Both the browser open and the local HTTP server are mocked
    so no real network or browser window is touched."""

    def _config(self):
        return ClientConfig(
            client_id="cid", client_secret="csecret", token_uri="https://oauth2.googleapis.com/token"
        )

    def test_success_returns_refresh_token(self, monkeypatch):
        fake_server = _wire_loopback(monkeypatch, {"code": ["auth-code-123"]})

        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"refresh_token": "rt-abc"}
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        token = run_authorization_flow(self._config())

        assert token == "rt-abc"
        fake_server.handle_request.assert_called_once()
        fake_server.server_close.assert_called_once()
        # token exchange posted the code + client creds to the token endpoint
        call = fake_session.post.call_args
        assert call.args[0] == "https://oauth2.googleapis.com/token"
        assert call.kwargs["data"]["code"] == "auth-code-123"
        assert call.kwargs["data"]["client_id"] == "cid"

    def test_denied_consent_raises(self, monkeypatch):
        _wire_loopback(monkeypatch, {"error": ["access_denied"]})

        with pytest.raises(OAuthFlowError, match="відхилив авторизацію"):
            run_authorization_flow(self._config())

    def test_timeout_with_no_code_raises(self, monkeypatch):
        _wire_loopback(monkeypatch, None)  # handle_request timed out, nothing arrived

        with pytest.raises(OAuthFlowError, match="тайм-аут"):
            run_authorization_flow(self._config(), timeout_seconds=0.05)

    def test_token_exchange_failure_raises(self, monkeypatch):
        _wire_loopback(monkeypatch, {"code": ["auth-code-123"]})

        fake_response = MagicMock(status_code=400, text="invalid_grant")
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        with pytest.raises(OAuthFlowError, match="обмін токена"):
            run_authorization_flow(self._config())

    def test_missing_refresh_token_raises(self, monkeypatch):
        _wire_loopback(monkeypatch, {"code": ["auth-code-123"]})

        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"access_token": "at-only"}  # no refresh_token
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        with pytest.raises(OAuthFlowError, match="refresh token"):
            run_authorization_flow(self._config())

    def test_stray_request_does_not_kill_the_flow(self, monkeypatch):
        """A page in the admin's browser can probe 127.0.0.1 ports. Such a GET
        used to eat our single handle_request() and the login died as a bare
        "timeout"; now it is ignored and we keep waiting for OUR redirect."""
        calls = {"n": 0}
        fake_server = MagicMock()
        fake_server.query_params = None

        def handle_request():
            calls["n"] += 1
            if calls["n"] == 1:
                fake_server.query_params = {"code": ["attacker-code"], "state": ["not-ours"]}
            else:
                fake_server.query_params = {"code": ["auth-code-123"], "state": [_STATE]}

        fake_server.handle_request.side_effect = handle_request
        monkeypatch.setattr("app.google_oauth._new_state", lambda: _STATE)
        monkeypatch.setattr(
            "app.google_oauth._run_local_server", lambda timeout_seconds: (fake_server, 54321)
        )
        monkeypatch.setattr("app.google_oauth.webbrowser.open", lambda url: None)

        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"refresh_token": "rt-abc"}
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        token = run_authorization_flow(self._config(), timeout_seconds=5)

        assert token == "rt-abc"
        assert calls["n"] == 2
        # the attacker's code was never exchanged
        assert fake_session.post.call_args.kwargs["data"]["code"] == "auth-code-123"

    def test_foreign_state_only_times_out(self, monkeypatch):
        """A redirect that never carries our state is never accepted."""
        _wire_loopback(monkeypatch, {"code": ["attacker-code"]}, echo_state=False)
        fake_session = MagicMock()
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        with pytest.raises(OAuthFlowError, match="тайм-аут"):
            run_authorization_flow(self._config(), timeout_seconds=0.05)
        fake_session.post.assert_not_called()

    def test_consent_url_and_exchange_carry_pkce(self, monkeypatch):
        """PKCE S256: the challenge goes to the consent screen, the verifier
        only to the token exchange — an intercepted code is then worthless."""
        opened: list[str] = []
        _wire_loopback(monkeypatch, {"code": ["auth-code-123"]}, opened=opened)

        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"refresh_token": "rt-abc"}
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        run_authorization_flow(self._config())

        # The browser thread is a daemon; give it a moment to record the URL.
        for _ in range(100):
            if opened:
                break
            time.sleep(0.01)
        assert opened, "consent URL was never opened"
        query = parse_qs(urlparse(opened[0]).query)
        assert query["state"] == [_STATE]
        assert query["code_challenge_method"] == ["S256"]
        challenge = query["code_challenge"][0]
        assert "=" not in challenge  # base64url, unpadded

        verifier = fake_session.post.call_args.kwargs["data"]["code_verifier"]
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        assert challenge == expected

    def test_server_closed_even_on_error(self, monkeypatch):
        """server_close() runs even if handle_request blows up — no leaked
        listening socket on a failed attempt."""
        fake_server = MagicMock()
        fake_server.handle_request.side_effect = RuntimeError("boom")
        monkeypatch.setattr("app.google_oauth._run_local_server", lambda timeout_seconds: (fake_server, 1))
        monkeypatch.setattr("app.google_oauth.webbrowser.open", lambda url: None)

        with pytest.raises(RuntimeError):
            run_authorization_flow(self._config())
        fake_server.server_close.assert_called_once()
