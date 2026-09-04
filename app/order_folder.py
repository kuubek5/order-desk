"""Resolves the on-disk export folder for an email-sourced Order, so the
queue screen (screen 1, CLAUDE.md section 9) can offer the operator a
`file://` link straight to the saved attachments.

Deliberately client-side (`file://` href, opened by the operator's own
browser/Explorer) rather than a server-side `os.startfile()` call — the
server may run on Proxmox/Linux while the operator sits at a Windows PC on
the network (CLAUDE.md section 6/11), same reasoning as the Sum3D
clipboard-copy button (level 1, no server-side app launch).

`Order` has no direct link to `Attachment`; the path is
Order <- EmailMessage.order_id, EmailMessage.attachments -> Attachment.saved_path
(app/mail_export.py writes saved_path when the email is accepted).
"""

import logging
import os
import threading
import time
from pathlib import Path, PureWindowsPath

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import MAIL_ATTACHMENTS_PATH
from app.models import Attachment, EmailMessage, Order
from app.settings_store import get_export_folder_path, get_technician_files_path
from app.stl_preview import (
    build_preview_token,
    build_preview_token_for_known_child,
    build_preview_token_lexical,
    validate_preview_roots,
)


logger = logging.getLogger(__name__)

# ── Перелік тек техніків: ОДИН обхід замість stat на кожен рядок ────────────
# Виміряно 03.09.26 (лічильник викликів, scratchpad/count_fs.py): рендер черги
# на 200 рядків робив **600 звернень до диска** — по 3 на рядок (два resolve()
# і один is_dir() у resolve_job_code_folder). Тека техніків лежить на
# МЕРЕЖЕВІЙ шарі, де кожне звернення — окремий round-trip: при 3 мс це 1.8 с,
# при 10 мс (Synology під навантаженням) — 6 с. І це ще не все: той самий
# код виконується не лише на відкритті сторінки, а й у поллі КОЖНІ 15 СЕКУНД,
# тобто застосунок забивав шару сам собі (звідси й розкид прогріву 2.1-7.1 с
# на однаковій роботі).
#
# Лікування не в мікрооптимізації, а в зміні порядку: назви тек у корені
# читаються ОДНИМ обходом і лежать у памʼяті, а перевірка «чи є тека для цього
# job_code» стає пошуком у множині — нуль звернень до мережі на рядок.
#
# TTL коротка: технік кладе теку посеред зміни, і рядок мусить стати «можна
# брати» без перезавантаження застосунку. 30 с — це два тіки полла черги.
_TECH_LISTING_TTL_SECONDS = 30.0
_tech_listing: dict[str, tuple[float, Path, frozenset[str]]] = {}
_tech_listing_lock = threading.Lock()
# Один скан на всіх (single-flight). Обхід шари коштує 4-5 с (виміряно
# /diag/perf 04.09.26), і без цього три вкладки, відкриті поспіль, давали ТРИ
# паралельні скани по 5 с — «стадо». Тепер перший потік сканує, решта чекає на
# ЙОГО результат під цим локом і читає вже теплий кеш, а не стукає в шару знову.
_tech_scan_locks: dict[str, threading.Lock] = {}
_tech_scan_locks_guard = threading.Lock()


def _scan_lock_for(path: str) -> threading.Lock:
    with _tech_scan_locks_guard:
        lock = _tech_scan_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _tech_scan_locks[path] = lock
        return lock


def _tech_root_children(technician_files_path: str) -> tuple[Path, frozenset[str]]:
    """(корінь, назви тек у ньому). Недосяжна шара дає ПОРОЖНЮ множину імен.

    Помилка обходу — це не виняток: шара може бути тимчасово недосяжна, і
    черга мусить намалюватись без посилань на теки, а не впасти. Порожня
    множина каже це саме, і каже однаково на будь-якому виклику."""
    now = time.monotonic()
    with _tech_listing_lock:
        hit = _tech_listing.get(technician_files_path)
        if hit is not None and now - hit[0] < _TECH_LISTING_TTL_SECONDS:
            return hit[1], hit[2]

    # Кеш протух або порожній — сканує ЛИШЕ один потік на шлях. Решта чекає тут
    # і за мить нижче знайде свіжий кеш (подвійна перевірка), а не скане вдруге.
    with _scan_lock_for(technician_files_path):
        now = time.monotonic()
        with _tech_listing_lock:
            hit = _tech_listing.get(technician_files_path)
            if hit is not None and now - hit[0] < _TECH_LISTING_TTL_SECONDS:
                return hit[1], hit[2]
        return _scan_tech_root(technician_files_path, now)


