"""Видача: два вигляди одного списку і зникла кнопка «Видати».

Картки по клієнтах — щоденний вигляд. Але частину дня оператор веде видачу за
самою Google-таблицею, згори вниз, і тоді картки заважають: у таблиці роботи
одного клієнта стоять врозкид, а картка зводить їх докупи й ламає збіг «рядок у
рядок» (прохання власника 07.09.26).
"""

import pytest

from app.models import User
from app.services.look_prefs import LookError, apply_handout_look


def _user() -> User:
    return User(username="oksana", password_hash="h", full_name="Оксана", role="оператор")


def test_the_flat_mode_is_saved_on_the_account():
    user = _user()

    apply_handout_look(user, flow="sheet")

    assert user.handout_flow == "sheet"


def test_coming_back_to_cards_has_its_own_word():
    """Порожнє поле форми не доходить до сервера відрізнено від «поля не було»
    — обидва читаються як None. Без власного слова кнопка вмикала б плаский
    режим і вже ніколи його не вимикала."""
    user = _user()
    apply_handout_look(user, flow="sheet")

    apply_handout_look(user, flow="clients")

    assert user.handout_flow == ""


def test_one_button_does_not_reset_the_other():
    """У шапці дві незалежні кнопки-іконки. Якби пропущене поле означало
    «порожньо», клік по покажчику дня скидав би порядок списку."""
    user = _user()
    apply_handout_look(user, layout="nav", flow="sheet")

    apply_handout_look(user, layout="list")      # клік лише по розкладці

    assert user.handout_layout == ""
    assert user.handout_flow == "sheet"          # порядок недоторканий


def test_the_day_index_can_be_switched_off_again():
    """Той самий дефект, що й у порядку списку: покажчик дня вмикався, а
    вимкнути його було нічим — порожнє значення сервер читає як «поля не
    було»."""
    user = _user()
    apply_handout_look(user, layout="nav")

    apply_handout_look(user, layout="list")

    assert user.handout_layout == ""


def test_an_unknown_flow_is_an_error_not_a_silent_default():
    """Значення приходить із розмітки; чуже там означає помилку, яку краще
    побачити, ніж мовчки з'їсти."""
    with pytest.raises(LookError):
        apply_handout_look(_user(), flow="вигадка")
