"""Fetch recent client-order mail into triage without changing mailbox flags."""

import logging
import mimetypes
import re
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

from imap_tools import AND, MailBox
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mail_filters import apply_filters_to_email
from app.mail_parser import guess_fields_from_text, guess_service_type
from app.models import Attachment, EmailMessage, Order
from app.settings_store import get_imap_login, get_imap_password

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.ukr.net"
IMAP_TIMEOUT_SECONDS = 20
IMAP_LOOKBACK_DAYS = 30
IMAP_MAX_MESSAGES = 250
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Tags that should force a line break in the extracted text so paragraphs/
# list items/table rows in the source HTML don't all run together into one
# unreadable line.
_HTML_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
)
# Tags whose contents are never real message text (CSS/JS payloads some mail
# clients embed directly in the HTML part).
_HTML_SKIP_TAGS = frozenset({"script", "style"})


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML -> plain text extractor, stdlib-only.

    Used instead of a third-party HTML parser (bs4/html2text aren't already a
    dependency here, see requirements.txt) because the job is narrow: strip
    markup, keep readable line breaks, never crash on malformed client HTML.
    `convert_charrefs=True` (the default) makes HTMLParser hand handle_data()
    already-unescaped text, so entities like `&amp;`/`&nbsp;` come out right
    without a separate `html.unescape()` pass.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _HTML_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def html_to_plain_text(html_body: str) -> str:
    """Best-effort, readable plain text for an HTML-only email body.

    Client mail clients commonly send no plain-text part at all — without
    this, the raw `<div>`/`<span>`/`<a href=...>` markup would land verbatim
    in EmailMessage.body_text, which mail_detail.html renders inside a bare
    `<pre>` (data-quality fix, not a rendering change — the browser must
    never be asked to sanitize-and-render raw HTML here, that stays out of
    scope). Malformed input must never raise — a lab operator doing morning
    triage should see *something* readable, not a broken sync loop.
    """
    if not html_body:
        return ""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html_body)
        extractor.close()
    except Exception:
        logger.warning("html_to_plain_text: falling back to partial parse", exc_info=True)
    raw_text = extractor.get_text()

    # Collapse the whitespace HTML itself doesn't care about (source
    # indentation, hard-wrapped lines) without collapsing intentional blank
    # lines between paragraphs.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw_text.splitlines()]
    collapsed: list[str] = []
    last_was_blank = True
    for line in lines:
        if line:
            collapsed.append(line)
            last_was_blank = False
        elif not last_was_blank:
            collapsed.append("")
            last_was_blank = True
    return "\n".join(collapsed).strip()


def safe_attachment_filename(filename: str | None, index: int, content_type: str) -> str:
    """Return a single safe filename for an untrusted MIME attachment name."""
    fallback = f"attachment_{index}{mimetypes.guess_extension(content_type) or ''}"
    if not filename:
        return fallback
    # Treat both POSIX and Windows separators as path separators regardless of
    # which OS currently runs Order Desk.
    basename = re.split(r"[\\/]", filename)[-1].strip().rstrip(". ")
    basename = _UNSAFE_FILENAME_CHARS.sub("_", basename)
    return basename if basename not in {"", ".", ".."} else fallback


