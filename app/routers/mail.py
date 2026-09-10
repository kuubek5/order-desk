"""Пошта: тріаж листів і фільтри.

Клієнти з усієї країни шлють файли листом на робочу скриньку. Тут лист
розбирають (матеріал, колір, кількість), скачують вкладення й за посиланням,
розпаковують архіви — і приймають у чергу або відсіюють правилом фільтра.

Два свідомі рішення живуть у цьому екрані:
- «не наша робота» (3D-друк, моделювання) прибирається НАВЧАЛЬНИМИ правилами
  MailFilterRule, а не бейджем — таке має зникати з фрезерної черги, а не
  стікеритись у ній;
- порядок списку тримає водяний знак `since`: полл показує лише листи до
  нього, новіші — банером «+N нових». Інакше фоновий синк вставляв би листи в
  середину й зсував рядки під курсором оператора.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_ as sa_and, func, select, update as sa_update
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from app.archive_extract import is_archive
from app.config import MAIL_ATTACHMENTS_PATH
from app.export_scanner import clear_export_cache
from app.link_attachments import (
    LinkAttachment,
    LinkDownloadError,
    download_link,
    extract_download_links,
    undownloaded_links,
)
from app.mail_export import (
    list_client_folders,
    preview_export_target,
    restore_attachments_to_spool,
    undo_moves,
)
from app.mail_filters import apply_rule_retroactively
from app.mail_parser import material_candidates
from app.mail_reader import (
    download_attachments_now,
    extract_archive_attachments,
    redownload_missing_attachments,
)
from app.mail_sync_service import (
    MailSyncBusyError,
    MailSyncError,
    run_sync_owned_session,
    zombie_fetch_blocks_files,
)
from app.mail_spool import spool_folder_name
from app.models import (
    Attachment,
    ClientSenderMemory,
    EmailMessage,
    MailFilterCategory,
    MailFilterRule,
    Order,
)
from app.order_folder import (
    attach_email_preview_tokens,
    resolve_email_attachment_folder,
)
from app.platform_windows import open_folder_in_explorer
from app.queue_filters import (
    SERVICE_TYPE_FILTERS,
    count_by_service_type,
    filter_emails_by_service_type,
)
from app.routers.section_gate import blocked_response
from app.routers.deps import (
    get_current_user,
    login_redirect,
    get_db,
    is_loopback_request,
    templates,
)
from app.sender_memory import list_sender_memories, lookup_sender
from app.services.mail_accept import accept_letter, resolve_wizard_overrides
from app.services.config_state import (
    mail_preview_roots,
    mail_trusted_roots,
)
from app.services.settings_nav import can_edit
from app.settings_store import (
    get_export_folder_path,
    get_imap_login,
    get_mail_download_all,
)
from app.sheet_erase_guard import SheetEraseBlocked
from app.sheet_writer import clear_order_row
from app.sheets import get_worksheet_by_name, open_spreadsheet

logger = logging.getLogger(__name__)

router = APIRouter()


# Emails that have left the triage queue: accepted into the queue or rejected.
# The archive view keeps them visible so a processed letter is never lost — the
# operator can look back at what came in and, for a mistaken reject, restore it.
_ARCHIVE_STATUSES = ("прийнято", "відхилено")


@router.get("/mail", response_class=HTMLResponse)
def get_mail(
    request: Request,
    db: Session = Depends(get_db),
    synced: int | None = None,
    error: str | None = None,
    service: str = "all",
    view: str = "pending",
    partial: str | None = None,
    open: int | None = None,
    since: int | None = None,
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    # Розділ може бути зачинений адміністратором (Налаштування → Доступ до
    # розділів): не-адмін бачить екран-блокатор, адмін — сам розділ.
    blocked = blocked_response(request, db, user, "mail")
    if blocked is not None:
        return blocked

    # Validate independently — an unknown/stale value degrades to "all"
    # (show everything) rather than erroring, same pattern as the queue
    # screen's source/ready filters.
    if service not in SERVICE_TYPE_FILTERS:
        service = "all"
    if view not in ("pending", "filtered", "archive", "auto"):
        view = "pending"
    # Pop the flash only on a full-page render — the 15s poll (partial="list")
    # would otherwise consume it before the real navigation shows it.
    toast_flash = request.session.pop("toast_flash", None) if partial != "list" else None

    # Three views: pending = "нове" NOT stamped by a filter rule; filtered =
    # "нове" stamped (kept, never deleted — one click brings a letter back);
    # archive = accepted/rejected.
    if view == "archive":
        status_clause = EmailMessage.status.in_(_ARCHIVE_STATUSES)
    elif view == "filtered":
        status_clause = sa_and(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_not(None)
        )
    else:
        status_clause = sa_and(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_(None)
        )
    # STABLE ORDER (pending view). The list polls every 15s; without this a
    # letter arriving mid-glance inserted itself and pushed every row down
    # under the operator's cursor — the same hazard the handout screen has a
    # written rule against (CLAUDE.md §2, rule 1). `since` is a high-water mark
    # of EmailMessage.id captured at full page render and echoed back by the
    # poll: the refreshed list shows only letters at or below it, so rows never
    # move. Anything newer is counted and offered as an explicit «+N нових»
    # banner, which the operator clicks when they are ready — that click is a
    # full navigation, which mints a new watermark.
    list_clause = status_clause
    if since is not None and view == "pending":
        list_clause = sa_and(status_clause, EmailMessage.id <= since)
    emails = db.scalars(
        select(EmailMessage)
        .where(list_clause)
        .options(selectinload(EmailMessage.attachments))
        .order_by(
            EmailMessage.received_at.desc().nullslast(),
            EmailMessage.created_at.desc()
        )
    ).all()
    # How many pending letters are being held back from the frozen list.
    held_back_count = 0
    if since is not None and view == "pending":
        held_back_count = db.scalar(
            select(func.count()).select_from(EmailMessage).where(
                status_clause, EmailMessage.id > since
            )
        ) or 0

    # Top-level view counts (pending vs filtered vs archive) for the tabs.
    pending_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_(None)
        )
    ) or 0
    filtered_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_not(None)
        )
    ) or 0
    archive_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status.in_(_ARCHIVE_STATUSES)
        )
    ) or 0
    sender_memories = list_sender_memories(db) if view == "auto" else []
    auto_count = db.scalar(
        select(func.count()).select_from(ClientSenderMemory).where(
            ClientSenderMemory.auto_accept.is_(True)
        )
    ) or 0

    # Pending letters no operator has opened yet — drives the animated
    # "unread by me" highlight and the accent count on the pending tab.
    unread_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове",
            EmailMessage.seen_at.is_(None),
            EmailMessage.filter_category.is_(None),
        )
    ) or 0

    # Watermark for the frozen list (see the `since` comment above). On a full
    # render it is the newest letter id in existence; the poll echoes it back
    # unchanged, so the visible set stays put until the operator asks for more.
    list_watermark = since if since is not None else (
        db.scalar(select(func.max(EmailMessage.id))) or 0
    )

    # Service-type chips only make sense for the pending triage list.
    service_counts = count_by_service_type(emails) if view == "pending" else None
    if view == "pending":
        emails = filter_emails_by_service_type(emails, service)
    attach_email_preview_tokens(emails, mail_trusted_roots(db), mail_preview_roots(db))

    # Filter rules — listed (and managed by the admin) on the filtered tab.
    filter_rules = (
        db.scalars(
            select(MailFilterRule).order_by(MailFilterRule.id.desc())
        ).all()
        if view == "filtered"
        else []
    )
    filter_categories = _mail_filter_categories(db)
    filter_category_rows = (
        db.scalars(
            select(MailFilterCategory).order_by(MailFilterCategory.id.asc())
        ).all()
        if view == "filtered"
        else []
    )

    # Learning nudge: a sender whose letters were rejected 2+ times and who has
    # no sender rule yet (enabled OR disabled — a disabled rule records "the
    # operator said no, don't ask again") gets a one-line suggestion banner on
    # the pending tab.
    filter_suggest = None
    if view == "pending":
        rejected_counts = db.execute(
            select(EmailMessage.from_address, func.count().label("cnt"))
            .where(
                EmailMessage.status == "відхилено",
                EmailMessage.from_address.is_not(None),
            )
            .group_by(EmailMessage.from_address)
            .having(func.count() >= 2)
            .order_by(func.count().desc())
        ).all()
        if rejected_counts:
            sender_patterns = {
                (r.pattern or "").strip().lower()
                for r in db.scalars(
                    select(MailFilterRule).where(MailFilterRule.kind == "sender")
                ).all()
            }
            for address, cnt in rejected_counts:
                if address.strip().lower() not in sender_patterns:
                    filter_suggest = {"address": address, "count": cnt}
                    break

    # The 15s triage poll asks for just the list wrapper (_mail_triage_list.html)
    # so new letters appear with the unread highlight without a full reload. The
    # fragment re-renders the same #mail-list-rows so its poll attrs persist.
    if partial == "list":
        return templates.TemplateResponse(
            request,
            "_mail_triage_list.html",
            {
                "emails": emails,
                "view": view,
                "service": service,
                "list_watermark": list_watermark,
                "held_back_count": held_back_count,
            },
        )

    # Pre-open a letter in the right-hand panel (used after a partial accept so
    # the operator lands back in the two-pane list with the letter already open,
    # not on the standalone card page). Only if it's in the list being shown.
    open_panel_html = None
    open_id = None
    if open is not None:
        open_email = next((e for e in emails if e.id == open), None)
        if open_email is not None:
            open_id = open
            open_panel_html = templates.env.get_template("_mail_detail_panel.html").render(
                _mail_panel_context(db, open_email, user)
            )

    return templates.TemplateResponse(
        request,
        "mail_triage.html",
        {
            "page_title": "Нові з пошти",
            "emails": emails,
            "open_panel_html": open_panel_html,
            "open_id": open_id,
            "toast_flash": toast_flash,
            "user": user,
            "synced": synced,
            "error": error,
            "service": service,
            "service_counts": service_counts,
            "view": view,
            "pending_count": pending_count,
            "filtered_count": filtered_count,
            "sender_memories": sender_memories,
            "auto_count": auto_count,
            "archive_count": archive_count,
            "unread_count": unread_count,
            "filter_rules": filter_rules,
            "filter_categories": filter_categories,
            "filter_category_rows": filter_category_rows,
            "filter_suggest": filter_suggest,
            # Адреса скриньки, яку моніторить система — показуємо в шапці, щоб
            # оператор бачив, звідки саме тягнуться листи (None → не налаштовано).
            "mailbox": get_imap_login(db),
            # «Скачувати всі вкладення» — той самий тоггл, що в налаштуваннях,
            # продубльований у шапці тріажу для швидкого доступу (адмін).
            "mail_download_all": get_mail_download_all(db),
            # Frozen-list state (see the `since` comment above).
            "list_watermark": list_watermark,
            "held_back_count": held_back_count,
        },
    )


@router.post("/mail/sync")
def sync_mail(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    # Own session, not the request's: on a watchdog timeout the hung fetch
    # thread still owns whatever session it was given (see
    # mail_sync_service._fetch_with_deadline), and get_db would otherwise
    # close the request session out from under that zombie. _run_sync_owned
    # closes the session itself only when the run actually finished.
    try:
        count = run_sync_owned_session(trigger="manual")
    except (MailSyncBusyError, MailSyncError) as exc:
        return RedirectResponse(f"/mail?error={quote(str(exc))}", status_code=303)

    return RedirectResponse(f"/mail?synced={count}", status_code=303)


@router.get("/mail/{email_id}", response_class=HTMLResponse)
def get_mail_detail(
    request: Request,
    email_id: int,
    error: str | None = None,
    panel: int = 0,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    # Opening the triage card clears the "unread by me" highlight for everyone
    # (shared seen state). Stamp once — later opens keep the original time.
    if email.seen_at is None:
        email.seen_at = datetime.now()
        db.commit()

    context = _mail_panel_context(db, email, user, error=error)

    # HTMX click from the triage list swaps just the detail into the right
    # column (panel=1, or any HX-Request); a plain navigation still gets the
    # standalone page — the shared _mail_detail_panel.html renders both.
    request_headers = getattr(request, "headers", None) or {}
    if panel or request_headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "_mail_detail_panel.html", context)

    return templates.TemplateResponse(request, "mail_detail.html", context)


def _email_partial_state(
    db: Session, email: EmailMessage, on_disk: set[int] | None = None
) -> dict:
    """Multi-colour partial-accept state for a letter: files not yet accepted
    (still in the spool) and how many order batches were already taken from it.
    Drives the wizard's file picker and the «частково прийнято» badge.

    ``on_disk`` — id вкладень, які вже перевірив ВИКЛИКАЧ. Один рендер панелі
    робив кілька окремих проходів по диску над тими самими файлами, а по
    мережевій шарі кожен `exists()` — це round-trip (ревʼю 07.09.26, M.7).
    """
    if on_disk is None:
        on_disk = {a.id for a in email.attachments if Path(a.saved_path).exists()}
    unclaimed = [
        a for a in email.attachments
        if a.order_id is None and a.id in on_disk
    ]
    accepted_batches = db.scalar(
        select(func.count()).select_from(Order).where(Order.source_email_id == email.id)
    ) or 0
    return {
        "unclaimed_attachments": unclaimed,
        "unclaimed_count": len(unclaimed),
        "accepted_batches": accepted_batches,
        "is_partial": accepted_batches > 0 and bool(unclaimed),
    }


def _mail_panel_context(db: Session, email: EmailMessage, user, **extra) -> dict:
    """Shared render context for the triage detail panel — wizard step 1 seed,
    material candidates and the whitelisted download links detected in the body.
    Reused by get_mail_detail (the fetch-link route renders just one row)."""
    attach_email_preview_tokens([email], mail_trusted_roots(db), mail_preview_roots(db))
    # ОДИН прохід по диску на весь рендер панелі: далі і «зниклі файли», і
    # стан часткового прийняття рахуються з цього набору.
    on_disk = {a.id for a in email.attachments if Path(a.saved_path).exists()}
    seed = (email.material_color_guess or "") or (email.subject or "")
    # Recurring client? Sender memory beats every guess for the name prefill.
    sender_hint = lookup_sender(db, email)
    context = {
        "email": email,
        "user": user,
        "error": None,
        "wizard_step": 1,
        "client_name": sender_hint.client_name if sender_hint else "",
        "sender_hint": sender_hint,
        "material_color": "",
        "kind": "",
        "quantity": "",
        "folder_pick": "",
        "folder_new": "",
        "material_folder": "",
        "material_cands": material_candidates(seed, _lab_material_colors(db)),
        "body_links": extract_download_links(email.body_text),
        # Один розрахунок на бейдж списку, панель і гейт прийняття —
        # link_attachments.undownloaded_links (аудит 05.09.26, UX 1.4).
        "undownloaded_links": undownloaded_links(email),
        "handled_link_refs": set(json.loads(email.handled_link_refs)) if email.handled_link_refs else set(),
        # Any ZIP/RAR still sitting among the attachments (auto-unpack failed or
        # is off) → offer the manual «Розпакувати» reserve button.
        "has_archive": any(is_archive(a.filename) for a in email.attachments),
        # ПРАВДА ПРО ДИСК, а не про базу. Панель рахувала рядки Attachment і
        # писала «Усі файли на диску: 4» навіть тоді, коли теку зі спула хтось
        # видалив: «Відкрити папку» падало, STL не малювався, і зробити з цим
        # не можна було нічого. Тепер зниклі файли названі прямо, і для них є
        # кнопка повторного скачування.
        "missing_attachment_ids": {
            a.id for a in email.attachments if a.id not in on_disk
        },
        "link_flash": None,
        # Admin-editable category names for the card's «У фільтр» select.
        "filter_categories": _mail_filter_categories(db),
        **_email_partial_state(db, email, on_disk),
    }
    context.update(extra)
    return context


def _attachment_counts(db: Session | None, email: EmailMessage) -> tuple[int, int]:
    """(рядків у базі, файлів реально на диску) — обидва числа СВІЖІ.

    Читаємо запитом, а не з email.attachments: сесія створена з
    expire_on_commit=False, тож після коміту колекція лишається старою (це вже
    коштувало розбіжності 4 проти 3 на одному екрані).

    Два числа, а не одне, бо рядок списку показує саме «N з M»: якщо слати
    лише кількість рядків, клієнт перепише «3 з 5 файл.» на «5 файл.» і
    поверне ту саму брехню «файл є», від якої рядок і лікували.
    """
    if db is None:
        rows = list(email.attachments)
    else:
        rows = list(
            db.scalars(
                select(Attachment).where(Attachment.email_message_id == email.id)
            )
        )
    on_disk = sum(
        1 for a in rows if a.order_id is not None or Path(a.saved_path).exists()
    )
    return len(rows), on_disk


def _files_changed_response(
    response, email: EmailMessage, extra: dict | None = None, db: Session | None = None
):
    """Сказати екрану, що склад файлів листа змінився.

    Рядок у списку тріажу несе `hx-preserve`, тому 15-секундний полл його НЕ
    перемальовує — і лічильник «N файл.» лишався таким, яким був на момент
    завантаження сторінки. Після розпакування архіву чи повторного скачування
    список показував старе число, а панель — нове. Це не косметика: оператор
    читає список і думає, що файл є, хоча його немає — саме так робота й
    губиться. Тому число їде окремим тригером, а клієнт вписує його в рядок.
    """
    triggers = dict(extra or {})
    _rows_count, _on_disk_count = _attachment_counts(db, email)
    triggers["mailFilesChanged"] = {
        "id": email.id,
        # Рахуємо ЗАПИТОМ, а не по email.attachments: сесія створена з
        # expire_on_commit=False, тож після коміту (розпакування архіву міняє
        # склад вкладень) колекція лишається старою. Список писав 4 файли там,
        # де панель уже показувала 3 — два числа про одне на одному екрані.
        # Спіймано живим прогоном повного циклу, не тестом.
        "count": _rows_count,
        "on_disk": _on_disk_count,
    }
    # ensure_ascii ОБОВ'ЯЗКОВО за замовчуванням (True): значення HTTP-заголовка
    # мусить бути latin-1, а тости тут українською — з ensure_ascii=False роут
    # падає в 500 на кодуванні відповіді.
    response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


@router.post("/mail/{email_id}/fetch-link", response_class=HTMLResponse)
def fetch_email_link(
    request: Request,
    email_id: int,
    ref: str = Form(...),
    db: Session = Depends(get_db),
):
    """Download ONE whitelisted share link (identified by its Drive file id or
    ukr.net URL) into the email's mail-spool folder as an attachment, and return
    just that link's row with its new status (done / skip / error). Per-link so
    the operator sees each file's progress separately. Only whitelisted hosts are
    ever fetched — see app/link_attachments.py."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Покинутий фетч ще качає в теку цього листа своєю сесією. Чіпати ті самі
    # файли зараз означає подвійні вкладення «(2)», а для приймання — рядки з
    # мертвими шляхами (аудит 08.09.26). Фоновий синк цей гейт мав, ручні
    # кнопки — ні.
    busy = zombie_fetch_blocks_files()
    if busy:
        raise HTTPException(status_code=409, detail=busy)


    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    link = next(
        (dl for dl in extract_download_links(email.body_text) if (dl.file_id or dl.url) == ref),
        None,
    )
    if link is None:
        return templates.TemplateResponse(
            request,
            "_mail_link_row.html",
            {"email": email, "link": LinkAttachment(kind="?", url=ref, display=ref),
             "link_status": "error", "link_message": "посилання не знайдено в листі"},
        )

    existing = frozenset(a.filename for a in email.attachments)
    status = message = result_name = None
    try:
        path = download_link(link, Path(MAIL_ATTACHMENTS_PATH) / email.uid, existing_names=existing)
    except LinkDownloadError as exc:
        status, message = "error", str(exc)
    except Exception:  # noqa: BLE001 — one bad link mustn't 500 the panel
        logger.exception("Link download failed for email %s: %s", email.id, link.url)
        host = (urlsplit(link.url).hostname or "сервер файлів")
        status, message = "error", f"немає з'єднання з {host} (інтернет / проксі?)"
    else:
        if path is None:
            status = "skip"
        else:
            attachment = Attachment(
                email_message_id=email.id,
                filename=path.name,
                saved_path=str(path),
                size_bytes=path.stat().st_size,
            )
            db.add(attachment)
            email.attachments_status = "ready"
            status, result_name = "done", path.name
    if status in ("done", "skip"):
        # Remember this link as handled so the «ще N за посиланням» count drops.
        handled = set(json.loads(email.handled_link_refs) if email.handled_link_refs else [])
        handled.add(ref)
        email.handled_link_refs = json.dumps(sorted(handled))
    try:
        db.commit()
    except Exception as commit_error:  # noqa: BLE001 — файл уже на диску
        # Файл скачано ДО коміту. Невдалий коміт відкочує рядок `Attachment`,
        # і в спулі лишається файл, якого в базі немає: у тріажі його не
        # видно, але «Відкрити папку» показує зайву коронку поруч зі
        # справжніми (ревʼю 07.09.26, LOW).
        db.rollback()
        logger.exception("Link download commit failed for email %s", email.id)
        if status == "done" and path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.error("Orphan link file left in spool: %s", path)
        status, result_name = "error", None
        message = f"не вдалося зберегти запис про файл: {commit_error}"

    # Auto-unpack a freshly downloaded archive (client packed the STL in a
    # .zip/.rar). Best-effort; the extracted files show on the next panel load,
    # and a toast tells the operator it happened.
    toast = None
    if status == "done" and result_name and is_archive(result_name):
        db.refresh(email)
        try:
            extracted, extract_errors = extract_archive_attachments(db, email)
            if extracted or extract_errors:
                db.commit()
            if extracted:
                toast = {"message": f"Розпаковано {extracted} файл(ів) з архіву — оновіть картку", "kind": "success"}
            elif extract_errors:
                toast = {"message": "Архів: " + extract_errors[0], "kind": "error"}
        except Exception:  # noqa: BLE001 — extraction must not 500 the panel
            logger.exception("Archive extract failed for email %s", email.id)
            db.rollback()

    response = templates.TemplateResponse(
        request,
        "_mail_link_row.html",
        {"email": email, "link": link, "link_status": status,
         "link_message": message, "result_name": result_name},
    )
    # A downloaded file changes the attachment list AND the STL gallery, which
    # this row-only swap can't refresh — signal the panel to re-render (app.js
    # debounces so "download all" refreshes once).
    if status in ("done", "skip"):
        db.refresh(email)
        return _files_changed_response(
            response, email, {"toast": toast} if toast is not None else None, db=db
        )
    if toast is not None:
        response.headers["HX-Trigger"] = json.dumps({"toast": toast})
    return response


