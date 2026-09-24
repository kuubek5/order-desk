"""Fetch recent client-order mail into triage without changing mailbox flags."""

import logging
import mimetypes
import re
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from imap_tools import AND, MailBox
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.business_day import business_today
from app.mail_filters import apply_filters_to_email
from app.mail_parser import guess_fields_from_text, guess_service_type
from app.material_catalog import ensure_seeded as ensure_materials_seeded, load_alias_rows
from app.material_classifier import AliasRow
from app.sender_memory import is_auto_sender
from app.safe_names import avoid_reserved_device_name
from app.mail_spool import spool_folder_name
from app.models import Attachment, EmailMessage, Order
from app.settings_store import (
    get_imap_login,
    get_imap_password,
    get_mail_default_material,
    get_mail_download_all,
    get_setting,
)

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.ukr.net"
IMAP_TIMEOUT_SECONDS = 20

# Per-file ceiling for a downloaded MIME attachment. Dental STL work and the
# ZIP/RAR clients send are single-digit-to-tens of MB; anything past this is
# not lab work, and with the «скачувати все» toggle on it would be written to
# the workstation's disk unattended. Oversized files are skipped with a log,
# never silently truncated.
MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024
IMAP_LOOKBACK_DAYS = 30
IMAP_MAX_MESSAGES = 250
# Скільки разів перепитуємо диск, перш ніж визнати файл вкладення втраченим,
# і пауза між спробами. Три швидкі спроби переживають типове моргання
# мережевої шари, не затримуючи операцію помітно (див. _file_is_missing).
MISSING_FILE_RETRIES = 3
MISSING_FILE_RETRY_DELAY = 0.3
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sender_display_name(msg) -> str | None:
    """Показне ім'я відправника з заголовка From ("Юрій Струбицький"), або None.

    imap-tools кладе його у `from_values.name` окремо від адреси (`from_`).
    Захищаємось від листа без From (`from_values` = None) і від порожнього
    імені (тоді список тріажу впаде на здогад/адресу). Ніколи не кидає —
    некритична підказка для екрана, не має валити синк.
    """
    values = getattr(msg, "from_values", None)
    name = (getattr(values, "name", "") or "").strip()
    return name or None


