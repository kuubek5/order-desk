"""Тека клієнта для пошти — з картки клієнта (власник 25.09.26).

Досі пошта брала теку лише з памʼяті відправника (за email), а тека,
прив'язана в картці клієнта («Клієнти»), діяла тільки на видачу. Тепер:
картка → памʼять відправника (лише для того самого імені) → нечіткий збіг.
Ручний вибір оператора перемагає все.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.client_folder import card_folder_for, preferred_client_folder
from app.models import ClientNameAlias, ClientSenderMemory, Order
from app.routers import mail as mail_router_mod
from tests.test_mail_transaction_safety import _database, _letter, _request, _user, _wire


def _bind(db, name, folder, confirmed=True):
    db.add(ClientNameAlias(sheet_name=name, export_folder_name=folder, confirmed=confirmed))
    db.commit()


def _accept(db, user, email, client_name="Люмі-Дент", folder_pick=""):
    return mail_router_mod.accept_email(
        request=_request(user.id), email_id=email.id,
        client_name=client_name, material_color="моно а3", kind="", quantity="",
        folder_pick=folder_pick, folder_new="", material_folder="", attachment_ids=[], db=db,
    )


def _landed_in(engine) -> str:
    with Session(engine) as db:
        order = db.scalar(select(Order))
        return order.export_folder_path.split("/", 1)[0]


def test_card_folder_wins_over_sender_memory(tmp_path, monkeypatch):
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    (export_root / "Люмі Дент (картка)").mkdir()
    (export_root / "Люмі-Дент").mkdir()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _bind(db, "Люмі-Дент", "Люмі Дент (картка)")
        db.add(ClientSenderMemory(sender_key="lumi@ukr.net", client_name="Люмі-Дент",
                                  export_folder="Люмі-Дент", orders_count=3, last_seen_at=datetime(2026, 9, 1)))
        db.commit()
        email, _ = _letter(db, mail_root / "u1")
        _accept(db, user, email)
    assert _landed_in(engine) == "Люмі Дент (картка)"


def test_sender_memory_used_when_card_has_no_folder(tmp_path, monkeypatch):
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    (export_root / "Lumi Dent стара").mkdir()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(ClientSenderMemory(sender_key="lumi@ukr.net", client_name="Люмі-Дент",
                                  export_folder="Lumi Dent стара", orders_count=3, last_seen_at=datetime(2026, 9, 1)))
        db.commit()
        email, _ = _letter(db, mail_root / "u1")
        _accept(db, user, email)
    assert _landed_in(engine) == "Lumi Dent стара"


def test_sender_memory_does_not_follow_a_different_client_name(tmp_path, monkeypatch):
    """Оператор змінив ім'я клієнта в картці листа — тека попереднього клієнта
    з памʼяті відправника не має туди переходити."""
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    (export_root / "Lumi Dent стара").mkdir()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(ClientSenderMemory(sender_key="lumi@ukr.net", client_name="Люмі-Дент",
                                  export_folder="Lumi Dent стара", orders_count=3, last_seen_at=datetime(2026, 9, 1)))
        db.commit()
        email, _ = _letter(db, mail_root / "u1")
        _accept(db, user, email, client_name="Зовсім Інша Клініка")
    assert _landed_in(engine) == "Зовсім Інша Клініка"


def test_card_folder_missing_on_disk_falls_back_to_fuzzy(tmp_path, monkeypatch):
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    (export_root / "Люмі-Дент").mkdir()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _bind(db, "Люмі-Дент", "Перейменована тека")  # на диску її немає
        email, _ = _letter(db, mail_root / "u1")
        _accept(db, user, email)
    assert _landed_in(engine) == "Люмі-Дент"


def test_manual_pick_beats_the_card_folder(tmp_path, monkeypatch):
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    (export_root / "Люмі Дент (картка)").mkdir()
    (export_root / "Ручна").mkdir()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _bind(db, "Люмі-Дент", "Люмі Дент (картка)")
        email, _ = _letter(db, mail_root / "u1")
        _accept(db, user, email, folder_pick="Ручна")
    assert _landed_in(engine) == "Ручна"


def test_card_folder_lookup_ignores_case_and_unconfirmed(tmp_path):
    engine = _database()
    with Session(engine) as db:
        _bind(db, "Люмі-Дент", "Тека A")
        _bind(db, "Непідтверджений", "Тека B", confirmed=False)
        assert card_folder_for(db, "  люмі-дент ") == "Тека A"
        assert card_folder_for(db, "Непідтверджений") is None
        assert card_folder_for(db, "") is None
        hint = SimpleNamespace(client_name="Люмі-Дент", export_folder="Памʼять")
        assert preferred_client_folder(db, "Люмі-Дент", hint) == "Тека A"
        assert preferred_client_folder(db, "Хтось", hint) is None
        assert preferred_client_folder(db, "", hint) == "Памʼять"
