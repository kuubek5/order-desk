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
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_ as sa_and, func, select, text, update as sa_update
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from app.archive_extract import is_archive
from app.business_day import business_today, get_rollover
from app.export_scanner import clear_export_cache
from app.link_attachments import (
    LinkAttachment,
    LinkDownloadError,
    download_link,
    extract_download_links,
    undownloaded_links,
)
from app.mail_export import (
    _contained_child,
    list_client_folders,
    preview_export_target,
    restore_attachments_to_spool,
    undo_moves,
)
from app.mail_body_view import inline_parts, letter_segments, useful_text
from app.mail_color_split import suggest_color_plan
from app.mail_duplicates import find_duplicates, has_duplicate_files
from app.mail_filters import apply_rule_retroactively
from app.mail_hold import (
    hold_label,
    not_on_hold,
    on_hold,
    put_on_hold,
    release_hold,
)
from app.services.material_suggest import (
    best_material,
    canonical_material,
    canonical_suggestions,
    row_label,
)
from app.material_catalog import load_alias_rows
from app.material_class import mail_material_badge
from app.mail_reader import (
    download_attachments_now,
    extract_archive_attachments,
    list_move_target_folders,
    move_message_back_to_inbox,
    move_message_to_folder,
    move_messages_to_folder,
    redownload_missing_attachments,
    return_messages_to_inbox,
)
from app.mail_sync_service import (
    MailSyncBusyError,
    MailSyncError,
    run_sync_owned_session,
    zombie_fetch_blocks_files,
)
from app.mail_spool import spool_folder_name
from app.client_matcher import match_client_name
from app.models import (
    ActionLog,
    Attachment,
    Client,
    ClientSenderMemory,
    EmailMessage,
    MailFilterCategory,
    MailFilterRule,
    Order,
)
from app.order_folder import (
    attach_email_preview_tokens,
    attach_export_folder_uris,
    attach_job_code_folder_uris,
    forget_email_preview_token,
    resolve_email_attachment_folder,
)
from app.queue_filters import (
    SERVICE_TYPE_FILTERS,
    count_by_service_type,
    filter_emails_by_service_type,
)
from app.routers.section_gate import blocked_response
from app.platform_windows import open_folder_in_explorer
from app.routers.deps import (
    TRUSTED_ONLY_DETAIL,
    get_current_user,
    is_trusted_request,
    login_redirect,
    get_db,
    open_folder_response,
    templates,
)
from app.sender_memory import list_sender_memories, lookup_sender
from app.services.mail_accept import accept_letter, resolve_wizard_overrides
from app.services.mail_mirror import mail_mirror_orders
from app.services.focus import focused_ids
from app.statuses import STATUSES
from app.services.config_state import (
    mail_preview_roots,
    mail_trusted_roots,
)
from app.services.settings_nav import can_edit
from app.settings_store import (
    get_export_folder_path,
    get_imap_login,
    get_mail_attachments_path,
    get_mail_download_all,
    get_setting,
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


def _parse_id_list(raw: str | None) -> list[int]:
    """Список id листів із коми-рядка (`?batch=3,7,12`). Порядок збережено, дублі
    прибрано, нецифрове тихо ігнорується — сирий рядок приходить з браузера."""
    seen: set[int] = set()
    out: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            n = int(part)
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _material_context(email: EmailMessage, customer_text: str | None = None) -> str:
    """Де шукати матеріал, коли здогад його не назвав: ТЕМА + слова замовника
    (без пересилання й підпису). Тема — бо клієнт часто пише матеріал лише там
    («емоутіонс а2» → `emo a2`)."""
    if customer_text is None:
        customer_text = useful_text(letter_segments(email.body_text))
    return f"{email.subject or ''}\n{customer_text}"


_BADGE_CLS = {"Zr": "mat-zr", "PMMA": "mat-pmma", "Ti": "mat-ti", "SLM": "mat-slm", "Wax": "mat-wax"}


def _row_badge(label: dict, old: dict | None) -> dict:
    """Чіп рядка з канону (`row_label`): символ категорії + `mono b1`. Клас
    кольору — зі старого чіпа, а без нього — за символом категорії."""
    symbol = label.get("badge") or (old or {}).get("symbol") or "?"
    cls = (old or {}).get("cls") or _BADGE_CLS.get(symbol, "mat-other")
    # Колір невідомий і лінія зветься як сама категорія («pmma» під «PMMA») —
    # не дублювати: просто «PMMA» (власник 25.09.26). «Zr mono» лишається.
    text = label["text"]
    if text.strip().lower() == symbol.strip().lower():
        text = ""
    # title — лише категорія: шаблон сам дописує «· <колір>» до підказки.
    return {
        "symbol": symbol, "cls": cls, "color": text,
        "title": (old or {}).get("title") or symbol,
    }


_MAIL_VIEWS = ("pending", "filtered", "archive", "auto", "processed", "gone", "hold")


def _mail_back_url(request: Request, open_id: int | None = None) -> str:
    """Куди повернути оператора після «↩» (повернути лист у «Вхідні»): у ту
    саму ВКЛАДКУ, з якої він натиснув, а не на «Вхідні» (власник 25.09.26 —
    розбираючи папку, після кожного повернення доводилось клацати її знову).

    Вкладку беремо з адреси сторінки: `HX-Current-URL` для htmx-кнопки картки,
    `Referer` для звичайної форми (як `queue.back_to_queue`). З адреси береться
    лише `view`/`service` із білого списку — відкритого редиректу немає.
    Невідома вкладка чи «Вхідні» → `/mail` (з `open`, якщо картку треба
    лишити відкритою: у «Вхідні» повернутий лист якраз і зʼявився)."""
    headers = getattr(request, "headers", None) or {}
    page = headers.get("HX-Current-URL") or headers.get("referer") or ""
    query = parse_qs(urlsplit(page).query) if urlsplit(page).path == "/mail" else {}
    view = (query.get("view") or ["pending"])[0]
    service = (query.get("service") or ["all"])[0]
    params: list[str] = []
    if view in _MAIL_VIEWS and view != "pending":
        params.append(f"view={view}")
        # «Усі в папці» (period=all) — лишитись у тому ж режимі вкладки.
        if view == "processed" and (query.get("period") or [""])[0] == "all":
            params.append("period=all")
    elif open_id is not None:
        params.append(f"open={open_id}")
    if service in SERVICE_TYPE_FILTERS and service != "all":
        params.append(f"service={quote(service)}")
    return "/mail" + (f"?{'&'.join(params)}" if params else "")


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
    batch: str | None = None,
    period: str = "",
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    # Вкладка папки: типово — лише сьогоднішні переноси; `period=all` — УСІ листи
    # в папках скриньки за весь час (власник 25.09.26: вчорашній перенесений лист
    # і перенесені до появи `mailbox_moved_at` не було видно ніде).
    processed_all = view == "processed" and period == "all"

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
    if view not in _MAIL_VIEWS:
        view = "pending"
    # Pop the flash only on a full-page render — the 15s poll (partial="list")
    # would otherwise consume it before the real navigation shows it.
    toast_flash = request.session.pop("toast_flash", None) if partial not in ("list", "batch") else None

    # Конвеєр (блок B): панель батчу — рядок на кожен ОБРАНИЙ лист. Той самий
    # партіал `#mail-detail`, що й картка (стан вибору лише в JS, mail.js), тож
    # НОВОГО роуту не заводимо — це `GET /mail` з `partial=batch`. Значення в
    # клітинках — ті самі здогади, що пішли б у картку (`_mail_panel_context`),
    # тож джерело правди одне. Рахуємо лише для обраних листів (їх кілька), не на
    # кожному полі й не на 15-секундному поллі.
    if partial == "batch":
        picked_ids = _parse_id_list(batch)
        by_id = {}
        if picked_ids:
            by_id = {
                e.id: e
                for e in db.scalars(
                    select(EmailMessage)
                    .where(EmailMessage.id.in_(picked_ids))
                    .options(selectinload(EmailMessage.attachments))
                ).all()
            }
        # Порядок — той, у якому клієнт надіслав id (порядок списку згори вниз),
        # щоб рядки батчу читались так само, як список. Лише листи «нове»: батч —
        # інструмент тріажу, уже оброблений лист сюди не потрапляє.
        batch_rows = [
            _mail_panel_context(db, by_id[eid], user)
            for eid in picked_ids
            if eid in by_id and by_id[eid].status == "нове"
        ]
        return templates.TemplateResponse(
            request, "_mail_batch_table.html",
            {"batch_rows": batch_rows, "user": user},
        )

    # Views: pending = "нове" NOT stamped by a filter rule; filtered = "нове"
    # stamped (kept, never deleted — one click brings a letter back); archive =
    # accepted/rejected; processed = перенесені в папку скриньки.
    #
    # Перенесений лист ПОКИДАЄ решту вкладок — як у справжній скриньці, де він
    # пішов з Inbox у папку (рішення власника 23.09.26). Тому `not_moved` висить
    # на pending/filtered/archive, а вкладка папки показує саме перенесені.
    not_moved = EmailMessage.mailbox_folder.is_(None)
    # Лист, який ПОКИНУВ Вхідні пошти (`inbox_gone_at`), виходить із черги тріажу
    # й живе у своїй вкладці «Покинули Вхідні» — CRM дзеркалить Вхідні (рішення
    # власника 24.09.26). `not_gone` тому висить на pending/filtered поряд із
    # `not_moved`.
    not_gone = EmailMessage.inbox_gone_at.is_(None)
    # Вкладка «Оброблено» — лише ПОТОЧНИЙ робочий день (власник 24.09.26: «ті, що
    # сьогодні обробляв, — тільки вони»), інакше вона росла б безмежно. Межа —
    # робоча доба (07:30), як усюди в §14: день D покриває [D 07:30, D+1 07:30).
    # Листи, перенесені до появи поля (mailbox_moved_at IS NULL), не сьогоднішні —
    # у вкладці не показуються, але фізично лишаються в папці й у базі.
    processed_today = sa_and(
        EmailMessage.mailbox_folder.is_not(None),
        EmailMessage.mailbox_moved_at.is_not(None),
        EmailMessage.mailbox_moved_at >= datetime.combine(business_today(), get_rollover()),
    )
    if view == "gone":
        status_clause = sa_and(EmailMessage.inbox_gone_at.is_not(None), not_on_hold)
    elif view == "hold":
        # «На уточненні» (власник 25.09.26): лише стан CRM, тож показуємо лист
        # на паузі, хоч би що сталося з ним у скриньці — пауза сильніша.
        status_clause = sa_and(EmailMessage.status == "нове", on_hold)
    elif view == "processed":
        status_clause = (
            EmailMessage.mailbox_folder.is_not(None) if processed_all else processed_today
        )
    elif view == "archive":
        status_clause = sa_and(EmailMessage.status.in_(_ARCHIVE_STATUSES), not_moved)
    elif view == "filtered":
        status_clause = sa_and(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_not(None),
            not_moved, not_gone, not_on_hold,
        )
    else:
        status_clause = sa_and(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_(None),
            not_moved, not_gone, not_on_hold,
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
    # Чіп матеріалу+кольору на рядку — здогад із листа (material_color_guess),
    # класифікований аліасами З БАЗИ (як синк), тож нові написання з бібліотеки
    # матеріалів чіп підхоплює. Аліаси читаємо ОДИН раз на список, не на рядок.
    _mat_aliases = load_alias_rows(db)
    for _email in emails:
        _email.mat_badge = mail_material_badge(_email.material_color_guess, _mat_aliases)
        # Бейдж готовності знає про дублі (triage_readiness): лише «Вхідні»,
        # де цей бейдж і малюється; вміст читається раз на набір файлів.
        if view == "pending":
            _email.has_duplicates = has_duplicate_files(_email.attachments)
        # Латинський канон у чіпі: «Zr mono b1» замість «Zr B1» (власник
        # 25.09.26) — та сама відповідь, що підставиться в поле картки, з тим
        # самим пошуком матеріалу в тексті замовника («B1» + «Monolight»).
        _context = _material_context(_email)
        _label = row_label(db, _email.material_color_guess, _context)
        if _label:
            _email.mat_badge = _row_badge(_label, _email.mat_badge)
            # Для бейджа готовності (triage_readiness): матеріал з відтінком
            # упізнано однозначно — поле картки заповниться саме.
            _email.material_known = bool(
                best_material(db, _email.material_color_guess, _context)
            )
    # How many pending letters are being held back from the frozen list.
    held_back_count = 0
    if since is not None and view == "pending":
        held_back_count = db.scalar(
            select(func.count()).select_from(EmailMessage).where(
                status_clause, EmailMessage.id > since
            )
        ) or 0

    # Top-level view counts for the tabs. Перенесені листи (`not_moved`)
    # виключені звідусіль, крім своєї вкладки — інакше значок вкладки бреше
    # проти списку, який її фільтр показує.
    pending_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_(None),
            not_moved, not_gone, not_on_hold,
        )
    ) or 0
    filtered_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_not(None),
            not_moved, not_gone, not_on_hold,
        )
    ) or 0
    gone_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.inbox_gone_at.is_not(None), not_on_hold,
        )
    ) or 0
    hold_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове", on_hold,
        )
    ) or 0
    archive_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status.in_(_ARCHIVE_STATUSES), not_moved
        )
    ) or 0
    processed_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(processed_today)
    ) or 0
    # Усі листи в папках за весь час — для посилання «Показати всі в папці».
    processed_all_count = 0
    if view == "processed":
        processed_all_count = db.scalar(
            select(func.count()).select_from(EmailMessage).where(
                EmailMessage.mailbox_folder.is_not(None)
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
            not_moved, not_gone, not_on_hold,
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
                # Потрібне рядку для кнопки «↦ у папку»: без нього полл кожні 15с
                # перемальовував би рядки без кнопки.
                "mail_processed_folder": get_setting(db, "mail_processed_folder") or "",
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
            "processed_count": processed_count,
            "processed_all": processed_all,
            "processed_all_count": processed_all_count,
            # «Покинули Вхідні» — листи, яких уже нема у Вхідних пошти (папка або
            # видалення). CRM дзеркалить Вхідні; вкладка показується лише коли є
            # такі листи.
            "gone_count": gone_count,
            # «На уточненні» — листи на паузі; вкладка видна завжди, бо це
            # місце, куди оператор кладе лист сам.
            "hold_count": hold_count,
            # Назва папки скриньки для підпису вкладки перенесених. Порожньо →
            # вкладка ховається (переносити нікуди не налаштовано).
            "mail_processed_folder": get_setting(db, "mail_processed_folder") or "",
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
            # Дзеркало черги внизу сторінки — ПОВНА копія рядків черги для робіт,
            # ПРИЙНЯТИХ саме з пошти, з тим самим inline-редагуванням. Контекст,
            # який очікує _order_row.html: статуси (меню статусу) і набір «мої
            # зараз» (персональна мітка; сторож test_order_focus вимагає його на
            # КОЖНОМУ рендері рядка). Іконки папок тут НЕ чіпляємо — їх дотягне
            # полл /mail/queue-mirror одразу після першого малюнку (hx-trigger
            # `load`), як #queue-rows у черзі, щоб не платити скан мережевої шари
            # на повному рендері сторінки.
            "mirror_orders": mail_mirror_orders(db),
            "statuses": STATUSES,
            "focused_ids": focused_ids(db, user),
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


@router.get("/mail/queue-mirror", response_class=HTMLResponse)
def get_mail_queue_mirror(request: Request, db: Session = Depends(get_db)):
    """Полл-фрагмент дзеркала черги (лише рядки) — свопається в .qmir-body кожні
    15с. Read-only список робіт, ПРИЙНЯТИХ з пошти (mail_mirror_orders).

    Оголошено ВИЩЕ за `/mail/{email_id}`: інакше FastAPI матчив би «queue-mirror»
    як email_id (та сама пастка, що з `/settings/furnaces/password`)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    blocked = blocked_response(request, db, user, "mail")
    if blocked is not None:
        return blocked
    orders = mail_mirror_orders(db)
    # Іконки папок (export + STL-прев'ю) — саме на поллі, як #queue-rows у черзі:
    # скан мережевої шари дорогий, тож повний рендер сторінки його пропускає, а
    # цей полл (спрацьовує на `load` одразу після малюнку) домальовує іконки.
    attach_export_folder_uris(db, orders)
    attach_job_code_folder_uris(db, orders)
    return templates.TemplateResponse(
        request, "_mail_queue_mirror.html",
        {
            "mirror_orders": orders,
            "statuses": STATUSES,
            "focused_ids": focused_ids(db, user),
        },
    )


# ВИЩЕ за `/mail/{email_id}`: інакше FastAPI бере «move-folders» за id і дає 422.
@router.get("/mail/move-folders", response_class=HTMLResponse)
def mail_move_folders(request: Request, db: Session = Depends(get_db)):
    """Меню «Перемістити» масових дій: папки скриньки (як у ukr.net). Тягнеться
    ЛИШЕ при відкритті меню (IMAP-логін ~1 с), не на кожному рендері списку.
    Папка «оброблено» з Налаштувань — першою, з позначкою."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    processed = (get_setting(db, "mail_processed_folder") or "").strip()
    error = ""
    folders: list[str] = []
    try:
        folders = list_move_target_folders(db)
    except Exception as exc:  # noqa: BLE001 — текст збою IMAP у меню
        error = f"Не вдалося зчитати папки: {exc}"
    if processed and processed in folders:
        folders = [processed] + [f for f in folders if f != processed]
    return templates.TemplateResponse(
        request, "_mail_move_folders.html",
        {"folders": folders, "processed_folder": processed, "error": error},
    )


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


def _client_card_id(db: Session, name: str) -> int | None:
    """id картки клієнта за іменем — щоб ім'я в картці листа вело на «Клієнти».

    Те саме зіставлення, що на видачі (`handout._client_id_for`): точний
    casefold, далі нечіткий матчер (той самий лаб пишеться кількома написаннями,
    усі мусять вести на одну картку). Картки НЕ створюємо тут (це робить черга/
    видача) — якщо клієнта ще немає, повертаємо None, і шаблон веде на пошук.
    """
    folded = (name or "").strip().casefold()
    if not folded:
        return None
    by_name = {
        c.canonical_name.strip().casefold(): c.id
        for c in db.scalars(select(Client)).all()
        if c.canonical_name
    }
    if folded in by_name:
        return by_name[folded]
    hit = match_client_name(folded, list(by_name), {}).matched_folder_name
    return by_name.get(hit) if hit else None


def _card_dir_context(
    db: Session,
    email: EmailMessage,
    partial_state: dict,
    sender_hint,
    *,
    client_name: str,
    material_color: str,
    folder_pick: str,
    folder_new: str,
    material_folder: str,
    attachment_ids: set[int] | None,
) -> dict:
    """Куди ляжуть файли (превʼю export) + скільки файлів у цій партії й скільки
    ще не скачано — контекст рядка шляху картки (блок A, «Стрічка»).

    Той самий розрахунок, що робив `_wizard_context` для кроків 2/3 майстра: тека
    рахується вже на КАРТЦІ (за іменем клієнта), як просив власник. Виведено сюди,
    бо картка більше не проходить через кроки майстра, але шле ту саму форму в
    `/mail/{id}/accept`, і мусить показати той самий шлях.

    Повертає й `folder_pick` — для постійного клієнта дефолтом підставляється його
    наявна тека (як робив крок 2 майстра), інакше файли постійного клієнта пішли б
    у НОВУ теку за (можливо брудним) іменем замість відомої export/<клієнт>.
    """
    export_root = Path(get_export_folder_path(db))
    existing_folders = list_client_folders(export_root)
    # Постійний клієнт: якщо оператор не задав теку вручну, дефолт — його наявна
    # export-тека (дзеркало кроку 2 майстра). Лише коли вона реально існує.
    if (
        not folder_pick.strip() and not folder_new.strip()
        and sender_hint and sender_hint.export_folder
        and sender_hint.export_folder in existing_folders
    ):
        folder_pick = sender_hint.export_folder
    client_override, material_override = resolve_wizard_overrides(
        folder_pick, folder_new, material_folder
    )
    preview = preview_export_target(
        export_root, client_name, material_color, client_override, material_override
    )
    unclaimed = partial_state["unclaimed_attachments"]
    # Файли, що поїдуть саме цією партією: позначені оператором, або всі
    # нерозібрані, коли нічого не позначено (типовий одноколірний лист).
    batch = (
        [a for a in unclaimed if a.id in attachment_ids]
        if attachment_ids
        else unclaimed
    )
    # Нерозібрані вкладення, яких ще НЕ на диску (гейт «не всі файли скачані»,
    # власник 24.09.26). unclaimed_attachments уже відфільтроване до on_disk, тож
    # різниця з усіма нерозібраними = не скачані — БЕЗ ще одного проходу по шарі.
    undownloaded_files = (
        sum(1 for a in email.attachments if a.order_id is None)
        - partial_state["unclaimed_count"]
    )
    return {
        "preview": preview,
        "existing_folders": existing_folders,
        "attachment_count": len(batch),
        "undownloaded_files": undownloaded_files,
        # Ефективна тека (з дефолтом постійного клієнта) — щоб select у dir_editor
        # показав саме її обраною, а не «авто-визначення».
        "folder_pick": folder_pick,
    }


def _mail_panel_context(
    db: Session,
    email: EmailMessage,
    user,
    *,
    client_name: str | None = None,
    material_color: str | None = None,
    kind: str | None = None,
    quantity: str | None = None,
    folder_pick: str = "",
    folder_new: str = "",
    material_folder: str = "",
    attachment_ids: list[int] | None = None,
    error: str | None = None,
) -> dict:
    """Shared render context for the triage detail CARD (блок A, «Стрічка»):
    seeded work fields, material candidates, whitelisted download links and the
    export-path preview — everything the one-screen card needs to accept a letter
    without the old three-step wizard. Reused by get_mail_detail, the file
    actions (archive/download re-render the whole card) and `_accept_failed`,
    which re-renders the card with the submitted values kept and an error banner.

    Значення полів: `None` → семена (постійний клієнт / показне ім'я / здогади);
    непорожні (невдале прийняття) підставляються, щоб оператор не втратив введене.
    """
    attach_email_preview_tokens([email], mail_trusted_roots(db), mail_preview_roots(db))
    # ОДИН прохід по диску на весь рендер панелі: далі і «зниклі файли», і
    # стан часткового прийняття рахуються з цього набору.
    on_disk = {a.id for a in email.attachments if Path(a.saved_path).exists()}
    seed = (email.material_color_guess or "") or (email.subject or "")
    # Recurring client? Sender memory beats every guess for the name prefill.
    sender_hint = lookup_sender(db, email)
    # Семена полів: постійний клієнт (пам'ять) → показне ім'я → здогад. НЕ адреса —
    # адреса лишається крайнім запасом уже в шаблоні (скарга власника 24.09.26).
    if client_name is None:
        if sender_hint and sender_hint.client_name:
            client_name = sender_hint.client_name
        elif email.from_name:
            client_name = email.from_name
        else:
            client_name = email.client_name_guess or ""
    # Лише слова замовника (без пересилання, підпису, списку файлів) — з них і
    # прев'ю, і розпізнавання матеріалу, коли здогад його не назвав.
    body_segments = letter_segments(email.body_text)
    customer_text = useful_text(body_segments)
    material_ctx = _material_context(email, customer_text)
    if material_color is None:
        # Поле — одразу латинський канон, коли здогад однозначний: матеріал
        # упізнано й відтінок рівно один («ПММА а2» → `pmma a2`). Інакше в таблицю
        # йшла б кирилиця, якщо оператор не клацне чіп (власник 25.09.26 —
        # «уніфікувати, як у ручному додаванні»). Неоднозначний здогад
        # («Monolith» без кольору) лишається як є: вгадувати колір не можна.
        # Слово матеріалу шукаємо й у тексті замовника, коли здогад його не
        # назвав («B1» + у тексті «Monolight» → `mono b1`).
        guess = email.material_color_guess or ""
        material_color = best_material(db, guess, material_ctx) or guess
    if kind is None:
        kind = email.kind_guess or ""
    if quantity is None:
        quantity = email.quantity_guess or ""
    selected_ids = set(attachment_ids) if attachment_ids else None
    partial_state = _email_partial_state(db, email, on_disk)
    # Однакові файли в листі (клієнт надіслав роботу двічі — окремо й в архіві).
    # Копії за замовчуванням НЕ позначені, щоб одну роботу не прийняли й не
    # відфрезерували двічі; рішення лишається за оператором (власник 25.09.26).
    dup_report = find_duplicates(
        [a for a in partial_state["unclaimed_attachments"] if a.id in on_disk]
    )
    if selected_ids is None and dup_report.copy_of:
        selected_ids = {
            a.id for a in partial_state["unclaimed_attachments"]
            if a.id in on_disk and a.id not in dup_report.copy_of
        }
    dir_ctx = _card_dir_context(
        db, email, partial_state, sender_hint,
        client_name=client_name, material_color=material_color,
        folder_pick=folder_pick, folder_new=folder_new,
        material_folder=material_folder, attachment_ids=selected_ids,
    )
    # Ефективна тека (з дефолтом постійного клієнта) перекриває вхідний folder_pick.
    folder_pick = dir_ctx.pop("folder_pick")
    # Підказка кольорів для багатокольорового листа (пацієнт у тексті біля
    # кольору ↔ пацієнт в імені файлу). Лише підказка: групу обирає оператор,
    # прийняття — звичайне часткове. Рахується по НЕРОЗІБРАНИХ файлах, тож
    # після кожної прийнятої партії показує вже решту кольорів.
    color_plan = (
        suggest_color_plan(
            email.body_text,
            email.material_color_guess or material_color,
            partial_state["unclaimed_attachments"],
        )
        if partial_state["unclaimed_count"] > 1
        else None
    )
    # Матеріал — ЛАТИНСЬКИЙ канон, як у ручному додаванні (власник 25.09.26:
    # «прибрати кирилицю — mono a2, emo a2»). Клік по групі кольору підставляє
    # `mono a3,5`, а не сире «Monolith a3.5»; чіпи під полем — ті самі канони
    # для відтінків листа. Одне правило — `material_suggest.canonical_*`.
    material_guess = email.material_color_guess or material_color or seed
    if color_plan:
        for group in color_plan.groups:
            group.material = (
                canonical_material(db, material_guess, group.shade, material_ctx) or group.material
            )
    # Найімовірніший першим (kind="best", підсвічений), далі варіанти ("alt").
    material_cands = canonical_suggestions(
        db, material_guess,
        [g.shade for g in color_plan.groups] if color_plan else None,
        context=material_ctx,
    )
    context = {
        "email": email,
        "user": user,
        "error": error,
        "client_name": client_name,
        # id картки клієнта (для кліку по імені в шапці) — за РЕЗОЛЬВНУТИМ іменем
        # клієнта (пам'ять/показне ім'я), не за адресою. None → шаблон веде на пошук.
        "client_card_id": _client_card_id(db, client_name),
        "sender_hint": sender_hint,
        "material_color": material_color,
        "kind": kind,
        "quantity": quantity,
        "folder_pick": folder_pick,
        "folder_new": folder_new,
        "material_folder": material_folder,
        # None → усі нерозібрані позначені (одноколірний дефолт); множина →
        # вибір оператора (часткове прийняття кількох кольорів).
        "attachment_ids": selected_ids,
        "dup_report": dup_report,
        "material_cands": material_cands,
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
        # Папка «оброблено» з налаштувань — картка показує кнопку переміщення лише
        # коли вона задана (інакше кнопці нема куди переносити).
        "mail_processed_folder": get_setting(db, "mail_processed_folder") or "",
        "color_plan": color_plan,
        # Текст листа, розкладений на частини (app/mail_body_view.py): у картці
        # — лише слова замовника (2 рядки), у режимі читання — усе, службове
        # приглушено. `letter_inline` — для підсвічування відтінків у рядку.
        "letter_segments": body_segments,
        "letter_preview": customer_text,
        "letter_inline": inline_parts,
        **partial_state,
        **dir_ctx,
    }
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
    # Склад файлів змінився → закешований токен прев'ю (часто «немає», поки
    # файлів не було) застарів. Картка, яку перемалює цей тригер, мусить
    # порахувати його наново, інакше STL-прев'ю з'являлось лише після F5.
    forget_email_preview_token(email.id)
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


def _mark_link_handled(db: Session, email_id: int, ref: str) -> None:
    """Додати посилання до `handled_link_refs` одним атомарним UPDATE.

    Список відсортований і без дублів (як і раніше писався з Python), але
    читається й пишеться всередині SQLite — паралельні запити не перетирають
    позначки одне одного. Комітить викликач."""
    db.execute(
        text(
            "UPDATE email_messages SET handled_link_refs = ("
            " SELECT json_group_array(value) FROM ("
            "  SELECT value FROM json_each(COALESCE(email_messages.handled_link_refs, '[]'))"
            "  UNION SELECT :ref ORDER BY 1))"
            " WHERE id = :id"
        ),
        {"ref": ref, "id": email_id},
    )


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
        # Та сама тека спулу, що й у вкладень листа (`<uidvalidity>_<uid>`,
        # mail_spool.spool_folder_name). Тут стояв голий `email.uid`, і файли
        # за посиланням лягали в ІНШУ теку, ніж вкладення того самого листа.
        spool_dir = Path(get_mail_attachments_path(db)) / spool_folder_name(email.uid, email.uid_validity)
        path = download_link(link, spool_dir, existing_names=existing)
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
        # АТОМАРНО, одним UPDATE: «Скачати за посиланням» шле запит на КОЖНЕ
        # посилання паралельно, і читання-зміна-запис у Python губив чужі
        # позначки — останній запис перемагав. Бойовий лист 25.09.26: 9 файлів
        # ukr.net скачано, «оброблених» записано 2, картка казала «🔗 7 не
        # скачано», гейт прийняття блокував, а повторне скачування дало б
        # дублі «(1)». Той самий принцип, що в лічильниках видачі (двоє
        # операторів): список міняє сам SQLite під своїм локом запису.
        _mark_link_handled(db, email.id, ref)
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
                toast = {"message": f"Розпаковано {extracted} файл(ів) з архіву", "kind": "success"}
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
    frag: str = Form(""),
    db: Session = Depends(get_db),
):
    """Render one step of the semi-automatic accept wizard, OR just the card's
    export-path preview line (`frag=path`, блок A «Стрічка»).

    Картка «Стрічка» шле ту саму форму, що майстер, але оновлює лише рядок шляху
    при зміні полів чи теки — тому `frag=path` повертає `_mail_path_preview.html`
    (той самий розрахунок теки), а не весь майстер. Класичні кроки (frag порожній)
    лишаються без змін: кожен Далі/Назад перемальовує `_mail_wizard.html`."""
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
    if frag == "path":
        return templates.TemplateResponse(request, "_mail_path_preview.html", ctx)
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
    # Candidates from the operator's current material text, or the recognised
    # guess / subject on the very first render. Латинський канон — те саме
    # правило, що й картка (`canonical_suggestions`).
    seed = material_color.strip() or (email.material_color_guess or "") or (email.subject or "")
    candidates = [s.text for s in canonical_suggestions(db, seed)]

    sender_hint = lookup_sender(db, email)
    # Step 1 opens with the remembered name when the operator hasn't typed one;
    # step 2 pre-selects the remembered folder (only if it still exists) when
    # no explicit pick/new-folder override was given.
    # Дефолт імені клієнта: постійний клієнт (пам'ять) → показне ім'я
    # відправника (from_name, «Стоматологія Ритченка») → здогад. НЕ email-адреса:
    # раніше поле падало на неї, і оператор бачив «irytchenkodental@gmail.com»
    # замість імені, а тека потім рахувалась від адреси (скарга власника
    # 24.09.26). Адреса лишається крайнім запасом уже в шаблоні.
    if step == 1 and not client_name.strip():
        if sender_hint and sender_hint.client_name:
            client_name = sender_hint.client_name
        elif email.from_name:
            client_name = email.from_name
        elif email.client_name_guess:
            client_name = email.client_name_guess
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
    # Скільки нерозібраних вкладень листа ще НЕ на диску (гейт «не всі файли
    # скачані», власник 24.09.26). unclaimed_attachments уже відфільтроване до
    # on_disk (див. _email_partial_state), тож різниця з усіма нерозібраними
    # вкладеннями = не скачані — БЕЗ ще одного проходу по мережевій шарі.
    _unclaimed_total = sum(1 for a in email.attachments if a.order_id is None)
    ctx["undownloaded_files"] = _unclaimed_total - ctx["unclaimed_count"]
    # Пропонована тека рахується вже на КРОЦІ 1 (прохання власника 24.09.26:
    # «щоб система орієнтувалась на ім'я замовника й пропонувала папку»). На
    # кроці 1 це підказка за поточним іменем; крок 2 її ж підтверджує й дає
    # перекрити. preview_export_target нічого не пише на диск.
    export_root = Path(get_export_folder_path(db))
    ctx["preview"] = preview_export_target(
        export_root, client_name, material_color, client_override, material_override
    )
    ctx["attachment_count"] = ctx["batch_count"]
    if step >= 2:
        ctx["existing_folders"] = list_client_folders(export_root)

    return ctx


@router.post("/mail/{email_id}/open-folder")
def open_mail_folder(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Гейт адреси ДО пошуку листа: чужій адресі не кажемо навіть, чи він існує.
    if not is_trusted_request(request, db):
        raise HTTPException(status_code=403, detail=TRUSTED_ONLY_DETAIL)

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

    return open_folder_response(
        request, db, folder, opener=open_folder_in_explorer, log_label=f"mail {email_id}"
    )


@router.post("/mail/{email_id}/open-client-folder")
def open_client_export_folder(
    request: Request,
    email_id: int,
    folder: str = "",
    db: Session = Depends(get_db),
):
    """Відкрити ТЕКУ КЛІЄНТА в export просто з картки (рядок шляху → «тека
    клієнта»). На відміну від `/open-folder` (тека вкладень у спулі), це існуюча
    тека `export/<клієнт>`, куди ляжуть файли — оператор хоче глянути, що там уже
    є. `folder` приходить у query (кнопка `data-open-folder-url` шле порожнє тіло),
    це поточна тека з рядка шляху. Той самий гейт адреси й та сама відповідь
    (loopback → відкрити Провідник; мережа → віддати шлях для копіювання), що
    в `/open-folder`. Шлях будується через `_contained_child` — захист від
    виходу за корінь export."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not is_trusted_request(request, db):
        raise HTTPException(status_code=403, detail=TRUSTED_ONLY_DETAIL)
    if db.get(EmailMessage, email_id) is None:
        raise HTTPException(status_code=404, detail="email not found")
    name = (folder or "").strip()
    if not name:
        raise HTTPException(status_code=404, detail="теку клієнта не вказано")
    export_root = Path(get_export_folder_path(db))
    try:
        client_dir = _contained_child(export_root, name)
    except ValueError:
        raise HTTPException(status_code=400, detail="небезпечне ім'я теки")
    if not client_dir.is_dir():
        raise HTTPException(status_code=404, detail="теки клієнта ще немає")
    return open_folder_response(
        request, db, client_dir, opener=open_folder_in_explorer,
        log_label=f"mail {email_id} client-folder",
    )


def _extract_after_download(db: Session, email: EmailMessage, what: str) -> None:
    """Розпакувати архіви листа після ручного скачування (спільне для «Скачати
    вкладення» і «Скачати наново»). Не кидає: розпакування не має ламати панель —
    архів тоді лишається, і кнопка «Розпакувати архіви» на видноті."""
    if not any(is_archive(a.filename) for a in email.attachments):
        return
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
        logger.exception("Розпакування після %s, лист %s", what, email.id)
        db.rollback()


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
        download_attachments_now(db, email, Path(get_mail_attachments_path(db)))
        db.commit()
        db.refresh(email)  # expire_on_commit=False: колекція вкладень інакше стара
    except Exception as exc:  # noqa: BLE001 — surface a friendly error, don't 500
        db.rollback()
        logger.exception("Manual attachment download failed for email %s", email.id)
        context = _mail_panel_context(db, email, user, error=f"Не вдалося скачати файли: {exc}")
        return templates.TemplateResponse(request, "_mail_detail_panel.html", context)
    # Архів у листі — розпакувати одразу, як це вже роблять скачування за
    # посиланням і повторне скачування. Без цього «Скачати вкладення» лишало
    # архів замість STL, а кнопка «Розпакувати» ховалась у меню чіпа (власник
    # 25.09.26: «я не бачу, що там архів»).
    _extract_after_download(db, email, "ручного скачування")
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
            db, email, Path(get_mail_attachments_path(db))
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
    if saved:
        _extract_after_download(db, email, "повторного скачування")

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
    sum3d_id: str = Form(""),
    opak: str = Form(""),
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
        """Помилка прийняття — назад у ТУ САМУ картку з поясненням, а не редіректом
        зі сторінки.

        Картка «Стрічка» живе у фрагменті `#mail-detail`, і редірект на
        `/mail/{id}?error=…` перезавантажував увесь екран: усе, що оператор
        заповнив, зникало, а лист доводилось відкривати заново (аудит 05.09.26,
        UX 1.2). Тепер повертаємо ту саму картку з тими ж значеннями і банером
        помилки вгорі. Не-HTMX виклик (форма без JS) лишається на старому
        редіректі — там фрагмент нікуди вставити.
        """
        if not _is_htmx(request):
            return RedirectResponse(
                f"/mail/{email.id}?error={quote(message)}", status_code=303
            )
        return templates.TemplateResponse(
            request, "_mail_detail_panel.html",
            _mail_panel_context(
                db, email, user,
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
        # Необовʼязкові; при прямому виклику функції (тести) пропущене поле —
        # обʼєкт Form, не рядок.
        sum3d_id=sum3d_id if isinstance(sum3d_id, str) else "",
        opak=opak if isinstance(opak, str) else "",
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


@router.post("/mail/accept-batch", response_class=HTMLResponse)
def accept_email_batch(
    request: Request,
    payload: str = Form(...),
    db: Session = Depends(get_db),
):
    """Конвеєр (блок B): прийняти кілька листів одним натиском.

    Тіло — JSON-масив рядків батч-таблиці; стан вибору живе ЛИШЕ в браузері
    (mail.js їх збирає в `payload`). Кожен лист приймається СВОЇМ `accept_letter`
    — під власним локом листа й у власній транзакції, тим самим кодом, що
    поодиноке прийняття. Помилка одного НЕ відкочує решту: успішні комітяться
    самі, невдалий лишається «нове» з текстом. Свідомий +1 роут (route_inventory).

    Синхронний `def` (threadpool), як `accept_email`: `accept_letter` робить
    блокуючі диск/таблицю, тож на event loop його пускати не можна (§14).
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Той самий гейт зомбі-фетчу, що на поодинокому прийнятті: покинутий фетч ще
    # качає у теку листа — рухати ті самі файли зараз означає дублі й мертві шляхи.
    busy = zombie_fetch_blocks_files()
    if busy:
        raise HTTPException(status_code=409, detail=busy)

    try:
        rows = json.loads(payload)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="некоректний payload батчу")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="порожній батч")

    results: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("email_id")
        if raw_id is None:
            continue
        try:
            eid = int(raw_id)
        except (TypeError, ValueError):
            continue
        email = db.get(EmailMessage, eid)
        display = ""
        if email is not None:
            display = (item.get("client_name") or "").strip() or (
                email.from_name or email.from_address or f"лист {eid}"
            )
        if email is None:
            results.append({"email_id": eid, "label": f"лист {eid}", "ok": False,
                            "error": "лист не знайдено"})
            continue
        if email.status != "нове":
            results.append({"email_id": eid, "label": display, "ok": False,
                            "error": "лист уже оброблено"})
            continue
        # Конвеєр бере ВСІ нерозібрані файли листа. Якщо серед них однакові
        # (клієнт надіслав роботу двічі) чи тезки з різним вмістом — мовчки
        # прийняти обидві копії означало б фрезерувати двічі, а мовчки викинути
        # одну — вирішити за оператора. Тому такий лист — лише через картку.
        dups = find_duplicates([
            a for a in email.attachments
            if a.order_id is None and Path(a.saved_path).exists()
        ])
        if dups:
            results.append({
                "email_id": eid, "label": display, "ok": False,
                "error": "у листі однакові файли (схоже, клієнт надіслав роботу двічі) — "
                         "відкрийте лист і оберіть, які брати",
            })
            continue
        result = accept_letter(
            db, user, email,
            client_name=(item.get("client_name") or ""),
            material_color=(item.get("material_color") or ""),
            kind=(item.get("kind") or ""),
            quantity=(item.get("quantity") or ""),
            folder_pick=(item.get("folder_pick") or ""),
            folder_new="", material_folder="",
            attachment_ids=[],
            accept_anyway=bool(item.get("accept_anyway")),
            sum3d_id=str(item.get("sum3d_id") or ""),
            opak=str(item.get("opak") or ""),
        )
        results.append({
            "email_id": eid,
            "label": display,
            "ok": result.ok,
            "error": result.error,
            "material_label": result.material_label,
            "saved_files": result.saved_files,
            "partial": result.partial,
        })

    accepted = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    response = templates.TemplateResponse(
        request, "_mail_batch_result.html",
        {"results": results, "accepted": accepted, "failed": failed},
    )
    # Клієнт (mail.js) прибирає прийняті рядки зі списку, знімає їх галочки й
    # оновлює дзеркало черги — за списком id у тригері.
    triggers: dict = {
        "mailBatchDone": {
            "accepted": [r["email_id"] for r in accepted],
            "failed": [r["email_id"] for r in failed],
        }
    }
    if accepted:
        n = len(accepted)
        word = "роботу" if n == 1 else ("роботи" if n < 5 else "робіт")
        msg = f"Прийнято {n} {word} в чергу"
        if failed:
            msg += f"; {len(failed)} не вдалося"
        triggers["toast"] = {"kind": "success" if not failed else "warning", "message": msg}
    # ensure_ascii=True (за замовчуванням): значення заголовка HTTP мусить бути
    # latin-1, а тости українською — інакше 500 на кодуванні (як у
    # _files_changed_response).
    response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


