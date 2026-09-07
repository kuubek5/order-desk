"""Бейджі рейки не відкривають сесію на кожен рендер (K.7).

Кожен із пʼяти глобалів (сповіщення, записки зміни, зайняті оператори, стан
синку, звернення) відкривав ВЛАСНУ сесію, а один екран малює себе й кілька
партіалів — на сторінку виходило під десяток зайвих походів до бази, яка
лежить на мережевій шарі.
"""

import time

import pytest

from app.routers import deps


@pytest.fixture(autouse=True)
def _fresh_cache():
    deps.clear_global_badge_cache()
    yield
    deps.clear_global_badge_cache()


def test_repeated_renders_share_one_computation(monkeypatch):
    calls = []
    monkeypatch.setattr(deps, "shift_pending_uncached", lambda: calls.append(1) or 7)

    first = deps.shift_pending()
    second = deps.shift_pending()
    third = deps.shift_pending()

    assert [first, second, third] == [7, 7, 7]
    assert calls == [1], "один рендер сторінки з партіалами = один похід у базу"


def test_value_refreshes_after_the_window(monkeypatch):
    """Витримка коротка навмисно: рейку однаково оновлює полл, і бейдж не має
    відставати від дії помітно для ока."""
    values = iter([1, 2])
    monkeypatch.setattr(deps, "shift_pending_uncached", lambda: next(values))
    monkeypatch.setattr(deps, "_GLOBALS_TTL_SECONDS", 0.01)

    assert deps.shift_pending() == 1
    time.sleep(0.02)
    assert deps.shift_pending() == 2


def test_each_badge_is_cached_separately(monkeypatch):
    monkeypatch.setattr(deps, "shift_pending_uncached", lambda: 3)
    monkeypatch.setattr(deps, "feedback_open_count_uncached", lambda: 5)

    assert deps.shift_pending() == 3
    assert deps.feedback_open_count() == 5


def test_a_failing_badge_still_never_breaks_the_render(monkeypatch):
    """Контракт кожного з цих глобалів: індикатор не сміє завалити сторінку."""
    monkeypatch.setattr(deps, "sync_state_uncached", lambda: None)

    assert deps.sync_state() is None
