"""Moves accepted mail attachments into the same client/batch/material tree
that the lab's export folder already uses (CLAUDE.md section 4), so the
morning-handout scanner (app/export_scanner.py) treats mail-sourced work the
same as lab drop-offs.

Runs at accept time (not at raw IMAP fetch) so the folder is named from the
operator-confirmed client name/material, not an unreviewed guess.
"""

from datetime import date
import hashlib
import re
import shutil
from pathlib import Path

from app.business_day import business_today
from app.client_matcher import match_client_name
from app.safe_names import avoid_reserved_device_name

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_NO_MATERIAL_NAME = "без_матеріалу"

# Гранична довжина ОДНОГО сегмента шляху. Дерево export має рівно три рівні
# (клієнт/дата/матеріал — CLAUDE.md §4, глибина зашита в app/export_scanner.py),
# і два з них приходять із тексту листа, тобто з довільного рядка. Збірка не
# `longPathAware`, тож шлях понад 260 символів Windows просто не створює.
# Найгірше не саме падіння, а його невидимість: `scan_export_client` ковтає
# `OSError` мовчки, тому тека не зʼявляється на видачі взагалі — коронка на
# диску лежить, а оператор її не бачить і сліду в логах немає.
# 60 × 3 сегменти ≈ 180 символів, решта ліміту — на корінь шари й імʼя файлу.
_MAX_SEGMENT_LEN = 60
# Хвіст-розрізнювач. Дві клініки з однаковим довгим початком назви (мережа,
# різні філії) після простого обрізання отримали б ОДНУ теку, і їхні роботи
# перемішалися б в одному лотку.
_DISCRIMINATOR_LEN = 6


def _batch_base_name(today: date) -> str:
    """The per-drop-off batch folder is named for the download date (dd.mm.yy),
    so the morning handout can orient by date instead of an opaque "нова папка".
    Same-day drop-offs reuse this folder (or a numbered sibling); a new day gets
    a fresh date folder.

    День тут РОБОЧИЙ (див. виклики нижче), і це не дрібниця: о 02:00 нічний
    оператор веде ще вчорашній день. З календарним днем його файли лягали б у
    теку «07.09.26», тоді як рядок-нотатка в таблиці — у вкладку «06.09.26»:
    видача шукала б роботу за одним днем, а файли лежали б під іншим.
    """
    return today.strftime("%d.%m.%y")


def _shorten_segment(name: str) -> str:
    """Обрізає задовгий сегмент, не втрачаючи його унікальності.

    Ріжемо по СИМВОЛАХ, а не по байтах: у кирилиці символ важить два байти, і
    байтове обрізання лишило б у назві половину літери — биту UTF-8, яку потім
    не зіставиш ні з чим. Якщо близько до межі є пробіл, ріжемо по ньому: у
    теці лишається ціле слово, а не огризок, і оператор упізнає клієнта.

    До хвоста додаємо короткий відбиток ПОВНОГО імені. Він мусить бути
    детермінований (`hashlib`, а не вбудований `hash()` з рандомним seed),
    інакше та сама клініка щозапуску діставала б нову теку, а історія її
    замовлень розсипалася б по export.
    """
    if len(name) <= _MAX_SEGMENT_LEN:
        return name
    digest = hashlib.blake2s(
        name.encode("utf-8"), digest_size=_DISCRIMINATOR_LEN // 2
    ).hexdigest()
    head_len = _MAX_SEGMENT_LEN - _DISCRIMINATOR_LEN - 1  # 1 — роздільник "~"
    head = name[:head_len]
    space = head.rfind(" ")
    if space >= head_len // 2:
        head = head[:space]
    # Windows мовчки відкидає кінцеві крапки й пробіли: створивши теку
    # "Клініка .~ab12cd", ми потім шукали б її під іншим імʼям, ніж на диску.
    head = head.rstrip(" .")
    return f"{head}~{digest}"


def sanitize_folder_name(name: str) -> str:
    name = _ILLEGAL_CHARS.sub("_", name).strip()
    # A dot-only component is meaningful to the filesystem even though it
    # contains none of Windows' forbidden filename characters.
    if name in {".", ".."}:
        return "без_імені"
    if not name:
        return "без_імені"
    # Обрізаємо ПІСЛЯ заміни заборонених символів (щоб відбиток рахувався від
    # остаточного тексту) і ДО перевірки device-імен: результат коротший за
    # межу, тож повторна санітизація нічого не змінить. Ідемпотентність тут
    # обовʼязкова — `_contained_child` санітизує вже санітизоване імʼя, і
    # «обрізання обрізаного» відводило б шлях від реальної теки.
    name = _shorten_segment(name)
    # A client literally named "AUX" (or a material folder "PRN") cannot be
    # created on Windows — same reserved-device rule as attachment filenames.
    return avoid_reserved_device_name(name)


