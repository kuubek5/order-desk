"""Прийняття листа в чергу — доменна частина, без жодного HTTP.

Раніше все це жило в тілі роуту `accept_email` (~296 рядків): гейти, створення
роботи, ФІЗИЧНИЙ перенос файлів у `export`, рядок-нотатка в Google-таблиці,
коміт із компенсацією й формулювання тосту. Саме тут аудит 05.09.26 знайшов
обидва critical пошти (C-1/C-2), і не випадково: логіку, замкнену в HTTP-шарі,
неможливо перевірити окремо від запиту.

Тут лишається все, що НЕ бачить `Request`. Роут відповідає за форму, фрагмент
візарда з помилкою, тост і редірект — і більше ні за що.

ГОЛОВНИЙ ІНВАРІАНТ (не послаблювати): файли переїжджають на диск ДО коміту,
тож будь-який провал коміту мусить повернути їх назад. Інакше база вважає, що
лист не прийнято, а вкладення вже лежать в `export` — тріаж каже «файли
зникли», хоча вони на місці. Компенсація — `mail_export.undo_moves`.
"""

from dataclasses import dataclass, field
from pathlib import Path
import logging

from sqlalchemy.orm import Session

from app.business_day import business_today
from app.export_scanner import clear_export_cache
from app.link_attachments import undownloaded_links
from app.mail_export import (
    save_attachments_to_export,
    undo_moves,
)
from app.material_catalog import (
    ensure_seeded,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.models import EmailMessage, Order, StatusEvent, SyncLog
from app.parser import HEADER_ROWS
from app.sender_memory import remember_sender
from app.settings_store import get_export_folder_path
from app.sheet_writer import append_mail_placeholder_row
from app.sheets import latest_worksheet_on_or_before, open_spreadsheet

logger = logging.getLogger(__name__)


def resolve_wizard_overrides(
    folder_pick: str, folder_new: str, material_folder: str
) -> tuple[str, str]:
    """Fold the step-2 directory controls into the two overrides
    save_attachments_to_export understands. A typed new folder name wins over
    the dropdown pick; an empty pick means "auto-resolve". Material subfolder is
    passed through as-is (empty -> derive from material_color)."""
    client_override = (folder_new or "").strip() or (folder_pick or "").strip()
    return client_override, (material_folder or "").strip()


@dataclass
class AcceptResult:
    """Що сталося з прийняттям — усе, що потрібно роуту, щоб відповісти.

    `error` непорожній означає: НІЧОГО не змінено (або зміни відкочено разом із
    файлами), і роут має показати цей текст у кроці 3 візарда.
    """

    error: str | None = None
    order: Order | None = None
    saved_files: int = 0
    remaining_files: int = 0
    material_label: str = ""
    partial: bool = field(default=False)

    @property
    def ok(self) -> bool:
        return self.error is None


def accept_letter(
    db: Session,
    user,
    email: EmailMessage,
    *,
    client_name: str,
    material_color: str = "",
    kind: str = "",
    quantity: str = "",
    folder_pick: str = "",
    folder_new: str = "",
    material_folder: str = "",
    attachment_ids: list[int] | None = None,
    accept_anyway: bool = False,
) -> AcceptResult:
    """Прийняти лист (або одну кольорову партію з нього) у чергу.

    Часткове прийняття — норма: багатокольоровий лист приймають партіями, і
    поки в ньому лишаються нерозібрані файли, він тримається в тріажі зі
    статусом «нове».
    """
    attachment_ids = list(attachment_ids or [])

    if email.attachments_status == "pending":
        # Вкладення ще качаються (двофазний фетч, app.mail_reader). Прийняти
        # зараз означало б створити роботу без жодного файлу, зняти статус
        # «нове» (а він і є дозволом на повтор) і осиротити файли, які друга
        # фаза збереже потім — перенести їх у export уже нікому.
        return AcceptResult(error="Вкладення ще завантажуються, зачекайте і спробуйте ще раз")

    # Файли за посиланням (Drive, ukr.net) не є вкладеннями листа, тому
    # attachments_status їх не бачить: лист зі статусом «skipped» і трьома STL
    # на Drive проходив прийняття мовчки, створюючи роботу БЕЗ жодного файлу.
    unfetched = undownloaded_links(email)
    if unfetched and not accept_anyway:
        return AcceptResult(
            error=(
                f"Ще {len(unfetched)} файл(ів) за посиланням не скачано — "
                "скачайте у вкладці «Файли + STL» або підтвердіть прийняття без них"
            )
        )

    target_tab, target_worksheet = _resolve_target_tab(db, email)

    new_order = Order(
        source="email",
        # Real наряд identifier from the sheet — email orders never get one,
        # but sheet_tab uses the same "%d.%m.%y" shape table tabs use, so period
        # tabs, is_overdue() and folder lookups treat a priced mail order exactly
        # like one entered from the sheet. row_number stays None on purpose —
        # that's the real signal that stops sheet write-back.
        sheet_tab=target_tab,
        row_number=None,
        client_name=client_name.strip() or None,
        material_color=material_color.strip() or None,
        kind=kind.strip() or None,
        quantity=quantity.strip() or None,
        status="нове",
    )
    ensure_seeded(db)
    new_order.material_id = resolve_material_id(
        new_order.material_color, load_alias_rows(db), material_id_by_name(db)
    )
    new_order.source_email_id = email.id
    db.add(new_order)
    db.flush()

    email.order_id = new_order.id
    db.add(
        StatusEvent(order_id=new_order.id, operator_id=user.id, status="нове", actor=user.username)
    )

    # Часткове прийняття: рухаються лише файли, обрані для ЦЬОГО кольору.
    # «Нерозібрані» = ще не забрані попередньою партією (order_id is None).
    # Порожній вибір означає «усі, що лишились» — типовий однокольоровий лист.
    unclaimed = [
        a for a in email.attachments
        if a.order_id is None and Path(a.saved_path).exists()
    ]
    selected_ids = set(attachment_ids)
    attachments = [a for a in unclaimed if a.id in selected_ids] if selected_ids else unclaimed

    moved_pairs: list[tuple[Path, Path]] = []
    if not attachments:
        # Рухати нічого, але звʼязок «відправник → клієнт» усе одно цінний.
        remember_sender(db, email, new_order.client_name or "", None)
    else:
        try:
            moved_pairs = _move_attachments(
                db, email, new_order, attachments,
                folder_pick=folder_pick, folder_new=folder_new,
                material_folder=material_folder,
            )
        except (OSError, ValueError) as exc:
            db.rollback()
            return AcceptResult(error="Не вдалося зберегти вкладення: " + str(exc))

    _write_placeholder_row(db, email, new_order, target_worksheet)

    # Частково чи повністю: якщо в листі лишились нерозібрані файли (інший
    # колір, який оператор ще не приймав), лишаємо «нове», щоб він тримався в
    # тріажі; інакше лист прийнято повністю.
    remaining = [
        a for a in email.attachments
        if a.order_id is None and Path(a.saved_path).exists()
    ]
    email.status = "нове" if remaining else "прийнято"

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — файли вже на диску, треба відкотити
        # Файли переїхали ДО цього коміту. Невдалий коміт (SQLite locked, диск
        # повний під WAL, …) відкочує базу в «лист не прийнято», тож файли
        # мусять повернутись у спул — інакше диск і база розійдуться назавжди.
        # Помилки відкату повідомляємо, ніколи не ковтаємо.
        db.rollback()
        undo_errors = undo_moves(moved_pairs)
        clear_export_cache()
        logger.exception("Accept commit failed for email %s", email.id)
        detail = str(exc)
        if undo_errors:
            logger.error("Could not return files to spool: %s", "; ".join(undo_errors))
            detail += (
                ". УВАГА: частину файлів не вдалося повернути в лист — "
                + "; ".join(undo_errors)
            )
        return AcceptResult(error="Не вдалося зберегти прийняття: " + detail)

    return AcceptResult(
        order=new_order,
        saved_files=len(attachments),
        remaining_files=len(remaining),
        material_label=(new_order.material_color or "").strip() or "без матеріалу",
        partial=bool(remaining),
    )


def _resolve_target_tab(db: Session, email: EmailMessage):
    """До якої датованої вкладки належить ця робота.

    Лабораторія часто працює на день-два позаду, тож вкладки на СЬОГОДНІ може
    ще не бути: запис нотатки в «16.08.26», коли найсвіжіша реальна — «15.08.26»,
    мовчки губить рядок і лишає роботу на фантомному дні. Беремо найсвіжішу
    датовану вкладку ≤ сьогодні; якщо таблиця недоступна — лишаємо назву
    сьогоднішнього дня, як було. Знайдений аркуш повертаємо, щоб не тягнути
    його вдруге при записі нотатки.

    День — РОБОЧИЙ (`business_today`), а не календарний: о 02:00 нічний
    оператор веде ще вчорашній день, і лист, прийнятий тоді, мусить лягти в
    ЙОГО вкладку. Це та сама межа, яку CLAUDE.md §14 вимагає скрізь; тут вона
    лишалась календарною з часів, коли правила ще не було, і сторож
    `tests/test_business_day.py` знайшов це, щойно код переїхав під його нагляд.
    """
    today = business_today()
    target_tab = today.strftime("%d.%m.%y")
    try:
        worksheet = latest_worksheet_on_or_before(open_spreadsheet(db=db), today)
    except Exception as exc:  # noqa: BLE001 — проблеми таблиці не блокують прийняття
        logger.warning("Could not resolve target sheet tab for email %s: %s", email.id, exc)
        return target_tab, None
    if worksheet is not None:
        target_tab = worksheet.title
    return target_tab, worksheet


def _move_attachments(
    db: Session,
    email: EmailMessage,
    new_order: Order,
    attachments: list,
    *,
    folder_pick: str,
    folder_new: str,
    material_folder: str,
) -> list[tuple[Path, Path]]:
    """Перенести файли партії в `export` і привʼязати їх до роботи.

    Повертає пари (звідки, куди) для КОЖНОГО фізично перенесеного файлу — без
    них невдалий коміт не мав би чим відкотити диск (аудит, пошта C-1).
    """
    export_root = Path(get_export_folder_path(db))
    # Файли, вже викладені в export автоматично (довірений відправник), НЕ
    # рухаємо вдруге — лише привʼязуємо до цієї роботи.
    to_move = [a for a in attachments if not a.staged_to_export]
    staged = [a for a in attachments if a.staged_to_export]
    moved_pairs: list[tuple[Path, Path]] = []
    used_folder = None

    if to_move:
        client_override, material_override = resolve_wizard_overrides(
            folder_pick, folder_new, material_folder
        )
        old_paths = [Path(a.saved_path) for a in to_move]
        new_paths = save_attachments_to_export(
            export_root,
            new_order.client_name or "",
            new_order.material_color or "",
            old_paths,
            client_folder_override=client_override,
            material_folder_override=material_override,
        )
        moved_pairs = list(zip(old_paths, new_paths))
        # Файли переїхали — кеш обходу export більше не відповідає диску.
        clear_export_cache()
        for attachment, new_path in zip(to_move, new_paths):
            attachment.saved_path = str(new_path)
            attachment.order_id = new_order.id
        try:
            used_folder = new_paths[0].relative_to(export_root).parts[0] if new_paths else None
        except (ValueError, IndexError):
            used_folder = None

    for attachment in staged:
        attachment.order_id = new_order.id
    if used_folder is None and staged:
        try:
            used_folder = Path(staged[0].saved_path).relative_to(export_root).parts[0]
        except (ValueError, IndexError):
            used_folder = None

    db.add(SyncLog(
        direction="mail_to_export", status="ok",
        message=f"email {email.id}: {len(attachments)} файл(ів)",
    ))
    remember_sender(db, email, new_order.client_name or "", used_folder)
    return moved_pairs


def _write_placeholder_row(db: Session, email: EmailMessage, new_order: Order, worksheet) -> None:
    """Рядок-нотатка в спільній таблиці — те, що оператори й так пишуть руками
    для телефонних/поштових замовлень (CLAUDE.md §2).

    Ніколи не блокує прийняття: відсутня вкладка чи мережевий збій ідуть у
    SyncLog, а не в обличчя операторові 500-ю.
    """
    try:
        if worksheet is None:
            db.add(SyncLog(
                direction="mail_to_sheet", sheet_tab=new_order.sheet_tab, status="error",
                message=(
                    f"email {email.id}: доступної датованої вкладки немає, "
                    "рядок-нотатку не записано"
                ),
            ))
            return
        note_row = append_mail_placeholder_row(
            worksheet,
            new_order.client_name or "",
            new_order.quantity or "",
            new_order.material_color or "",
        )
        # Привʼязуємо роботу до щойно записаного рядка. Без цього наступний синк
        # імпортує наряд-less рядок як ОКРЕМУ роботу source="sheet_client" — та
        # сама робота зʼявилась би двічі (як «Пошта» і як «Клієнт»).
        new_order.row_number = note_row - HEADER_ROWS
        db.add(SyncLog(
            direction="mail_to_sheet", sheet_tab=new_order.sheet_tab, status="ok",
            message=f"email {email.id}: рядок-нотатка записана в рядок {note_row}",
        ))
    except Exception as exc:  # noqa: BLE001 — зручність, а не умова прийняття
        db.add(SyncLog(
            direction="mail_to_sheet", sheet_tab=new_order.sheet_tab, status="error",
            message=f"email {email.id}: не вдалося записати рядок-нотатку: {exc}",
        ))
