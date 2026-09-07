import pytest
import re
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.business_day import business_today
from app.db import Base
from app.mail_reader import (
    IMAP_LOOKBACK_DAYS,
    IMAP_MAX_MESSAGES,
    IMAP_TIMEOUT_SECONDS,
    fetch_new_emails,
    html_to_plain_text,
    safe_attachment_filename,
    unique_destination,
)
from app.models import Attachment, EmailMessage


def test_attachment_filename_strips_path_traversal():
    assert safe_attachment_filename("../../outside.stl", 1, "application/octet-stream") == "outside.stl"
    assert safe_attachment_filename(r"..\..\outside.stl", 1, "application/octet-stream") == "outside.stl"


def test_attachment_filename_replaces_illegal_characters():
    assert safe_attachment_filename('case:<1>|?.stl', 1, "application/octet-stream") == "case__1___.stl"


def test_attachment_filename_escapes_reserved_windows_names(tmp_path):
    """`con.stl` — легальне MIME-ім'я, але Windows відмовляє у створенні файлу,
    і лист залипав у "pending" назавжди (аудит 05.09.26, пошта H-4). Перевіряємо
    не лише рядок, а й що такий файл реально записується на диск."""
    safe = safe_attachment_filename("con.stl", 1, "application/octet-stream")
    assert safe == "_con.stl"
    assert safe_attachment_filename("NUL", 1, "application/octet-stream") == "_NUL"
    assert safe_attachment_filename("Lpt1.zip", 1, "application/zip") == "_Lpt1.zip"
    # Схоже, але не зарезервоване — не чіпаємо.
    assert safe_attachment_filename("console.stl", 1, "application/octet-stream") == "console.stl"

    (tmp_path / safe).write_bytes(b"STL")
    assert (tmp_path / safe).read_bytes() == b"STL"


def test_attachment_filename_uses_fallback_for_missing_name():
    assert safe_attachment_filename(None, 3, "image/png") == "attachment_3.png"


def test_unique_destination_does_not_overwrite_existing_file(tmp_path):
    (tmp_path / "case.stl").write_bytes(b"first")
    (tmp_path / "case (2).stl").write_bytes(b"second")

    destination = unique_destination(tmp_path, "case.stl")

    assert destination.name == "case (3).stl"


def _header_message(uid, subject="case", from_="client@example.test"):
    return SimpleNamespace(uid=uid, from_=from_, subject=subject, date=None)


def _full_message(
    uid, *, text="zircon A2", html="", subject="case", from_="client@example.test", attachments=None
):
    return SimpleNamespace(
        uid=uid,
        from_=from_,
        subject=subject,
        text=text,
        html=html,
        date=None,
        attachments=attachments or [],
    )


def _fake_attachment(filename="case.stl", content_type="application/octet-stream", payload=b"binary"):
    return SimpleNamespace(filename=filename, content_type=content_type, payload=payload)


_UID_RE = re.compile(r"UID (\d+)")


class FakeMailbox:
    """Mock of imap_tools.MailBox distinguishing the headers-only pass from
    the per-uid full-fetch pass by the ``headers_only`` kwarg and by parsing
    the requested UID out of the ``AND(uid=...)`` criteria string.
    """

    def __init__(self, headers, full_by_uid, *, raise_for_uids=None, calls=None,
                 uidvalidity=None):
        self.headers = headers
        self.full_by_uid = full_by_uid
        self.raise_for_uids = raise_for_uids or set()
        self.calls = calls if calls is not None else []
        # Без uidvalidity `folder` лишається None — mailbox.folder.status падає,
        # і mail_reader деградує до дедупу самим uid (як було до колонки).
        self.folder = (
            SimpleNamespace(status=lambda options=None: {"UIDVALIDITY": int(uidvalidity)})
            if uidvalidity is not None
            else None
        )

    def __call__(self, host, timeout):
        self.calls.append(("connect", host, timeout))
        return self

    def login(self, login, password):
        self.calls.append(("login", login, password))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def fetch(self, criteria=None, **kwargs):
        self.calls.append(("fetch", str(criteria), kwargs))
        if kwargs.get("headers_only"):
            return iter(self.headers)
        match = _UID_RE.search(str(criteria))
        uid = match.group(1) if match else None
        if uid in self.raise_for_uids:
            raise OSError(f"simulated network failure for uid {uid}")
        message = self.full_by_uid.get(uid)
        return iter([message] if message is not None else [])


