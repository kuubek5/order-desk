"""Файли і БД не мають розходитись (аудит 05.09.26, пошта C-1/C-2).

Прийняття листа спершу ФІЗИЧНО переносить вкладення в export, а вже потім
комітить метадані. Якщо коміт падає (SQLite locked, диск повний під WAL),
транзакція відкочується — і без компенсації файли лишаються в export, тоді як
БД вважає їх у спулі: тріаж каже «файли зникли», хоча вони на місці.
Відкат прийняття (`restore_email`) — дзеркальний випадок.
"""

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
    return mail_router_mod.accept_email(
        request=_request(user.id), email_id=email.id,
        client_name="Люмі-Дент", material_color="моно а3", kind="", quantity="",
        folder_pick="", folder_new="", material_folder="", attachment_ids=[], db=db,
    )


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


def test_failed_first_commit_never_writes_the_sheet_placeholder_row(tmp_path, monkeypatch):
    """F14: `_write_placeholder_row` appends a REAL row to the shared Google
    Sheet. If it ran before the DB commit, a failed commit would compensate
    file moves but leave that row behind — the next sync would import it as a
    second наряд-less order. It must run only AFTER a successful commit, so a
    failing commit must never reach it at all."""
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)

    placeholder_calls = []
    monkeypatch.setattr(
        mail_accept_svc, "append_mail_placeholder_row",
        lambda *a, **k: placeholder_calls.append((a, k)) or 999,
    )
    # Дати `_write_placeholder_row` дійсний аркуш — інакше вона піде гілкою
    # "вкладки немає" й нічого не доведе про порядок викликів.
    fake_worksheet = SimpleNamespace(title="01.01.26")
    monkeypatch.setattr(
        mail_accept_svc, "latest_worksheet_on_or_before",
        lambda *args, **kwargs: fake_worksheet,
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, stl = _letter(db, mail_root / "u1")

        def failing_commit():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "commit", failing_commit)
        response = _accept(db, user, email)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    assert placeholder_calls == []


def test_successful_accept_writes_the_sheet_placeholder_row_once(tmp_path, monkeypatch):
    """F14 (успішний шлях): рядок-нотатка пишеться РІВНО раз, після коміту
    бази, і привʼязує щойно записаний рядок таблиці до нової роботи."""
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)

    placeholder_calls = []

    def _fake_append(worksheet, client_name, quantity, material_color, **kwargs):
        placeholder_calls.append((client_name, quantity, material_color))
        return 65  # рядок таблиці (з урахуванням заголовків)

    monkeypatch.setattr(mail_accept_svc, "append_mail_placeholder_row", _fake_append)
    fake_worksheet = SimpleNamespace(title="01.01.26")
    # `_wire` stubs `open_spreadsheet` to throw (no other test here needs the
    # sheet), which makes `_resolve_target_tab` swallow the error and return
    # worksheet=None before `latest_worksheet_on_or_before` is even called.
    # This test is specifically about the sheet write happening — give it a
    # spreadsheet that resolves.
    monkeypatch.setattr(mail_accept_svc, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(
        mail_accept_svc, "latest_worksheet_on_or_before",
        lambda *args, **kwargs: fake_worksheet,
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, stl = _letter(db, mail_root / "u1")
        response = _accept(db, user, email)
        assert response.status_code == 303
        assert "error=" not in response.headers["location"]

    assert len(placeholder_calls) == 1

    with Session(engine) as db:
        order = db.scalar(select(Order))
        assert order is not None
        from app.parser import HEADER_ROWS
        assert order.row_number == 65 - HEADER_ROWS


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
        response = mail_router_mod.restore_email(request=_request(user.id), email_id=email.id, db=db)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    # Файл знову в export — там, куди дивиться saved_path у БД.
    assert Path(moved).is_file()
    assert Path(moved).read_bytes() == b"STL"
    assert not stl.exists()
    with Session(engine) as db:
        assert db.scalar(select(Attachment)).saved_path == moved
        assert db.scalars(select(Order)).all() != []


def test_network_blink_does_not_accept_a_letter_whose_files_stayed_in_the_spool(
    tmp_path, monkeypatch
):
    """M.2: `Path.exists()` ковтає будь-яку OSError, тож коротке моргання
    мережевої шари виглядає точно як видалений файл. Наслідок несиметричний:
    файл не їде в export, а лист усе одно позначається «прийнято» — робота
    тихо лишається в спулі назавжди. Тепер перевірка йде через
    `_file_is_missing`, який вважає втратою лише чистий FileNotFoundError."""
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)

    blinked = {"n": 0}
    real_stat = Path.stat

    def flaky_stat(self, *a, **k):
        # Перші звернення до файлу вкладення — «шара недоступна», далі норма.
        if self.name == "crown.stl" and blinked["n"] < 1:
            blinked["n"] += 1
            raise OSError(64, "The specified network name is no longer available")
        return real_stat(self, *a, **k)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, stl = _letter(db, mail_root / "u1")

        # Після першого підняття OSError stat поводиться нормально, тож
        # скасовувати підміну не треба (і не варто: undo зняв би й решту).
        monkeypatch.setattr(Path, "stat", flaky_stat)
        _accept(db, user, email)

        db.refresh(email)
        moved = list(export_root.rglob("crown.stl"))

    assert blinked["n"] == 1, "тест мусить справді змоделювати моргання"
    assert moved, "після моргання файл усе одно має переїхати в export"
    assert email.status == "прийнято"
