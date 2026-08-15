"""app/google_oauth.py: parsing the Desktop-client JSON and the loopback
authorization flow (no real network — the token exchange and the browser
open are both mocked)."""

import json
from unittest.mock import MagicMock

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


class TestRunAuthorizationFlow:
    """The loopback flow: local server catches the redirect, then a token
    exchange POST. Both the browser open and the local HTTP server are mocked
    so no real network or browser window is touched."""

    def _config(self):
        return ClientConfig(
            client_id="cid", client_secret="csecret", token_uri="https://oauth2.googleapis.com/token"
        )

    def test_success_returns_refresh_token(self, monkeypatch):
        fake_server = MagicMock()
        fake_server.query_params = {"code": ["auth-code-123"]}

        def fake_run_local_server(timeout_seconds):
            return fake_server, 54321

        monkeypatch.setattr("app.google_oauth._run_local_server", fake_run_local_server)
        monkeypatch.setattr("app.google_oauth.webbrowser.open", lambda url: None)

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
        fake_server = MagicMock()
        fake_server.query_params = {"error": ["access_denied"]}
        monkeypatch.setattr("app.google_oauth._run_local_server", lambda timeout_seconds: (fake_server, 1))
        monkeypatch.setattr("app.google_oauth.webbrowser.open", lambda url: None)

        with pytest.raises(OAuthFlowError, match="відхилив авторизацію"):
            run_authorization_flow(self._config())

    def test_timeout_with_no_code_raises(self, monkeypatch):
        fake_server = MagicMock()
        fake_server.query_params = None  # handle_request timed out, nothing arrived
        monkeypatch.setattr("app.google_oauth._run_local_server", lambda timeout_seconds: (fake_server, 1))
        monkeypatch.setattr("app.google_oauth.webbrowser.open", lambda url: None)

        with pytest.raises(OAuthFlowError, match="тайм-аут"):
            run_authorization_flow(self._config())

    def test_token_exchange_failure_raises(self, monkeypatch):
        fake_server = MagicMock()
        fake_server.query_params = {"code": ["auth-code-123"]}
        monkeypatch.setattr("app.google_oauth._run_local_server", lambda timeout_seconds: (fake_server, 1))
        monkeypatch.setattr("app.google_oauth.webbrowser.open", lambda url: None)

        fake_response = MagicMock(status_code=400, text="invalid_grant")
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        with pytest.raises(OAuthFlowError, match="обмін токена"):
            run_authorization_flow(self._config())

    def test_missing_refresh_token_raises(self, monkeypatch):
        fake_server = MagicMock()
        fake_server.query_params = {"code": ["auth-code-123"]}
        monkeypatch.setattr("app.google_oauth._run_local_server", lambda timeout_seconds: (fake_server, 1))
        monkeypatch.setattr("app.google_oauth.webbrowser.open", lambda url: None)

        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"access_token": "at-only"}  # no refresh_token
        fake_session = MagicMock()
        fake_session.post.return_value = fake_response
        monkeypatch.setattr("app.google_oauth.new_legacy_session", lambda: fake_session)

        with pytest.raises(OAuthFlowError, match="refresh token"):
            run_authorization_flow(self._config())

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
