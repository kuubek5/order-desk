"""Кількість одиниць з назв STL (STL_UNITS_BRIEF.md, власник 08.10.26)."""

from __future__ import annotations

import re

import pytest

from app.models import Attachment, EmailMessage
from app.services.stl_units import count_units, letter_quantity, teeth_in_filename
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")

# Тека з живого цеху (скріншот власника 08.10.26): у таблиці в цієї роботи 14.
EXOCAD_FOLDER = [
    "2026-10-06_00033-001",
    "2026-10-06_00033-001.dentalProject",
    "2026-10-06_00033-001.sfv",
    "2026-10-06_00033-001-17-16-15-14-waxup_cad.stl",
    "2026-10-06_00033-001-24-25-26-27-waxup_cad.stl",
    "2026-10-06_00033-001-34-crown_cad.stl",
    "2026-10-06_00033-001-35-crown_cad.stl",
    "2026-10-06_00033-001-44-45-46-47-waxup_cad.stl",
]


def test_live_exocad_folder_gives_the_sheet_quantity():
    result = count_units(EXOCAD_FOLDER)
    assert result.units == 14
    assert result.model_files == 5
    assert result.summary == "14,15,16,17 · 24,25,26,27 · 34,35 · 44,45,46,47"


@pytest.mark.parametrize(
    "name, teeth",
    [
        ("2026-10-06_00033-001-34-crown_cad.stl", [34]),
        ("2026-10-06_00033-001-17-16-15-14-waxup_cad.stl", [17, 16, 15, 14]),
        ("case-11-11-21_cad.stl", [11, 21]),            # повтор в одній назві — раз
        ("2026-10-07_23-37-14-crown.stl", []),           # час після дати — не зуби
        ("Ivanov-55-65-crown.stl", []),                  # молочні не рахуємо
        ("upper_jaw.stl", []),
        ("model-19-09-49.stl", []),                      # не коди FDI
        ("job 36 crown.STL", [36]),
        # Справжні назви з бази dev (08.10.26): назва проєкту — будь-яка.
        ("16.09.2026-Проєкт юшков вч цр-17-16-15-bridge_cad.stl", [17, 16, 15]),
        ("16.09.2026-Проєкт роман цр-46-crown_cad.stl", [46]),
        ("662026-333307-13_00001-001-47-crown_cad (2).stl", [47]),
        ("2024-02-14_7324612-001-41-bitesplint_cad — копия (3).stl", [41]),
        ("24122_11.stl", [11]),
        ("16.09.26 crown 36.stl", [36]),
    ],
)
def test_teeth_in_filename(name, teeth):
    assert teeth_in_filename(name) == teeth


def test_same_tooth_in_two_files_is_one_unit():
    """Рішення власника: головна кількість, не дублікат — waxup і коронка 34
    в одній роботі — одна одиниця, але повтор видно в підказці."""
    result = count_units(["a-34-waxup_cad.stl", "a-34-35-crown_cad.stl", "a-model.stl", "a.zip"])
    assert result.units == 2
    assert result.repeated == [34]
    assert result.without_teeth == ["a-model.stl"]
    assert "у кількох файлах: 34" in result.summary


def test_bite_splint_is_not_auto_counted():
    """Капа: у таблиці «kappa 14», а в назві один зуб — число з назви хибне."""
    result = count_units(["2024-02-14_7324612-001-41-bitesplint_cad.stl"])
    assert result.units == 0 and result.uncounted
    mixed = count_units(["a-41-bitesplint_cad.stl", "a-36-crown_cad.stl"])
    assert mixed.units == 0, "з капою в роботі неповне число не підставляємо"


def test_letter_quantity():
    assert letter_quantity("5") == 5
    assert letter_quantity(" ") is None
    assert letter_quantity("5-6") is None


