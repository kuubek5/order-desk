"""app/sheets.py transient-error classification and bounded retry with
backoff. No real network or sleeping: the failing callables are fakes and
call_with_retry's sleep is injected as a recorder."""

import gspread
import pytest
import requests

from app.sheets import call_with_retry, is_transient_sheet_error


def _api_error(status_code: int) -> gspread.exceptions.APIError:
    """Build a real gspread APIError carrying the given HTTP status, the way
    is_transient_sheet_error reads it (exc.response.status_code)."""
    from unittest.mock import MagicMock

    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = {"error": {"code": status_code, "message": "x"}}
    response.text = "x"
    return gspread.exceptions.APIError(response)


# --- is_transient_sheet_error -------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_google_rate_limit_and_5xx_are_transient(status):
    assert is_transient_sheet_error(_api_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_other_than_429_are_permanent(status):
    # A revoked service account (403) or wrong Sheet ID (404) must fail fast,
    # not spin through retries.
    assert is_transient_sheet_error(_api_error(status)) is False


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.SSLError("renegotiation"),
        requests.exceptions.Timeout("slow"),
    ],
)
def test_connection_ssl_timeout_are_transient(exc):
    assert is_transient_sheet_error(exc) is True


def test_unrelated_exception_is_not_transient():
    assert is_transient_sheet_error(ValueError("bug")) is False


# --- call_with_retry -----------------------------------------------------


def test_returns_first_result_without_sleeping():
    sleeps: list[float] = []
    result = call_with_retry(lambda: "ok", sleep=sleeps.append)
    assert result == "ok"
    assert sleeps == []


def test_retries_transient_then_succeeds_with_exponential_backoff():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _api_error(503)
        return "recovered"

    result = call_with_retry(flaky, base_delay=1.0, sleep=sleeps.append)
    assert result == "recovered"
    assert calls["n"] == 3
    # two failures before success → two backoff waits: 1s, 2s
    assert sleeps == [1.0, 2.0]


def test_permanent_error_is_reraised_immediately_without_retry():
    calls = {"n": 0}
    sleeps: list[float] = []

    def boom():
        calls["n"] += 1
        raise _api_error(403)

    with pytest.raises(gspread.exceptions.APIError):
        call_with_retry(boom, sleep=sleeps.append)
    assert calls["n"] == 1
    assert sleeps == []


def test_exhausts_attempts_and_reraises_last_transient_error():
    calls = {"n": 0}
    sleeps: list[float] = []

    def always_flaky():
        calls["n"] += 1
        raise _api_error(429)

    with pytest.raises(gspread.exceptions.APIError):
        call_with_retry(always_flaky, attempts=4, base_delay=1.0, sleep=sleeps.append)
    assert calls["n"] == 4  # initial + 3 retries
    # sleeps between the 4 attempts: 1, 2, 4 (no sleep after the last failure)
    assert sleeps == [1.0, 2.0, 4.0]
