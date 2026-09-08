"""Файли і БД не мають розходитись (аудит 05.09.26, пошта C-1/C-2).

Прийняття листа спершу ФІЗИЧНО переносить вкладення в export, а вже потім
комітить метадані. Якщо коміт падає (SQLite locked, диск повний під WAL),
транзакція відкочується — і без компенсації файли лишаються в export, тоді як
БД вважає їх у спулі: тріаж каже «файли зникли», хоча вони на місці.
Відкат прийняття (`restore_email`) — дзеркальний випадок.
"""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_two_operators_cannot_accept_the_same_letter_at_once(tmp_path, monkeypatch):
    """M.7: операторів двоє (CLAUDE.md §1), а гейт `status != "нове"` стоїть у
    роуті ДО виклику сервісу — між перевіркою і створенням роботи не було
    нічого. Два кліки на одному листі давали дві роботи, два рядки в таблиці й
    подвоєні файли в export. Лок саме на ЛИСТ: паралельне приймання РІЗНИХ
    листів — нормальна робота вдвох."""
    from app.services import mail_accept as svc

    engine = _database()
    _wire(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _ = _letter(db, tmp_path / "spool" / "u1")

        # Перший оператор «усередині» приймання цього листа.
        busy = svc._letter_lock(email.id)
        assert busy.acquire(blocking=False)
        try:
            result = svc.accept_letter(
                db, user, email, client_name="Люмі-Дент", material_color="моно а3"
            )
        finally:
            busy.release()

    assert not result.ok
    assert "інший оператор" in result.error


def test_a_busy_letter_does_not_block_another_one(tmp_path, monkeypatch):
    from app.services import mail_accept as svc

    engine = _database()
    _wire(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        first, _ = _letter(db, tmp_path / "spool" / "u1")
        (tmp_path / "spool" / "u2").mkdir(parents=True)
        stl = tmp_path / "spool" / "u2" / "bridge.stl"
        stl.write_bytes(b"STL")
        second = EmailMessage(uid="u2", status="нове", from_address="lumi@ukr.net",
                              subject="моно а3", attachments_status="ready")
        db.add(second)
        db.flush()
        db.add(Attachment(email_message_id=second.id, filename="bridge.stl",
                          saved_path=str(stl)))
        db.commit()

        busy = svc._letter_lock(first.id)
        assert busy.acquire(blocking=False)
        try:
            result = svc.accept_letter(
                db, user, second, client_name="Люмі-Дент", material_color="моно а3"
            )
        finally:
            busy.release()

    assert result.ok, result.error


def test_a_database_error_after_the_move_still_returns_files_to_the_spool(
    tmp_path, monkeypatch
):
    """C.1: ловимо будь-який виняток, а не лише файловий.

    Файли переїжджають ПЕРШИМИ, і аж потім ідуть записи в базу. Раніше
    прийняття ловило тільки `OSError/ValueError`: помилка бази між переносом і
    комітом вилітала 500-ю, `moved_pairs` помирали разом із кадром — і файли
    лишались у export при листі «нове». Диск і база розходились назавжди.
    """
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, stl = _letter(db, mail_root / "u1")

        def boom(*args, **kwargs):
            raise RuntimeError("несподівана помилка бази")

        # Падає ПІСЛЯ фізичного переносу — це і є небезпечне вікно.
        monkeypatch.setattr(mail_accept_svc, "remember_sender", boom)
        response = _accept(db, user, email)

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    assert stl.read_bytes() == b"STL"                      # файл повернувся в спул
    assert [p for p in export_root.rglob("*") if p.is_file()] == []

    with Session(engine) as db:
        assert db.scalars(select(Order)).all() == []
        assert db.scalar(select(EmailMessage)).status == "нове"
        assert db.scalar(select(Attachment)).saved_path == str(stl)


def test_rejecting_an_accepted_letter_is_refused_not_half_done(tmp_path, monkeypatch):
    """C.2: «відхилено» описує лист, якого не брали в роботу.

    Для вже прийнятого листа сам статус нічого не прибирає: робота лишається в
    черзі, файли в export. База казала б «відхилено» про роботу, яку цех у цей
    час фрезерує. Відкат уміє лише «Повернути в тріаж».
    """
    engine = _database()
    _wire(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, tmp_path / "spool" / "u1")
        accepted = _accept(db, user, email)
        assert accepted.status_code in (303, 204)
        db.refresh(email)
        assert email.status == "прийнято"

        response = mail_router_mod.reject_email(
            request=_request(user.id), email_id=email.id, db=db
        )

        assert response.status_code == 303
        db.refresh(email)
        assert email.status == "прийнято"                  # рішення не переписане
        assert db.scalars(select(Order)).all() != []       # робота на місці


def test_a_failed_rollback_of_the_spool_return_is_reported_not_swallowed(tmp_path, monkeypatch):
    """LOW: відкат `restore_attachments_to_spool` мовчав про власні помилки.

    Файл, який не вдалося повернути, зависає між export і спулом: у базі його
    вже немає, на диску ще є. Без повідомлення про це не знає ніхто."""
    from pathlib import Path as _Path

    from app import mail_export

    spool = tmp_path / "spool" / "u9"
    export_dir = tmp_path / "export" / "Клієнт"
    export_dir.mkdir(parents=True)
    first = export_dir / "a.stl"
    second = export_dir / "b.stl"
    first.write_bytes(b"A")
    second.write_bytes(b"B")

    real_move = mail_export._move_file
    calls = {"n": 0}

    def flaky_move(source: _Path, destination: _Path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("шара зникла")      # падаємо на другому файлі
        if calls["n"] > 2:
            raise OSError("і відкотити не вийшло")
        return real_move(source, destination)

    monkeypatch.setattr(mail_export, "_move_file", flaky_move)

    with pytest.raises(OSError) as failed:
        mail_export.restore_attachments_to_spool(
            tmp_path / "spool", "u9", [first, second]
        )

    assert "відкат" in str(failed.value)       # сказано, що відкат теж не вдався
    assert str(spool.parent) in str(spool.parent)


def test_imap_window_follows_the_working_day_not_the_calendar(tmp_path, monkeypatch):
    """LOW: о 00:05 нічна зміна ще веде вчорашній день.

    Календарна дата о цій порі вже перекинулась, і вікно пошуку листів
    стрибало на добу раніше — найстаріші листи випадали з нього посеред
    зміни."""
    from datetime import date as real_date

    from app import mail_reader

    seen: dict = {}

    class _Mailbox:
        """Рівно стільки поштової скриньки, скільки треба, щоб дійти до запиту."""

        def __init__(self, *args, **kwargs):
            pass

        def login(self, *args, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch(self, criteria, **kwargs):
            return []

        @property
        def folder(self):
            return SimpleNamespace(status=lambda *a, **k: {})

    monkeypatch.setattr(mail_reader, "business_today", lambda: real_date(2026, 9, 6))
    monkeypatch.setattr(mail_reader, "MailBox", _Mailbox)
    monkeypatch.setattr(mail_reader, "_folder_uidvalidity", lambda box: "2")
    monkeypatch.setattr(mail_reader, "get_imap_login", lambda db: "u")
    monkeypatch.setattr(mail_reader, "get_imap_password", lambda db: "p")
    monkeypatch.setattr(mail_reader, "get_mail_download_all", lambda db: False)
    monkeypatch.setattr(mail_reader, "ensure_materials_seeded", lambda db: None)
    monkeypatch.setattr(mail_reader, "load_alias_rows", lambda db: [])
    monkeypatch.setattr(mail_reader, "AND", lambda **kw: seen.update(kw) or "query")

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        mail_reader.fetch_new_emails(db, tmp_path)

    assert seen["date_gte"] == real_date(2026, 9, 6) - timedelta(
        days=mail_reader.IMAP_LOOKBACK_DAYS
    )


# ── Шара впала посеред переносу: жодної коронки не губимо ──────────────────
# Аудит 08.09.26. Внутрішній відкат `save_attachments_to_export` повертає файли
# в спул — але якщо шара досі лежить, він падає теж. Раніше викликач дізнавався
# про перенесені файли лише з ПОВЕРНЕНОГО значення, тобто тільки при успіху:
# його `undo_moves` отримував порожній список, база відкочувалась у «лист не
# прийнято», а `saved_path` вказував у спул, де файлів уже не було. При
# повторному прийнятті ці вкладення тихо випадали зі списку.


class TestPartialMoveIsVisible:
    def test_moved_out_is_filled_even_when_the_move_raises_midway(self, tmp_path):
        """Головне: викликач бачить, ЩО саме поїхало, навіть коли функція кинула."""
        from app import mail_export

        spool = tmp_path / "spool"
        spool.mkdir()
        sources = []
        for i in range(3):
            f = spool / f"crown-{i}.stl"
            f.write_text("stl", encoding="utf-8")
            sources.append(f)

        export_root = tmp_path / "export"
        export_root.mkdir()

        real_move = mail_export._move_file
        calls = {"n": 0}

        def flaky_move(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:      # третій файл — шара відпала
                raise OSError("мережеве імʼя більше недоступне")
            return real_move(src, dst)

        mail_export._move_file = flaky_move
        moved_out: list = []
        try:
            with pytest.raises(Exception):
                mail_export.save_attachments_to_export(
                    export_root, "Іваненко", "mono a3", sources, moved_out=moved_out,
                )
        finally:
            mail_export._move_file = real_move

        # Відкат теж ішов через flaky_move, але після третього виклику він уже
        # не кидає — файли повернулись, і перелік порожній.
        assert all(src.exists() for src in sources[:2]), "файли не повернулись у спул"
        assert moved_out == [], f"у export лишились сліди: {moved_out}"

    def test_moved_out_keeps_what_the_rollback_could_not_return(self, tmp_path):
        """Коли відкат теж падає — перелік застряглих файлів НЕ порожній.

        Саме він потім іде в `undo_moves` викликача і в слід у базі. Порожній
        перелік означав би «нічого не поїхало», що є неправдою.
        """
        from app import mail_export

        spool = tmp_path / "spool"
        spool.mkdir()
        sources = []
        for i in range(3):
            f = spool / f"crown-{i}.stl"
            f.write_text("stl", encoding="utf-8")
            sources.append(f)

        export_root = tmp_path / "export"
        export_root.mkdir()

        real_move = mail_export._move_file
        state = {"n": 0}

        def dying_share(src, dst):
            state["n"] += 1
            if state["n"] >= 3:      # перенос третього І весь відкат падають
                raise OSError("мережеве імʼя більше недоступне")
            return real_move(src, dst)

        mail_export._move_file = dying_share
        moved_out: list = []
        try:
            with pytest.raises(Exception):
                mail_export.save_attachments_to_export(
                    export_root, "Іваненко", "mono a3", sources, moved_out=moved_out,
                )
        finally:
            mail_export._move_file = real_move

        assert moved_out, (
            "перелік застряглих файлів порожній — викликач вважатиме, що нічого "
            "не поїхало, і дві коронки зникнуть тихо"
        )
        assert all(dest.exists() for _, dest in moved_out)


# ── Ручні кнопки листа не працюють поверх покинутого фетчу ─────────────────
# Аудит 08.09.26. Гейт `zombie_fetch_running` мав лише фоновий синк. «Скачати
# файли», «Скачати наново», «Забрати за посиланням» і «Прийняти» працювали з
# тим самим листом, поки покинутий фетч качав у ту саму теку своєю сесією:
# подвійні вкладення «(2)», а для приймання — рядки з мертвими шляхами.


class TestZombieFetchGate:
    ROUTES = (
        "fetch_email_link",
        "download_email_attachments",
        "redownload_email_attachments",
        "accept_email",
    )

    def test_every_file_touching_route_asks_the_gate(self):
        """Сторож проти найлегшої регресії — додати п'ятий роут і забути гейт."""
        import inspect

        from app.routers import mail as mail_router

        for name in self.ROUTES:
            source = inspect.getsource(getattr(mail_router, name))
            assert "zombie_fetch_blocks_files()" in source, (
                f"{name} чіпає файли листа без перевірки покинутого фетчу"
            )

    def test_gate_is_silent_when_no_zombie_is_running(self):
        from app.mail_sync_service import _reset_zombies_for_tests, zombie_fetch_blocks_files

        _reset_zombies_for_tests()
        assert zombie_fetch_blocks_files() is None

    def test_gate_speaks_while_a_zombie_is_alive(self):
        import threading

        from app import mail_sync_service

        mail_sync_service._reset_zombies_for_tests()
        stop = threading.Event()
        zombie = threading.Thread(target=stop.wait, daemon=True)
        zombie.start()
        with mail_sync_service._zombie_lock:
            mail_sync_service._zombie_fetches.append(zombie)
        try:
            assert mail_sync_service.zombie_fetch_blocks_files() is not None
        finally:
            stop.set()
            zombie.join(timeout=2)
            mail_sync_service._reset_zombies_for_tests()