@router.post("/mail/{email_id}/extract-archives", response_class=HTMLResponse)
def extract_mail_archives(request: Request, email_id: int, db: Session = Depends(get_db)):
    """Manual reserve for the auto-unpack: extract every ZIP/RAR attachment of
    the letter now, and re-render the triage detail so the STL files appear."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    try:
        extracted, extract_errors = extract_archive_attachments(db, email)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Manual archive extract failed for email %s", email.id)
        db.rollback()
        extracted, extract_errors = 0, ["не вдалося розпакувати"]

    db.refresh(email)
    context = _mail_panel_context(db, email, user)
    response = templates.TemplateResponse(request, "_mail_detail_panel.html", context)
    if extracted:
        toast = {"message": f"Розпаковано {extracted} файл(ів) з архіву", "kind": "success"}
    elif extract_errors:
        toast = {"message": "Архів: " + extract_errors[0], "kind": "error"}
    else:
        toast = {"message": "Архівів для розпакування немає", "kind": "info"}
    # Розпакування міняє склад файлів найпомітніше: архів зникає, на його місці
    # з'являються кілька STL. Рядок у списку мусить дізнатись нове число.
    return _files_changed_response(response, email, {"toast": toast}, db=db)


def _lab_material_colors(db: Session) -> list[str]:
    """Distinct free-text material/colour strings the lab actually used in the
    sheet (source=="lab") — the reference list the accept wizard matches a
    client's mangled spelling against."""
    return sorted(
        {
            m
            for (m,) in db.execute(
                select(Order.material_color).where(
                    Order.source == "lab", Order.material_color.is_not(None)
                )
            ).all()
            if m and m.strip()
        }
    )