def _engine_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _patch_common(monkeypatch, mailbox):
    monkeypatch.setattr("app.mail_reader.MailBox", mailbox)
    monkeypatch.setattr("app.mail_reader.get_imap_login", lambda session: "account")
    monkeypatch.setattr("app.mail_reader.get_imap_password", lambda session: "secret")
    # Existing download tests predate the auto-download whitelist gate; default
    # every sender to trusted so they exercise the download path. The gate
    # itself is covered by test_fetch_gates_attachment_download_by_whitelist.
    monkeypatch.setattr("app.mail_reader.is_auto_sender", lambda session, email: True)


def test_fetch_reads_recent_seen_mail_without_marking_seen(monkeypatch, tmp_path):
    mailbox = FakeMailbox(
        headers=[_header_message("42")],
        full_by_uid={"42": _full_message("42")},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        email = session.scalar(select(EmailMessage))
        assert email.attachments_status == "ready"

    header_call = next(c for c in mailbox.calls if c[0] == "fetch" and c[2].get("headers_only"))
    assert header_call[2] == {
        "mark_seen": False,
        "reverse": True,
        "limit": IMAP_MAX_MESSAGES,
        "headers_only": True,
    }
    # Робоча доба, а не календарна: зміна працює за північ, і о 00:30
    # «сьогодні» для пошти — це ще вчорашній день (business_today).
    # З date.today() цей тест був зеленим удень і червоним уночі.
    cutoff = business_today() - timedelta(days=IMAP_LOOKBACK_DAYS)
    assert f"{cutoff.day}-{cutoff.strftime('%b-%Y')}" in header_call[1]

    uid_call = next(c for c in mailbox.calls if c[0] == "fetch" and not c[2].get("headers_only"))
    assert uid_call[2] == {"mark_seen": False}
    assert "42" in uid_call[1]
    assert next(c for c in mailbox.calls if c[0] == "connect")[2] == IMAP_TIMEOUT_SECONDS


def test_fetch_deduplicates_uid_within_and_across_runs(monkeypatch, tmp_path):
    header = _header_message("55")
    mailbox = FakeMailbox(
        headers=[header, header],
        full_by_uid={"55": _full_message("55")},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        assert fetch_new_emails(session, tmp_path) == 0
        assert session.query(EmailMessage).count() == 1


def test_phase_one_creates_pending_rows_without_attachments(monkeypatch, tmp_path):
    """Headers-only pass alone must create visible rows even if phase 2
    (the full per-message fetch) fails for every message afterwards."""
    mailbox = FakeMailbox(
        headers=[_header_message("1"), _header_message("2")],
        full_by_uid={},
        raise_for_uids={"1", "2"},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        created = fetch_new_emails(session, tmp_path)
        assert created == 2

        emails = session.scalars(select(EmailMessage).order_by(EmailMessage.uid)).all()
        assert [e.uid for e in emails] == ["1", "2"]
        for email in emails:
            assert email.attachments_status == "pending"
            assert email.body_text is None
            assert email.attachments == []
        assert session.query(Attachment).count() == 0


def test_phase_two_downloads_attachments_and_marks_ready(monkeypatch, tmp_path):
    attachment = _fake_attachment(filename="case.stl", payload=b"stl-bytes")
    mailbox = FakeMailbox(
        headers=[_header_message("7")],
        full_by_uid={"7": _full_message("7", attachments=[attachment])},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr(
        "app.mail_reader.guess_fields_from_text",
        lambda *a, **kw: {"material_color_guess": "цирконій A2"},
    )

    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        email = session.scalar(select(EmailMessage))
        assert email.attachments_status == "ready"
        assert email.material_color_guess == "цирконій A2"
        assert email.body_text == "zircon A2"

        saved = session.scalar(select(Attachment))
        assert saved.filename == "case.stl"
        assert (tmp_path / "7" / "case.stl").read_bytes() == b"stl-bytes"
        assert saved.saved_path == str(tmp_path / "7" / "case.stl")


def test_html_only_message_stores_readable_stripped_text(monkeypatch, tmp_path):
    """Item 6: when a client's mail client sent no plain-text part at all
    (msg.text falsy, msg.html present), body_text must end up readable
    plain text, never the raw markup — mail_detail.html renders whatever
    lands here verbatim inside a bare <pre>."""
    html_body = (
        "<html><body><p>Матеріал: цирконій A2</p>"
        "<p>Дякую, чекаю на дзвінок&nbsp;&amp; фото.</p></body></html>"
    )
    mailbox = FakeMailbox(
        headers=[_header_message("101")],
        full_by_uid={"101": _full_message("101", text="", html=html_body)},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        email = session.scalar(select(EmailMessage))

        assert "<p>" not in email.body_text
        assert "<html>" not in email.body_text
        assert "Матеріал: цирконій A2" in email.body_text
        # `&nbsp;` decodes to a real non-breaking space (U+00A0), which is
        # correct — it renders identically to a normal space in the <pre>
        # block mail_detail.html uses, no need to normalize it away.
        assert "Дякую, чекаю на дзвінок\xa0& фото." in email.body_text


def test_plain_text_part_is_preferred_over_html_when_both_present(monkeypatch, tmp_path):
    mailbox = FakeMailbox(
        headers=[_header_message("102")],
        full_by_uid={
            "102": _full_message("102", text="звичайний текст", html="<p>html текст</p>")
        },
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        email = session.scalar(select(EmailMessage))
        assert email.body_text == "звичайний текст"


def test_html_to_plain_text_strips_tags_and_keeps_paragraph_breaks():
    html_body = "<div><p>Перший рядок</p><p>Другий рядок</p></div>"

    text = html_to_plain_text(html_body)

    assert "<p>" not in text
    assert "Перший рядок" in text
    assert "Другий рядок" in text
    lines = [line for line in text.splitlines() if line]
    assert lines == ["Перший рядок", "Другий рядок"]


def test_html_to_plain_text_unescapes_entities():
    text = html_to_plain_text("<p>А&amp;Б В</p>")
    assert text == "А&Б В"


def test_html_to_plain_text_drops_script_and_style_content():
    html_body = "<style>.x{color:red}</style><script>alert(1)</script><p>Текст листа</p>"

    text = html_to_plain_text(html_body)

    assert text == "Текст листа"


def test_html_to_plain_text_handles_malformed_markup_without_raising():
    text = html_to_plain_text("<p>Незакритий тег <div>вкладений")

    assert "Незакритий тег" in text
    assert "вкладений" in text


def test_html_to_plain_text_empty_input_returns_empty_string():
    assert html_to_plain_text("") == ""
    assert html_to_plain_text(None) == ""


def test_leftover_pending_row_from_previous_run_is_completed(monkeypatch, tmp_path):
    """A row left "pending" by a crashed/interrupted earlier run — created
    directly in the DB, not through this run's phase 1 — must be picked up
    and finished by phase 2 of the next sync call."""
    mailbox = FakeMailbox(
        headers=[],  # nothing new on the server this run
        full_by_uid={"99": _full_message("99", attachments=[_fake_attachment()])},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        session.add(EmailMessage(uid="99", status="нове", attachments_status="pending"))
        session.commit()

        created = fetch_new_emails(session, tmp_path)
        assert created == 0  # no new header this run

        email = session.scalar(select(EmailMessage).where(EmailMessage.uid == "99"))
        assert email.attachments_status == "ready"
        assert session.query(Attachment).filter_by(email_message_id=email.id).count() == 1


def test_phase_two_failure_on_one_message_does_not_abort_others(monkeypatch, tmp_path):
    mailbox = FakeMailbox(
        headers=[_header_message("10"), _header_message("11")],
        full_by_uid={"11": _full_message("11", attachments=[])},
        raise_for_uids={"10"},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        created = fetch_new_emails(session, tmp_path)
        assert created == 2  # both header rows were created in phase 1

        bad = session.scalar(select(EmailMessage).where(EmailMessage.uid == "10"))
        good = session.scalar(select(EmailMessage).where(EmailMessage.uid == "11"))
        assert bad.attachments_status == "pending"  # left for retry, no crash
        assert good.attachments_status == "ready"


def test_phase_two_retries_previously_failed_message_on_next_run(monkeypatch, tmp_path):
    mailbox = FakeMailbox(
        headers=[],
        full_by_uid={"3": _full_message("3", attachments=[])},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})

    with _engine_session() as session:
        session.add(EmailMessage(uid="3", status="нове", attachments_status="pending"))
        session.commit()

        fetch_new_emails(session, tmp_path)

        email = session.scalar(select(EmailMessage).where(EmailMessage.uid == "3"))
        assert email.attachments_status == "ready"


def test_fetch_gates_attachment_download_by_whitelist(monkeypatch, tmp_path):
    """Whitelisted sender → attachments download ("ready"); everyone else →
    headers only, no files ("skipped"). Body/guesses parse either way."""
    from app.models import EmailMessage

    attachment = _fake_attachment("crown.stl", b"STL")
    mailbox = FakeMailbox(
        headers=[_header_message("7", from_="client@example.test")],
        full_by_uid={"7": _full_message("7", from_="client@example.test", attachments=[attachment])},
    )
    _patch_common(monkeypatch, mailbox)
    # real gate: not whitelisted
    monkeypatch.setattr("app.mail_reader.is_auto_sender", lambda session, email: False)

    with _engine_session() as session:
        created = fetch_new_emails(session, tmp_path)
        assert created == 1
        email = session.query(EmailMessage).one()
        assert email.attachments_status == "skipped"
        assert email.attachments == []
        assert email.body_text  # body still parsed for preview


def test_download_all_toggle_overrides_whitelist(monkeypatch, tmp_path):
    """With the admin "download all" toggle on, a NON-whitelisted sender's
    attachments still download ("ready") — the toggle overrides the gate."""
    from app.models import EmailMessage
    from app.settings_store import set_mail_download_all

    attachment = _fake_attachment("crown.stl", b"STL")
    mailbox = FakeMailbox(
        headers=[_header_message("8", from_="stranger@example.test")],
        full_by_uid={"8": _full_message("8", from_="stranger@example.test", attachments=[attachment])},
    )
    _patch_common(monkeypatch, mailbox)
    # not whitelisted — normally "skipped"
    monkeypatch.setattr("app.mail_reader.is_auto_sender", lambda session, email: False)

    with _engine_session() as session:
        set_mail_download_all(session, True)
        session.commit()
        fetch_new_emails(session, tmp_path)
        email = session.query(EmailMessage).one()
        assert email.attachments_status == "ready"
        assert len(email.attachments) == 1


def test_manual_download_pulls_skipped_letter(monkeypatch, tmp_path):
    from app.mail_reader import download_attachments_now
    from app.models import EmailMessage

    attachment = _fake_attachment("crown.stl", b"STL")
    mailbox = FakeMailbox(
        headers=[_header_message("7")],
        full_by_uid={"7": _full_message("7", attachments=[attachment])},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.is_auto_sender", lambda session, email: False)
    with _engine_session() as session:
        fetch_new_emails(session, tmp_path)
        email = session.query(EmailMessage).one()
        assert email.attachments_status == "skipped"
        # operator pulls the files by hand
        n = download_attachments_now(session, email, tmp_path)
        session.commit()
        assert n == 1
        assert email.attachments_status == "ready"
        assert len(email.attachments) == 1


def test_safe_filename_repairs_utf8_mojibake():
    """A UTF-8 attachment name mis-read as Latin-1 (Cyrillic → "ÐºÐ¾Ð¿Ð¸Ñ")
    is repaired back; a clean name is left untouched."""
    from app.mail_reader import _repair_mojibake, safe_attachment_filename

    orig = "bitesplint_cad — копия (3).stl"
    mojibake = orig.encode("utf-8").decode("latin-1")
    assert _repair_mojibake(mojibake) == orig
    assert safe_attachment_filename(mojibake, 1, "application/octet-stream") == orig
    # clean ASCII / already-correct Cyrillic names are unchanged
    assert _repair_mojibake("crown_A2.stl") == "crown_A2.stl"
    assert _repair_mojibake("коронка.stl") == "коронка.stl"


def test_redownload_pulls_back_files_deleted_from_disk(monkeypatch, tmp_path):
    """Реальний глухий кут: файли скачались, потім теку в спулі прибрали.
    Рядки в базі лишались, панель писала «Усі файли на диску», «Відкрити
    папку» падало — і зробити було нічого. Тепер зниклі скачуються наново, а
    ті, що ціло лежать, не чіпаються (інакше поруч ляг би «(1)»-двійник)."""
    from pathlib import Path

    from app.mail_reader import redownload_missing_attachments
    from app.models import EmailMessage

    attachments = [
        _fake_attachment("crown.stl", payload=b"STL-1"),
        _fake_attachment("bridge.stl", payload=b"STL-2"),
    ]
    mailbox = FakeMailbox(
        headers=[_header_message("9")],
        full_by_uid={"9": _full_message("9", attachments=attachments)},
    )
    _patch_common(monkeypatch, mailbox)
    with _engine_session() as session:
        fetch_new_emails(session, tmp_path)
        email = session.query(EmailMessage).one()
        assert len(email.attachments) == 2

        # Хтось видалив ОДИН файл з диска.
        gone = next(a for a in email.attachments if a.filename == "crown.stl")
        survivor = next(a for a in email.attachments if a.filename == "bridge.stl")
        survivor_path = Path(survivor.saved_path)
        Path(gone.saved_path).unlink()

        removed, saved = redownload_missing_attachments(session, email, tmp_path)
        session.commit()

        assert (removed, saved) == (1, 1)
        session.refresh(email)
        names = sorted(a.filename for a in email.attachments)
        assert names == ["bridge.stl", "crown.stl"], "двійників бути не має"
        assert all(Path(a.saved_path).exists() for a in email.attachments)
        # Цілий файл не перезаписувався і не подвоївся.
        assert survivor_path.read_bytes() == b"STL-2"
        assert len(list(survivor_path.parent.glob("bridge*"))) == 1


def test_redownload_is_a_no_op_when_every_file_is_in_place(monkeypatch, tmp_path):
    """Кнопка не має тихо перескачувати цілі файли: без втрат — без дій."""
    from app.mail_reader import redownload_missing_attachments
    from app.models import EmailMessage

    mailbox = FakeMailbox(
        headers=[_header_message("11")],
        full_by_uid={"11": _full_message("11", attachments=[_fake_attachment("a.stl", payload=b"X")])},
    )
    _patch_common(monkeypatch, mailbox)
    with _engine_session() as session:
        fetch_new_emails(session, tmp_path)
        email = session.query(EmailMessage).one()
        assert redownload_missing_attachments(session, email, tmp_path) == (0, 0)
        assert len(email.attachments) == 1


def test_network_blink_is_not_treated_as_a_lost_file(monkeypatch, tmp_path):
    """Мережева шара «моргнула» — рядок Attachment мусить лишитись живим.

    Path.exists() ковтає будь-яку OSError і повертає False, тож недоступність
    UNC-шляху виглядала як видалений файл: рядок зносився назавжди, а файл
    лишався сиротою на диску. Тут stat() кидає WinError-подібну OSError (НЕ
    FileNotFoundError) — нічого видаляти й перекачувати не можна.

    Регрес, який ловить тест: повернути `not Path(a.saved_path).exists()` —
    тоді (removed, saved) стане (1, 1) і поруч ляже двійник.
    """
    from pathlib import Path

    from app.mail_reader import redownload_missing_attachments
    from app.models import EmailMessage

    mailbox = FakeMailbox(
        headers=[_header_message("21")],
        full_by_uid={"21": _full_message("21", attachments=[_fake_attachment("a.stl", payload=b"X")])},
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.MISSING_FILE_RETRY_DELAY", 0)
    with _engine_session() as session:
        fetch_new_emails(session, tmp_path)
        email = session.query(EmailMessage).one()
        saved_path = Path(email.attachments[0].saved_path)

        real_stat = Path.stat

        def blinking_stat(self, *args, **kwargs):
            if self == saved_path:
                raise OSError(64, "The specified network name is no longer available")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", blinking_stat)
        assert redownload_missing_attachments(session, email, tmp_path) == (0, 0)
        monkeypatch.undo()

        assert len(email.attachments) == 1
        assert len(list(saved_path.parent.glob("a*.stl"))) == 1


def test_transient_error_then_success_keeps_the_attachment(monkeypatch, tmp_path):
    """Перша спроба падає, друга бачить файл — це не втрата, а моргання."""
    from pathlib import Path

    from app.mail_reader import _file_is_missing

    target = tmp_path / "case.stl"
    target.write_bytes(b"X")
    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self, *args, **kwargs):
        if self == target:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(1231, "network location cannot be reached")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr("app.mail_reader.MISSING_FILE_RETRY_DELAY", 0)
    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert _file_is_missing(str(target)) is False
    assert calls["n"] >= 2, "мусить бути повторна спроба, а не один вирок"


def test_really_deleted_file_is_still_reported_missing(tmp_path, monkeypatch):
    """Ретраї не мусять ховати справжню втрату: чистий FileNotFoundError."""
    from app.mail_reader import _file_is_missing

    monkeypatch.setattr("app.mail_reader.MISSING_FILE_RETRY_DELAY", 0)
    assert _file_is_missing(str(tmp_path / "nope.stl")) is True
    assert _file_is_missing(None) is True


# --- UIDVALIDITY -------------------------------------------------------------

def test_uidvalidity_change_does_not_hide_a_new_letter(monkeypatch, tmp_path):
    """Тека перестворена → UID почались спочатку → лист із «зайнятим» номером
    мусить усе одно потрапити в тріаж.

    Регрес, який ловить тест: повернути дедуп самим uid
    (`select(EmailMessage.uid).where(...)`) — рядок зі старої нумерації видасть
    новий лист за «вже імпортований», і в базі лишиться один запис зі СТАРОЮ
    темою. Лист зникне мовчки, а це рівно те, що заборонено (CLAUDE.md, екран 2).
    """
    from app.models import EmailMessage

    _patch_common(
        monkeypatch,
        FakeMailbox(
            headers=[_header_message("5", subject="стара робота")],
            full_by_uid={"5": _full_message("5", subject="стара робота")},
            uidvalidity=100,
        ),
    )
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})
    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        assert session.query(EmailMessage).one().uid_validity == "100"

        _patch_common(
            monkeypatch,
            FakeMailbox(
                headers=[_header_message("5", subject="НОВА робота")],
                full_by_uid={"5": _full_message("5", subject="НОВА робота")},
                uidvalidity=200,
            ),
        )
        assert fetch_new_emails(session, tmp_path) == 1

        rows = session.query(EmailMessage).order_by(EmailMessage.id).all()
        assert [r.uid_validity for r in rows] == ["100", "200"]
        assert [r.subject for r in rows] == ["стара робота", "НОВА робота"]


def test_same_uidvalidity_still_deduplicates(monkeypatch, tmp_path):
    """Звичайний випадок (номер теки не мінявся) працює як раніше."""
    from app.models import EmailMessage

    def mailbox():
        return FakeMailbox(
            headers=[_header_message("7")],
            full_by_uid={"7": _full_message("7")},
            uidvalidity=100,
        )

    _patch_common(monkeypatch, mailbox())
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})
    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        _patch_common(monkeypatch, mailbox())
        assert fetch_new_emails(session, tmp_path) == 0
        assert session.query(EmailMessage).count() == 1


def test_rows_from_before_the_column_adopt_the_current_uidvalidity(monkeypatch, tmp_path):
    """Рядок зі старої бази (uid_validity порожній) — не дублікат, а «namespace
    невідомий»: приймаємо його за поточний і проставляємо значення."""
    from app.models import EmailMessage

    _patch_common(
        monkeypatch,
        FakeMailbox(
            headers=[_header_message("8")],
            full_by_uid={"8": _full_message("8")},
            uidvalidity=100,
        ),
    )
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})
    with _engine_session() as session:
        session.add(EmailMessage(uid="8", uid_validity="", status="нове",
                                 attachments_status="ready"))
        session.commit()

        assert fetch_new_emails(session, tmp_path) == 0
        row = session.query(EmailMessage).one()
        assert row.uid_validity == "100"


def test_pending_row_from_a_dead_namespace_is_not_refetched(monkeypatch, tmp_path):
    """Добирати вкладення за мертвим uid не можна — на сервері під цим номером
    тепер ЧУЖИЙ лист, і його файли причепились би не туди."""
    from app.models import Attachment, EmailMessage

    mailbox = FakeMailbox(
        headers=[],
        full_by_uid={"5": _full_message("5", attachments=[_fake_attachment("alien.stl")])},
        uidvalidity=200,
    )
    _patch_common(monkeypatch, mailbox)
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})
    with _engine_session() as session:
        session.add(EmailMessage(uid="5", uid_validity="100", status="нове",
                                 attachments_status="pending"))
        session.commit()

        fetch_new_emails(session, tmp_path)

        assert session.query(Attachment).count() == 0
        assert session.query(EmailMessage).one().attachments_status == "pending"


