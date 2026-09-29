"""Бейдж «опак» у пошті: замовник просить покрити коронку опаком власними
словами (власник 29.09.26: «блокир», «заопачити», «з опаком» — усе одне)."""

import pytest

from app.services.opak import letter_opak_hint


@pytest.mark.parametrize(
    "text",
    [
        "МОНОЛИТ А3 БЛОКИР.",  # бойовий лист Франчук 29.09.26
        "Робота N2 Середюк Олег 1 одиниця Колір A3.5 Monolith З ОПАКОМ!",  # Sereduk 29.09.26
        "моно а2, опак",
        "заопачити 2 коронки",
        "опачити будь ласка",
        "треба заблокувати",
        "заблокировать а3",
        "блокуйте",
        "mono A2 opaq",
        "with opaque please",
        "покрити опаком 14,15",
    ],
)
def test_customer_asks_for_opak(text):
    assert letter_opak_hint(text)


@pytest.mark.parametrize(
    "text",
    [
        "моно а3 без опаку",
        "без опака, дякую",
        "не покривати опаком",
        "не блокувати",
        "блок коронок 13-23",  # конструкція, не покриття
        "запакуйте надійно",  # «пак» усередині слова — не опак
        "Колір A3.5 Monolith",
        "",
        None,
    ],
)
def test_no_opak_badge(text):
    assert letter_opak_hint(text) is None