def unique_destination(directory: Path, filename: str) -> Path:
    """Avoid silently replacing two attachments with the same MIME filename."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    number = 2
    while True:
        candidate = directory / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def extract_archive_attachments(
    session: Session, email_message: EmailMessage
) -> tuple[int, list[str]]:
    """Replace each ZIP/RAR attachment of the email with its extracted files —
    clients pack the STL work into an archive, the CAM operator needs it loose.

    For every archive attachment: extract its files into the same mail-spool
    folder (safe basenames, no zip-slip — see app/archive_extract.py), add an
    Attachment row per extracted file, then drop the archive's own row and file
    (its contents now stand on their own). Returns (files_extracted, errors);
    a failed archive is left untouched and its reason collected. Caller commits.

    Lazy import of archive_extract avoids a circular import (it reuses this
    module's filename helpers)."""
    from app.archive_extract import (
        ArchiveExtractError,
        extract_archive,
        is_archive,
    )

    extracted_total = 0
    errors: list[str] = []
    for attachment in list(email_message.attachments):
        if not is_archive(attachment.filename):
            continue
        archive_path = Path(attachment.saved_path)
        if not archive_path.is_file():
            continue
        existing = frozenset(a.filename for a in email_message.attachments)
        try:
            written = extract_archive(archive_path, archive_path.parent, existing)
        except ArchiveExtractError as exc:
            errors.append(f"{attachment.filename}: {exc}")
            continue
        for path in written:
            session.add(
                Attachment(
                    email_message_id=email_message.id,
                    filename=path.name,
                    saved_path=str(path),
                    size_bytes=path.stat().st_size,
                )
            )
        session.delete(attachment)
        archive_path.unlink(missing_ok=True)
        extracted_total += len(written)
    return extracted_total, errors


def _apply_attachments(
    session: Session,
    email_message: EmailMessage,
    msg,
    attachments_dir: Path,
    known_materials: list[str],
) -> None:
    """Fill guess fields and save attachments for one fully-fetched message.

    Shared by phase 2 below. Raises on any I/O failure — the caller is
    responsible for rolling back so a partially-written message never ends
    up marked "ready".
    """
    # Prefer the real plain-text part when the client sent one. Only fall
    # back to the HTML part — stripped to readable text, never stored as raw
    # markup — when no plain-text alternative exists at all.
    if msg.text:
        body = msg.text
    elif msg.html:
        body = html_to_plain_text(msg.html)
    else:
        body = ""
    combined_text = f"{msg.subject or ''}\n{body}"
    guesses = guess_fields_from_text(
        combined_text,
        subject=msg.subject,
        body=body,
        known_materials=known_materials,
    )
    service_type_guess = guess_service_type(combined_text)

    email_message.body_text = body
    email_message.service_type_guess = service_type_guess
    for field, value in guesses.items():
        setattr(email_message, field, value)

    # Admin filter rules (keyword/sender → category): a match routes the letter
    # to the triage screen's «Відфільтровані» tab instead of the main list.
    # Never deletes — the stamp is one click to undo.
    apply_filters_to_email(session, email_message)

    message_dir = attachments_dir / email_message.uid
    for i, att in enumerate(msg.attachments, start=1):
        message_dir.mkdir(parents=True, exist_ok=True)
        # Inline attachments (embedded images, signatures, calendar
        # invites) can arrive with no filename — write_bytes() to a
        # bare directory path would otherwise raise PermissionError.
        filename = safe_attachment_filename(att.filename, i, att.content_type)
        dest_path = unique_destination(message_dir, filename)
        dest_path.write_bytes(att.payload)
        session.info.setdefault("mail_sync_created_paths", []).append(dest_path)
        session.add(
            Attachment(
                email_message_id=email_message.id,
                filename=dest_path.name,
                saved_path=str(dest_path),
                size_bytes=len(att.payload),
            )
        )

    email_message.attachments_status = "ready"


def fetch_new_emails(session: Session, attachments_dir: Path) -> int:
    """Import recent messages without changing any mailbox flags.

    Two phases, both inside this call, both committing progressively so the
    triage screen (``/mail``) shows a new email within seconds rather than
    after the whole batch's attachments have downloaded:

    Phase 1 (fast, headers only) creates an ``EmailMessage`` row per new
    message immediately, with ``attachments_status="pending"`` and guess
    fields left blank — committed one row at a time.

    Phase 2 (slow, full fetch) then processes every ``attachments_status ==
    "pending"`` row — both the ones just created above *and* any left over
    from a previous, interrupted run (crash, dropped connection) — filling
    in body/guess fields and downloading attachments, one commit per
    message. A failure on one message is logged and left "pending" for the
    next sync run to retry; it never aborts the rest of the batch.

    We intentionally inspect *all* messages from the bounded recent window,
    rather than only unread messages: opening an order in webmail must not make
    it invisible to Order Desk.  The database UID constraint makes repeat runs
    idempotent.
    """
    login = get_imap_login(session)
    password = get_imap_password(session)
    if not login or not password:
        raise RuntimeError("IMAP не налаштовано — задайте логін і пароль у Налаштуваннях")

    # Reference list of material/color values technologs/admins actually typed
    # into the sheet (source=="lab", synced via app/sync.py) — used to fuzzy-
    # check the subject/body fallback guess in guess_fields_from_text so a
    # garbled/made-up word from a client email isn't accepted as a real
    # material/color (see app/mail_parser.fuzzy_match_material_color). Computed
    # once up front and reused by both phase 1 (unused there) and phase 2.
    known_materials = list(
        session.scalars(
            select(Order.material_color)
            .where(Order.source == "lab", Order.material_color.is_not(None))
            .distinct()
        )
    )

    cutoff = date.today() - timedelta(days=IMAP_LOOKBACK_DAYS)
    created = 0

    with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password) as mailbox:
        # --- Phase 1: headers-only, fast, one row (and commit) per message ---
        headers = list(
            mailbox.fetch(
                AND(date_gte=cutoff),
                mark_seen=False,
                reverse=True,
                limit=IMAP_MAX_MESSAGES,
                headers_only=True,
            )
        )

        incoming_uids = {str(msg.uid) for msg in headers}
        existing_uids = (
            set(session.scalars(select(EmailMessage.uid).where(EmailMessage.uid.in_(incoming_uids))))
            if incoming_uids
            else set()
        )
        seen_uids: set[str] = set()
        for msg in headers:
            uid = str(msg.uid)
            if uid in seen_uids or uid in existing_uids:
                continue
            seen_uids.add(uid)

            session.add(
                EmailMessage(
                    uid=uid,
                    from_address=msg.from_,
                    subject=msg.subject,
                    received_at=msg.date,
                    status="нове",
                    attachments_status="pending",
                )
            )
            session.commit()
            created += 1

        # --- Phase 2: full fetch of every pending row, self-healing across runs ---
        pending = list(
            session.scalars(select(EmailMessage).where(EmailMessage.attachments_status == "pending"))
        )
        for email_message in pending:
            # Scope disk cleanup to just this message: session.info's shared
            # list also holds paths from earlier messages in this same phase
            # 2 loop that already committed successfully and must not be
            # touched if this one fails.
            created_paths = session.info.setdefault("mail_sync_created_paths", [])
            mark = len(created_paths)
            try:
                full_messages = list(
                    mailbox.fetch(AND(uid=email_message.uid), mark_seen=False)
                )
                if not full_messages:
                    # Message vanished from the mailbox between phase 1 and
                    # phase 2 (deleted/moved elsewhere) — nothing to fetch.
                    # Leave it "pending"; a future run will see the same
                    # thing and skip it again rather than crash.
                    logger.warning(
                        "Mail sync: uid %s no longer found on server, skipping",
                        email_message.uid,
                    )
                    continue
                _apply_attachments(session, email_message, full_messages[0], attachments_dir, known_materials)
                session.commit()
                # This message's files are now safely persisted (DB row
                # committed, "ready"). Drop them from the shared "at risk"
                # list so a later, unrelated failure elsewhere in this run
                # (caught by app.mail_sync_service's all-or-nothing cleanup)
                # can never delete already-successful attachments out from
                # under an already-committed Attachment row.
                del created_paths[mark:]
                # Auto-unpack any ZIP/RAR attachment so the STL work is loose,
                # not zipped. Best-effort and isolated: a bad/unsupported archive
                # (e.g. no UnRAR for RAR) is logged and the letter keeps its
                # files — never fails the sync.
                try:
                    extracted, extract_errors = extract_archive_attachments(
                        session, email_message
                    )
                    if extracted or extract_errors:
                        session.commit()
                    if extract_errors:
                        logger.warning(
                            "Archive extract for uid %s: %s",
                            email_message.uid,
                            "; ".join(extract_errors),
                        )
                except Exception:
                    logger.exception(
                        "Archive extract failed for uid %s", email_message.uid
                    )
                    session.rollback()
            except Exception:
                logger.exception(
                    "Mail sync: failed to fetch attachments for uid %s, will retry next run",
                    email_message.uid,
                )
                session.rollback()
                # Undo just this message's partially-written attachment
                # files; the DB rollback already undid the Attachment rows
                # and status change, so "pending" and disk agree again.
                orphaned = created_paths[mark:]
                del created_paths[mark:]
                for path in reversed(orphaned):
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError:
                        pass
                continue

    return created