def _contained_child(root: Path, name: str) -> Path:
    """Return a sanitized direct child and reject any root escape."""
    resolved_root = root.resolve()
    child = (resolved_root / sanitize_folder_name(name)).resolve()
    if child.parent != resolved_root:
        raise ValueError("небезпечне ім'я папки")
    return child


def _resolve_client_folder_name(
    export_root: Path, client_name: str, preferred_folder: str | None = None
) -> str:
    """Reuses an existing client folder if this client already has one.

    A client who sends a second (or fifth) request weeks later rarely types
    their own name identically each time (whitespace, a typo, "Іванов" vs
    the email address the first time) — without this, every retyped variant
    would fork off its own top-level folder and their order history would
    scatter across the export tree instead of accumulating as batches under
    one client. Reuses the same fuzzy-match this codebase already applies
    the other direction (app/client_matcher.py, sheet name -> export folder
    for Ранкова видача) — same threshold, so the two stay consistent about
    what counts as "the same client". Falls back to a freshly sanitized name
    when there's no confident existing match, which is also what creates the
    very first folder for a brand-new client.
    """
    try:
        existing_folders = sorted(p.name for p in export_root.iterdir() if p.is_dir())
    except (OSError, FileNotFoundError):
        existing_folders = []

    # Тека клієнта з картки / памʼяті відправника (app/client_folder.py) — ПЕРШОЮ,
    # але лише коли вона справді є на диску: перейменовану теку не вигадуємо,
    # а падаємо на нечітке зіставлення нижче.
    preferred = (preferred_folder or "").strip()
    if preferred and preferred in existing_folders:
        return preferred

    match = match_client_name(client_name, existing_folders, known_aliases={})
    if match.matched_folder_name:
        return match.matched_folder_name
    return sanitize_folder_name(client_name)


def _unique_material_folder(batch_dir: Path, material_name: str) -> Path:
    """Тека матеріалу в межах ОДНІЄЇ дата-теки дня.

    Раніше повтор того самого матеріалу за день плодив нову НУМЕРОВАНУ ДАТА-теку
    (`24.09.26 (2)`, `(3)`…), і клієнт з 15 роботами за день давав до 15 дата-тек
    — «дублікати» на око (скарга власника 24.09.26). Тепер дата-тека дня ОДНА, а
    нумерується саме підпапка МАТЕРІАЛУ: `mono a3`, `mono a3 (2)`, … Тобто 15
    робіт = 1 дата-тека з 15 підпапками.

    Це стало безпечним завдяки Частині A: кожна поштова робота знаходиться у
    видачі ПРЯМО через `Order.export_folder_path`, тож назва підпапки для видачі
    вже не критична (нечіткий збіг лишається тільки запаскою для legacy-робіт без
    прямого шляху). Глибина export незмінна — client/дата/матеріал.

    `material_name` уже санітизований (без роздільників); перша спроба йде через
    `_contained_child` (та сама гарантія від виходу за корінь), а суфікс ` (N)`
    додає лише цифри/пробіл/дужки — плоский join лишається всередині дата-теки,
    як і стара нумерація дата-тек.
    """
    candidate = _contained_child(batch_dir, material_name)
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        numbered = batch_dir / f"{material_name} ({n})"
        if not numbered.exists():
            return numbered
        n += 1


def list_client_folders(export_root: Path) -> list[str]:
    """Existing top-level client folder names under the export root, sorted.
    Feeds the accept wizard's "or pick an existing folder" override list."""
    try:
        return sorted(p.name for p in export_root.iterdir() if p.is_dir())
    except (OSError, FileNotFoundError):
        return []


