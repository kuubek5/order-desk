"""Зауваження CAM-оператора до роботи (власник 08.10.26).

Оператор у черзі ставить зауваження («немає папки», «кількість не сходиться»…),
і воно лягає в «Коментар для CAM» (колонка K) ОКРЕМИМ РЯДКОМ, червоним жирним:
технік читає й пише саме цю клітинку, тож зауваження в полі його зору, а його
власний текст не чіпається. Рядок зауваження впізнається за позначкою «❗» на
початку — так CRM знає, який рядок клітинки її (замінити, прибрати), а синк,
що читає K назад, не потребує окремого поля: технік стер рядок у таблиці —
зауваження знято й у CRM.

Тут лише текст: розбір і складання клітинки. Запис — `sheet_writer`.
"""

from __future__ import annotations

from datetime import datetime

REMARK_MARK = "❗"

# Готові варіанти (власник 08.10.26). Своє слово — поле поруч у меню.
PRESETS: tuple[str, ...] = (
    "немає папки",
    "кількість не сходиться",
    "колір/матеріал незрозумілий",
    "тонкі шийки",
)


def _is_remark_line(line: str) -> bool:
    return line.lstrip().startswith(REMARK_MARK)


def remark_of(comment: str | None) -> str | None:
    """Текст зауваження з клітинки (без позначки) або None."""
    for line in reversed((comment or "").splitlines()):
        if _is_remark_line(line):
            text = line.lstrip()[len(REMARK_MARK):].strip()
            return text or None
    return None


def without_remark(comment: str | None) -> str:
    """Коментар техніка без рядків зауваження."""
    lines = [line for line in (comment or "").splitlines() if not _is_remark_line(line)]
    return "\n".join(lines).strip()


def remark_line(text: str, author: str, now: datetime | None = None) -> str:
    """Рядок зауваження для клітинки: «❗ немає папки · Роман 08.10 04:30»."""
    moment = now or datetime.now()
    who = f" · {author}" if author else ""
    return f"{REMARK_MARK} {text.strip()}{who} {moment:%d.%m %H:%M}"


def compose(comment: str | None, line: str | None) -> str:
    """Клітинка K з новим зауваженням (або без нього): текст техніка лишається,
    старий рядок зауваження замінюється, новий — останнім рядком."""
    base = without_remark(comment)
    if not line:
        return base
    return f"{base}\n{line}" if base else line


def remark_start(text: str) -> int | None:
    """Звідки в клітинці починається рядок зауваження — у ОДИНИЦЯХ UTF-16:
    саме так рахує `textFormatRuns.startIndex` Google Sheets (емодзі поза
    BMP займає дві одиниці, і зсув на одну фарбував би не той символ)."""
    offset = 0
    for line in text.splitlines(keepends=True):
        if _is_remark_line(line):
            return offset + (len(line) - len(line.lstrip()))
        offset += len(line.encode("utf-16-le")) // 2
    return None
