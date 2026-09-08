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
from threading import Lock
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
from app.mail_reader import _file_is_missing
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


# Лист приймається В ОДНОМУ процесі, але операторів двоє (CLAUDE.md §1), і
# нічого не заважало їм натиснути «Прийняти» на одному листі одночасно: гейт
# `email.status != "нове"` стоїть у роуті ДО виклику, а між перевіркою і
# створенням роботи немає нічого — виходило дві роботи на один лист, два рядки
# в таблиці й подвоєні файли в export (ревʼю 07.09.26, M.7).
#
# Лок саме на ЛИСТ, а не глобальний: паралельне приймання РІЗНИХ листів —
# нормальна робота вдвох, гальмувати її нема за що.
_letter_locks_guard = Lock()
_letter_locks: dict[int, Lock] = {}


def _letter_lock(email_id: int) -> Lock:
    with _letter_locks_guard:
        lock = _letter_locks.get(email_id)
        if lock is None:
            lock = Lock()
            _letter_locks[email_id] = lock
        return lock


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

    lock = _letter_lock(email.id)
    if not lock.acquire(blocking=False):
        return AcceptResult(
            error="Цей лист саме приймає інший оператор — оновіть сторінку за мить"
        )
    try:
        return _accept_letter_locked(
            db, user, email,
            client_name=client_name, material_color=material_color, kind=kind,
            quantity=quantity, folder_pick=folder_pick, folder_new=folder_new,
            material_folder=material_folder, attachment_ids=attachment_ids,
            accept_anyway=accept_anyway,
        )
    finally:
        lock.release()