def message_id_of(msg) -> str | None:
    """Заголовок Message-ID листа — стабільний ідентифікатор, що НЕ міняється при
    переміщенні між папками (на відміну від UID). None, якщо листа без нього.
    Ніколи не кидає — некритична підказка, не має валити синк."""
    try:
        value = (msg.obj.get("Message-ID") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    return value or None

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


def _repair_mojibake(name: str) -> str:
    """Undo the classic "UTF-8 filename mis-decoded as Latin-1/CP1252" garble
    (e.g. "копия" arriving as "ÐºÐ¾Ð¿Ð¸Ñ", em-dash as "â€""). Some senders /
    forwarders label attachments with raw UTF-8 bytes that the mail library then
    reads as Latin-1; round-tripping the bytes back through UTF-8 restores the
    Cyrillic. Applied only when the string carries the tell-tale mojibake marks
    AND the round-trip succeeds — otherwise the original name is kept."""
    if not any(mark in name for mark in ("Ð", "Ñ", "Ã", "â")):
        return name
    try:
        repaired = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    return repaired or name


def safe_attachment_filename(filename: str | None, index: int, content_type: str) -> str:
    """Return a single safe filename for an untrusted MIME attachment name."""
    fallback = f"attachment_{index}{mimetypes.guess_extension(content_type) or ''}"
    if not filename:
        return fallback
    filename = _repair_mojibake(filename)
    # Treat both POSIX and Windows separators as path separators regardless of
    # which OS currently runs KuubMill.
    basename = re.split(r"[\\/]", filename)[-1].strip().rstrip(". ")
    basename = _UNSAFE_FILENAME_CHARS.sub("_", basename)
    if basename in {"", ".", ".."}:
        return fallback
    # "con.stl" is a legal MIME name but Windows refuses to create the file, and
    # the resulting OSError leaves the whole letter pending forever (re-fetched
    # on every sync). Same rule as export folder names — app/safe_names.py.
    return avoid_reserved_device_name(basename)


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

    The archive FILE is deleted only after the caller's commit succeeds (via a
    one-shot after_commit hook), never before. If the commit fails and rolls
    back, the Attachment rows for the extracted files vanish — and if the
    archive had already been unlinked, the letter would be left "ready" with
    neither the archive nor any row pointing at the loose files: the work would
    be silently lost for the UI. Keeping the archive on disk until the commit
    lands makes the whole step all-or-nothing; a re-run just re-extracts.

    Lazy import of archive_extract avoids a circular import (it reuses this
    module's filename helpers)."""
    from app.archive_extract import (
        ArchiveExtractError,
        extract_archive,
        is_archive,
    )

    extracted_total = 0
    errors: list[str] = []
    archives_to_unlink: list[Path] = []
    # Імена, зайняті ДО розпакування — рахуються ОДИН раз, а не на кожен архів
    # (у циклі це був той самий набір: нові рядки лише `session.add`-нуті й у
    # колекції ще не зʼявляються).
    #
    # І вони свідомо НЕ поповнюються тим, що розпакували зараз. Цей набір існує
    # проти повторного розпакування того самого архіву; два РІЗНІ архіви в
    # одному листі законно несуть однакові імена («crown.stl» у part1 і part2),
    # і пропустити другий означало б тихо втратити коронку. Колізію імен
    # розводить `unique_destination` — обидва файли лишаються на диску.
    existing = frozenset(a.filename for a in email_message.attachments)
    for attachment in list(email_message.attachments):
        if not is_archive(attachment.filename):
            continue
        archive_path = Path(attachment.saved_path)
        if not archive_path.is_file():
            continue
        try:
            written = extract_archive(archive_path, archive_path.parent, existing)
        except ArchiveExtractError as exc:
            errors.append(f"{attachment.filename}: {exc}")
            continue
        # Розпаковані файли — у той самий перелік «створене цим прогоном», яким
        # уже користується фаза 2 синку. Без цього збій ПІСЛЯ вдалого
        # розпакування (напр. `path.stat()` на шарі, що моргнула) відкочував
        # рядки в базі, але лишав файли на диску — а лист уже `ready`, тож
        # повторного розпакування не буде, і в спулі назавжди лежать STL без
        # жодного рядка (аудит 08.09.26). `session.info` тут єдина правильна
        # адреса: перелік переживає rollback, бо не є частиною транзакції.
        created_paths = session.info.setdefault("mail_sync_created_paths", [])
        for path in written:
            created_paths.append(path)
            session.add(
                Attachment(
                    email_message_id=email_message.id,
                    filename=path.name,
                    saved_path=str(path),
                    size_bytes=path.stat().st_size,
                )
            )
        session.delete(attachment)
        archives_to_unlink.append(archive_path)
        extracted_total += len(written)

    if archives_to_unlink:
        _unlink_after_commit(session, archives_to_unlink)
    return extracted_total, errors


def _unlink_after_commit(session: Session, paths: list[Path]) -> None:
    """Delete `paths` once — and only if — the session's next commit succeeds.

    Registered as a one-shot pair of ``after_commit`` / ``after_rollback``
    listeners so every caller of extract_archive_attachments (background sync,
    link-download, the manual «Розпакувати» button) gets the same
    all-or-nothing guarantee without each having to remember the ordering.
    On rollback the pair is removed WITHOUT deleting — this matters inside the
    sync loop, where the same session goes on to commit the NEXT letter: an
    un-removed listener would fire on that later commit and delete an archive
    whose extracted rows had already been rolled back."""
    from sqlalchemy import event

    # SQLAlchemy forbids event.remove() from inside the listener itself
    # ("deque mutated during iteration"), so one-shot-ness is a shared flag:
    # whichever of commit/rollback fires first consumes it and the other
    # becomes a no-op. The dead listeners are then harmless until the session
    # closes (sessions here are per-request / per-sync-tick).
    state = {"armed": True}

    def _on_commit(_session: Session) -> None:
        if not state["armed"]:
            return
        state["armed"] = False
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove extracted archive %s", path)

    def _on_rollback(_session: Session) -> None:
        state["armed"] = False

    event.listen(session, "after_commit", _on_commit)
    event.listen(session, "after_rollback", _on_rollback)


def _apply_attachments(
    session: Session,
    email_message: EmailMessage,
    msg,
    attachments_dir: Path,
    known_materials: list[str],
    material_alias_rows: "list[AliasRow] | None" = None,
    default_material: str | None = None,
    download_all: bool = False,
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
        material_alias_rows=material_alias_rows,
    )
    # Default-material rule (admin toggle): a milling letter with no material
    # signal at all is assumed to be the lab's dominant material (цирконій by
    # convention) so the triage card isn't left blank. Only fills a genuine gap
    # — never overrides a real guess — and skips 3D-print letters, which aren't
    # milled here. Operator still reviews.
    service_type_guess = guess_service_type(combined_text)
    if (
        default_material
        and not guesses.get("material_color_guess")
        and service_type_guess != "3d_print"
    ):
        guesses["material_color_guess"] = default_material

    email_message.body_text = body
    email_message.service_type_guess = service_type_guess
    for field, value in guesses.items():
        setattr(email_message, field, value)

    # Admin filter rules (keyword/sender → category): a match routes the letter
    # to the triage screen's «Відфільтровані» tab instead of the main list.
    # Never deletes — the stamp is one click to undo.
    apply_filters_to_email(session, email_message)

    # Download attachments for senders on the auto (preview) list — OR for
    # everyone when the admin flipped the "download all" toggle. Without the
    # toggle the lab gets many letters with files that don't concern it, so junk
    # from unknown senders is left as headers-only ("skipped") until an operator
    # pulls it by hand («Скачати файли»). Body/guesses above are always parsed,
    # so the letter is still readable and filterable regardless. The whitelist
    # check needs the body (set above) for the forwarded-sender key, so it runs
    # here, not in phase 1.
    if download_all or is_auto_sender(session, email_message):
        _save_message_attachments(session, email_message, msg, attachments_dir)
        email_message.attachments_status = "ready"
    else:
        email_message.attachments_status = "skipped"


def _save_message_attachments(
    session, email_message, msg, attachments_dir: Path, skip_names: set[str] | None = None
) -> int:
    """Write a fetched message's attachments to the spool and add Attachment
    rows. Returns how many were saved. Shared by the whitelisted auto-download
    and the manual «Скачати файли» action.

    Files above MAX_ATTACHMENT_BYTES are skipped (logged, no row) rather than
    written: real dental STL/archives are single-digit-to-tens of MB, and with
    the «скачувати все» toggle on, one junk letter with a giant attachment
    would otherwise fill the workstation's disk. The letter itself still
    imports — only that one oversized file is left behind on the server."""
    # Тека — за складеним ключем листа, не за самим uid: uid унікальний лише в
    # межах UIDVALIDITY, і після перестворення скриньки два різні листи
    # ділили б одну теку (app/mail_spool.spool_folder_name).
    message_dir = attachments_dir / spool_folder_name(
        email_message.uid, email_message.uid_validity
    )
    saved = 0
    for i, att in enumerate(msg.attachments, start=1):
        payload = att.payload or b""
        if len(payload) > MAX_ATTACHMENT_BYTES:
            logger.warning(
                "Mail sync: skipping oversized attachment %r (%.1f МБ) on uid %s",
                att.filename,
                len(payload) / (1024 * 1024),
                email_message.uid,
            )
            continue
        # Inline attachments (embedded images, signatures, calendar
        # invites) can arrive with no filename — write_bytes() to a
        # bare directory path would otherwise raise PermissionError.
        filename = safe_attachment_filename(att.filename, i, att.content_type)
        # Повторне скачування: те, що ціле лежить на диску, пропускаємо, інакше
        # поруч із живим файлом лягав би його «(1)»-двійник.
        if skip_names and filename in skip_names:
            continue
        message_dir.mkdir(parents=True, exist_ok=True)
        dest_path = unique_destination(message_dir, filename)
        dest_path.write_bytes(payload)
        session.info.setdefault("mail_sync_created_paths", []).append(dest_path)
        session.add(
            Attachment(
                email_message_id=email_message.id,
                filename=dest_path.name,
                saved_path=str(dest_path),
                size_bytes=len(payload),
            )
        )
        saved += 1
    return saved


def _file_is_missing(raw_path: str | None) -> bool:
    r"""Чи файл вкладення СПРАВДІ зник з диска.

    ЧОМУ не `Path(...).exists()`: він ковтає будь-яку OSError і повертає False,
    тож коротке моргання мережі на UNC-шляху (`\\host\share`, WinError 53/64/1231)
    виглядає точно так само, як видалений файл. А ціна помилки несиметрична:
    «зник» → рядок Attachment видаляється НАЗАВЖДИ, справжній файл лишається на
    диску сиротою без рядка, а докачана копія отримує ім'я «(1)» — на видачі
    оператор бачить двійника і не знає, який справжній.

    Тому: кілька спроб із паузою, і втратою вважається ЛИШЕ чистий
    FileNotFoundError на останній спробі. Будь-яка інша OSError — це
    «шара недоступна», і вкладення не чіпаємо.
    """
    if not raw_path:
        return True
    path = Path(raw_path)
    missing = True
    for attempt in range(MISSING_FILE_RETRIES):
        try:
            path.stat()
            return False
        except FileNotFoundError:
            missing = True
        except OSError as exc:
            # Недоступність, а не відсутність — краще нічого не робити.
            logger.warning("Не вдалося перевірити %s: %s", raw_path, exc)
            missing = False
        if attempt + 1 < MISSING_FILE_RETRIES:
            time.sleep(MISSING_FILE_RETRY_DELAY)
    return missing


def redownload_missing_attachments(
    session: Session, email_message: EmailMessage, attachments_dir: Path
) -> tuple[int, int]:
    """Повторно скачати вкладення листа, файли якого зникли з диска.

    Реальний випадок: файли скачались, а потім теку в спулі хтось прибрав
    (чистка диска, ручне видалення). У базі рядки лишались, панель писала
    «Усі файли на диску», «Відкрити папку» падало — і зробити з цим не можна
    було нічого.

    Правила:
    - зносяться рядки ЛИШЕ тих вкладень, файлів яких справді немає; те, що
      лежить на диску, не чіпається (інакше повторне скачування створювало б
      «(1)»-двійників поруч із цілими файлами);
    - вкладення, вже прийняте в чергу (order_id), не чіпається взагалі: його
      файл переїхав у export, і його відсутність у спулі — норма, а не втрата;
    - якщо після чистки не лишилось жодного живого файлу, скидаються
      handled_link_refs: файли, стягнуті за посиланням, у самому листі не
      лежать, і без цього дістати їх повторно було б нічим.

    Повертає (скільки рядків прибрано, скільки скачано наново).
    """
    login = get_imap_login(session)
    password = get_imap_password(session)
    if not login or not password:
        raise RuntimeError("IMAP не налаштовано — задайте логін і пароль у Налаштуваннях")

    # Один прохід по диску на вкладення: _file_is_missing ретраїть із паузою,
    # тож повторна перевірка того самого шляху коштувала б ще одну паузу.
    checked = [(a, _file_is_missing(a.saved_path)) for a in email_message.attachments]
    missing = [a for a, gone in checked if a.order_id is None and gone]
    if not missing:
        return (0, 0)
    alive_names = {a.filename for a, gone in checked if not gone}

    with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password) as mailbox:
        # Гейт нумерації — ДО видалення рядків: інакше «скачати наново» стирало б
        # записи про STL клієнта й клало на їхнє місце чужі файли.
        _refuse_stale_uid_namespace(mailbox, email_message)
        for attachment in missing:
            session.delete(attachment)
        # Рядки треба прибрати з сесії ДО повторного збереження, інакше
        # unique_destination побачить на диску лише те, що є, а в базі — і старе.
        session.flush()
        full = list(mailbox.fetch(AND(uid=email_message.uid), mark_seen=False))
        if not full:
            raise RuntimeError("Лист більше недоступний на сервері")
        saved = _save_message_attachments(
            session, email_message, full[0], attachments_dir, skip_names=alive_names
        )
    if not alive_names:
        email_message.handled_link_refs = None
    email_message.attachments_status = "ready"
    return (len(missing), saved)


