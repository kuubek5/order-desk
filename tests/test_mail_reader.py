import re
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

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

    def __init__(self, headers, full_by_uid, *, raise_for_uids=None, calls=None):
        self.headers = headers
        self.full_by_uid = full_by_uid
        self.raise_for_uids = raise_for_uids or set()
        self.calls = calls if calls is not None else []

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
    cutoff = date.today() - timedelta(days=IMAP_LOOKBACK_DAYS)
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