def _letter(factory, tmp_path, names, quantity_guess=None):
    with factory() as db:
        email = EmailMessage(uid="1", uid_validity="1", from_address="lab@x.ua", subject="робота",
                             status="нове", attachments_status="ready", message_id="<1@x>",
                             quantity_guess=quantity_guess)
        db.add(email)
        db.flush()
        for name in names:
            path = tmp_path / name
            # Свій вміст кожному: однакові файли картка свідомо лишає копіями.
            path.write_bytes(f"solid {name}\nendsolid\n".encode())
            db.add(Attachment(email_message_id=email.id, filename=name, saved_path=str(path)))
        db.commit()
        return email.id


def _qty_field(card: str) -> str:
    match = re.search(r'<input type="text" id="mc-qty"[^>]*>', card)
    assert match, "поле кількості є"
    return match.group(0)


def test_card_fills_quantity_from_files_with_badge(app_db, tmp_path):  # noqa: F811
    app, factory = app_db
    eid = _letter(factory, tmp_path, EXOCAD_FOLDER[3:])
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, card = client.get(f"/mail/{eid}?panel=1")
    assert status == 200, card[:300]
    field = _qty_field(card)
    assert 'value="14"' in field and 'data-qty-auto="14"' in field
    assert re.search(r'<span class="qty-auto"\s+title=', card), "бейдж «?» видно"
    assert "Кількість не сходиться" not in card


def test_card_warns_when_letter_says_otherwise(app_db, tmp_path):  # noqa: F811
    app, factory = app_db
    eid = _letter(factory, tmp_path, EXOCAD_FOLDER[3:], quantity_guess="12")
    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, card = client.get(f"/mail/{eid}?panel=1")
    assert 'value="14"' in _qty_field(card), "поле — з файлів"
    assert "Кількість не сходиться" in card and ">12</b>" in card and ">14</b>" in card


def test_card_without_teeth_keeps_the_letter_guess_and_no_badge(app_db, tmp_path):  # noqa: F811
    app, factory = app_db
    eid = _letter(factory, tmp_path, ["upper_jaw.stl", "lower_jaw.stl"], quantity_guess="3")
    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, card = client.get(f"/mail/{eid}?panel=1")
    field = _qty_field(card)
    assert 'value="3"' in field and "data-qty-auto" not in field
    assert 'class="qty-auto"' not in card


def test_mcp_mail_units_compares_without_leaking_file_names(app_db):  # noqa: F811
    from datetime import timedelta

    from app.business_day import business_today
    from app.models import Order
    from app.services.mcp_tools import tool_mail_units

    _, factory = app_db
    tab = business_today().strftime("%d.%m.%y")
    with factory() as db:
        good = Order(source="email", sheet_tab=tab, client_name="Lab A", quantity="14",
                     material_color="mono a3", status="прийнято")
        bad = Order(source="email", sheet_tab=tab, client_name="Lab B", quantity="3",
                    material_color="mono a3", status="прийнято")
        old = Order(source="email", sheet_tab=(business_today() - timedelta(days=20)).strftime("%d.%m.%y"),
                    client_name="Lab C", quantity="1", material_color="mono a3", status="прийнято")
        db.add_all([good, bad, old])
        db.flush()
        email = EmailMessage(uid="9", uid_validity="1", from_address="x@x.ua", status="прийнято",
                             attachments_status="ready", message_id="<9@x>")
        db.add(email)
        db.flush()
        for name in EXOCAD_FOLDER[3:]:
            db.add(Attachment(email_message_id=email.id, order_id=good.id, filename=name, saved_path="x"))
        db.add(Attachment(email_message_id=email.id, order_id=bad.id,
                          filename="Іваненко-46-47-crown_cad.stl", saved_path="x"))
        db.add(Attachment(email_message_id=email.id, order_id=old.id,
                          filename="a-11-crown_cad.stl", saved_path="x"))
        db.commit()
        out = tool_mail_units(db, {"days": 3})
    assert out["підсумок"] == {"збіг": 1, "розбіжність": 1, "без зубів у назвах": 0, "капа (вручну)": 0}
    first = out["роботи"][0]
    assert first["висновок"] == "розбіжність" and first["з_назв"] == 2 and first["кількість_у_роботі"] == "3"
    assert "Іваненко" not in repr(out), "назви файлів (імена пацієнтів) не віддаються"