def _refuse_stale_uid_namespace(mailbox, email_message: EmailMessage) -> None:
    """Той самий гейт, що й у fetch_new_emails: після перестворення теки
    провайдером uid належить ЧУЖОМУ листу, і ручне скачування причепило б до
    цієї роботи файли стороннього клієнта (ревʼю 07.09.26, mail CRITICAL-1/2).
    Порожній UIDVALIDITY з будь-якого боку = «не знаємо» → поводимось як раніше."""
    current = _folder_uidvalidity(mailbox)
    stored = (email_message.uid_validity or "").strip()
    if current and stored and current != stored:
        raise RuntimeError(
            "Скриньку перенумеровано — на сервері під цим номером тепер інший лист. "
            "Файли треба взяти з нового листа, а цей відхилити."
        )


def list_mailbox_folders(session: Session) -> list[str]:
    """Усі папки скриньки — для вибору в Налаштуваннях, куди переносити оброблені
    листи. Порядок як віддає сервер; службові (Inbox/Trash/Spam) лишаємо, адмін
    вирішує сам. Порожній список, якщо IMAP не налаштовано — екран підкаже."""
    login = get_imap_login(session)
    password = get_imap_password(session)
    if not login or not password:
        return []
    with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password) as mailbox:
        return [f.name for f in mailbox.folder.list()]