def preview_export_target(
    export_root: Path,
    client_name: str,
    material_color: str,
    client_folder_override: str | None = None,
    material_folder_override: str | None = None,
    today: date | None = None,
    preferred_client_folder: str | None = None,
) -> dict:
    """Compute where save_attachments_to_export WOULD put this email's files,
    without touching the filesystem — drives the wizard's directory step so the
    operator confirms the path before committing. Mirrors the same resolver /
    batch-reuse / material-folder logic; keep the two in step."""
    base = _batch_base_name(today or business_today())
    override = (client_folder_override or "").strip()
    if override:
        client_folder = sanitize_folder_name(override)
    else:
        client_folder = _resolve_client_folder_name(
            export_root, client_name, preferred_client_folder
        )
    client_dir = _contained_child(export_root, client_folder)
    client_folder_existing = client_dir.is_dir()

    material_override = (material_folder_override or "").strip()
    material_folder = sanitize_folder_name(
        material_override or material_color or _NO_MATERIAL_NAME
    )
    # Дзеркало save_attachments_to_export: ОДНА дата-тека дня, повтор матеріалу
    # нумерує підпапку матеріалу (не дата-теку). `batch_reused` тепер = «дата-тека
    # дня вже є» (дописуємо в неї, а не створюємо першу).
    batch_dir = _contained_child(client_dir, base)
    batch_reused = batch_dir.is_dir()
    material_folder = _unique_material_folder(batch_dir, material_folder).name

    return {
        "client_folder": client_folder,
        "client_folder_existing": client_folder_existing,
        "batch_folder": base,
        "batch_reused": batch_reused,
        "material_folder": material_folder,
        "rel_path": f"{client_folder}/{base}/{material_folder}",
    }


def _move_file(source: Path, destination: Path) -> None:
    """shutil.move that never leaves a truncated file behind.

    Across volumes (spool on C:, export on a Synology UNC share) shutil.move is
    copy2 + unlink. A copy that dies halfway — network blip, full disk — leaves a
    partial file at `destination` that no rollback list knows about, and the
    morning handout would show it as real work. Delete the fragment, then let the
    caller's rollback run.
    """
    try:
        shutil.move(str(source), str(destination))
    except Exception:
        if destination.exists() and source.exists():
            # Source still there → the copy died mid-flight; the leftover at the
            # destination is a fragment, not the file. (If the source is already
            # gone the move completed and the failure came from elsewhere — then
            # the destination is the only copy and must NOT be touched.)
            try:
                destination.unlink()
            except OSError:
                pass
        raise


def undo_moves(moved: list[tuple[Path, Path]]) -> list[str]:
    """Compensate already-completed moves after the DB commit that was supposed
    to record them failed (files on disk, nothing in the database).

    Takes the (source, destination) pairs that actually moved and puts each file
    back at its original path. Returns human-readable errors instead of raising:
    the caller is already handling a failure and must report BOTH problems, never
    swallow this one silently (CLAUDE.md — a lost attachment has no paper trail).
    """
    errors: list[str] = []
    for source, destination in reversed(moved):
        try:
            if not destination.exists() or source.exists():
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            _move_file(destination, source)
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            errors.append(f"{destination}: {exc}")
    return errors


def restore_attachments_to_spool(
    attachments_root: Path, folder_name: str, current_paths: list[Path]
) -> list[Path]:
    """Move accepted files back from export to their original mail-spool folder
    (attachments_root/<folder_name>/, див. mail_spool.spool_folder_name) — the
    inverse of save_attachments_to_export, used
    when an accepted email is un-accepted. Returns the new spool paths in input
    order (a still-missing source keeps its computed destination so the caller
    can repoint saved_path anyway). Unique-renames on name collision and rolls
    back a partial move, mirroring save_attachments_to_export."""
    if not current_paths:
        return []
    spool_dir = _contained_child(attachments_root, folder_name)
    spool_dir.mkdir(parents=True, exist_ok=True)
    reserved: set[Path] = set()
    moves = [
        (source, _unique_destination(spool_dir, source.name, reserved))
        for source in current_paths
    ]
    completed: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            if source.is_file():
                _move_file(source, destination)
                completed.append((source, destination))
    except Exception:
        # Дзеркало відкату в `save_attachments_to_export`: помилки самого
        # відкату не ковтаємо. Файл, який не вдалося повернути, зависає між
        # export і спулом — у базі його вже немає, на диску ще є, і без цього
        # рядка про нього не дізнається ніхто (ревʼю 07.09.26, LOW).
        rollback_errors = []
        for source, destination in reversed(completed):
            try:
                if destination.exists():
                    _move_file(destination, source)
            except Exception as rollback_error:  # noqa: BLE001 — повідомляємо, не кидаємо
                rollback_errors.append(f"{destination}: {rollback_error}")
        if rollback_errors:
            raise OSError(
                "помилка повернення файлів і відкату: " + "; ".join(rollback_errors)
            )
        raise
    return [destination for _, destination in moves]


def _unique_destination(directory: Path, filename: str, reserved: set[Path]) -> Path:
    candidate = directory / filename
    stem = candidate.stem
    suffix = candidate.suffix
    number = 2
    while candidate.exists() or candidate in reserved:
        candidate = directory / f"{stem} ({number}){suffix}"
        number += 1
    reserved.add(candidate)
    return candidate


