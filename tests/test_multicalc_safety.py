"""Запобіжники мультипрорахунку (бриф MULTICALC_SAFETY_BRIEF.md, 05.10.26).

Конвеєр із Sum3D переносить файли кожного листа в export по мережевій шарі.
Тут — збої посеред цього переносу, як їх насправді бачить Windows:

* недоступна шара (`\\\\host\\share`) приходить НЕ як «інша OSError», а як
  `FileNotFoundError` з `winerror` 53/67 — тобто точнісінько як видалений
  файл. `Path.exists()` її ковтає й каже False. Старі тести моделювали обрив
  як `OSError(64, ...)` (errno 64), якої Windows для UNC не дає, тому й вада
  не ловилась: відкат «забував» файли, які лишились у export;
* міжтомовий перенос (спул на C:, export на шарі) — копія + видалення, і
  обрізок копії не мусить лягати під справжнім іменем `.stl`;
* непередбачений виняток посеред партії — не 500, а «цей лист не вдався»,
  бо решта листів уже прийнята й браузер мусить про це дізнатись.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app import mail_export
from app.models import SyncLog
from app.routers import mail as mail_router_mod
from app.services.mail_accept import AcceptResult
from tests.asgi_client import MiniClient
from tests.test_mail_bulk import _letter as _bulk_letter
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура


def _netpath_error(path) -> OSError:
    """Як Windows віддає недоступну шару: FileNotFoundError із winerror 53."""
    exc = OSError(None, "The network path was not found", str(path), 53)
    assert isinstance(exc, FileNotFoundError) and exc.winerror == 53
    return exc


class _Share:
    """Шара, що падає на ходу: після `drop()` будь-який stat під `root` — як
    у Windows при обриві, і будь-який `_move_file` туди чи звідти теж падає."""

    def __init__(self, monkeypatch, root: Path):
        self.root = root
        self.down = False
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            if self.down and self._under(path):
                raise _netpath_error(path)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)

    def _under(self, path) -> bool:
        try:
            Path(path).relative_to(self.root)
            return True
        except ValueError:
            return False

    def drop(self):
        self.down = True

    def restore(self):
        self.down = False


# ── Недоступна шара ≠ видалений файл ──────────────────────────────────────


def test_unreachable_share_is_not_a_missing_file(tmp_path, monkeypatch):
    from app.mail_reader import _file_is_missing

    target = tmp_path / "spool" / "crown.stl"
    target.parent.mkdir()
    target.write_bytes(b"STL")
    monkeypatch.setattr("app.mail_reader.MISSING_FILE_RETRY_DELAY", 0)
    share = _Share(monkeypatch, tmp_path / "spool")
    share.drop()
    assert _file_is_missing(str(target)) is False, (
        "обрив шари прочитано як видалений файл — прийняття мовчки пропустить "
        "коронку, а лист стане «прийнято» без неї"
    )


def test_really_deleted_file_on_a_live_disk_is_still_missing(tmp_path, monkeypatch):
    from app.mail_reader import _file_is_missing

    monkeypatch.setattr("app.mail_reader.MISSING_FILE_RETRY_DELAY", 0)
    assert _file_is_missing(str(tmp_path / "gone.stl")) is True


# ── Відкат переносу при обриві шари ───────────────────────────────────────


def _three_crowns(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    sources = []
    for i in range(3):
        f = spool / f"crown-{i}.stl"
        f.write_bytes(b"STL")
        sources.append(f)
    export_root = tmp_path / "export"
    export_root.mkdir()
    return sources, export_root


def test_rollback_keeps_files_it_cannot_see_when_the_share_drops(tmp_path, monkeypatch):
    """Два файли переїхали, на третьому шара відпала. Відкат не бачить файлів
    в export (stat → «мережевий шлях не знайдено») — і раніше вирішував, що
    їх там немає, викреслював з переліку, а викликач уже не мав що повертати
    й що записати в слід. Файли лишались в export, база — «лист не прийнято»,
    `saved_path` — у спул, де їх немає."""
    sources, export_root = _three_crowns(tmp_path)
    share = _Share(monkeypatch, export_root)
    real_move = mail_export._move_file
    calls = {"n": 0}

    def move(src, dst):
        calls["n"] += 1
        if calls["n"] >= 3:
            share.drop()
            raise _netpath_error(dst)
        return real_move(src, dst)

    monkeypatch.setattr(mail_export, "_move_file", move)
    moved_out: list = []
    with pytest.raises(OSError):
        mail_export.save_attachments_to_export(
            export_root, "Іваненко", "mono a3", sources, moved_out=moved_out,
        )
    share.restore()

    landed = [dest for _, dest in moved_out]
    assert len(landed) == 2, f"перелік застряглих файлів неповний: {moved_out}"
    assert all(dest.is_file() for dest in landed)


def test_undo_moves_reports_a_file_it_cannot_see(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    spool.mkdir()
    export = tmp_path / "export" / "Клієнт" / "05.10.26" / "mono a3"
    export.mkdir(parents=True)
    source = spool / "crown.stl"
    destination = export / "crown.stl"
    destination.write_bytes(b"STL")
    share = _Share(monkeypatch, tmp_path / "export")
    share.drop()

    errors = mail_export.undo_moves([(source, destination)])

    assert errors, "файл у недоступному export мовчки вважався «не переїжджав»"


def test_undo_moves_still_skips_a_file_that_never_moved(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    source = spool / "crown.stl"
    source.write_bytes(b"STL")
    destination = tmp_path / "export" / "crown.stl"
    assert mail_export.undo_moves([(source, destination)]) == []
    assert source.read_bytes() == b"STL"


# ── Міжтомовий перенос: копія → перевірка → справжнє ім'я ────────────────


@pytest.fixture
def cross_volume(monkeypatch):
    """Спул і export на різних томах: перенос = копія + видалення."""
    monkeypatch.setattr(mail_export, "_same_volume", lambda a, b: False)


def test_cross_volume_move_lands_the_whole_file(tmp_path, cross_volume):
    src = tmp_path / "spool" / "crown.stl"
    src.parent.mkdir()
    src.write_bytes(b"STL-DATA")
    dst = tmp_path / "export" / "crown.stl"
    dst.parent.mkdir()

    mail_export._move_file(src, dst)

    assert dst.read_bytes() == b"STL-DATA"
    assert not src.exists()
    assert list(dst.parent.iterdir()) == [dst]


def test_cross_volume_copy_dying_midway_never_leaves_a_real_name(tmp_path, monkeypatch, cross_volume):
    """Обірвана копія, а шара не дає прибрати обрізок: обрізок лишається, але
    НЕ під іменем `crown.stl` — видача його не покаже, а повторне прийняття
    не отримає «crown (2).stl» поруч із фальшивим «crown.stl»."""
    src = tmp_path / "spool" / "crown.stl"
    src.parent.mkdir()
    src.write_bytes(b"STL-DATA")
    dst = tmp_path / "export" / "crown.stl"
    dst.parent.mkdir()

    def dying_copy(a, b, *args, **kwargs):
        Path(b).write_bytes(b"STL")
        raise _netpath_error(b)

    monkeypatch.setattr(mail_export.shutil, "copy2", dying_copy)
    real_unlink = Path.unlink

    def stuck_unlink(path, *args, **kwargs):
        if path.parent == dst.parent:
            raise _netpath_error(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stuck_unlink)
    with pytest.raises(OSError):
        mail_export._move_file(src, dst)

    assert not os.path.exists(dst), "обрізок ліг під справжнім іменем коронки"
    assert src.read_bytes() == b"STL-DATA"


def test_cross_volume_short_copy_is_rejected(tmp_path, monkeypatch, cross_volume):
    src = tmp_path / "spool" / "crown.stl"
    src.parent.mkdir()
    src.write_bytes(b"STL-DATA")
    dst = tmp_path / "export" / "crown.stl"
    dst.parent.mkdir()

    def short_copy(a, b, *args, **kwargs):
        Path(b).write_bytes(b"STL")  # «успішна» копія, але неповна

    monkeypatch.setattr(mail_export.shutil, "copy2", short_copy)
    with pytest.raises(OSError):
        mail_export._move_file(src, dst)

    assert not dst.exists()
    assert list(dst.parent.iterdir()) == []
    assert src.read_bytes() == b"STL-DATA"


def test_cross_volume_locked_source_leaves_a_single_copy(tmp_path, monkeypatch, cross_volume):
    """Sum3D тримає файл у спулі: копія вдалась, а видалити джерело не можна.
    Лишається ОДНА копія — у спулі, як і до спроби."""
    src = tmp_path / "spool" / "crown.stl"
    src.parent.mkdir()
    src.write_bytes(b"STL-DATA")
    dst = tmp_path / "export" / "crown.stl"
    dst.parent.mkdir()
    real_unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path == src:
            raise PermissionError(13, "file is being used by another process", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(PermissionError):
        mail_export._move_file(src, dst)

    assert src.read_bytes() == b"STL-DATA"
    assert list(dst.parent.iterdir()) == []


def test_cross_volume_never_overwrites_an_existing_file(tmp_path, cross_volume):
    src = tmp_path / "spool" / "crown.stl"
    src.parent.mkdir()
    src.write_bytes(b"NEW")
    dst = tmp_path / "export" / "crown.stl"
    dst.parent.mkdir()
    dst.write_bytes(b"OLD")
    with pytest.raises(FileExistsError):
        mail_export._move_file(src, dst)
    assert dst.read_bytes() == b"OLD" and src.read_bytes() == b"NEW"


# ── Поштова робота без рядка в таблиці і синк ─────────────────────────────
# Рядок-нотатка не ліг (мережа) або ліг, але відповідь загубилась: робота є,
# `row_number` порожній. Мультипрорахунок дає кілька робіт ОДНОГО клієнта з тим
# самим кольором і к-стю 1 — однаковий ключ identity.


def _mail_order(session, **kw):
    from datetime import datetime

    from app.models import Order

    values = dict(source="email", sheet_tab="22.06.26", row_number=None,
                  client_name="Басараб", material_color="mono a3", quantity="1",
                  sum3d_id="10-19-48", status="прораховано",
                  sheet_row_pending=datetime.now())
    values.update(kw)
    order = Order(**values)
    session.add(order)
    session.flush()
    return order


def test_sync_survives_a_rowless_mail_order_next_to_its_twin():
    """Поштова робота без рядка + інша робота з тим самим ключем у вкладці.
    `_relink_moved_rows` сортував групу за `row_number` — None проти числа
    дає TypeError, і синк вкладки падав на кожному проході."""
    from tests.test_sync import make_client_row, make_session
    from app.sync import sync_tab

    with make_session() as session:
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)])
        session.commit()
        mail = _mail_order(session)
        legacy = _mail_order(session, sheet_row_pending=None)
        session.commit()

        sync_tab(session, "22.06.26", [make_client_row(row_number=5)])
        session.commit()

        # Рядок лишився за тим, у кого він був; поштова й далі чекає свій.
        assert mail.row_number is None and mail.sheet_row_pending is not None
        assert legacy.row_number is None


def test_legacy_rowless_mail_order_does_not_take_a_human_row():
    """Стара поштова робота без рядка (без позначки «чекає») не забирає рядок,
    який людина вписала з тим самим клієнтом і кольором."""
    from sqlalchemy import select as _select
    from tests.test_sync import make_client_row, make_session
    from app.models import Order
    from app.sync import sync_tab

    with make_session() as session:
        legacy = _mail_order(session, sheet_row_pending=None)
        session.commit()
        sync_tab(session, "22.06.26", [make_client_row(row_number=7)])
        session.commit()
        assert legacy.row_number is None
        assert len(session.scalars(_select(Order)).all()) == 2


def test_sync_binds_an_orphan_row_to_the_rowless_mail_order_not_a_duplicate():
    """Відповідь на запис загубилась: рядок у таблиці є, робота про нього не
    знає. Синк мусить зчепити рядок із цією роботою, а не завести другу."""
    from sqlalchemy import select as _select
    from tests.test_sync import make_client_row, make_session
    from app.models import Order
    from app.sync import sync_tab

    with make_session() as session:
        mail = _mail_order(session)
        session.commit()

        sync_tab(session, "22.06.26", [make_client_row(row_number=7)])
        session.commit()

        orders = session.scalars(_select(Order)).all()
        assert [o.id for o in orders] == [mail.id], "з рядка заведено дубль"
        assert mail.row_number == 7 and mail.source == "email"
        assert mail.sheet_row_pending is None


def test_two_rowless_mail_twins_take_two_orphan_rows():
    from sqlalchemy import select as _select
    from tests.test_sync import make_client_row, make_session
    from app.models import Order
    from app.sync import sync_tab

    with make_session() as session:
        first = _mail_order(session)
        second = _mail_order(session)
        session.commit()

        sync_tab(session, "22.06.26", [make_client_row(row_number=7), make_client_row(row_number=8)])
        session.commit()

        assert len(session.scalars(_select(Order)).all()) == 2
        assert {first.row_number, second.row_number} == {7, 8}


# ── Рядок-нотатка: позначка, банер, повтор ────────────────────────────────


@pytest.fixture
def rowdb(monkeypatch):
    """База в памʼяті, яку бачать і тест, і «воркер» write-back."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.services import sheet_stuck_writes, sheet_writeback

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        sheet_writeback, "writeback_session",
        lambda: _Session(bind=engine, autoflush=False, expire_on_commit=False),
    )
    sheet_stuck_writes.reset()
    yield engine
    sheet_stuck_writes.reset()


