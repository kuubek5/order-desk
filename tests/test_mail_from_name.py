"""Показне ім'я відправника захоплюється при імпорті листа.

imap-tools кладе його у `from_values.name` окремо від адреси; ми його раніше
викидали. Список тріажу веде саме ним (як ukr.net), тож воно має долітати з
листа, а порожнє/відсутнє — тихо ставати None, щоб список впав на здогад/адресу.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.mail_reader import sender_display_name


def _msg(name):
    return SimpleNamespace(from_values=SimpleNamespace(name=name, email="x@ukr.net"))


def test_display_name_is_returned():
    assert sender_display_name(_msg("Юрій Струбицький")) == "Юрій Струбицький"


def test_blank_name_becomes_none():
    assert sender_display_name(_msg("")) is None
    assert sender_display_name(_msg("   ")) is None


def test_missing_from_values_is_none():
    # Лист без заголовка From: from_values може бути None — не падаємо.
    assert sender_display_name(SimpleNamespace(from_values=None)) is None
    assert sender_display_name(SimpleNamespace()) is None
