"""Файли і БД не мають розходитись (аудит 05.09.26, пошта C-1/C-2).

Прийняття листа спершу ФІЗИЧНО переносить вкладення в export, а вже потім
комітить метадані. Якщо коміт падає (SQLite locked, диск повний під WAL),
транзакція відкочується — і без компенсації файли лишаються в export, тоді як
БД вважає їх у спулі: тріаж каже «файли зникли», хоча вони на місці.
Відкат прийняття (`restore_email`) — дзеркальний випадок.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Attachment, EmailMessage, Order, User
from app.routers import mail as mail_router_mod
from app.services import mail_accept as mail_accept_svc
from app.services import config_state


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Operator")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int):
    return SimpleNamespace(
        session={"user_id": user_id}, client=SimpleNamespace(host="127.0.0.1")
    )


def _wire(monkeypatch, tmp_path):
    """Спул + export + заглушені таблиця й шаблони."""
    export_root = tmp_path / "export"
    export_root.mkdir()
    mail_root = tmp_path / "spool"
    (mail_root / "u1").mkdir(parents=True)
    for module in (mail_router_mod, config_state):
        monkeypatch.setattr(module, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    # Прийняття листа переїхало в сервіс (аудит, крок 2.8), а відкат прийняття
    # лишився в роуті — тож підміняємо в ОБОХ модулях. Якби ми лишили тільки
    # роутер, тест став би зеленим і порожнім: сервіс читав би справжні
    # налаштування (CLAUDE.md §14).
    # Прийняття листа переїхало в сервіс (крок 2.8), відкат прийняття лишився
    # в роуті — тож `get_export_folder_path` і `open_spreadsheet` підміняємо в
    # ОБОХ. `latest_worksheet_on_or_before` тепер кличе лише сервіс, і в роуті
    # цього імені вже немає: підміна там впала б з AttributeError, і це добре —
    # мовчазний no-op був би гіршим (CLAUDE.md §14).
    for module in (mail_router_mod, mail_accept_svc):
        monkeypatch.setattr(module, "get_export_folder_path", lambda _db: str(export_root))
        monkeypatch.setattr(
            module, "open_spreadsheet",
            lambda db=None: (_ for _ in ()).throw(RuntimeError("no sheet")),
        )
    monkeypatch.setattr(
        mail_accept_svc, "latest_worksheet_on_or_before",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    return export_root, mail_root


def _letter(db: Session, spool_dir: Path) -> tuple[EmailMessage, Path]:
    stl = spool_dir / "crown.stl"
    stl.write_bytes(b"STL")
    email = EmailMessage(
        uid="u1", status="нове", from_address="lumi@ukr.net",
        subject="моно а3", attachments_status="ready",
    )
    db.add(email)
    db.flush()
    db.add(Attachment(email_message_id=email.id, filename="crown.stl", saved_path=str(stl)))
    db.commit()
    return email, stl


def _accept(db, user, email):
    return asyncio.run(mail_router_mod.accept_email(
        request=_request(user.id), email_id=email.id,
        client_name="Люмі-Дент", material_color="моно а3", kind="", quantity="",
        folder_pick="", folder_new="", material_folder="", attachment_ids=[], db=db,
    ))


def test_failed_accept_commit_returns_files_to_the_spool(tmp_path, monkeypatch):
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, stl = _letter(db, mail_root / "u1")

        boom = SimpleNamespace(called=False)

        def failing_commit():
            boom.called = True
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "commit", failing_commit)
        response = _accept(db, user, email)

        assert boom.called
        # Оператор бачить помилку, а не 500 і не «успішно прийнято».
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    # Файл повернувся в спул, у export нічого не лишилось.
    assert stl.read_bytes() == b"STL"
    assert [p for p in export_root.rglob("*") if p.is_file()] == []

    # БД не має ані роботи, ані сліду прийняття.
    with Session(engine) as db:
        assert db.scalars(select(Order)).all() == []
        stored = db.scalar(select(EmailMessage))
        assert stored.status == "нове"
        assert db.scalar(select(Attachment)).saved_path == str(stl)


def test_failed_unaccept_commit_returns_files_to_export(tmp_path, monkeypatch):
    """Дзеркальний випадок: відкат прийняття перемістив файли назад у спул, але
    коміт впав → БД знову вважає лист прийнятим, тож файли мусять повернутись
    у export, інакше saved_path показує в порожнечу."""
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, stl = _letter(db, mail_root / "u1")
        _accept(db, user, email)

        moved = db.scalar(select(Attachment)).saved_path
        assert Path(moved).is_file(), "прийняття мало перенести файл у export"
        assert not stl.exists()

        def failing_commit():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "commit", failing_commit)
        response = asyncio.run(
            mail_router_mod.restore_email(request=_request(user.id), email_id=email.id, db=db)
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    # Файл знову в export — там, куди дивиться saved_path у БД.
    assert Path(moved).is_file()
    assert Path(moved).read_bytes() == b"STL"
    assert not stl.exists()
    with Session(engine) as db:
        assert db.scalar(select(Attachment)).saved_path == moved
        assert db.scalars(select(Order)).all() != []