def _scan_tech_root(technician_files_path: str, now: float) -> tuple[Path, frozenset[str]]:
    try:
        root = Path(technician_files_path).resolve()
        # os.scandir, а НЕ Path.iterdir(): scandir віддає ознаку «це тека»
        # прямо із запису каталогу, тоді як iterdir()+is_dir() робить окремий
        # stat на КОЖЕН запис — на шарі з сотнями тек техніків це сотні зайвих
        # round-trip'ів (виміряно: 80 тек = 80 stat'ів проти нуля).
        with os.scandir(root) as it:
            names = frozenset(entry.name for entry in it if entry.is_dir())
    except (OSError, RuntimeError):
        logger.warning("Тека техніків недоступна: %s", technician_files_path)
        with _tech_listing_lock:
            # Порожній перелік теж кешуємо: інакше кожен рядок кожного полла
            # знову стукав би в мертву шару й чекав на її таймаут. Корінь тут
            # НЕ розвʼязаний (resolve і впав) — але з порожньою множиною імен
            # ним ніхто не скористається: жоден job_code не пройде перевірку
            # `name in names`. Кладемо саме кортеж, а не None, щоб стан «шара
            # мовчить» виглядав однаково і на першому виклику, і на наступних
            # 30 секундах — інакше та сама ситуація давала б різні типи.
            _tech_listing[technician_files_path] = (
                now, Path(technician_files_path), frozenset()
            )
        return Path(technician_files_path), frozenset()

    with _tech_listing_lock:
        _tech_listing[technician_files_path] = (now, root, names)
    return root, names


