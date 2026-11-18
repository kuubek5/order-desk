"""Капа й назва архіву в листі (`app/services/mail_letter_signals.py`, 18.11.26).

Два листи власника того самого дня — обидва фрезерована капа, а поле ставало
«PMMA» (у другому першим чіпом ішов `pmma a3`). Цех пише такі роботи `kappa`
без кольору. І матеріал з кольором, що їх клієнт написав лише в назві архіву:
після розпакування назва губилась."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import Attachment, EmailMessage, Order
from app.routers import mail as mail_router_mod
from app.services import material_suggest
from app.services.mail_letter_signals import archive_names, is_splint_letter, text_says_splint
from tests.test_mail_transaction_safety import _database, _user, _wire

CASE_1_FILES = [
    "2026-11-17_00006-001.constructionInfo",
    "2026-11-17_00006-001-37-36-35-34-33-32-31-41-42-43-44-45-46-47-bitesplint_cad.stl",
]
CASE_2_FILES = [
    "Акуленко__splint-KD-46-bitesplint_cad.stl",
    "Акуленко__splint-KD.constructionInfo",
    "Акуленко__splint-KD.dentalProject",
    "Акуленко__splint-KD.sfv",
]


# ── Ознаки ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("files", [CASE_1_FILES, CASE_2_FILES])
def test_owner_letters_are_splints_by_file_name_alone(files):
    assert is_splint_letter("", files) is True


@pytest.mark.parametrize(
    "text",
    ["сплінт РММА прозорий", "робота- шина фрезерована прозора ПММА", "Капа на фрезерування",
     "kappa", "Bite splint", "міорелаксуюча капа"],
)
def test_splint_words_in_text(text):
    assert text_says_splint(text) is True


@pytest.mark.parametrize("text", ["моно а3", "Шинкаренко", "капітан", "pmma a2 тимчасові"])
def test_ordinary_words_are_not_a_splint(text):
    assert text_says_splint(text) is False


def test_text_names_splint_for_model_without_type():
    assert is_splint_letter("капа прозора", ["model.stl"]) is True


def test_letter_with_crowns_too_is_not_all_splint():
    files = ["2026-11-17_00006-001-46-bitesplint_cad.stl", "2026-11-17_00006-001-11-crown_cad.stl"]
    assert is_splint_letter("капа і коронка", files) is False


def test_plain_crown_letter_is_not_a_splint():
    assert is_splint_letter("моно а3", ["2026-11-17_00006-001-11-crown_cad.stl"]) is False


def test_archive_names_are_read_without_extension():
    atts = [
        SimpleNamespace(filename="a.stl", archive_name="Іванов_zr A2.zip"),
        SimpleNamespace(filename="b.stl", archive_name="Іванов_zr A2.zip"),
        SimpleNamespace(filename="c.stl", archive_name=None),
    ]
    assert archive_names(atts) == ["Іванов zr A2"]


# ── Картка й рядок списку ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_material_cache():
    material_suggest.invalidate_cache()
    yield
    material_suggest.invalidate_cache()


def _shop_history(db: Session) -> None:
    """Як цех пише в таблиці: `kappa` без кольору, `pmma a3`, `mono a2`."""
    for text in ["kappa"] * 6 + ["pmma a3"] * 8 + ["mono a2"] * 8:
        db.add(Order(source="email", material_color=text, quantity="1", created_at=datetime.now()))
    db.commit()


def _email(db: Session, spool, subject: str, body: str, files: list[str], archive: str | None = None):
    email = EmailMessage(uid="u1", status="нове", from_address="lumi@ukr.net",
                         subject=subject, body_text=body, attachments_status="ready",
                         material_color_guess="ПММА")
    db.add(email)
    db.flush()
    for name in files:
        path = spool / name
        path.write_bytes(b"STL")
        db.add(Attachment(email_message_id=email.id, filename=name, saved_path=str(path),
                          archive_name=archive))
    db.commit()
    return email


@pytest.mark.parametrize(
    ("subject", "body", "files"),
    [
        ("Мостова Валерія сплінт РММА прозорий", "сплінт РММА прозорий", CASE_1_FILES),
        ("фрезерування", "Доброго дня! робота- шина фрезерована прозора ПММА", CASE_2_FILES),
    ],
)
def test_owner_splint_letters_fill_kappa(tmp_path, monkeypatch, subject, body, files):
    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _shop_history(db)
        email = _email(db, mail_root / "u1", subject, body, files)
        ctx = mail_router_mod._mail_panel_context(db, email, user)
    assert ctx["material_color"] == "kappa"
    assert ctx["material_cands"][0].text == "kappa"
    # «ПММА» в тексті капи — її ж матеріал, а не друга робота.
    assert ctx["material_mix"] == []


def test_operator_value_is_not_overwritten_for_splint(tmp_path, monkeypatch):
    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _shop_history(db)
        email = _email(db, mail_root / "u1", "сплінт", "", CASE_1_FILES)
        ctx = mail_router_mod._mail_panel_context(db, email, user, material_color="pmma a2")
    assert ctx["material_color"] == "pmma a2"


def test_without_kappa_in_shop_data_nothing_is_invented(tmp_path, monkeypatch):
    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        for _ in range(8):
            db.add(Order(source="email", material_color="pmma a3", quantity="1", created_at=datetime.now()))
        db.commit()
        email = _email(db, mail_root / "u1", "сплінт", "", CASE_1_FILES)
        ctx = mail_router_mod._mail_panel_context(db, email, user)
    assert ctx["material_color"] != "kappa"


def test_material_and_colour_from_archive_name(tmp_path, monkeypatch):
    """У листі ні матеріалу, ні кольору — лише в назві архіву."""
    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _shop_history(db)
        email = _email(db, mail_root / "u1", "робота", "Доброго дня", ["crown.stl"],
                       archive="Петренко моноліт A2.zip")
        email.material_color_guess = None
        db.commit()
        ctx = mail_router_mod._mail_panel_context(db, email, user)
    assert ctx["material_color"] == "mono a2"


def test_letter_text_beats_archive_name(tmp_path, monkeypatch):
    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _shop_history(db)
        email = _email(db, mail_root / "u1", "pmma a3", "pmma a3", ["crown.stl"], archive="робота.zip")
        email.material_color_guess = "pmma a3"
        db.commit()
        ctx = mail_router_mod._mail_panel_context(db, email, user)
    assert ctx["material_color"] == "pmma a3"


def test_extracted_files_remember_their_archive(tmp_path):
    import zipfile

    from app.mail_reader import extract_archive_attachments

    engine = _database()
    archive = tmp_path / "Іванов zr A2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("crown.stl", b"STL")
    with Session(engine, expire_on_commit=False) as db:
        email = EmailMessage(uid="u1", status="нове")
        db.add(email)
        db.flush()
        db.add(Attachment(email_message_id=email.id, filename=archive.name, saved_path=str(archive)))
        db.commit()
        extracted, errors = extract_archive_attachments(db, email)
        db.commit()
        rows = db.query(Attachment).filter(Attachment.email_message_id == email.id).all()
    assert (extracted, errors) == (1, [])
    assert [(r.filename, r.archive_name) for r in rows] == [("crown.stl", "Іванов zr A2.zip")]
