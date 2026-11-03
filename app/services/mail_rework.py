"""«Прийняти як переробку» — лист, повернутий у Вхідні на переробку (03.11.26).

Адміністратори повертають у «Вхідні» лист клієнта, чию роботу вже
відфрезерували й видали, і просять переробити (лист 2319, Oleksandr). Досі CRM
показувала «↺ повернуто», але зробити не давала нічого: прийняти вдруге не
можна (лист «прийнято», файли прив'язані до першої роботи), а відкат прийняття
для виданої роботи неможливий — і правильно, бо знищив би історію першого
фрезерування.

Рішення власника:
  * переробка з пошти — НОВА звичайна робота (`source="email"`, той самий
    лист) у вкладці дня, у ЗАГАЛЬНУ кількість одиниць: без винуватця, без
    `ReworkRecord` і колонок БРАК, без «Який раз» (виробіток не рахує
    `mill_count` ≥ 2, `vyrobitok._is_rework`);
  * у таблиці й для CAM її видно з коментаря «переробка»;
  * файли першої роботи КОПІЮЮТЬСЯ в теку сьогоднішнього дня
    (`mail_export.copy_files_to_export`), тека закріплюється за новою роботою
    (`export_folder_path`), тож видача знаходить саме її;
  * перша робота не змінюється ніяк — ні статус, ні рядок, ні тека, ні
    історія, ні вкладення листа (вони лишаються за нею).

Позначка «це переробка» — рядок журналу дій (`ActionLog`, поле
`REWORK_FIELD`, у `new_value` — id першої роботи). Вона потрібна двом: формі
(джерелом файлів знову береться перша робота, а не попередня переробка — у
переробки вкладень листа немає) і відкату прийняття (лист із переробкою не
відкочується, інакше копії лишились би в export нічиїми).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import perf
from app.client_folder import learn_client_folder, preferred_client_folder
from app.export_scanner import clear_export_cache
from app.fs_probe import PRESENT, UNREACHABLE, path_state
from app.mail_export import copy_files_to_export, preview_export_target, remove_copies
from app.material_catalog import (
    ensure_seeded,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.models import ActionLog, Attachment, EmailMessage, Order, StatusEvent, SyncLog
from app.sender_memory import lookup_sender
from app.services.mail_accept import (
    _letter_lock,
    _resolve_target_tab_warm,
    stamp_accept_sum3d,
    schedule_folder_move,
    write_letter_row,
)
from app.services.opak import format_opak, opak_units
from app.services.undo import log_action
from app.settings_store import get_export_folder_path
from app.statuses import STATUS_NEW

logger = logging.getLogger(__name__)

# Поле рядка журналу дій, яким позначена робота-переробка з листа.
REWORK_FIELD = "rework_of"
# Слово в «Коментар для CAM-оператора» — так переробку бачать таблиця й CAM.
REWORK_MARK = "переробка"


@dataclass
class ReworkResult:
    """Що сталося — усе, що потрібно роуту для відповіді. `error` непорожній:
    нічого не змінено (або відкочено разом із копіями)."""

    error: str | None = None
    order: Order | None = None
    copied_files: int = 0
    skipped_files: int = 0
    target: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def can_rework(email: EmailMessage) -> bool:
    """Лист прийнятий і повернутий у Вхідні прямо в пошті (стан «↺ повернуто»,
    `app/mail_inbox.py::reopened`)."""
    return email.status == "прийнято" and email.reopened_at is not None


def rework_order_ids(db: Session, order_ids) -> set[int]:
    """Котрі з цих робіт — переробки з листа (див. докстрінг модуля)."""
    ids = [i for i in order_ids if i is not None]
    if not ids:
        return set()
    return {
        order_id
        for order_id in db.scalars(
            select(ActionLog.order_id).where(
                ActionLog.field == REWORK_FIELD,
                ActionLog.order_id.in_(ids),
            )
        )
        if order_id is not None
    }


def source_orders(db: Session, email: EmailMessage) -> list[Order]:
    """Роботи листа, які можна переробити: ті, що тримають його файли
    (вкладення з `order_id`; зведена робота Конвеєра — теж), та, на яку
    вказує лист, і роботи з `source_email_id` листа — БЕЗ попередніх
    переробок. Перша — найстарша."""
    ids = {a.order_id for a in email.attachments if a.order_id is not None}
    if email.order_id is not None:
        ids.add(email.order_id)
    ids.update(db.scalars(select(Order.id).where(Order.source_email_id == email.id)))
    if not ids:
        return []
    reworks = rework_order_ids(db, ids)
    return list(db.scalars(
        select(Order).where(Order.id.in_(ids - reworks)).order_by(Order.id)
    ))


def _files_of(email: EmailMessage, source: Order | None) -> list[Attachment]:
    """Файли ЦЬОГО листа в першій роботі. У зведеної роботи Конвеєра там і
    файли інших листів — їх не беремо: переробляють за цим листом."""
    if source is None:
        return []
    return [a for a in email.attachments if a.order_id == source.id]


def _client_folder_of(source: Order | None) -> str | None:
    rel = (getattr(source, "export_folder_path", None) or "").strip().strip("/")
    return rel.split("/", 1)[0] if rel else None


def _preferred_folder(db: Session, email: EmailMessage, source: Order | None, client_name: str) -> str | None:
    """Тека клієнта для копії: та, де лежить перша робота, поки клієнт той
    самий (ім'я в таблиці й тека часто різняться — нечіткий збіг може не
    впізнати); інший клієнт — як у прийнятті (картка, памʼять відправника)."""
    same_client = source is not None and (
        (source.client_name or "").strip().casefold() == client_name.strip().casefold()
    )
    if same_client:
        folder = _client_folder_of(source)
        if folder:
            return folder
    return preferred_client_folder(db, client_name or None, lookup_sender(db, email))


def _opak_text(order: Order | None) -> str:
    units = getattr(order, "opak_units", None)
    return str(units) if units else ""


def rework_comment(opak: str) -> str:
    """«переробка» + опак, як його пише прийняття («2 opaq»)."""
    return ", ".join(part for part in (REWORK_MARK, format_opak(opak)) if part)


def rework_form(
    db: Session,
    email: EmailMessage,
    *,
    on_disk: set[int],
    values: dict | None = None,
    error: str | None = None,
) -> dict | None:
    """Контекст форми «Прийняти як переробку» для картки листа, або None,
    коли лист не повернуто. `values` — те, що оператор уже вписав (невдала
    спроба), інакше поля заповнюються з першої роботи; Sum3D — порожній."""
    if not can_rework(email):
        return None
    values = values or {}
    sources = source_orders(db, email)
    wanted = str(values.get("source_order_id") or "").strip()
    source = next((o for o in sources if str(o.id) == wanted), sources[0] if sources else None)

    def _value(key: str, default: str) -> str:
        value = values.get(key)
        return default if value is None else str(value)

    def _target(order: Order | None, client: str, material: str) -> str | None:
        try:
            preview = preview_export_target(
                Path(get_export_folder_path(db)), client.strip(), material.strip(),
                preferred_client_folder=_preferred_folder(db, email, order, client),
            )
        except (OSError, ValueError) as exc:
            logger.warning("Переробка листа %s: теку export не прочитано: %s", email.id, exc)
            return None
        return preview.get("rel_path") or None

    def _choice(order: Order) -> dict:
        """Що підставляє вибір цієї роботи: її поля, файли й тека копії."""
        files = _files_of(email, order)
        return {
            "order": order,
            "client_name": order.client_name or "",
            "material_color": order.material_color or "",
            "quantity": order.quantity or "",
            "opak": _opak_text(order),
            "files": sum(1 for a in files if a.id in on_disk),
            "missing": sum(1 for a in files if a.id not in on_disk),
            "target": _target(order, order.client_name or "", order.material_color or ""),
        }

    client_name = _value("client_name", (source.client_name if source else "") or "")
    material_color = _value("material_color", (source.material_color if source else "") or "")
    files = _files_of(email, source)
    return {
        "sources": sources,
        "source": source,
        # Вибір роботи в багатокольоровому листі підставляє її поля й теку
        # (mail.js), тож кожна несе свої — рядок шляху не бреше після вибору.
        "choices": [_choice(o) for o in sources] if len(sources) > 1 else [],
        "client_name": client_name,
        "material_color": material_color,
        "quantity": _value("quantity", (source.quantity if source else "") or ""),
        "opak": _value("opak", _opak_text(source)),
        "sum3d_id": _value("sum3d_id", ""),
        "files": sum(1 for a in files if a.id in on_disk),
        "missing": sum(1 for a in files if a.id not in on_disk),
        "target": _target(source, client_name, material_color),
        "error": error,
    }


def accept_rework(
    db: Session,
    user,
    email: EmailMessage,
    *,
    source_order_id: str = "",
    client_name: str = "",
    material_color: str = "",
    quantity: str = "",
    opak: str = "",
    sum3d_id: str = "",
    accept_anyway: bool = False,
) -> ReworkResult:
    """Створити переробку з поверненого листа (див. докстрінг модуля).

    Під тим самим локом листа, що прийняття, відкат і фоновий перенос у папку
    скриньки: двоє операторів, що натиснули одночасно, не створять дві
    переробки. Лист у папку «оброблено» — у фоні, ПІСЛЯ зняття локу (фон бере
    той самий лок)."""
    lock = _letter_lock(email.id)
    if not lock.acquire(blocking=False):
        return ReworkResult(
            error="Цей лист саме обробляє інший оператор — оновіть сторінку за мить"
        )
    try:
        result = _accept_rework_locked(
            db, user, email,
            source_order_id=source_order_id, client_name=client_name,
            material_color=material_color, quantity=quantity, opak=opak,
            sum3d_id=sum3d_id, accept_anyway=accept_anyway,
        )
    finally:
        lock.release()
    if result.ok:
        schedule_folder_move(db, email.id, getattr(user, "username", None))
    return result


def _accept_rework_locked(
    db: Session,
    user,
    email: EmailMessage,
    *,
    source_order_id: str,
    client_name: str,
    material_color: str,
    quantity: str,
    opak: str,
    sum3d_id: str,
    accept_anyway: bool,
) -> ReworkResult:
    # Перечитати ПІД локом: другий оператор, що відкрив картку раніше, інакше
    # бачив би лист ще повернутим і створив би другу переробку.
    db.refresh(email)
    if not can_rework(email):
        return ReworkResult(
            error="Лист не повернуто у Вхідні на переробку — оновіть сторінку"
        )
    client_name = (client_name or "").strip()
    if not client_name:
        return ReworkResult(error="Впишіть клієнта")

    sources = source_orders(db, email)
    wanted = (source_order_id or "").strip()
    if wanted:
        source = next((o for o in sources if str(o.id) == wanted), None)
        if source is None:
            return ReworkResult(error="Цієї роботи серед робіт листа немає — оновіть сторінку")
    else:
        source = sources[0] if sources else None

    files = _files_of(email, source)
    present: list[Attachment] = []
    missing: list[Attachment] = []
    for attachment in files:
        state = path_state(Path(attachment.saved_path))
        if state == UNREACHABLE:
            # «Не бачу» ≠ «немає» (app/fs_probe.py): шара моргнула — без
            # файлів переробку не створюємо мовчки.
            return ReworkResult(
                error=f"Тека з файлами першої роботи зараз недоступна ({attachment.saved_path}) — спробуйте за хвилину"
            )
        (present if state == PRESENT else missing).append(attachment)
    if missing and not accept_anyway:
        return ReworkResult(
            error=(
                f"{len(missing)} файл(ів) першої роботи немає на диску — "
                "підтвердіть переробку без них або поверніть файли"
            ),
            skipped_files=len(missing),
        )

    with perf.span("rework:tab"):
        target_tab, tab_found = _resolve_target_tab_warm(db, email)

    order = Order(
        source="email",
        sheet_tab=target_tab,
        row_number=None,
        client_name=client_name,
        material_color=(material_color or "").strip() or None,
        kind=source.kind if source is not None else None,
        quantity=(quantity or "").strip() or None,
        status=STATUS_NEW,
        source_email_id=email.id,
        # Чекає рядок-нотатку з першого коміту — як прийняття (`mail_row_retry`).
        sheet_row_pending=datetime.now(),
    )
    stamp_accept_sum3d(order, user, sum3d_id)
    order.cam_comment = rework_comment(opak)
    order.opak_units = opak_units(order.cam_comment)
    ensure_seeded(db)
    order.material_id = resolve_material_id(
        order.material_color, load_alias_rows(db), material_id_by_name(db)
    )
    db.add(order)
    db.flush()

    first_label = f"#{source.id}" if source is not None else "без першої роботи"
    db.add(StatusEvent(
        order_id=order.id, operator_id=user.id, status=order.status,
        actor=user.username, note=f"переробка з листа (перша робота {first_label})",
    ))
    log_action(
        db, order=order, operator=user, action_type="create",
        field=REWORK_FIELD, new=source.id if source is not None else None,
        note=f"переробка з листа: {client_name}, перша робота {first_label}",
    )

    copied: list[Path] = []
    target: str | None = None
    try:
        if present:
            export_root = Path(get_export_folder_path(db))
            with perf.span("rework:files"):
                new_paths = copy_files_to_export(
                    export_root, client_name, order.material_color or "",
                    [Path(a.saved_path) for a in present],
                    preferred_client_folder=_preferred_folder(db, email, source, client_name),
                    copied_out=copied,
                )
            clear_export_cache()
            rel = _relative(new_paths[0].parent, export_root)
            if rel is not None:
                target = rel.as_posix()
                # Тека закріплена за переробкою: видача бере STL прямо звідси.
                order.export_folder_path = target
                learn_client_folder(db, client_name, rel.parts[0])
        db.add(SyncLog(
            direction="mail_to_export", status="ok",
            message=(
                f"email {email.id}: переробка роботи {first_label} — "
                f"{len(present)} файл(ів) скопійовано{f' в {target}' if target else ''}"
            ),
        ))
        # Повернення відпрацьовано — лист іде з «Вхідних» (а фон перенесе його
        # в папку «оброблено» скриньки).
        email.reopened_at = None
        db.commit()
    except Exception as exc:  # noqa: BLE001 — копії вже на диску, треба прибрати
        db.rollback()
        errors = remove_copies(copied)
        clear_export_cache()
        logger.exception("Переробка листа %s не вдалась", email.id)
        detail = str(exc) or type(exc).__name__
        if errors:
            logger.error("Не вдалося прибрати копії переробки: %s", "; ".join(errors))
            detail += ". УВАГА: частина копій лишилась в export — " + "; ".join(errors)
        return ReworkResult(error="Не вдалося створити переробку: " + detail)

    write_letter_row(db, email, order, tab_found)
    logger.info(
        "Переробка з листа %s: робота %s (перша %s), %d файл(ів) у %s",
        email.id, order.id, first_label, len(present), target or "—",
    )
    return ReworkResult(
        order=order, copied_files=len(present), skipped_files=len(missing), target=target,
    )


def _relative(folder: Path, export_root: Path) -> Path | None:
    """Шлях теки відносно export. Тека будується від РОЗВ'ЯЗАНОГО кореня
    (`_contained_child`), а корінь у налаштуваннях буває літерою диска, яку
    `resolve()` перетворює на UNC — тому дві спроби."""
    for root in (export_root, export_root.resolve()):
        try:
            return folder.relative_to(root)
        except ValueError:
            continue
    logger.warning("Тека переробки %s поза export %s — не закріплено", folder, export_root)
    return None