def move_message_to_folder(session: Session, email_message: EmailMessage, folder: str) -> None:
    """Перемістити лист у папку скриньки за UID — ПЕРШИЙ запис у скриньку (досі
    лише читали). Той самий гейт нумерації, що й у скачуванні: після
    перестворення теки провайдером uid належить чужому листу, і ми пересунули б
    не той. Кидає на будь-якій невдачі (imap-tools підніме виняток, якщо папки
    немає або COPY не пройшов) — роут покаже помилку, поле в базі не ставиться."""
    login = get_imap_login(session)
    password = get_imap_password(session)
    if not login or not password:
        raise RuntimeError("IMAP не налаштовано — задайте логін і пароль у Налаштуваннях")
    if not (folder or "").strip():
        raise RuntimeError("Не вибрано папку для переміщення — задайте її в Налаштуваннях пошти")
    with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password) as mailbox:
        _refuse_stale_uid_namespace(mailbox, email_message)
        # Беккфіл Message-ID, поки лист ще в Inbox під відомим UID — потрібен,
        # щоб потім можна було повернути його з папки назад (реверс шукає за ним).
        if not email_message.message_id:
            got = list(
                mailbox.fetch(AND(uid=email_message.uid), mark_seen=False, headers_only=True)
            )
            if got:
                email_message.message_id = message_id_of(got[0])
        mailbox.move(email_message.uid, folder)


