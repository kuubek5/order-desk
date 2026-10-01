"""Зведення Конвеєра для мультипрорахунку (власник 02.10.26)."""

from __future__ import annotations

from app.services.mail_conveyor import ConveyorCard, conveyor_summary, material_group_key


def _c(eid, client, material, qty=""):
    return ConveyorCard(email_id=eid, client=client, material=material, quantity=qty)


def test_cyrillic_and_spacing_variants_are_one_disc():
    assert material_group_key("моно А 3,5") == material_group_key("mono a3.5")
    assert material_group_key("пмма а2") == material_group_key("PMMA A2")
    assert material_group_key("pmma a2") != material_group_key("pmma a3")


def test_majority_material_units_and_clients():
    s = conveyor_summary([
        _c(1, "Середюк", "pmma a2", "1"),
        _c(2, "Середюк", "pmma a2", "14"),
        _c(3, "Іваненко", "пмма а2", "2"),
        _c(4, "Петренко", "pmma a3", "2"),
    ])
    assert s.label == "pmma a2"
    assert (s.letters, s.units, s.clients, s.total) == (3, 17, 2, 4)
    assert s.odd_ids == {4}


def test_unknown_quantity_is_counted_not_guessed():
    s = conveyor_summary([_c(1, "А", "mono a3", "3"), _c(2, "Б", "mono a3", "")])
    assert (s.units, s.units_unknown) == (3, 1)


def test_tie_goes_to_the_letter_higher_in_the_list():
    s = conveyor_summary([_c(1, "А", "mono a3"), _c(2, "Б", "emo a2")])
    assert s.label == "mono a3" and s.odd_ids == {2}


def test_card_without_material_is_neither_main_nor_odd():
    s = conveyor_summary([_c(1, "А", "mono a3"), _c(2, "Б", "")])
    assert s.letters == 1 and s.odd_ids == set()
    assert conveyor_summary([_c(1, "А", "")]).label == ""