def _client_line(client, material, qty, sum3d=""):
    line = [""] * 14
    line[2], line[3], line[4], line[11] = qty, material, client, sum3d
    return line


def _raw(*lines):
    """Вкладка: 6 рядків заголовків (порожні — звірка структури мовчить),
    далі рядки даних; рядок даних N = `lines[N-1]`."""
    return [[""] * 14 for _ in range(6)] + [list(x) for x in lines]


class _FakeSheet:
    def __init__(self, raw):
        self.raw = raw

    def get_all_values(self):
        return self.raw


def _wire_sheet(monkeypatch, raw, appended):
    from app import sheets
    from app.services import mail_accept as mail_accept_svc

    sheet = _FakeSheet(raw)
    monkeypatch.setattr(sheets, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(sheets, "get_worksheet_by_name", lambda ss, name: sheet)
    monkeypatch.setattr(sheets, "call_with_retry", lambda fn: fn())

    def append(worksheet, client, qty, material, **kw):
        appended.append((client, qty, material, kw.get("sum3d_id")))
        sheet.raw.append(_client_line(client, material, qty, kw.get("sum3d_id", "")))
        return len(sheet.raw)  # номер рядка АРКУША (1-based)

    monkeypatch.setattr(mail_accept_svc, "append_mail_placeholder_row", append)
    return sheet


def _pending_mail(engine, **kw):
    from sqlalchemy.orm import Session as _Session

    with _Session(engine, expire_on_commit=False) as db:
        order = _mail_order(db, **kw)
        db.commit()
        return order.id


def test_failed_note_keeps_the_work_waiting_and_shows_it_in_the_banner(rowdb, monkeypatch):
    from sqlalchemy.orm import Session as _Session
    from types import SimpleNamespace

    from app.models import Order
    from app.services import mail_accept as mail_accept_svc
    from app.services.sheet_stuck_writes import failed_sheet_writes

    def boom(*a, **kw):
        raise ConnectionError("Connection aborted")

    monkeypatch.setattr(mail_accept_svc, "append_mail_placeholder_row", boom)
    order_id = _pending_mail(rowdb)
    with _Session(rowdb) as db:
        order = db.get(Order, order_id)
        mail_accept_svc._write_placeholder_row(db, SimpleNamespace(id=7), order, object())
        db.commit()
        assert order.row_number is None and order.sheet_row_pending is not None
    items = failed_sheet_writes()
    assert [(i.kind, i.order_id, i.reason) for i in items] == [("row", order_id, "net")]
    assert items[0].kind_text == "рядок роботи"


def test_successful_note_clears_the_wait(rowdb, monkeypatch):
    from sqlalchemy.orm import Session as _Session
    from types import SimpleNamespace

    from app.models import Order
    from app.services import mail_accept as mail_accept_svc

    monkeypatch.setattr(mail_accept_svc, "append_mail_placeholder_row", lambda *a, **kw: 13)
    order_id = _pending_mail(rowdb)
    with _Session(rowdb) as db:
        order = db.get(Order, order_id)
        mail_accept_svc._write_placeholder_row(db, SimpleNamespace(id=7), order, object())
        db.commit()
        assert order.row_number == 7 and order.sheet_row_pending is None


def test_retry_waits_for_the_sync_first_then_submits(rowdb, monkeypatch):
    from datetime import datetime, timedelta
    from sqlalchemy.orm import Session as _Session

    from app import sheets
    from app.services import mail_row_retry, sheet_writeback

    submitted = []
    monkeypatch.setattr(sheet_writeback, "submit_sheet_write", lambda fn, *a: submitted.append(a))
    monkeypatch.setattr(sheets, "quota_is_tight", lambda: False)
    mail_row_retry._attempts.clear()
    since = datetime.now()
    order_id = _pending_mail(rowdb, sheet_row_pending=since)
    with _Session(rowdb) as db:
        assert mail_row_retry.retry_pending_mail_rows(db, now=0.0, wall=since + timedelta(seconds=60)) == 0
        assert mail_row_retry.retry_pending_mail_rows(db, now=1.0, wall=since + timedelta(seconds=200)) == 1
        # тротл: та сама робота не ставиться вдруге за 2 хв
        assert mail_row_retry.retry_pending_mail_rows(db, now=30.0, wall=since + timedelta(seconds=230)) == 0
    assert submitted == [(order_id,)]


def test_retry_binds_the_orphan_row_instead_of_writing_a_second_one(rowdb, monkeypatch):
    """Запис дійшов, відповідь загубилась: у таблиці вже є рядок цієї роботи.
    Двійник (той самий клієнт, колір, к-сть — мультипрорахунок) тримає свій
    рядок 1; «нічий» рядок 3 — наш."""
    from sqlalchemy.orm import Session as _Session

    from app.models import Order
    from app.services.mail_row_retry import append_pending_mail_row_warm

    appended: list = []
    line = _client_line("Басараб", "mono a3", "1", "10-19-48")
    _wire_sheet(monkeypatch, _raw(line, _client_line("Інший", "pmma a2", "2"), line), appended)
    _pending_mail(rowdb, row_number=1, sheet_row_pending=None)  # двійник із рядком
    order_id = _pending_mail(rowdb)

    assert append_pending_mail_row_warm(order_id) is None

    assert appended == [], "дописано другий рядок поверх загубленої відповіді"
    with _Session(rowdb) as db:
        order = db.get(Order, order_id)
        assert order.row_number == 3 and order.sheet_row_pending is None


def test_retry_writes_the_row_when_it_is_really_missing(rowdb, monkeypatch):
    from sqlalchemy.orm import Session as _Session

    from app.models import Order
    from app.services.mail_row_retry import append_pending_mail_row_warm

    appended: list = []
    line = _client_line("Басараб", "mono a3", "1", "10-19-48")
    # Рядок 2 — та сама людина й колір, але ІНШИЙ Sum3D: чужа робота.
    _wire_sheet(monkeypatch, _raw(line, _client_line("Басараб", "mono a3", "1", "09-00-00")), appended)
    _pending_mail(rowdb, row_number=1, sheet_row_pending=None)
    order_id = _pending_mail(rowdb)

    assert append_pending_mail_row_warm(order_id) is None

    assert appended == [("Басараб", "1", "mono a3", "10-19-48")]
    with _Session(rowdb) as db:
        order = db.get(Order, order_id)
        assert order.row_number == 3 and order.sheet_row_pending is None


def test_retry_does_nothing_for_a_work_that_already_has_its_row(rowdb, monkeypatch):
    from app.services.mail_row_retry import append_pending_mail_row_warm

    appended: list = []
    _wire_sheet(monkeypatch, _raw(), appended)
    order_id = _pending_mail(rowdb, row_number=4)
    assert append_pending_mail_row_warm(order_id) is None
    assert appended == []


# ── Конвеєр: виняток посеред партії ───────────────────────────────────────


def _accept_batch(app, cards):
    client = MiniClient(app)
    client.login(*OPERATOR)
    return client.post(
        "/mail/accept-batch",
        {"payload": json.dumps(cards), "confirm_missing": "1"},
        {"HX-Request": "true"},
    )


@pytest.mark.usefixtures("weekday_clock")
def test_unexpected_error_in_one_letter_does_not_hide_the_accepted_ones(app_db, monkeypatch):  # noqa: F811
    """Лист 1 прийнято (закомічено), на листі 2 — непередбачений виняток.
    Раніше він спливав 500-ю: браузер не дізнавався, що лист 1 уже в черзі,
    картка лишалась, і повторне «Прийняти» диска з Sum3D відмовляло всім
    («лист уже оброблено»). Тепер лист 2 — невдалий з причиною, лист 3
    приймається, відповідь перелічує всіх."""
    app, session_factory = app_db
    with session_factory() as db:
        a = _bulk_letter(db, "1")
        b = _bulk_letter(db, "2")
        c = _bulk_letter(db, "3")
    calls = []

    def fake(db, user, email, **kw):
        calls.append(email.id)
        if email.id == b:
            raise RuntimeError("database disk image is malformed")
        return AcceptResult(order=None, material_label="pmma a2")

    monkeypatch.setattr(mail_router_mod, "accept_letter", fake)
    status, headers, body = _accept_batch(app, [
        {"email_id": eid, "client_name": n, "material_color": "pmma a2",
         "quantity": "1", "sum3d_id": "12-01-45"}
        for eid, n in ((a, "А"), (b, "Б"), (c, "В"))
    ])

    assert status == 200, body[:300]
    assert calls == [a, b, c]
    trigger = json.loads({k.lower(): v for k, v in dict(headers).items()}["hx-trigger"])
    assert trigger["mailBatchDone"] == {"accepted": [a, c], "failed": [b]}
    assert "database disk image is malformed" in body or "непередбачена" in body.lower()

    with session_factory() as db:
        trace = db.scalars(select(SyncLog).where(SyncLog.direction == "mail_to_export")).all()
        assert any(str(b) in (t.message or "") and t.status == "error" for t in trace), (
            "непередбачена помилка прийняття не лишила сліду в журналі синку"
        )