def _accept_letter_locked(
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
    """Тіло `accept_letter` під локом листа — див. коментар до `_letter_locks`."""
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

    # Тільки ПЕРША робота листа. Багатокольоровий лист приймається партіями, і
    # переписування на кожній лишало в листі памʼять лише про ОСТАННЮ; звʼязок
    # з рештою тримає Order.source_email_id, а це поле лишається legacy-містком
    # для листів до міграції 0012 (ревʼю 07.09.26, M.7).
    if email.order_id is None:
        email.order_id = new_order.id
    db.add(
        StatusEvent(order_id=new_order.id, operator_id=user.id, status="нове", actor=user.username)
    )

    # Часткове прийняття: рухаються лише файли, обрані для ЦЬОГО кольору.
    # «Нерозібрані» = ще не забрані попередньою партією (order_id is None).
    # Порожній вибір означає «усі, що лишились» — типовий однокольоровий лист.
    # `_file_is_missing`, а не `Path.exists()`: exists() ковтає будь-яку
    # OSError, тож коротке моргання мережевої шари (UNC) виглядає точно як
    # видалений файл. Тут ціна помилки — файл не поїде в export, а лист
    # позначиться прийнятим (див. `remaining` нижче), тобто робота тихо
    # лишиться в спулі назавжди.
    unclaimed = [
        a for a in email.attachments
        if a.order_id is None and not _file_is_missing(a.saved_path)
    ]
    selected_ids = set(attachment_ids)
    attachments = [a for a in unclaimed if a.id in selected_ids] if selected_ids else unclaimed

    moved_pairs: list[tuple[Path, Path]] = []
    if not attachments:
        # Рухати нічого, але звʼязок «відправник → клієнт» усе одно цінний.
        remember_sender(db, email, new_order.client_name or "", None)
    else:
        try:
            # `moved_pairs` наповнює САМ `_move_attachments`, одразу після
            # фізичного переносу: раніше пари верталися лише при успіху, тож
            # падіння ПІСЛЯ переносу (запис у базу, `remember_sender`) лишало
            # файли в export при листі «нове» — диск і база розходились
            # назавжди, і компенсувати вже не було чим.
            _move_attachments(
                db, email, new_order, attachments,
                folder_pick=folder_pick, folder_new=folder_new,
                material_folder=material_folder,
                moved_out=moved_pairs,
            )
        except Exception as exc:  # noqa: BLE001 — файли могли поїхати, треба відкотити
            # Не лише OSError/ValueError: усе між переносом і поверненням —
            # робота з базою, і будь-яка її помилка мусить повернути файли в
            # спул, а не спливти 500-ю зі слідами на диску (ревʼю 07.09.26, C.1).
            db.rollback()
            undo_errors = undo_moves(moved_pairs)
            clear_export_cache()
            logger.exception("Accept failed while moving files for email %s", email.id)
            detail = str(exc)
            if undo_errors:
                logger.error("Could not return files to spool: %s", "; ".join(undo_errors))
                detail += (
                    ". УВАГА: частину файлів не вдалося повернути в лист — "
                    + "; ".join(undo_errors)
                )
                # Слід у БАЗІ, не лише в тексті помилки й лозі. Текст побачить
                # той, хто саме зараз дивиться на екран, і він зникне з першим
                # оновленням сторінки; лог у зібраному застосунку не читає
                # ніхто. А стан тут найгірший з можливих: частина коронок
                # фізично лежить в export, база каже «лист не прийнято», і
                # `saved_path` вказує у спул, де їх уже немає. При повторному
                # прийнятті вони тихо випадуть зі списку — лист стане
                # «прийнято» без цих робіт (аудит 08.09.26).
                _record_stranded_files(db, email, moved_pairs, undo_errors)
            return AcceptResult(error="Не вдалося зберегти вкладення: " + detail)

    # Частково чи повністю: якщо в листі лишились нерозібрані файли (інший
    # колір, який оператор ще не приймав), лишаємо «нове», щоб він тримався в
    # тріажі; інакше лист прийнято повністю.
    remaining = [
        a for a in email.attachments
        if a.order_id is None and not _file_is_missing(a.saved_path)
    ]
    email.status = "нове" if remaining else "прийнято"

    # Порядок навмисний: файли → база → таблиця. Рядок-нотатку в спільну
    # Google-таблицю пишемо ОСТАННІМ і лише після успішного коміту бази —
    # append у чужу таблицю не відкотити, на відміну від файлів (undo_moves)
    # і рядків SQLite (db.rollback). Раніше рядок писався ДО db.commit(): невдалий
    # коміт компенсував лише переміщення файлів, а рядок-нотатка лишався в
    # таблиці — наступний синк імпортував його як ДРУГИЙ наряд-less наряд.
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

    # Прийняття вже успішне (перший коміт пройшов) — рядок-нотатка лише
    # зручність. `_write_placeholder_row` сама ковтає мережеві помилки в
    # SyncLog, але задля цього ж SyncLog-рядка й `new_order.row_number`
    # потрібен ще один коміт; його невдача лише логується, прийняття листа
    # назад не відкочуємо.
    _write_placeholder_row(db, email, new_order, target_worksheet)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — прийняття вже відбулось, це лише журнал
        db.rollback()
        logger.exception(
            "Could not persist placeholder-row bookkeeping for email %s", email.id
        )

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


def _record_stranded_files(
    db: Session,
    email: EmailMessage,
    moved_pairs: list[tuple[Path, Path]],
    undo_errors: list[str],
) -> None:
    """Записати в журнал синку, які файли лишились в export після невдалого
    відкату.

    Окремою транзакцією ПІСЛЯ `db.rollback()`: основна відкочена навмисно (лист
    не прийнято), а цей слід має пережити відкат — інакше єдиним свідченням
    лишиться рядок у лозі, якого ніхто не читає.

    Сам запис не має права завалити відповідь операторові: він і так бачить
    помилку, і друга помилка поверх першої нічого не додасть.
    """
    try:
        stranded = "; ".join(str(dest) for _, dest in moved_pairs) or "(перелік порожній)"
        db.add(SyncLog(
            direction="mail_to_disk",
            status="error",
            message=(
                f"лист {email.id}: файли лишились в export після невдалого "
                f"повернення — {stranded}. Причини: {'; '.join(undo_errors)}"
            ),
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — слід важливий, але не важливіший за відповідь
        db.rollback()
        logger.exception("Не вдалося записати слід про застряглі файли листа %s", email.id)


def _move_attachments(
    db: Session,
    email: EmailMessage,
    new_order: Order,
    attachments: list,
    *,
    folder_pick: str,
    folder_new: str,
    material_folder: str,
    moved_out: list[tuple[Path, Path]] | None = None,
) -> list[tuple[Path, Path]]:
    """Перенести файли партії в `export` і привʼязати їх до роботи.

    Повертає пари (звідки, куди) для КОЖНОГО фізично перенесеного файлу — без
    них невдалий коміт не мав би чим відкотити диск (аудит, пошта C-1).

    `moved_out` — той самий перелік, але відданий викликачеві ОДРАЗУ після
    переносу, ще до записів у базу. Повернене значення дістається лише тому,
    хто дожив до кінця функції; список бачить і той, хто ловить виняток
    посередині (ревʼю 07.09.26, C.1).
    """
    export_root = Path(get_export_folder_path(db))
    to_move = list(attachments)
    moved_pairs: list[tuple[Path, Path]] = []
    used_folder = None

    if to_move:
        client_override, material_override = resolve_wizard_overrides(
            folder_pick, folder_new, material_folder
        )
        old_paths = [Path(a.saved_path) for a in to_move]
        # `moved_out` іде ВСЕРЕДИНУ: перенос наповнює його одразу після кожного
        # файлу, тож викликач бачить перелік і тоді, коли ця функція кинула
        # посеред переносу. Раніше список наповнювався ТУТ, після повернення —
        # тобто лише при успіху, і при падінні всередині `undo_moves` у
        # викликача отримував порожньо, а файли лишались в export без сліду
        # (аудит 08.09.26).
        new_paths = save_attachments_to_export(
            export_root,
            new_order.client_name or "",
            new_order.material_color or "",
            old_paths,
            client_folder_override=client_override,
            material_folder_override=material_override,
            moved_out=moved_out,
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