@router.post("/mail/{email_id}/move-processed", response_class=HTMLResponse)
def move_email_processed(
    request: Request, email_id: int, row: str = Form(""), db: Session = Depends(get_db)
):
    """Перемістити лист у налаштовану папку «оброблено» (робота пішла в цех).

    Перший ЗАПИС у скриньку — свідома кнопка оператора, не авто. Два викликачі,
    як у reject: РЯДОК списку (`row=1`, HTMX, hx-swap="delete" — хоче лише
    прибрати цей рядок) і КАРТКА листа (повний перехід/HX-Redirect). На невдачі
    поле в базі не ставиться, лист лишається в Inbox."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    folder = (get_setting(db, "mail_processed_folder") or "").strip()
    if not folder:
        raise HTTPException(
            status_code=409,
            detail="Папку «оброблено» не налаштовано — задайте її в Налаштуваннях пошти.",
        )
    if email.mailbox_folder:
        raise HTTPException(status_code=409, detail="лист уже переміщено")

    hx = (getattr(request, "headers", None) or {}).get("HX-Request") == "true"
    from_row = bool(row)
    try:
        move_message_to_folder(db, email, folder)
    except Exception as exc:  # noqa: BLE001 — текст будь-якої помилки IMAP у тост
        message = f"Не вдалося перемістити лист: {exc}"
        if from_row and hx:
            # Рядок лишається (htmx не свапає на 409); коротке пояснення в тілі.
            return HTMLResponse(message, status_code=409)
        request.session["toast_flash"] = {"kind": "error", "message": message}
        target = f"/mail?open={email.id}"
        if hx:
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    email.mailbox_folder = folder
    email.mailbox_moved_at = datetime.now()  # для вкладки «Оброблено за сьогодні»
    db.commit()
    # Рядок списку: тихо прибрати його (порожній 200 → hx-swap="delete"), без
    # тосту через сесію — зникнення рядка і є сигнал, як у ✕. Картка: тост +
    # перехід, щоб кнопка стала «переміщено».
    if from_row and hx:
        return HTMLResponse("", status_code=200)
    request.session["toast_flash"] = {
        "kind": "success", "message": f"Лист переміщено в «{folder}».",
    }
    target = f"/mail?open={email.id}"
    if hx:
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@router.post("/mail/{email_id}/move-to-inbox", response_class=HTMLResponse)
def move_email_to_inbox(
    request: Request, email_id: int, row: str = Form(""), db: Session = Depends(get_db)
):
    """Повернути перенесений лист із папки назад у Inbox (зворотна до
    move-processed дія). Два викликачі, як у move-processed: РЯДОК вкладки папки
    (`row=1`, прибрати рядок) і КАРТКА (перехід)."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    if not email.mailbox_folder:
        raise HTTPException(status_code=409, detail="лист не в папці — повертати нічого")

    hx = (getattr(request, "headers", None) or {}).get("HX-Request") == "true"
    from_row = bool(row)
    try:
        move_message_back_to_inbox(db, email)
    except Exception as exc:  # noqa: BLE001
        message = f"Не вдалося повернути лист: {exc}"
        if from_row and hx:
            return HTMLResponse(message, status_code=409)
        request.session["toast_flash"] = {"kind": "error", "message": message}
        target = f"/mail?view=processed&open={email.id}"
        if hx:
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    db.commit()
    if from_row and hx:
        return HTMLResponse("", status_code=200)
    request.session["toast_flash"] = {
        "kind": "success", "message": "Лист повернуто у «Вхідні».",
    }
    target = _mail_back_url(request, open_id=email.id)
    if hx:
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
    release_hold(email)
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


