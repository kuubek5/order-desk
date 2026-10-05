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