def _is_link(path: Path) -> bool:
    """Treat both regular symlinks and Windows junctions as links."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def warm_tech_listing(technician_files_path: str) -> int:
    """ПРИМУСОВО оновити кеш переліку тек техніків — для фонового грійника.

    Саме примусово, а не через `_tech_root_children`: той на 20-секундному
    інтервалі грійника знайшов би кеш іще теплим (TTL 30с) і не оновив би
    нічого. Грійник має тримати кеш свіжим САМ, щоб оператор завжди читав
    готове (~0с) замість 4-5с холодного скану Synology (заміряно /diag/perf
    04.09.26). Один скан на всіх (той самий лок) — грійник не б'ється з
    запитом оператора. Порожній шлях — нічого не робимо."""
    if not technician_files_path:
        return 0
    with _scan_lock_for(technician_files_path):
        _, names = _scan_tech_root(technician_files_path, time.monotonic())
    return len(names)


def resolve_email_attachment_folder(
    attachments: list[Attachment], trusted_roots: list[Path]
) -> Path | None:
    """Return a safe, existing attachment directory under a trusted root.

    Attachment paths originate in email metadata/database state and are never
    passed through by the browser.  Reject relative paths, traversal, links,
    missing files, and paths escaping either the private mail spool or the
    configured export root.
    """
    roots: list[tuple[Path, Path]] = []
    for root_value in trusted_roots:
        root = Path(root_value)
        try:
            lexical_root = root.absolute()
            if _is_link(lexical_root):
                continue
            resolved_root = lexical_root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved_root.is_dir():
            roots.append((lexical_root, resolved_root))

    for attachment in attachments:
        raw = Path(attachment.saved_path)
        if not raw.is_absolute() or ".." in raw.parts:
            continue

        lexical_file = raw.absolute()
        for lexical_root, resolved_root in roots:
            try:
                relative = lexical_file.relative_to(lexical_root)
            except ValueError:
                continue

            current = lexical_root
            link_found = False
            for part in relative.parts:
                current = current / part
                try:
                    if _is_link(current):
                        link_found = True
                        break
                except OSError:
                    link_found = True
                    break
            if link_found:
                continue

            try:
                resolved_file = lexical_file.resolve(strict=True)
                resolved_file.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                continue
            # Return the lexical (drive-letter) form, not resolved_file —
            # resolve(strict=True) above is only a safety check (catches
            # symlink/junction escapes and confirms the target really exists);
            # on a mapped network drive it can rewrite the path to a UNC form
            # (e.g. P:\... -> \\host\share\...), which then fails to compare
            # against the lexical roots callers pass to build_preview_token.
            if lexical_file.is_file() and lexical_file.parent.is_dir():
                return lexical_file.parent

        # Fallback for a MAPPED NETWORK DRIVE where the stored path and the
        # trusted root disagree on form — e.g. saved_path is the UNC
        # "\\host\share\...\mail_attachments\14\file" while the root is the
        # "P:\...\mail_attachments" drive letter (or vice versa). Lexical
        # relative_to can't see they're the same place, so compare the fully
        # resolved canonical forms: resolve(strict=True) has already followed
        # every symlink/junction, so containment under a resolved root is safe.
        try:
            resolved_file = lexical_file.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        for _lexical_root, resolved_root in roots:
            try:
                resolved_file.relative_to(resolved_root)
            except ValueError:
                continue
            if resolved_file.is_file() and resolved_file.parent.is_dir():
                return resolved_file.parent

    return None


def attach_email_folder_availability(
    emails: list[EmailMessage], trusted_roots: list[Path]
) -> None:
    """Annotate pending messages for queue rendering without exposing paths."""
    for email in emails:
        email.folder_available = (
            resolve_email_attachment_folder(email.attachments, trusted_roots) is not None
        )


def attach_email_preview_tokens(
    emails: list[EmailMessage], trusted_roots: list[Path], preview_roots: dict[str, str | None]
) -> None:
    """Sets a transient `stl_preview_token` (str | None) on each email so
    mail_triage.html/_pending_mail_row.html/mail_detail.html can offer the
    same hover STL preview as the queue/handout folder links (app/stl_preview.py).

    `trusted_roots` decides WHICH folder (mail_attachments pre-accept, export
    post-accept) actually holds the files; `preview_roots` is the root_key ->
    path map build_preview_token needs to encode that folder into a token.
    """
    for email in emails:
        folder = resolve_email_attachment_folder(email.attachments, trusted_roots)
        email.stl_preview_token = (
            build_preview_token(folder, preview_roots) if folder is not None else None
        )


def pick_existing_attachment_folder(attachments: list[Attachment]) -> Path | None:
    """Returns the parent directory of the first attachment whose saved_path
    still exists on disk, or None if none of them do (e.g. moved/deleted
    since acceptance)."""
    for attachment in attachments:
        path = Path(attachment.saved_path)
        if path.exists():
            return path.parent
    return None


def folder_to_file_uri(folder: Path | None) -> str | None:
    """Converts a filesystem directory into a ready-to-use `file://` URI.

    Uses Path.as_uri() (which handles backslashes, spaces, and non-ASCII
    characters correctly) instead of hand-building the string in a template.
    """
    if folder is None:
        return None
    return folder.resolve().as_uri()


def attach_export_folder_uris(db: Session, orders: list[Order]) -> None:
    """Sets transient `export_folder_uri` and `export_folder_preview_token`
    attributes (str | None, not mapped columns) on every Order in `orders`.

    Runs a single batched query for all email-sourced orders in the list
    (plus one eager-loaded attachments fetch) instead of querying per row,
    so this stays cheap on the queue screen where many rows render at once.
    Call again after any operation that changes an order's status/fields if
    that order gets re-rendered (_order_row.html reads these attributes).

    `export_folder_uri` stays a client-resolved `file://` link (see module
    docstring). `export_folder_preview_token` is the separate, server-side
    validated token the hover STL preview route accepts — the attachment
    folder is checked against both the export root and the mail spool root,
    since a not-yet-moved attachment can still live under the mail spool.
    """
    for order in orders:
        order.export_folder_uri = None
        order.export_folder_preview_token = None

    email_order_ids = [order.id for order in orders if order.source == "email"]
    if not email_order_ids:
        return

    emails = db.scalars(
        select(EmailMessage)
        .where(EmailMessage.order_id.in_(email_order_ids))
        .options(selectinload(EmailMessage.attachments))
    ).all()

    preview_roots = {
        "export": get_export_folder_path(db),
        "mail": str(MAIL_ATTACHMENTS_PATH),
    }

    # Корені перевіряються ОДИН раз на пакет, а не на кожну роботу: це та сама
    # перевірка, просто не помножена на кількість рядків. Виміряно 03.09.26 —
    # без цього одна поштова робота коштувала 18 звернень до мережевої шари.
    validated_roots = validate_preview_roots(preview_roots)

    uri_by_order_id: dict[int, str] = {}
    token_by_order_id: dict[int, str] = {}
    for email in emails:
        if email.order_id is None:
            continue
        folder = pick_existing_attachment_folder(email.attachments)
        uri = folder_to_file_uri(folder)
        if uri is not None:
            uri_by_order_id[email.order_id] = uri
        if folder is not None:
            token = build_preview_token_lexical(
                folder, preview_roots, validated_roots
            )
            if token is not None:
                token_by_order_id[email.order_id] = token

    for order in orders:
        if order.id in uri_by_order_id:
            order.export_folder_uri = uri_by_order_id[order.id]
        if order.id in token_by_order_id:
            order.export_folder_preview_token = token_by_order_id[order.id]


def _job_code_segment(job_code: str | None) -> str | None:
    """`job_code` як ОДНА назва теки — або None, якщо назвою бути не може.

    `job_code` приходить зі спільної Google-таблиці, тобто ззовні. Трактуємо
    його виключно як ім'я однієї теки, ніколи як шлях. PureWindowsPath
    потрібен навіть на Linux-розгортанні, щоб зловити диск/UNC.

    Спільне для обох входів — пакетного (attach_job_code_folder_uris) і
    поодинокого (resolve_job_code_folder). Роздвоїти ці правила означало б
    мати два різні уявлення про те, що безпечно, і одне з них рано чи пізно
    відстало б."""
    if not job_code:
        return None
    clean = job_code.strip()
    if (
        not clean
        or clean in {".", ".."}
        or "/" in clean
        or "\\" in clean
        or Path(clean).is_absolute()
        or PureWindowsPath(clean).is_absolute()
    ):
        return None
    return clean


def resolve_job_code_folder(technician_files_path: str | None, job_code: str | None) -> Path | None:
    """Resolves the on-disk directory a lab technician's CAM export lands in
    for a given `Order.job_code`.

    `job_code` (CLAUDE.md section 3, "Номер роботи", e.g.
    `2026-07-21_00016-007`) turned out not to be a bare identifier but the
    path *segment* under the technician-files root (Налаштування screen,
    app/settings_store.py::get_technician_files_path) where the technician
    drops files once their part of the job is done — i.e. the full path is
    `Path(technician_files_path) / job_code`.

    Returns None (silent degradation, no exception) when either piece is
    missing or the resulting directory doesn't actually exist on disk — e.g.
    the setting isn't configured yet, or the technician hasn't dropped files
    for this job yet.
    """
    if not technician_files_path:
        return None

    clean_job_code = _job_code_segment(job_code)
    if clean_job_code is None:
        return None

    root = Path(technician_files_path).resolve()
    folder = (root / clean_job_code).resolve()
    if folder.parent != root or not folder.is_dir():
        return None
    return folder


def attach_job_code_folder_uris(db: Session, orders: list[Order]) -> None:
    """Sets transient `job_code_folder_uri` and `job_code_folder_preview_token`
    attributes (str | None, not mapped columns) on every Order in `orders`,
    mirroring `attach_export_folder_uris` above but for the technician-files
    root instead of the email export folder.

    Reads the `technician_files_path` setting once for the whole batch
    (instead of per row) since it's the same for every order — only the
    per-order `Path.exists()` check varies.
    """
    for order in orders:
        order.job_code_folder_uri = None
        order.job_code_folder_preview_token = None

    technician_files_path = get_technician_files_path(db)
    if not technician_files_path:
        return

    # ОДИН обхід кореня на весь пакет — далі жодного звернення до мережі на
    # рядок (див. коментар до _tech_root_children). Раніше тут стояв виклик
    # resolve_job_code_folder на кожну роботу, тобто три round-trip'и на SMB
    # помножені на кількість рядків, ще й у поллі кожні 15 с.
    root, names = _tech_root_children(technician_files_path)
    if not names:
        return          # шара мовчить або тека порожня — посилань просто немає

    for order in orders:
        name = _job_code_segment(order.job_code)
        if name is None or name not in names:
            continue
        # root уже розвʼязаний, а name — простий сегмент із самого переліку,
        # тож шлях коректний без повторного resolve() (це була б ще одна
        # ходка на мережу на кожен рядок).
        order.job_code_folder_uri = (root / name).as_uri()
        # Належність кореню вже доведена обходом — токен збирається без
        # повторної перевірки диском (див. build_preview_token_for_known_child).
        token = build_preview_token_for_known_child("tech", name)
        if token is not None:
            order.job_code_folder_preview_token = token