@router.post("/mail/{email_id}/hold")
def hold_email(
    request: Request,
    email_id: int,
    reason: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """«На уточнення» (власник 25.09.26): лист на паузі, поки адміністратори
    уточнюють у замовника. Лише стан CRM — скринька не змінюється. Оператор
    лишається у своїй вкладці; лист звідти зникає."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    error = put_on_hold(email, reason, note, user.username)
    if error:
        request.session["toast_flash"] = {"kind": "error", "message": error}
        return RedirectResponse(f"/mail?open={email.id}", status_code=303)
    db.commit()
    request.session["toast_flash"] = {
        "kind": "success",
        "message": f"Лист на уточненні: {hold_label(email)}",
    }
    return RedirectResponse(_mail_back_url(request), status_code=303)


@router.post("/mail/{email_id}/unhold")
def unhold_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Зняти паузу: лист повертається у «Вхідні». Оператор лишається у вкладці
    «На уточненні», якщо натиснув звідти."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    release_hold(email)
    db.commit()
    # Рядок вкладки (htmx, hx-swap="delete") — порожні 200, як у unfilter;
    # 303 на всю сторінку згодувався б свопу й стер список.
    headers = getattr(request, "headers", None) or {}
    if headers.get("HX-Request") == "true":
        return HTMLResponse("", status_code=200)
    request.session["toast_flash"] = {"kind": "success", "message": "Лист повернуто у «Вхідні»."}
    return RedirectResponse(_mail_back_url(request, open_id=email.id), status_code=303)


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
    # the card's «Повернути з фільтра» → stay on the tab it was pressed from.
    request_headers = getattr(request, "headers", None) or {}
    if request_headers.get("HX-Request") == "true":
        return HTMLResponse("", status_code=200)
    return RedirectResponse(_mail_back_url(request), status_code=303)


# Масові дії вкладок (власник 25.09.26: «виділити всі й щось зробити з ними
# всіма»). Кожна — та сама дія, що кнопка рядка, з ТИМИ САМИМИ гейтами, лише для
# кількох листів. Прийнятий лист (чи лист, з якого вже створено роботу) масово НЕ
# повертається і НЕ відхиляється: його «↩» видаляє живі роботи з черги — це лише
# поштучно, з підтвердженням (рішення власника). Галочка на такому рядку
# вимкнена, а сервер перевіряє ще раз — список міг застаріти.
_BULK_LABELS = {
    "reject": "Відхилено",
    "move_processed": "Перенесено в папку",
    "move_to": "Перенесено в папку",
    "unfilter": "Повернуто в чергу",
    "restore": "Повернуто в «Вхідні»",
    "to_inbox": "Повернуто у Вхідні",
    "return_inbox": "Повернуто у «Вхідні»",
    "unhold": "Повернуто у «Вхідні»",
    "hold": "На уточненні",
}


@router.post("/mail/bulk")
def bulk_mail_action(
    request: Request,
    action: str = Form(""),
    ids: str = Form(""),
    folder: str = Form(""),
    reason: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if action not in _BULK_LABELS:
        raise HTTPException(status_code=400, detail="невідома дія")

    picked = _parse_id_list(ids)
    emails = db.scalars(select(EmailMessage).where(EmailMessage.id.in_(picked))).all() if picked else []
    with_orders = set(
        db.scalars(
            select(Order.source_email_id).where(Order.source_email_id.in_(picked))
        ).all()
    ) if picked else set()

    done = 0
    skipped = 0
    errors: list[str] = []

    # Перенос у папку — ОДНИМ IMAP-входом на всі листи (move_messages_to_folder).
    # `move_processed` = та сама дія в папку «оброблено» з Налаштувань.
    if action in ("move_processed", "move_to"):
        if action == "move_processed":
            folder = get_setting(db, "mail_processed_folder") or ""
        folder = folder.strip()
        if not folder or folder.upper() == "INBOX":
            raise HTTPException(status_code=400, detail="не вибрано папку")
        movable = [e for e in emails if not e.mailbox_folder]
        skipped = len(emails) - len(movable)
        failed: dict[int, str] = {}
        if movable:
            try:
                failed = move_messages_to_folder(db, movable, folder)
            except Exception as exc:  # noqa: BLE001 — вхід в IMAP не вдався
                db.rollback()
                failed = {e.id: str(exc) for e in movable}
        moved_at = datetime.now()
        for email in movable:
            if email.id in failed:
                errors.append(failed[email.id])
                continue
            email.mailbox_folder = folder
            email.mailbox_moved_at = moved_at
            done += 1
        db.commit()
        emails = []  # решта циклу — для інших дій

    # «Покинули Вхідні» → назад у «Вхідні» (власник 25.09.26). Лист
    # шукається в скриньці (Вхідні або будь-яка папка) і, якщо треба,
    # переноситься — ОДНИМ IMAP-входом. Відхилений повертається в тріаж
    # («нове»), бо «Вхідні» показують лише нові. Прийнятий / з роботою —
    # пропуск, як у решті масових повернень. `inbox_returned_at` не дає синку
    # за віком одразу позначити лист «покинув» знову.
    if action == "return_inbox":
        returnable = [
            e for e in emails
            if not (e.status == "прийнято" or e.order_id or e.id in with_orders)
        ]
        skipped = len(emails) - len(returnable)
        failed = {}
        if returnable:
            try:
                failed = return_messages_to_inbox(db, returnable)
            except Exception as exc:  # noqa: BLE001 — вхід в IMAP не вдався
                db.rollback()
                failed = {e.id: str(exc) for e in returnable}
        now = datetime.now()
        for email in returnable:
            if email.id in failed:
                errors.append(failed[email.id])
                continue
            email.inbox_gone_at = None
            email.inbox_returned_at = now
            email.mailbox_folder = None
            email.mailbox_moved_at = None
            if email.status == "відхилено":
                email.status = "нове"
            done += 1
        db.commit()
        emails = []

    for email in emails:
        has_work = email.status == "прийнято" or bool(email.order_id) or email.id in with_orders
        if action == "reject":
            if has_work or email.status != "нове":
                skipped += 1
                continue
            email.status = "відхилено"
            release_hold(email)
        elif action == "hold":
            # Пункт «На уточнення» в меню «Перемістити» (власник 25.09.26) — та
            # сама пауза, що кнопка картки; лише стан CRM, скринька не
            # змінюється. Невірна причина — одна відмова на всю партію.
            if has_work:
                skipped += 1
                continue
            refusal = put_on_hold(email, reason, note, user.username)
            if refusal:
                errors.append(refusal)
                continue
        elif action == "unhold":
            # «На уточненні» → «Вхідні» (лише стан CRM, скринька не змінюється).
            if email.hold_at is None:
                skipped += 1
                continue
            release_hold(email)
        elif action == "unfilter":
            if not email.filter_category:
                skipped += 1
                continue
            email.filter_category = None
            email.filter_rule_id = None
        elif action == "restore":
            if has_work or email.status != "відхилено":
                skipped += 1
                continue
            email.status = "нове"
        elif action == "to_inbox":
            if has_work or not email.mailbox_folder:
                skipped += 1
                continue
            try:
                move_message_back_to_inbox(db, email)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                errors.append(str(exc))
                continue
        # Комітимо ПО ОДНОМУ: IMAP-переніс уже відбувся в скриньці, і збій на
        # наступному листі не має відкотити базу вже перенесених (інакше база
        # розійдеться зі скринькою).
        db.commit()
        done += 1

    label = f"Перенесено в «{folder}»" if action in ("move_processed", "move_to") else _BULK_LABELS[action]
    parts = [f"{label}: {done}"]
    if skipped:
        parts.append(
            f"пропущено {skipped} — прийняті або вже не в цьому стані"
            if action in ("reject", "restore", "to_inbox", "return_inbox")
            else f"пропущено {skipped}"
        )
    if errors:
        parts.append(f"не вдалося {len(errors)}: {errors[0]}")
    request.session["toast_flash"] = {
        "kind": "error" if errors and not done else "success",
        "message": " · ".join(parts),
    }
    return RedirectResponse(_mail_back_url(request), status_code=303)


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


def _unaccept_email(
    db: Session,
    email: EmailMessage,
    moved_out: list[tuple[Path, Path]] | None = None,
) -> list[tuple[Path, Path]]:
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
            Path(get_mail_attachments_path(db)),
            spool_folder_name(email.uid, email.uid_validity),
            old_paths,
        )
        moved_pairs = list(zip(old_paths, new_paths))
        # Викликачу — ОДРАЗУ після переносу, не лише з поверненням: помилка
        # бази нижче (FK, коміт) інакше лишала йому порожній список, і файли,
        # уже повернуті в спул, не верталися назад, хоча база відкотилась у
        # «файли в export» (той самий урок, що `moved_out` у прийнятті).
        if moved_out is not None:
            moved_out.extend(moved_pairs)
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

    # Спершу ЗНЯТИ всі посилання на роботи й зафіксувати це, лише потім
    # видаляти. `email.order_id` — голий FK без relationship, тож порядку
    # «UPDATE листа → DELETE роботи» unit-of-work не знає, а `db.delete` другої
    # роботи ліниво вантажить звʼязки й автофлашить DELETE першої, поки лист
    # ще на неї посилається → FOREIGN KEY constraint failed. З однією роботою
    # проскакувало; багатокольоровий лист (дві партії — дві роботи) не
    # відкочувався взагалі (бойовий прогін 25.09.26).
    email.order_id = None
    # Журнал дій (`ActionLog`) посилається на роботу без каскаду, і
    # `order_id` там nullable навмисно: рядок журналу «хто що зробив» мусить
    # пережити видалену роботу. Без цього відкат роботи, з якою оператор уже
    # щось робив (статус, Sum3D), падав на FK — і «↩» з папки «Скачано-
    # прошитано» не повертав лист у «Вхідні» (бойовий випадок 25.09.26).
    order_ids = [o.id for o in orders]
    if order_ids:
        db.execute(
            sa_update(ActionLog)
            .where(ActionLog.order_id.in_(order_ids))
            .values(order_id=None)
        )
    db.flush()
    for order in orders:
        db.delete(order)
    email.status = "нове"
    email.attachments_status = "ready"
    # Симетрія до прийняття: якщо accept переніс лист у папку «оброблено»,
    # відкат повертає його у Вхідні (і в скриньці, і в базі — `mailbox_folder`
    # знімається всередині move_message_back_to_inbox). Інакше лист завис би
    # «нове», але у вкладці «Оброблено». Best-effort: збій IMAP не має валити
    # відкат — файли й база вже повернені, лишиться слід у лозі.
    if email.mailbox_folder:
        try:
            move_message_back_to_inbox(db, email)
        except Exception:  # noqa: BLE001 — відкат важливіший за цей крок
            logger.exception(
                "Відкат листа %s: не вдалося повернути з папки у Вхідні", email.id
            )
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
        request.session["toast_flash"] = {"kind": "success", "message": "Лист повернуто в «Вхідні»."}
    elif email.status == "прийнято" or has_orders:
        # "прийнято" = fully accepted; a "нове" letter WITH orders = partially
        # accepted (some colours taken, more remain). Either way, undo every
        # order and put all files back — a clean restart of the whole letter.
        moved_pairs: list[tuple[Path, Path]] = []
        try:
            _unaccept_email(db, email, moved_out=moved_pairs)
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

    return RedirectResponse(_mail_back_url(request), status_code=303)
