"""Групування Конвеєра (власник 02.10.26).

Листи одного клієнта з тим самим матеріалом, прийняті РАЗОМ одним Конвеєром,
лягають в одну теку. Прийняті окремо (зранку один, увечері другий) — як і
раніше, у різні: нові файли не змішуються з уже прорахованими. Видача бачить
одну теку з файлами обох листів під обома рядками; повернення одного листа
забирає з теки лише його файли.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.export_scanner import entry_for_folder
from app.models import Attachment, EmailMessage, Order
from app.routers import mail as mail_router_mod
from tests.test_mail_transaction_safety import _database, _request, _user, _wire


def _letter(db: Session, spool: Path, uid: str, filename: str = "crown.stl") -> EmailMessage:
    folder = spool / uid
    folder.mkdir(parents=True, exist_ok=True)
    stl = folder / filename
    stl.write_bytes(f"STL-{uid}".encode())
    email = EmailMessage(uid=uid, status="нове", from_address="lumi@ukr.net",
                         subject="моно а3", attachments_status="ready")
    db.add(email)
    db.flush()
    db.add(Attachment(email_message_id=email.id, filename=filename, saved_path=str(stl)))
    db.commit()
    return email


def _batch(monkeypatch, db, user, cards):
    # _wire підміняє TemplateResponse на «віддай контекст»; батчу потрібні ще
    # й заголовки відповіді — даємо обʼєкт із ними.
    monkeypatch.setattr(
        mail_router_mod.templates, "TemplateResponse",
        lambda request, template, context: SimpleNamespace(headers={}, context=context),
    )
    return mail_router_mod.accept_email_batch(
        request=_request(user.id), payload=json.dumps(cards), confirm_missing="1", db=db
    )


def _card(email, material="моно а3", client="Люмі-Дент", **extra):
    return {"email_id": email.id, "client_name": client, "material_color": material, **extra}


def test_two_letters_of_one_client_and_colour_in_one_conveyor_share_a_folder(tmp_path, monkeypatch):
    engine = _database()
    export_root, spool = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        a = _letter(db, spool, "a1")
        b = _letter(db, spool, "b1")  # те саме ім'я файлу — не перезаписати
        out = _batch(monkeypatch, db, user, [_card(a), _card(b)])
        assert len(out.context["accepted"]) == 2, out.context
    with Session(engine) as db:
        folders = {o.export_folder_path for o in db.scalars(select(Order))}
        assert len(folders) == 1
        rel = folders.pop()
        assert rel.split("/")[2] == "моно а3"  # не « (2)»
        files = sorted(p.name for p in export_root.joinpath(*rel.split("/")).iterdir())
        assert files == ["crown (2).stl", "crown.stl"]
    # Видача: тека одна, і в ній файли обох листів.
    entry = entry_for_folder(export_root, rel)
    assert entry is not None and len(entry.files) == 2


def test_morning_letter_keeps_its_folder_evening_conveyor_gets_a_new_one(tmp_path, monkeypatch):
    """Окреме прийняття — окрема тека: ранкова вже прорахована, вечірні два
    листи лягають разом, але в НОВУ « (2)», а не в ранкову."""
    engine = _database()
    export_root, spool = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        morning = _letter(db, spool, "m1")
        _batch(monkeypatch, db, user, [_card(morning)])
        e1 = _letter(db, spool, "e1")
        e2 = _letter(db, spool, "e2")
        _batch(monkeypatch, db, user, [_card(e1), _card(e2)])
        by_email = {o.source_email_id: o.export_folder_path for o in db.scalars(select(Order))}
    assert by_email[morning.id].endswith("/моно а3")
    assert by_email[e1.id] == by_email[e2.id]
    assert by_email[e1.id].endswith("/моно а3 (2)")


def test_different_colour_or_client_is_not_grouped(tmp_path, monkeypatch):
    engine = _database()
    _export_root, spool = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        a = _letter(db, spool, "a1")
        b = _letter(db, spool, "b1")
        c = _letter(db, spool, "c1")
        _batch(monkeypatch, db, user, [
            _card(a), _card(b, material="pmma a2"), _card(c, client="Інший Клієнт"),
        ])
        folders = [o.export_folder_path for o in db.scalars(select(Order))]
    assert len(set(folders)) == 3


def test_returning_one_grouped_letter_takes_only_its_files(tmp_path, monkeypatch):
    engine = _database()
    export_root, spool = _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        a = _letter(db, spool, "a1")
        b = _letter(db, spool, "b1", filename="bridge.stl")
        _batch(monkeypatch, db, user, [_card(a), _card(b)])
        rel = db.scalar(select(Order).where(Order.source_email_id == b.id)).export_folder_path
        response = mail_router_mod.restore_email(request=_request(user.id), email_id=b.id, db=db)
        assert response.status_code == 303 and "error=" not in response.headers["location"]
    folder = export_root.joinpath(*rel.split("/"))
    assert [p.name for p in folder.iterdir()] == ["crown.stl"]  # лист «a» лишився
    assert (spool / "b1" / "bridge.stl").is_file()  # «b» повернувся в свою теку спулу


def test_join_ignores_a_folder_outside_export(tmp_path):
    from app.mail_export import _joinable_material_dir

    root = tmp_path / "export"
    (root / "Кл" / "02.10.26" / "моно а3").mkdir(parents=True)
    assert _joinable_material_dir(root, "Кл/02.10.26/моно а3") is not None
    assert _joinable_material_dir(root, "Кл/02.10.26/немає") is None
    assert _joinable_material_dir(root, "../x/y") is None
    assert _joinable_material_dir(root, "Кл/02.10.26") is None
    assert _joinable_material_dir(root, "") is None