# Правило «яка тека перемагає» живе поруч із самим переносом файлів
# (app/services/mail_accept.py). Тут лишається імʼя, бо на нього спираються
# візард і тести — але це той САМИЙ код, не друга копія.
_resolve_wizard_overrides = resolve_wizard_overrides


@router.post("/mail/{email_id}/wizard", response_class=HTMLResponse)
def mail_wizard(
    request: Request,
    email_id: int,
    step: int = Form(1),
    client_name: str = Form(""),
    material_color: str = Form(""),
    kind: str = Form(""),
    quantity: str = Form(""),
    folder_pick: str = Form(""),
    folder_new: str = Form(""),
    material_folder: str = Form(""),
    attachment_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Render one step of the semi-automatic accept wizard (client+material →
    directory → confirm). Each Next/Back re-renders the shared _mail_wizard.html
    fragment with the accumulated values carried in hidden inputs; nothing is
    written until the final step POSTs to /mail/{id}/accept."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    ctx = _wizard_context(
        db, user, email, step,
        client_name=client_name, material_color=material_color, kind=kind,
        quantity=quantity, folder_pick=folder_pick, folder_new=folder_new,
        material_folder=material_folder, attachment_ids=attachment_ids,
    )
    return templates.TemplateResponse(request, "_mail_wizard.html", ctx)


