"""Передача зміни: записки нічного оператора наступному.

Нічний оператор іде о ~05:00, наступний приходить о ~08:00 — три години, коли
в цеху нікого. Печі (свідомо поза системою), стан верстатів, «цю не запускай,
чекаємо скани», матеріали, що закінчуються, — усе це зараз передається
СМС-ками. Тут воно стає записками, які видно на окремому екрані й на картці
черги.

Модуль без Request/Response (правило межі, ARCHITECTURE_PLAN §2) і без
`db.commit()`: транзакцією володіє роут — так само, як `log_action`
(app/services/undo.py).

Час скрізь `datetime.now()`, локальний: для передачі зміни час і є змістом
(«піч №2 відкрити о 9:00»), а server_default=func.now() на SQLite пише UTC —
живий наслідок цього вже видно на /journal (зсув 3 години).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import ShiftNote, User

# «до відома» зникає з дошки, щойно хтось прийняв; «потребує дії» лишається
# видимим і після прийняття, доки хтось не закриє його окремою кнопкою.
KIND_INFO = "info"
KIND_ACTION = "action"
KINDS = (KIND_INFO, KIND_ACTION)

KIND_LABELS = {KIND_INFO: "до відома", KIND_ACTION: "потребує дії"}

# Скільки записок віддає історія за раз (форма /journal: беремо limit+1, щоб
# відрізнити «рівно стільки» від «є ще»).
HISTORY_LIMIT = 300

# Ніч перетинає північ, тож календарна дата розірвала б одну передачу на дві.
# Робоча доба починається о 06:00: усе з 06:00 до 05:59 наступного ранку
# належить одній ночі.
NIGHT_START_HOUR = 6


class ShiftNoteError(ValueError):
    """Записку не прийнято: порожній текст, невідомий тип, не той тип дії."""


def _open_predicate():
    """ЄДИНИЙ предикат «записка ще на дошці».

    Навмисно один хелпер на обидві функції (`open_notes` і `open_note_count`):
    якщо їхні фільтри розійдуться, бейдж покаже «2» над порожньою карткою — і
    бейджу перестануть вірити. Читається так: не прийнято АБО (потребує дії
    І не виконано).
    """
    return or_(
        ShiftNote.acknowledged_at.is_(None),
        (ShiftNote.kind == KIND_ACTION) & ShiftNote.resolved_at.is_(None),
    )


def _with_people(stmt):
    """Автор і ті, хто прийняв/закрив, показуються в кожному рядку — тягнемо
    їх одним запитом, інакше стрічка на 50 записок дає 150 добірних SELECT."""
    return stmt.options(
        selectinload(ShiftNote.author),
        selectinload(ShiftNote.acknowledged_by),
        selectinload(ShiftNote.resolved_by),
        selectinload(ShiftNote.images),
    )


def create_note(
    db: Session,
    *,
    kind: str,
    text: str,
    author: User | None,
    now: datetime | None = None,
) -> ShiftNote:
    """Створити записку. Не комітить — транзакція за роутом."""
    kind = (kind or "").strip()
    if kind not in KINDS:
        raise ShiftNoteError("Невідомий тип записки")
    text = (text or "").strip()
    if not text:
        raise ShiftNoteError("Порожня записка")

    note = ShiftNote(
        kind=kind,
        text=text,
        author_id=author.id if author is not None else None,
        created_at=now or datetime.now(),
    )
    db.add(note)
    return note


def edit_note(
    db: Session,
    note: ShiftNote,
    *,
    text: str,
    now: datetime | None = None,
) -> ShiftNote:
    """Змінити текст записки — і СКИНУТИ прийняття.

    Інакше Вадим «прийняв» один текст, а на дошці висить інший, і кнопка
    перестає щось означати. Виконання («потребує дії» закрито) не чіпаємо:
    справу вже зроблено, редагування формулювання її не відкручує.
    """
    text = (text or "").strip()
    if not text:
        raise ShiftNoteError("Порожня записка")
    if text == note.text:
        return note

    note.text = text
    note.edited_at = now or datetime.now()
    note.acknowledged_at = None
    note.acknowledged_by_id = None
    return note


def acknowledge(
    db: Session,
    note: ShiftNote,
    *,
    user: User | None,
    now: datetime | None = None,
) -> bool:
    """«Прийняв» — прочитано. Одне на записку, не персональний стан.

    Ідемпотентне: перший, хто натиснув, закриває для всіх; повторний виклик
    нічого не переписує й повертає False (дві людини можуть натиснути майже
    одночасно — ім'я не має підмінятись).
    """
    if note.acknowledged_at is not None:
        return False
    note.acknowledged_at = now or datetime.now()
    note.acknowledged_by_id = user.id if user is not None else None
    return True


def resolve(
    db: Session,
    note: ShiftNote,
    *,
    user: User | None,
    now: datetime | None = None,
) -> bool:
    """Закрити записку типу «потребує дії». Ідемпотентне, як і прийняття."""
    if note.kind != KIND_ACTION:
        raise ShiftNoteError("Закривати можна лише записки «потребує дії»")
    if note.resolved_at is not None:
        return False
    note.resolved_at = now or datetime.now()
    note.resolved_by_id = user.id if user is not None else None
    return True


def open_notes(db: Session) -> list[ShiftNote]:
    """Записки, які ще на дошці, найновіші зверху."""
    stmt = _with_people(
        select(ShiftNote).where(_open_predicate()).order_by(ShiftNote.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def open_note_count(db: Session) -> int:
    """Число для бейджа в рейці. Той самий предикат, що й `open_notes` —
    навмисно через спільний хелпер, а не другий однаковий фільтр."""
    stmt = select(func.count(ShiftNote.id)).where(_open_predicate())
    return int(db.execute(stmt).scalar_one())


def feed(db: Session, *, limit: int = HISTORY_LIMIT) -> tuple[list[ShiftNote], bool]:
    """Уся стрічка, найновіші зверху. Повертає (записки, чи є ще)."""
    stmt = _with_people(
        select(ShiftNote).order_by(ShiftNote.created_at.desc()).limit(limit + 1)
    )
    rows = list(db.execute(stmt).scalars().all())
    truncated = len(rows) > limit
    return rows[:limit], truncated


def history(
    db: Session, *, limit: int = HISTORY_LIMIT
) -> tuple[list[tuple[datetime, list[ShiftNote]]], bool]:
    """Минулі ночі списком: [(дата ночі, записки), ...] + чи є ще."""
    rows, truncated = feed(db, limit=limit)
    return group_by_night(rows), truncated


def night_of(moment: datetime) -> datetime:
    """До якої ночі належить мітка часу — дата її ПОЧАТКУ.

    Зсув на 6 годин назад: 23:50 і 01:10 дають однакову відповідь, тож одна
    передача не розривається опівночі на два заголовки.
    """
    return (moment - timedelta(hours=NIGHT_START_HOUR)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def group_by_night(notes: list[ShiftNote]) -> list[tuple[datetime, list[ShiftNote]]]:
    """Згрупувати записки по ночах, зберігаючи порядок, у якому прийшли."""
    groups: list[tuple[datetime, list[ShiftNote]]] = []
    for note in notes:
        key = night_of(note.created_at)
        if groups and groups[-1][0] == key:
            groups[-1][1].append(note)
        else:
            groups.append((key, [note]))
    return groups


@dataclass(frozen=True)
class NightLabel:
    """Заголовок ночі: «Ніч 27→28.08»."""

    start: datetime

    @property
    def text(self) -> str:
        end = self.start + timedelta(days=1)
        return f"Ніч {self.start.strftime('%d')}→{end.strftime('%d.%m')}"


def night_label(start: datetime) -> str:
    return NightLabel(start).text
