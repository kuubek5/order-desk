"""«Табличка на дверях»: синк не створює дублів, поки додають руками.

Ручне додавання робиться у ДВА кроки — рядок у таблицю, потім робота в базу, —
і між ними є щілина. Фоновий синк, влучивши в неї, бачить рядок у таблиці,
не знаходить його в базі й СТВОРЮЄ свою копію тієї самої роботи. Коронку
прораховують і фрезерують двічі (аудит 08.09.26).

Чому не обмеження в базі: спробував і відкотив — при зсуві рядків синк
перенумеровує роботи, і два записи законно ділять номер посеред перенумерації
(докази в RESILIENCE_PLAN.md).
"""

import pytest

from app.manual_add_flag import (
    _TTL_SECONDS,
    _reset_for_tests,
    manual_add_in_flight,
    manual_add_running,
)


@pytest.fixture(autouse=True)
def _clean():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_flag_is_down_by_default():
    assert manual_add_running() is False


def test_flag_is_up_inside_the_block():
    with manual_add_in_flight():
        assert manual_add_running() is True
    assert manual_add_running() is False


def test_flag_comes_down_even_when_the_add_fails():
    """Виняток посеред додавання не має лишати позначку висіти."""
    with pytest.raises(ValueError):
        with manual_add_in_flight():
            raise ValueError("таблиця не відповіла")
    assert manual_add_running() is False


def test_two_operators_do_not_take_the_sign_from_each_other():
    """Вихід першого не знімає позначку другого — тому лічильник, а не булеве."""
    with manual_add_in_flight():
        with manual_add_in_flight():
            assert manual_add_running() is True
        assert manual_add_running() is True
    assert manual_add_running() is False


def test_a_forgotten_flag_expires_by_itself(monkeypatch):
    """Головна відмінність від замка: забутий замок тримається вічно, забута
    позначка перестає діяти сама. Синк не може стати мертво."""
    import app.manual_add_flag as flag

    clock = {"t": 1000.0}
    monkeypatch.setattr(flag, "monotonic", lambda: clock["t"])

    entered = manual_add_in_flight()
    entered.__enter__()          # навмисно НЕ виходимо — імітуємо падіння
    assert manual_add_running() is True

    clock["t"] += _TTL_SECONDS + 1
    assert manual_add_running() is False, (
        "позначка не протухла — забутий прапорець зупинив би створення робіт "
        "назавжди, а це гірше за проблему, яку він лікує"
    )