def save_attachments_to_export(
    export_root: Path,
    client_name: str,
    material_color: str,
    attachment_paths: list[Path],
    client_folder_override: str | None = None,
    material_folder_override: str | None = None,
    today: date | None = None,
    moved_out: list[tuple[Path, Path]] | None = None,
    preferred_client_folder: str | None = None,
) -> list[Path]:
    """Moves each file in attachment_paths into export_root/<client>/<date>/<material>/.

    The batch folder is named for the download date (dd.mm.yy). Same-day
    drop-offs for one client reuse that date folder as long as each new email
    brings a material it doesn't already have — several materials for the same
    client on the same day pile up inside one date folder. A second order for a
    material the day's folder already holds gets a numbered sibling ("17.08.26
    (2)"); a different day gets a fresh date folder. This mirrors how the
    operator physically drops off boxes, and lets the morning handout orient by
    date. The 3-level client/date/material structure keeps the exact depth
    app/export_scanner.py depends on.

    Returns the new paths in the same order as attachment_paths. Raises on
    filesystem errors (permission denied, unreachable network path, ...) —
    callers decide whether that should block acceptance or just be logged.

    `moved_out` — перелік файлів, які ЗАРАЗ фізично лежать в export. Він
    наповнюється одразу після кожного переносу і чиститься від тих, кого
    вдалося повернути. Викликач бачить його і тоді, коли ця функція кинула.

    Навіщо. Внутрішній відкат нижче повертає файли в спул — але якщо шара
    впала посеред переносу, він падає теж, і частина файлів лишається в export
    БЕЗ жодного сліду назовні. Раніше викликач дізнавався про перенесені файли
    лише з ПОВЕРНЕНОГО значення, тобто тільки при успіху: його власний
    `undo_moves` отримував порожній список, база відкочувалась у «лист не
    прийнято», а `saved_path` вказував у спул, де файлів уже не було. При
    повторному прийнятті ці вкладення тихо випадали зі списку — лист ставав
    «прийнято» без двох коронок (аудит 08.09.26).
    """
    if not attachment_paths:
        return []

    base = _batch_base_name(today or business_today())

    # The accept wizard's directory step lets the operator pin an exact client
    # folder (e.g. reuse "Vision Dental" when the fuzzy match would have made a
    # new "Vision"). An explicit override skips the fuzzy resolver but still
    # goes through _contained_child, so a crafted "../" name can't escape the
    # export root. Empty/whitespace override falls back to the auto resolver.
    override = (client_folder_override or "").strip()
    resolved_name = (
        sanitize_folder_name(override)
        if override
        else _resolve_client_folder_name(export_root, client_name, preferred_client_folder)
    )
    client_dir = _contained_child(export_root, resolved_name)
    material_override = (material_folder_override or "").strip()
    material_name = sanitize_folder_name(
        material_override or material_color or _NO_MATERIAL_NAME
    )

    # ОДНА дата-тека клієнта на день; повтор матеріалу нумерує МАТЕРІАЛ-підпапку,
    # а не плодить нові дата-теки (рішення власника 24.09.26). Див.
    # _unique_material_folder.
    batch_dir = _contained_child(client_dir, base)
    material_dir = _unique_material_folder(batch_dir, material_name)
    missing = [path for path in attachment_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"вкладення не знайдено: {missing[0]}")

    material_dir.mkdir(parents=True, exist_ok=True)
    reserved: set[Path] = set()
    moves = [
        (source, _unique_destination(material_dir, source.name, reserved))
        for source in attachment_paths
    ]
    completed: list[tuple[Path, Path]] = []

    def _remember(pair: tuple[Path, Path]) -> None:
        completed.append(pair)
        if moved_out is not None:
            moved_out.append(pair)

    def _forget(pair: tuple[Path, Path]) -> None:
        """Файл повернувся у спул — його більше немає в export."""
        if moved_out is not None and pair in moved_out:
            moved_out.remove(pair)

    try:
        for source, destination in moves:
            _move_file(source, destination)
            _remember((source, destination))
    except Exception:
        rollback_errors = []
        for source, destination in reversed(completed):
            try:
                if destination.exists():
                    _move_file(destination, source)
                # Знімаємо з переліку і тоді, коли файла в призначенні вже
                # немає: в export його однаково нема, а зайвий запис змусив би
                # викликача «повертати» неіснуючий файл.
                _forget((source, destination))
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise OSError("помилка перенесення і відкату: " + "; ".join(rollback_errors))
        raise

    return [destination for _, destination in moves]
