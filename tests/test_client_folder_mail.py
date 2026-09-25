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


# ── Картка клієнта за адресою відправника (власник 25.09.26, «Oleksandr») ──────


def _card(db, name, email_field):
    from app.models import Client
    db.add(Client(canonical_name=name, email=email_field))
    db.commit()


def test_card_with_sender_address_names_the_client_and_its_folder(tmp_path, monkeypatch):
    """Адресу вписано в контакти картки «Oleksandr», тека прив'язана, а відправник
    доданий в автоскачування (памʼять з імʼям-заглушкою = адреса). Картка листа
    має відкритись з «Oleksandr» і його текою, а не з теки `lumi@ukr.net`."""
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    (export_root / "Oleksandr").mkdir()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _card(db, "Oleksandr", "+380..., LUMI@ukr.net")
        _bind(db, "Oleksandr", "Oleksandr")
        db.add(ClientSenderMemory(sender_key="lumi@ukr.net", client_name="lumi@ukr.net",
                                  export_folder=None, orders_count=0, auto_accept=True,
                                  last_seen_at=datetime(2026, 9, 1)))
        db.commit()
        email, _ = _letter(db, mail_root / "u1")
        ctx = mail_router_mod._mail_panel_context(db, email, user)
    assert ctx["client_name"] == "Oleksandr"
    assert ctx["preview"]["client_folder"] == "Oleksandr"
    assert ctx["preview"]["client_folder_existing"] is True


def test_placeholder_address_from_auto_download_is_not_a_name(tmp_path, monkeypatch):
    """Без картки: заглушка-адреса з «Автоскачування» не стає імʼям — береться
    показне імʼя відправника."""
    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(ClientSenderMemory(sender_key="lumi@ukr.net", client_name="lumi@ukr.net",
                                  export_folder=None, orders_count=0, auto_accept=True,
                                  last_seen_at=datetime(2026, 9, 1)))
        db.commit()
        email, _ = _letter(db, mail_root / "u1")
        email.from_name = "Люмі-Дент"
        db.commit()
        ctx = mail_router_mod._mail_panel_context(db, email, user)
    assert ctx["client_name"] == "Люмі-Дент"


def test_two_cards_with_same_address_are_not_guessed(tmp_path, monkeypatch):
    from app.client_folder import client_for_sender

    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        _card(db, "Клініка А", "lumi@ukr.net")
        _card(db, "Клініка Б", "lumi@ukr.net")
        email, _ = _letter(db, mail_root / "u1")
        assert client_for_sender(db, email) is None


def test_real_remembered_name_still_wins_over_from_name(tmp_path, monkeypatch):
    """Без картки справжнє імʼя з памʼяті (після прийняття) лишається першим."""
    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(ClientSenderMemory(sender_key="lumi@ukr.net", client_name="Люмі-Дент",
                                  export_folder=None, orders_count=3,
                                  last_seen_at=datetime(2026, 9, 1)))
        db.commit()
        email, _ = _letter(db, mail_root / "u1")
        email.from_name = "Lumi Admin"
        db.commit()
        ctx = mail_router_mod._mail_panel_context(db, email, user)
    assert ctx["client_name"] == "Люмі-Дент"
