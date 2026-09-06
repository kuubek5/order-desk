"""Токен бота не має потрапляти в текст помилки (ревʼю 07.09.26).

requests вкладає в повідомлення повний URL `…/bot<TOKEN>/sendMessage`; той
текст осідав у `Feedback.telegram_error`, у бекапах і в `title=` списку звернень.
"""

from app.services.telegram import _net_error


def test_token_is_masked_in_network_errors():
    token = "123456:ABC-secret"
    exc = RuntimeError(f"HTTPSConnectionPool: https://api.telegram.org/bot{token}/sendMessage timed out")
    text = _net_error(exc, token)
    assert token not in text
    assert "***" in text
    assert text.startswith("мережа: ")


def test_empty_token_does_not_break_the_message():
    assert _net_error(RuntimeError("boom"), "") == "мережа: boom"