def _imap_quote(value: str) -> str:
    """Значення для IMAP-рядка в лапках: екрануємо \\ і ", решта — як є."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def move_message_back_to_inbox(session: Session, email_message: EmailMessage) -> None:
    """Повернути перенесений лист із папки назад у Inbox (зворотна до
    move_message_to_folder дія).

    UID листа в папці — інший, ніж збережений, а після повернення в Inbox стане
    ще іншим, тож знаходимо лист за Message-ID (стабільний). ОБОВʼЯЗКОВО оновлюємо
    email.uid на новий Inbox-UID: інакше наступний синк побачив би цей лист як
    «новий» (його UID не в existing_uids) і створив би дубль. Кидає на будь-якій
    невдачі — роут покаже помилку, поле в базі лишиться."""
    login = get_imap_login(session)
    password = get_imap_password(session)
    if not login or not password:
        raise RuntimeError("IMAP не налаштовано — задайте логін і пароль у Налаштуваннях")
    mid = (email_message.message_id or "").strip()
    if not mid:
        raise RuntimeError(
            "Цей лист імпортовано до появи функції (немає Message-ID) — "
            "повернути автоматично не можемо, перенесіть у пошті вручну."
        )
    folder = (email_message.mailbox_folder or "").strip()
    if not folder:
        raise RuntimeError("Лист не в папці — повертати нічого")

    search = f'HEADER MESSAGE-ID "{_imap_quote(mid)}"'
    with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password) as mailbox:
        mailbox.folder.set(folder)
        found = list(mailbox.fetch(search, mark_seen=False, headers_only=True))
        if not found:
            raise RuntimeError(
                "Лист не знайдено в папці — можливо, його вже перемістили у пошті вручну."
            )
        mailbox.move(found[0].uid, "INBOX")
        # Лист тепер в Inbox під новим UID — знаходимо його й оновлюємо базу.
        mailbox.folder.set("INBOX")
        back = list(mailbox.fetch(search, mark_seen=False, headers_only=True))
        if back:
            email_message.uid = str(back[0].uid)
            email_message.uid_validity = (
                _folder_uidvalidity(mailbox) or email_message.uid_validity
            )
    email_message.mailbox_folder = None
    email_message.mailbox_moved_at = None


def _reflect_processed_folder(session: Session, mailbox, folder: str, cutoff) -> int:
    """Дзеркалити папку «оброблено» в ОБИДВА боки за фізичним станом скриньки.

    Модель (рішення власника 24.09.26): місце листа визначає, у якій він вкладці.
    У папці «Скачано-прошитано» → «Оброблено»; десь інде (інша папка / видалено) →
    «Покинули Вхідні»; у Вхідних → тріаж. Рівно ОДИН стан, і папка «оброблено»
    перемагає «покинули»: лист у ній опрацьований, а не загублений.

    ВПЕРЕД (щось → папка): лист, перенесений у папку прямо в пошті (або кнопкою
    «оброблено»), стає перенесеним і в базі. Заразом ЗНІМАЄ `inbox_gone_at`:
    `_reconcile_inbox_gone` біжить РАНІШЕ в цьому ж синку й міг помітити його
    «покинув Вхідні» (він фізично зник з Inbox), а лист насправді опрацьований —
    без цього рядка він двоївся б у вкладках «Оброблено» і «Покинули Вхідні»
    водночас.

    НАЗАД (папка → десь інде): лист, який база вважає в папці, а фізично його там
    уже НЕМА, — знімаємо мітку папки. Повернення саме в Inbox обробляє фаза 1 за
    Message-ID (оновлює UID); сюди доходять лише ті, кого фаза 1 не всиновила,
    тобто перенесені в ІНШУ папку чи видалені → у «Покинули Вхідні». Робимо це
    ЛИШЕ для листів у вікні синку (`received_at >= cutoff`): фетч папки теж
    обмежений `date_gte`, тож для старішого листа «немає в папці» довести не
    можна — його не чіпаємо (лишається «оброблено»), інакше він злітав би сам
    щойно постаріє за 30 днів.

    Зіставлення — за Message-ID (стабільний між папками; UID у папці інший). Без
    Message-ID пропускаємо: беккфіл (фаза 1) проставляє його всім листам, поки
    вони ще в Inbox. Не кидає: збій читання папки не має валити синк. Повертає,
    скільки листів змінено.
    """
    folder = (folder or "").strip()
    if not folder:
        return 0
    try:
        mailbox.folder.set(folder)
        folder_msgs = list(
            mailbox.fetch(
                AND(date_gte=cutoff), mark_seen=False, headers_only=True,
                limit=IMAP_MAX_MESSAGES,
            )
        )
    except Exception:
        logger.exception("Синк: не вдалося прочитати папку '%s' для звірки", folder)
        return 0
    mids_in_folder = {
        mid for m in folder_msgs if (mid := message_id_of(m))
    }
    now = datetime.now()
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())
    changed = 0

    # ВПЕРЕД: рядки, які додаток вважає НЕ в папці, але фізично вони вже в ній.
    forward = list(
        session.scalars(
            select(EmailMessage).where(
                EmailMessage.mailbox_folder.is_(None),
                EmailMessage.message_id.is_not(None),
            )
        )
    )
    for row in forward:
        if (row.message_id or "").strip() in mids_in_folder:
            row.mailbox_folder = folder
            row.mailbox_moved_at = now  # для вкладки «Оброблено за сьогодні»
            row.inbox_gone_at = None  # у папці «оброблено» ⇒ не «покинув»
            changed += 1

    # НАЗАД: рядки, позначені цією папкою, яких фізично в ній уже немає.
    marked_rows = list(
        session.scalars(
            select(EmailMessage).where(
                EmailMessage.mailbox_folder == folder,
                EmailMessage.message_id.is_not(None),
            )
        )
    )
    for row in marked_rows:
        if (row.message_id or "").strip() in mids_in_folder:
            continue  # усе ще в папці
        received = row.received_at
        if received is None or received < cutoff_dt:
            continue  # поза вікном — відсутність у папці не довести
        # У вікні, але в папці немає, і фаза 1 не всиновила назад у Inbox →
        # перенесений в іншу папку або видалений: «Покинули Вхідні».
        row.mailbox_folder = None
        row.mailbox_moved_at = None
        row.inbox_gone_at = now
        changed += 1

    if changed:
        session.commit()
        logger.info("Синк: дзеркало папки '%s' змінило листів: %d", folder, changed)
    return changed


def _reconcile_inbox_gone(
    session: Session, incoming_uids: set[str], cutoff, complete_fetch: bool
) -> int:
    """CRM «Нові з пошти» дзеркалить Вхідні пошти: лист, якого вже НЕМА у
    Вхідних (переклали в будь-яку папку АБО видалили — усі чистять скриньку
    напряму), виходить із черги тріажу у вкладку «Покинули Вхідні»
    (`inbox_gone_at`). Ловимо за ФАКТОМ відсутності, не за Message-ID — щоб
    брати й старі листи без нього (рішення власника 24.09.26).

    Дві гілки:
      1. Лист у вікні синку (`received_at >= cutoff`) — мітимо ЛИШЕ коли вибірка
         Вхідних ПОВНА (`complete_fetch`, не вперлись у ліміт) і листа в ній
         немає: тоді відсутність доведена.
      2. Лист СТАРШИЙ за вікно (`received_at < cutoff`) — синк його взагалі не
         тягне, тож відсутність не довести; але скриньку чистять щодня, і
         «нове», що висить понад вікно, майже напевно вже оброблене — прибираємо
         в ту саму вкладку (лист не зникає, лежить у «Покинули Вхідні»).

    Самовиправно: якщо лист знову зʼявився у Вхідних (той самий UID) — мітку
    знімаємо. Лист без `received_at` не чіпаємо (дати немає — не гадаємо).
    Не чіпаємо перенесені кнопкою (`mailbox_folder` задано) й уже архівні
    (статус не «нове»). Повертає, скільки нових листів позначено."""
    now = datetime.now()
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())

    # Самовиправлення: лист повернувся у Вхідні під тим самим UID → знімаємо.
    if incoming_uids:
        for row in session.scalars(
            select(EmailMessage).where(
                EmailMessage.inbox_gone_at.is_not(None),
                EmailMessage.uid.in_(incoming_uids),
            )
        ):
            row.inbox_gone_at = None

    candidates = session.scalars(
        select(EmailMessage).where(
            EmailMessage.status == "нове",
            EmailMessage.mailbox_folder.is_(None),
            EmailMessage.inbox_gone_at.is_(None),
        )
    ).all()
    marked = 0
    for row in candidates:
        received = row.received_at
        if received is None:
            continue  # дати немає — не гадаємо
        if received >= cutoff_dt:
            if complete_fetch and str(row.uid) not in incoming_uids:
                row.inbox_gone_at = now
                marked += 1
        else:
            row.inbox_gone_at = now  # старший за вікно синку
            marked += 1
    if marked or incoming_uids:
        session.commit()
    if marked:
        logger.info("Синк: покинули Вхідні (прибрано з черги тріажу): %d", marked)
    return marked


def download_attachments_now(session: Session, email_message: EmailMessage, attachments_dir: Path) -> int:
    """Manually pull a "skipped" letter's attachments on demand (operator
    decided a non-whitelisted letter is relevant after all). Re-fetches the
    message by UID and saves its files, flipping status to "ready". Returns the
    count saved. Raises on IMAP/IO failure — the caller rolls back."""
    login = get_imap_login(session)
    password = get_imap_password(session)
    if not login or not password:
        raise RuntimeError("IMAP не налаштовано — задайте логін і пароль у Налаштуваннях")
    with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password) as mailbox:
        _refuse_stale_uid_namespace(mailbox, email_message)
        full = list(mailbox.fetch(AND(uid=email_message.uid), mark_seen=False))
        if not full:
            raise RuntimeError("Лист більше недоступний на сервері")
        saved = _save_message_attachments(session, email_message, full[0], attachments_dir)
    email_message.attachments_status = "ready"
    return saved


def _folder_uidvalidity(mailbox) -> str:
    """UIDVALIDITY поточної теки, або "" якщо сервер його не віддав.

    Це число — namespace для UID: воно змінюється рівно тоді, коли провайдер
    перестворює теку, і саме тоді старі UID перестають щось означати. Порожній
    рядок = «не знаємо», і тоді поводимось як раніше (дедуп самим uid): краще
    не імпортувати дублікати через тимчасову невдачу STATUS-команди.
    """
    try:
        status = mailbox.folder.status(options=["UIDVALIDITY"])
        value = status.get("UIDVALIDITY")
    except Exception:
        logger.warning("Не вдалося прочитати UIDVALIDITY теки — дедуп лише за uid")
        return ""
    return "" if value is None else str(value)


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
    it invisible to KuubMill.  The database's unique (uid, uid_validity) pair
    makes repeat runs idempotent — the UIDVALIDITY half matters because an
    IMAP UID is only unique while the folder keeps its current UIDVALIDITY.
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

    # Editable material dictionary (admin-maintained, /settings/materials):
    # material aliases feed the triage material guess. Loaded once per sync and
    # reused for every message, like known_materials above. ensure_seeded is
    # idempotent — a no-op once the migration has seeded, but covers
    # create_all/first-boot installs. default_material is the fallback for a
    # milling letter that carries no material signal at all.
    ensure_materials_seeded(session)
    material_alias_rows = load_alias_rows(session)
    default_material = get_mail_default_material(session)
    # Admin toggle: when on, every incoming letter's attachments auto-download to
    # the spool (not only whitelisted senders). Read once per sync.
    download_all = get_mail_download_all(session)

    # Робоча доба, не календарна: о 00:05 нічна зміна ще веде вчорашній день, а
    # `date.today()` уже перекинувся — вікно пошуку стрибало на добу раніше, і
    # найстаріші листи випадали з нього просто посеред зміни (CLAUDE.md §14,
    # ревʼю 07.09.26, LOW).
    cutoff = business_today() - timedelta(days=IMAP_LOOKBACK_DAYS)
    created = 0

    with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password) as mailbox:
        uid_validity = _folder_uidvalidity(mailbox)

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
        # Дедуп у межах ОДНОГО namespace. Рядок з іншим (мертвим) UIDVALIDITY
        # збігом не вважається: інакше новий лист, якому дістався старий номер,
        # мовчки не імпортувався б узагалі. Порожній uid_validity у рядку —
        # спадок часів до колонки: namespace невідомий, тож приймаємо його за
        # поточний і одразу проставляємо, щоб наступного разу вже знати.
        existing_rows = (
            session.execute(
                select(EmailMessage).where(EmailMessage.uid.in_(incoming_uids))
            ).scalars().all()
            if incoming_uids
            else []
        )
        existing_uids: set[str] = set()
        adopted = False
        for row in existing_rows:
            if not row.uid_validity:
                row.uid_validity = uid_validity
                adopted = bool(uid_validity)
                existing_uids.add(row.uid)
            elif not uid_validity or row.uid_validity == uid_validity:
                existing_uids.add(row.uid)
            else:
                logger.warning(
                    "UIDVALIDITY змінився (%s → %s): uid %s належить мертвій "
                    "нумерації, лист імпортуємо як новий",
                    row.uid_validity, uid_validity, row.uid,
                )
        if adopted:
            session.commit()
        # Наявні рядки цього namespace за uid — щоб добілити поля, яких у них
        # ще немає (лист синкнули ДО появи колонки). from_name зʼявився пізніше,
        # тож усі старіші листи показувались адресою замість імені (скарга
        # власника 24.09.26); message_id теж — без нього перенесення в папку не
        # знаходить лист назад. Пишемо лише коли поле порожнє: не перетираємо
        # правок і не чіпаємо те, що вже стоїть.
        existing_by_uid = {row.uid: row for row in existing_rows}

        # Лист, який ПОВЕРНУВСЯ у Вхідні (перенесли назад прямо в пошті з папки
        # «оброблено» чи з іншої): у Inbox він тепер під НОВИМ uid. Без цього
        # фаза 1 створила б дубль, а стара мітка (`mailbox_folder`/`inbox_gone_at`)
        # лишилася б на старому рядку — лист висів би в «Оброблено»/«Покинули»
        # і водночас з'явився б новим у тріажі. Зіставляємо за Message-ID зі
        # ЗМІЩЕНИМИ рядками (не у Вхідних за нашою базою) і всиновлюємо той самий
        # рядок: оновлюємо uid, знімаємо мітки — лист знову в тріажі. Це і є
        # зворотний бік дзеркала папки (рішення власника 24.09.26).
        header_mids = {mid for msg in headers if (mid := message_id_of(msg))}
        displaced_by_mid: dict[str, EmailMessage] = {}
        if header_mids:
            for row in session.scalars(
                select(EmailMessage).where(
                    EmailMessage.message_id.in_(header_mids),
                    or_(
                        EmailMessage.mailbox_folder.is_not(None),
                        EmailMessage.inbox_gone_at.is_not(None),
                    ),
                )
            ):
                displaced_by_mid.setdefault((row.message_id or "").strip(), row)

        seen_uids: set[str] = set()
        for msg in headers:
            uid = str(msg.uid)
            if uid in seen_uids or uid in existing_uids:
                row = existing_by_uid.get(uid)
                if row is not None:
                    if not row.from_name:
                        row.from_name = sender_display_name(msg)
                    if not row.message_id:
                        row.message_id = message_id_of(msg)
                continue
            seen_uids.add(uid)

            mid = message_id_of(msg)
            adopted = displaced_by_mid.pop((mid or "").strip(), None) if mid else None
            if adopted is not None:
                # Той самий лист під новим uid — повернувся у Вхідні.
                adopted.uid = uid
                adopted.uid_validity = uid_validity
                adopted.mailbox_folder = None
                adopted.mailbox_moved_at = None
                adopted.inbox_gone_at = None
                if not adopted.from_name:
                    adopted.from_name = sender_display_name(msg)
                session.commit()
                continue

            session.add(
                EmailMessage(
                    uid=uid,
                    uid_validity=uid_validity,
                    from_address=msg.from_,
                    from_name=sender_display_name(msg),
                    message_id=message_id_of(msg),
                    subject=msg.subject,
                    received_at=msg.date,
                    status="нове",
                    attachments_status="pending",
                )
            )
            session.commit()
            created += 1

        # Зберегти добілені поля наявних рядків (from_name / message_id): у циклі
        # вище вони лише виставляються на обʼєктах, а комітяться лише вставки
        # нових листів. Один коміт наприкінці фази — не на кожен рядок.
        session.commit()

        # CRM-черга дзеркалить Вхідні: лист, якого вже нема у Вхідних (папка або
        # видалення), покидає чергу тріажу. `complete_fetch` — вибірка Вхідних
        # повна (не вперлись у ліміт), тож відсутність у вікні доведена.
        # Обгорнуто: збій звірки не має валити весь синк (Фаза 2 важливіша).
        try:
            _reconcile_inbox_gone(
                session, incoming_uids, cutoff,
                complete_fetch=len(headers) < IMAP_MAX_MESSAGES,
            )
        except Exception:
            logger.exception("Синк: звірка «Покинули Вхідні» впала — пропускаємо")
            session.rollback()

        # --- Phase 2: full fetch of every pending row, self-healing across runs ---
        pending = list(
            session.scalars(select(EmailMessage).where(EmailMessage.attachments_status == "pending"))
        )
        for email_message in pending:
            # Рядок із мертвої нумерації добирати НЕ можна: той самий uid на
            # сервері тепер належить ЧУЖОМУ листу, і ми акуратно причепили б
            # його вкладення не туди. Лишаємо «pending» — видно, що не добрано.
            if (
                uid_validity
                and email_message.uid_validity
                and email_message.uid_validity != uid_validity
            ):
                logger.warning(
                    "Mail sync: uid %s з нумерації %s (поточна %s) — не добираємо",
                    email_message.uid, email_message.uid_validity, uid_validity,
                )
                continue
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
                _apply_attachments(
                    session,
                    email_message,
                    full_messages[0],
                    attachments_dir,
                    known_materials,
                    material_alias_rows,
                    default_material,
                    download_all,
                )
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

        # --- Phase 3: звірка папки «оброблено» — ОСТАННЯ в блоці, бо перемикає
        # активну папку скриньки з Inbox (далі в блоці нічого не читає Inbox).
        # Листи, перенесені в папку прямо в пошті, стають перенесеними й у базі.
        _reflect_processed_folder(
            session, mailbox, get_setting(session, "mail_processed_folder") or "", cutoff
        )

    return created