def _wizard_context(
    db: Session, user, email: EmailMessage, step: int, *,
    client_name: str = "", material_color: str = "", kind: str = "",
    quantity: str = "", folder_pick: str = "", folder_new: str = "",
    material_folder: str = "", attachment_ids: list[int] | None = None,
    error: str = "",
) -> dict:
    """Контекст одного кроку візарда — окремо від роуту, бо його рендерить не
    лише сам візард: прийняття, що не пройшло, повертає ТОЙ САМИЙ крок 3 з
    поясненням, замість того щоб редіректом викинути оператора зі сторінки й
    стерти все введене (аудит 05.09.26, UX 1.2)."""
    attachment_ids = list(attachment_ids or [])
    step = max(1, min(3, step))
    known = _lab_material_colors(db)
    # Candidates from the operator's current material text, or the recognised
    # guess / subject on the very first render.
    seed = material_color.strip() or (email.material_color_guess or "") or (email.subject or "")
    candidates = material_candidates(seed, known)

    sender_hint = lookup_sender(db, email)
    # Step 1 opens with the remembered name when the operator hasn't typed one;
    # step 2 pre-selects the remembered folder (only if it still exists) when
    # no explicit pick/new-folder override was given.
    if step == 1 and not client_name.strip() and sender_hint:
        client_name = sender_hint.client_name
    if (
        step >= 2 and sender_hint and sender_hint.export_folder
        and not folder_pick.strip() and not folder_new.strip()
    ):
        export_root_probe = Path(get_export_folder_path(db))
        if sender_hint.export_folder in list_client_folders(export_root_probe):
            folder_pick = sender_hint.export_folder

    client_override, material_override = _resolve_wizard_overrides(
        folder_pick, folder_new, material_folder
    )

    ctx = {
        "email": email,
        "user": user,
        "wizard_step": step,
        "sender_hint": sender_hint,
        "client_name": client_name,
        "material_color": material_color,
        "kind": kind,
        "quantity": quantity,
        "folder_pick": folder_pick,
        "folder_new": folder_new,
        "material_folder": material_folder,
        "material_cands": candidates,
        "attachment_ids": attachment_ids,
        # Крок 3 мусить бачити те саме, що й картка листа: без цього ключа
        # попередження про нескачані файли за посиланням фізично не могло
        # відрендеритись, бо візард будується не з _mail_panel_context.
        "undownloaded_links": undownloaded_links(email),
        # Порожній рядок — банера немає; шаблон малює його лише коли є текст.
        "error": error,
        **_email_partial_state(db, email),
    }
    # Files that will move in THIS batch: the operator's selection, or all
    # unclaimed when nothing is ticked (single-colour default).
    selected_ids = set(attachment_ids)
    _batch = [a for a in ctx["unclaimed_attachments"] if a.id in selected_ids] if selected_ids else ctx["unclaimed_attachments"]
    ctx["batch_count"] = len(_batch)
    if step >= 2:
        export_root = Path(get_export_folder_path(db))
        ctx["preview"] = preview_export_target(
            export_root, client_name, material_color, client_override, material_override
        )
        ctx["existing_folders"] = list_client_folders(export_root)
        ctx["attachment_count"] = ctx["batch_count"]

    return ctx