def test_unreadable_uidvalidity_falls_back_to_uid_dedup(monkeypatch, tmp_path):
    """Сервер не віддав UIDVALIDITY (стара поведінка) — дедуп мусить лишитись
    робочим, а не почати плодити дублікати на кожен синк."""
    from app.models import EmailMessage

    def mailbox():
        return FakeMailbox(
            headers=[_header_message("9")],
            full_by_uid={"9": _full_message("9")},
        )

    _patch_common(monkeypatch, mailbox())
    monkeypatch.setattr("app.mail_reader.guess_fields_from_text", lambda *a, **kw: {})
    with _engine_session() as session:
        assert fetch_new_emails(session, tmp_path) == 1
        _patch_common(monkeypatch, mailbox())
        assert fetch_new_emails(session, tmp_path) == 0
        assert session.query(EmailMessage).count() == 1


# --- ручне скачування й нумерація скриньки (ревʼю 07.09.26) -------------------

def test_manual_download_refuses_a_stale_uid_namespace(monkeypatch, tmp_path):
    """Після перестворення теки той самий uid — ЧУЖИЙ лист. fetch_new_emails це
    ловить; ручне «Скачати вкладення» мусить теж, інакше файли стороннього
    клієнта причепляться до цієї роботи."""
    from app.mail_reader import download_attachments_now
    from app.models import EmailMessage

    _patch_common(
        monkeypatch,
        FakeMailbox(headers=[], full_by_uid={"5": _full_message("5")}, uidvalidity=200),
    )
    with _engine_session() as session:
        email = EmailMessage(uid="5", uid_validity="100", status="нове", attachments_status="skipped")
        session.add(email)
        session.commit()
        with pytest.raises(RuntimeError, match="перенумеровано"):
            download_attachments_now(session, email, tmp_path)
        assert email.attachments_status == "skipped"


def test_redownload_refuses_a_stale_uid_namespace_before_deleting_rows(monkeypatch, tmp_path):
    """«Скачати наново» видаляв рядки Attachment ДО фетчу — при зміні нумерації
    STL клієнта замінялись чужими без сліду. Гейт стоїть перед видаленням."""
    from app.mail_reader import redownload_missing_attachments
    from app.models import Attachment, EmailMessage

    _patch_common(
        monkeypatch,
        FakeMailbox(headers=[], full_by_uid={"5": _full_message("5")}, uidvalidity=200),
    )
    with _engine_session() as session:
        email = EmailMessage(uid="5", uid_validity="100", status="нове", attachments_status="ready")
        session.add(email)
        session.flush()
        session.add(Attachment(
            email_message_id=email.id, filename="crown.stl",
            saved_path=str(tmp_path / "5" / "crown.stl"),
        ))
        session.commit()
        with pytest.raises(RuntimeError, match="перенумеровано"):
            redownload_missing_attachments(session, email, tmp_path)
        session.rollback()
        assert session.query(Attachment).count() == 1