@router.post("/mail/{email_id}/open-folder", status_code=204)
def open_mail_folder(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    email = db.scalar(
        select(EmailMessage)
        .where(EmailMessage.id == email_id)
        .options(selectinload(EmailMessage.attachments))
    )
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    folder = resolve_email_attachment_folder(
        email.attachments,
        mail_trusted_roots(db),
    )
    if folder is None:
        raise HTTPException(status_code=404, detail="папку вкладень не знайдено")

    try:
        open_folder_in_explorer(folder)
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="відкриття папки підтримується лише у Windows")
    except OSError:
        logger.exception("Could not open attachment folder for email %s", email_id)
        raise HTTPException(status_code=500, detail="не вдалося відкрити папку")
    return Response(status_code=204)


@router.post("/mail/{email_id}/download-attachments", response_class=HTMLResponse)
def download_email_attachments(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Pull a non-whitelisted letter's files on demand ("skipped" → "ready").
    Re-renders the detail panel so the STL/preview and accept wizard appear."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Покинутий фетч ще качає в теку цього листа своєю сесією. Чіпати ті самі
    # файли зараз означає подвійні вкладення «(2)», а для приймання — рядки з
    # мертвими шляхами (аудит 08.09.26). Фоновий синк цей гейт мав, ручні
    # кнопки — ні.
    busy = zombie_fetch_blocks_files()
    if busy:
        raise HTTPException(status_code=409, detail=busy)

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    try:
        download_attachments_now(db, email, Path(MAIL_ATTACHMENTS_PATH))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — surface a friendly error, don't 500
        db.rollback()
        logger.exception("Manual attachment download failed for email %s", email.id)
        context = _mail_panel_context(db, email, user, error=f"Не вдалося скачати файли: {exc}")
        return templates.TemplateResponse(request, "_mail_detail_panel.html", context)
    context = _mail_panel_context(db, email, user)
    return _files_changed_response(
        templates.TemplateResponse(request, "_mail_detail_panel.html", context), email, db=db
    )


@router.post("/mail/{email_id}/redownload", response_class=HTMLResponse)
def redownload_email_attachments(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Скачати наново вкладення, файли яких зникли з диска.

    Раніше цей стан був глухим кутом: рядки в базі є, файлів немає, «Відкрити
    папку» падає, STL не малюється — і жодної дії, крім як відхилити лист.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Покинутий фетч ще качає в теку цього листа своєю сесією. Чіпати ті самі
    # файли зараз означає подвійні вкладення «(2)», а для приймання — рядки з
    # мертвими шляхами (аудит 08.09.26). Фоновий синк цей гейт мав, ручні
    # кнопки — ні.
    busy = zombie_fetch_blocks_files()
    if busy:
        raise HTTPException(status_code=409, detail=busy)

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    try:
        removed, saved = redownload_missing_attachments(
            db, email, Path(MAIL_ATTACHMENTS_PATH)
        )
        db.commit()
        # БЕЗУМОВНО, одразу після коміту. Видалення йде через session.delete(),
        # а нові вкладення додаються через session.add() — тобто в
        # email.attachments видалені лишаються, а нові не з'являються. Сесія
        # створена з expire_on_commit=False, тож колекція не оновиться сама.
        # Наслідків два, обидва тихі: перевірка «чи є архів» нижче дивилась би
        # на СТАРІ імена (лист, що приїхав ZIP-ом, не розпакувався б, і
        # оператор отримав би архів замість STL), а панель відрендерилась би з
        # id уже видалених рядків — прев'ю й посилання давали б 404.
        db.refresh(email)
    except Exception as exc:  # noqa: BLE001 — показати причину, а не 500
        db.rollback()
        logger.exception("Повторне скачування не вдалось для листа %s", email.id)
        context = _mail_panel_context(
            db, email, user, error=f"Не вдалося скачати файли наново: {exc}"
        )
        return templates.TemplateResponse(request, "_mail_detail_panel.html", context)

    # Файли клієнта часто приїздять у ZIP/RAR, і на диску живуть уже
    # РОЗПАКОВАНІ. Повторне скачування тягне з пошти сам архів — без цього
    # кроку кнопка повертала б архів замість робочих STL, і оператору
    # довелось би окремо шукати «Розпакувати». Те саме вже робить скачування
    # за посиланням, тож поведінка збігається.
    if saved and any(is_archive(a.filename) for a in email.attachments):
        try:
            extracted, extract_errors = extract_archive_attachments(db, email)
            if extracted or extract_errors:
                db.commit()
                # Перечитати ОБОВ'ЯЗКОВО: розпакування міняє склад вкладень
                # (архів зникає, з нього з'являються файли), і без цього число
                # у тригері рахувалось по застарілій колекції — список писав
                # 5 файлів там, де панель уже показувала 4. Спіймано живим
                # прогоном повного циклу, не тестом.
                db.refresh(email)
        except Exception:  # noqa: BLE001 — розпакування не має ламати панель
            logger.exception("Розпакування після повторного скачування, лист %s", email.id)
            db.rollback()

    if removed and not saved:
        # Лист на сервері вже без вкладень (їх видалили і там) — сказати прямо,
        # інакше порожня панель виглядає як зламана кнопка.
        note = "Файлів більше немає й на сервері пошти — скачувати нічого."
        context = _mail_panel_context(db, email, user, error=note)
    else:
        context = _mail_panel_context(db, email, user)
    return _files_changed_response(
        templates.TemplateResponse(request, "_mail_detail_panel.html", context), email, db=db
    )


@router.post("/mail/senders/add")
def add_sender_auto(
    request: Request,
    email_address: str = Form(...),
    db: Session = Depends(get_db),
):
    """Manually add an email to the trusted auto-download list without waiting
    for a first acceptance. Creates a sender-memory row (client name = the
    address until the first real accept fills it in) with auto on. Idempotent —
    an existing key is just switched on.

    Гейт той самий, що в решти роутів фільтрів: додати довіреного відправника
    означає ввімкнути автоматичне скачування вкладень з цієї адреси, тобто
    писати чужі файли на диск лабораторії (ревʼю 07.09.26, M.7)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")
    key = (email_address or "").strip().lower()
    if key:
        row = db.scalar(select(ClientSenderMemory).where(ClientSenderMemory.sender_key == key))
        if row is None:
            db.add(ClientSenderMemory(
                sender_key=key, client_name=email_address.strip(),
                export_folder=None, orders_count=0, auto_accept=True,
                last_seen_at=datetime.now(),
            ))
        else:
            row.auto_accept = True
        db.commit()
    return RedirectResponse("/mail?view=auto", status_code=303)


@router.post("/mail/senders/{memory_id}/auto")
def toggle_sender_auto(
    request: Request,
    memory_id: int,
    db: Session = Depends(get_db),
):
    """Flip a sender's trusted auto-download flag. Trusting a sender means
    their future letters have attachments downloaded automatically; existing
    letters already in triage are untouched. Same gate as the other filter
    routes — це рішення про запис чужих файлів на диск."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")
    row = db.get(ClientSenderMemory, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="sender not found")
    row.auto_accept = not row.auto_accept
    db.commit()
    return RedirectResponse("/mail?view=auto", status_code=303)


@router.post("/mail/{email_id}/accept", response_class=HTMLResponse)
def accept_email(
    request: Request,
    email_id: int,
    client_name: str = Form(...),
    material_color: str = Form(""),
    kind: str = Form(""),
    quantity: str = Form(""),
    folder_pick: str = Form(""),
    folder_new: str = Form(""),
    material_folder: str = Form(""),
    attachment_ids: list[int] = Form(default=[]),
    accept_anyway: str = Form(""),
    db: Session = Depends(get_db),
):
    """Прийняти лист (або одну кольорову партію) у чергу.

    Тут лишився ЛИШЕ HTTP: форма, фрагмент візарда з помилкою, тост і куди
    вести далі. Уся доменна робота — створення роботи, перенос файлів у
    export з компенсацією, рядок-нотатка в таблиці — у
    `app.services.mail_accept.accept_letter` (аудит 05.09.26, крок 2.8).
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Покинутий фетч ще качає в теку цього листа своєю сесією. Чіпати ті самі
    # файли зараз означає подвійні вкладення «(2)», а для приймання — рядки з
    # мертвими шляхами (аудит 08.09.26). Фоновий синк цей гейт мав, ручні
    # кнопки — ні.
    busy = zombie_fetch_blocks_files()
    if busy:
        raise HTTPException(status_code=409, detail=busy)


    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    if email.status != "нове":
        raise HTTPException(status_code=409, detail="лист уже оброблено")

    def _accept_failed(message: str):
        """Помилка прийняття — НАЗАД у крок 3, а не редіректом зі сторінки.

        Візард живе у фрагменті `#mail-wizard`, і редірект на
        `/mail/{id}?error=…` перезавантажував увесь екран: усе, що оператор
        заповнив у трьох кроках, зникало, а лист доводилось відкривати заново
        (аудит 05.09.26, UX 1.2). Тепер повертаємо той самий крок із тими ж
        значеннями і поясненням угорі. Не-HTMX виклик (форма без JS) лишається
        на старому редіректі — там фрагмент нікуди вставити.
        """
        if not _is_htmx(request):
            return RedirectResponse(
                f"/mail/{email.id}?error={quote(message)}", status_code=303
            )
        return templates.TemplateResponse(
            request, "_mail_wizard.html",
            _wizard_context(
                db, user, email, 3,
                client_name=client_name, material_color=material_color, kind=kind,
                quantity=quantity, folder_pick=folder_pick, folder_new=folder_new,
                material_folder=material_folder, attachment_ids=attachment_ids,
                error=message,
            ),
        )

    result = accept_letter(
        db, user, email,
        client_name=client_name, material_color=material_color, kind=kind,
        quantity=quantity, folder_pick=folder_pick, folder_new=folder_new,
        material_folder=material_folder, attachment_ids=attachment_ids,
        accept_anyway=bool(accept_anyway),
    )
    if not result.ok:
        return _accept_failed(result.error)

    # Чесний тост: скільки файлів збережено саме цією партією і скільки ще
    # чекає в листі, якщо кольорів кілька.
    if result.partial:
        message = (
            f"Прийнято партію «{result.material_label}»: збережено "
            f"{result.saved_files} файл(ів). Лишилось {result.remaining_files} "
            "файл(ів) у листі — прийміть наступний колір."
        )
        toast_kind = "success"
    elif result.saved_files:
        message = (
            f"Роботу «{result.material_label}» прийнято в чергу: "
            f"збережено {result.saved_files} файл(ів)."
        )
        toast_kind = "success"
    else:
        message = (
            f"Роботу «{result.material_label}» прийнято в чергу без файлів "
            "(файлів не знайдено)."
        )
        toast_kind = "warning"
    request.session["toast_flash"] = {"kind": toast_kind, "message": message}

    # Куди вести: ЛИШАЄМОСЬ У ТРІАЖІ в обох випадках. Є ще файли → назад у цей
    # лист, приймати наступний колір; готово → у список, щоб оператор не
    # втратив місце і наступний лист був за один клік. Візард шле форму через
    # HTMX, тож 303 підмінив би HTML сторінки в панель — потрібен HX-Redirect.
    target = f"/mail?open={email.id}" if result.partial else "/mail"
    request_headers = getattr(request, "headers", None) or {}
    if request_headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@router.post("/mail/{email_id}/reject")
def reject_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    # «Відхилено» описує лист, якого НЕ брали в роботу: файли лежать у спулі,
    # робіт немає. Для вже прийнятого листа сам по собі новий статус нічого не
    # прибирає — робота лишається в черзі, файли в export, рядок-нотатка в
    # таблиці, — і база починає казати «відхилено» про роботу, яку цех у цей
    # час фрезерує. Відкат прийняття вміє рівно одна дія — «Повернути в
    # тріаж» (`/mail/{id}/restore`): вона повертає файли, стирає рядок і
    # видаляє роботу. Тому тут не «зробимо як зможемо», а відмова з підказкою
    # (ревʼю 07.09.26, C.2).
    has_orders = bool(
        db.scalar(select(func.count()).select_from(Order).where(Order.source_email_id == email.id))
        or email.order_id
    )
    if email.status == "прийнято" or has_orders:
        message = (
            "Лист уже прийнято в роботу. Спершу «Повернути в тріаж» — "
            "воно поверне файли й прибере роботу, — і аж тоді відхиляйте."
        )
        request_headers = getattr(request, "headers", None) or {}
        if request_headers.get("HX-Request") == "true":
            return HTMLResponse(message, status_code=409)
        request.session["toast_flash"] = {"kind": "error", "message": message}
        return RedirectResponse(f"/mail?open={email.id}", status_code=303)

    email.status = "відхилено"
    db.commit()

    # Two callers: the triage LIST row (HTMX, hx-swap="delete" — wants just that
    # one row gone) and the detail card's plain form (full navigation). For the
    # HTMX case return an empty 200 so htmx deletes only the target row; a 303
    # to the full /mail page would be followed and its whole-page body fed to the
    # delete swap, which (with the polled #mail-list-rows wrapper + hx-preserve)
    # wiped the entire list. 204 is unusable here — htmx skips the swap on 204.
    request_headers = getattr(request, "headers", None) or {}
    if request_headers.get("HX-Request") == "true":
        return HTMLResponse("", status_code=200)
    return RedirectResponse("/mail", status_code=303)


@router.post("/mail/{email_id}/unfilter")
def unfilter_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Bring a rule-filtered letter back to the main triage list. Clearing the
    stamp is the whole undo — and apply_filters_to_email never re-stamps an
    already-processed letter, so the operator's decision sticks."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    email.filter_category = None
    email.filter_rule_id = None
    db.commit()

    # HX = the «↩» on a filtered-list row (delete just that row); plain POST =
    # the card's «Повернути з фільтра» → land on the pending list, where the
    # returned letter now lives.
    request_headers = getattr(request, "headers", None) or {}
    if request_headers.get("HX-Request") == "true":
        return HTMLResponse("", status_code=200)
    return RedirectResponse("/mail", status_code=303)


_DEFAULT_FILTER_CATEGORIES = ["3D-друк", "бухгалтерія", "спам", "інше"]


def _mail_filter_categories(db: Session) -> list[str]:
    """Admin-editable category names (settings screen), falling back to the
    four defaults if the table is somehow empty — the selects must never render
    without options."""
    names = db.scalars(
        select(MailFilterCategory.name).order_by(MailFilterCategory.id.asc())
    ).all()
    return list(names) or list(_DEFAULT_FILTER_CATEGORIES)


def _filters_panel_response(
    request: Request, db: Session, return_to: str, *, error: str = ""
):
    """Панель фільтрів як фрагмент — відповідь на будь-яку дію з нею по HTMX.

    До аудиту 05.09.26 (UX 1.3) кожна кнопка тут була звичайною формою з
    редіректом: створення правила з тріажу перекидало на іншу вкладку, і лист,
    заради якого правило й створювали, доводилось шукати заново. Тепер
    оновлюється сама панель, а екран лишається на місці.
    """
    return templates.TemplateResponse(
        request,
        "_mail_filter_panel.html",
        {
            "filter_rules": db.scalars(
                select(MailFilterRule).order_by(MailFilterRule.id.desc())
            ).all(),
            "filter_categories": _mail_filter_categories(db),
            "filter_category_rows": db.scalars(
                select(MailFilterCategory).order_by(MailFilterCategory.id.asc())
            ).all(),
            "return_to": return_to,
            "filter_panel_error": error,
        },
    )


def _is_htmx(request: Request) -> bool:
    return (getattr(request, "headers", {}) or {}).get("HX-Request") == "true"


def _filters_return_url(return_to: str) -> str:
    """Where a filter-rule/category action lands: the settings section when the
    form lives there, the filtered tab otherwise."""
    return "/settings#mail-filters" if return_to == "settings" else "/mail?view=filtered"


@router.post("/mail/{email_id}/filter")
def filter_email_manually(
    request: Request,
    email_id: int,
    category: str = Form("інше"),
    db: Session = Depends(get_db),
):
    """Manually move ONE letter to the «Відфільтровані» tab — no rule is
    created, nothing else is affected. The stamp has no rule FK, so the letter
    reads "filtered by hand"; «↩» brings it back like any other. Той самий
    гейт, що й у решти роутів фільтрів."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    email.filter_category = (category or "").strip() or "інше"
    email.filter_rule_id = None
    db.commit()
    return RedirectResponse("/mail", status_code=303)


@router.post("/mail/filters")
def create_mail_filter(
    request: Request,
    kind: str = Form(...),
    pattern: str = Form(...),
    category: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a triage filter rule (admin) and apply it retroactively to the
    letters currently in the pending list — the reason the admin is creating it
    is usually a letter they're looking at right now."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    kind = kind.strip()
    pattern = pattern.strip()
    category = category.strip()
    if kind not in ("keyword", "sender") or not pattern or not category:
        message = "Правило: вкажіть тип, шаблон і категорію"
        if _is_htmx(request):
            return _filters_panel_response(request, db, return_to, error=message)
        return RedirectResponse(
            f"/mail?view=filtered&error={quote(message)}", status_code=303,
        )

    rule = MailFilterRule(
        kind=kind, pattern=pattern, category=category,
        created_by=user.username,
    )
    db.add(rule)
    db.flush()
    moved = apply_rule_retroactively(db, rule)
    db.commit()

    # «Навчити фільтр» тиснуть, стоячи в самому листі. Відповідь — підтвердження
    # на місці блоку, а не редірект на іншу вкладку (аудит 05.09.26, UX 1.3).
    if return_to == "letter" and _is_htmx(request):
        return templates.TemplateResponse(
            request, "_mail_filter_taught.html",
            {
                "user": user,
                "taught_pattern": pattern,
                "taught_category": category,
                "taught_hits": moved if isinstance(moved, int) else None,
            },
        )

    if _is_htmx(request):
        return _filters_panel_response(request, db, return_to)
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@router.post("/mail/filters/{rule_id}/edit")
def edit_mail_filter(
    request: Request,
    rule_id: int,
    kind: str = Form(...),
    pattern: str = Form(...),
    category: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Edit a rule in place (admin) — no more delete-and-recreate. Letters the
    OLD version already stamped keep their stamp (history); the edited rule is
    re-applied retroactively so a broadened pattern catches pending letters
    right away."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    rule = db.get(MailFilterRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")

    kind = kind.strip()
    pattern = pattern.strip()
    category = category.strip()
    if kind not in ("keyword", "sender") or not pattern or not category:
        message = "Правило: вкажіть тип, шаблон і категорію"
        if _is_htmx(request):
            return _filters_panel_response(request, db, return_to, error=message)
        return RedirectResponse(
            f"{_filters_return_url(return_to)}&error={quote(message)}"
            if return_to != "settings"
            else _filters_return_url(return_to),
            status_code=303,
        )

    rule.kind = kind
    rule.pattern = pattern
    rule.category = category
    apply_rule_retroactively(db, rule)
    db.commit()
    if _is_htmx(request):
        return _filters_panel_response(request, db, return_to)
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@router.post("/mail/filter-categories")
def create_filter_category(
    request: Request,
    name: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    name = name.strip()
    if name and not db.scalar(
        select(MailFilterCategory).where(func.lower(MailFilterCategory.name) == name.lower())
    ):
        db.add(MailFilterCategory(name=name))
        db.commit()
    if _is_htmx(request):
        return _filters_panel_response(request, db, return_to)
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@router.post("/mail/filter-categories/{category_id}/rename")
def rename_filter_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rename a category (admin). Cascades into existing rules AND stamped
    letters so the badge language stays consistent everywhere."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    cat = db.get(MailFilterCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="category not found")
    new_name = name.strip()
    if new_name and new_name != cat.name:
        old_name = cat.name
        cat.name = new_name
        db.execute(
            sa_update(MailFilterRule)
            .where(MailFilterRule.category == old_name)
            .values(category=new_name)
        )
        db.execute(
            sa_update(EmailMessage)
            .where(EmailMessage.filter_category == old_name)
            .values(filter_category=new_name)
        )
        db.commit()
    if _is_htmx(request):
        return _filters_panel_response(request, db, return_to)
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@router.post("/mail/filter-categories/{category_id}/delete")
def delete_filter_category(
    request: Request,
    category_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Delete a category (admin) — refused while any rule still uses it (edit
    those rules first). Stamped letters keep the old string as history and
    never block."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    cat = db.get(MailFilterCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="category not found")
    in_use = db.scalar(
        select(func.count()).select_from(MailFilterRule).where(
            MailFilterRule.category == cat.name
        )
    ) or 0
    if in_use:
        message = "Категорію використовують правила — спершу змініть їх"
        if _is_htmx(request):
            return _filters_panel_response(request, db, return_to, error=message)
        target = _filters_return_url(return_to)
        sep = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{sep}error={quote(message)}"
            if return_to != "settings" else target,
            status_code=303,
        )
    db.delete(cat)
    db.commit()
    if _is_htmx(request):
        return _filters_panel_response(request, db, return_to)
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@router.post("/mail/filters/dismiss-suggest")
def dismiss_filter_suggest(
    request: Request,
    address: str = Form(...),
    db: Session = Depends(get_db),
):
    """«Ні» on the suggestion banner: record the refusal as a DISABLED sender
    rule so the banner never nags about this sender again. Costs nothing — a
    disabled rule filters nothing and can be enabled later from the rules
    panel if the operator changes their mind."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    address = address.strip()
    if address:
        db.add(
            MailFilterRule(
                kind="sender", pattern=address, category="відхилені",
                enabled=False, created_by=user.username,
            )
        )
        db.commit()
    return RedirectResponse("/mail", status_code=303)


@router.post("/mail/filters/{rule_id}/toggle")
def toggle_mail_filter(
    request: Request,
    rule_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Enable/disable a rule (admin). Disabling never un-stamps already
    filtered letters — those return via each letter's own «Повернути» button,
    keeping the two decisions independent and predictable."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    rule = db.get(MailFilterRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    rule.enabled = not rule.enabled
    db.commit()
    if _is_htmx(request):
        return _filters_panel_response(request, db, return_to)
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@router.post("/mail/filters/{rule_id}/delete")
def delete_mail_filter(
    request: Request,
    rule_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Delete a rule (admin). Letters it filtered keep their category badge
    (historical fact) but lose the FK; they stay on the filtered tab until an
    operator returns them."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not can_edit(user, "mail-filters"):
        raise HTTPException(status_code=403, detail="недостатньо прав")

    rule = db.get(MailFilterRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    db.execute(
        sa_update(EmailMessage)
        .where(EmailMessage.filter_rule_id == rule.id)
        .values(filter_rule_id=None)
    )
    db.delete(rule)
    db.commit()
    if _is_htmx(request):
        return _filters_panel_response(request, db, return_to)
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


# Статуси, з яких лист ще можна повернути в тріаж без втрати історії.
UNACCEPT_ALLOWED_STATUSES = frozenset({"нове", "прийнято", "прораховано"})


def _unaccept_email(db: Session, email: EmailMessage) -> list[tuple[Path, Path]]:
    """Fully undo EVERY order accepted from this letter (a multi-colour letter
    can have several), returning it to the pre-accept "нове" state: move all
    claimed attachments from export back to the mail spool, blank each order's
    sheet placeholder row, and delete the orders. Raises on a filesystem error
    (the move has its own rollback) so the caller can abort cleanly; sheet
    blanking is best-effort. Side effects first, DB mutations last."""
    orders = db.scalars(
        select(Order).where(Order.source_email_id == email.id)
    ).all()
    # Legacy safety net: pre-0012 accepts linked only via email.order_id.
    if not orders and email.order_id:
        legacy = db.get(Order, email.order_id)
        if legacy is not None:
            orders = [legacy]

    # Відкат — лише поки робота на етапі прийняття. Відфрезеровану чи видану
    # роботу «повернути в тріаж» означало б видалити її разом з історією
    # (StatusEvent/Comment/ReworkRecord — cascade), а §5 вимагає точної історії
    # «хто що зробив». Далі — лише «Видалити з черги» в паспорті роботи, з
    # окремим підтвердженням (ревʼю 07.09.26, mail CRITICAL-4).
    advanced = [o for o in orders if o.status not in UNACCEPT_ALLOWED_STATUSES]
    if advanced:
        labels = ", ".join(f"{o.work_order_no or o.client_name or o.id} ({o.status})" for o in advanced)
        raise ValueError(
            "Робота вже далі за прийняття — відкат неможливий: " + labels
            + ". Якщо треба, видаліть її з черги в паспорті роботи."
        )

    # Лише ті, що справді виїжджали в export (order_id проставляється при
    # переміщенні). Раніше бралися ВСІ: файли, які нікуди не рухались,
    # «поверталися» в ту саму теку й діставали суфікс « (2)» —
    # crown.stl -> crown (2).stl, і кожен відкат частково прийнятого листа
    # множив суфікс далі (ревʼю 07.09.26, M.7).
    attachments = [a for a in email.attachments if a.order_id is not None]
    moved_pairs: list[tuple[Path, Path]] = []
    if attachments:
        old_paths = [Path(a.saved_path) for a in attachments]
        new_paths = restore_attachments_to_spool(
            Path(MAIL_ATTACHMENTS_PATH),
            spool_folder_name(email.uid, email.uid_validity),
            old_paths,
        )
        moved_pairs = list(zip(old_paths, new_paths))
        # Файли переїхали — кеш обходу export більше не відповідає диску.
        clear_export_cache()
        for attachment, new_path in zip(attachments, new_paths):
            attachment.saved_path = str(new_path)
            attachment.order_id = None

    spreadsheet = None
    for order in orders:
        if order.sheet_tab and order.row_number is not None:
            try:
                if spreadsheet is None:
                    spreadsheet = open_spreadsheet(db=db)
                worksheet = get_worksheet_by_name(spreadsheet, order.sheet_tab)
                if worksheet is not None:
                    # Через identity, не за збереженою позицією: стирання чистить
                    # A:K, тож влучання в сусідній рядок знищує чужу живу роботу
                    # (аудит 05.09.26, синк H-5).
                    try:
                        if not clear_order_row(worksheet, order):
                            logger.warning(
                                "Sheet row for email %s not confirmed — placeholder left as is",
                                email.id,
                            )
                    except SheetEraseBlocked as blocked:
                        # Стеля стирань за годину: рядок лишається в таблиці,
                        # відкат прийняття все одно доводиться до кінця.
                        logger.error("Відкат листа %s: %s", email.id, blocked)
            except Exception:  # noqa: BLE001 — sheet cleanup must not block the undo
                logger.exception("Could not blank sheet placeholder row for email %s", email.id)

    for order in orders:
        db.delete(order)
    email.order_id = None
    email.status = "нове"
    email.attachments_status = "ready"
    return moved_pairs


@router.post("/mail/{email_id}/restore")
def restore_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Return a processed email to the triage queue (status → "нове"). A rejected
    letter just flips status (its files never left the spool). An ACCEPTED letter
    is fully un-accepted: attachments move back from export, the sheet placeholder
    row is blanked and the created Order is deleted, so re-processing can't leave
    a duplicate."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    has_orders = bool(
        db.scalar(select(func.count()).select_from(Order).where(Order.source_email_id == email.id))
        or email.order_id
    )
    if email.status == "відхилено":
        email.status = "нове"
        db.commit()
        request.session["toast_flash"] = {"kind": "success", "message": "Лист повернуто в «Усі листи»."}
    elif email.status == "прийнято" or has_orders:
        # "прийнято" = fully accepted; a "нове" letter WITH orders = partially
        # accepted (some colours taken, more remain). Either way, undo every
        # order and put all files back — a clean restart of the whole letter.
        moved_pairs: list[tuple[Path, Path]] = []
        try:
            moved_pairs = _unaccept_email(db, email)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — mirror image of accept (C-2)
            # Two kinds of failure, one compensation. A filesystem error leaves
            # `moved_pairs` empty (restore_attachments_to_spool rolls its own
            # move back) and undo_moves is then a no-op. A failed COMMIT is the
            # dangerous one: files are already back in the spool while the DB
            # rolls back to "прийнято" with saved_path pointing into export.
            db.rollback()
            undo_errors = undo_moves(moved_pairs)
            clear_export_cache()
            logger.exception("Un-accept commit failed for email %s", email.id)
            detail = str(exc)
            if undo_errors:
                logger.error("Could not return files to export: %s", "; ".join(undo_errors))
                detail += (
                    ". УВАГА: частину файлів не вдалося повернути в export — "
                    + "; ".join(undo_errors)
                )
            return RedirectResponse(
                f"/mail?view=archive&error={quote('Не вдалося відкотити прийняття: ' + detail)}",
                status_code=303,
            )
        request.session["toast_flash"] = {
            "kind": "success",
            "message": "Прийняття відкочено: роботи видалено, файли повернуто в лист.",
        }

    return RedirectResponse("/mail", status_code=303)
