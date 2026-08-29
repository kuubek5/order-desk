"""Скріншоти до записок передачі зміни: запис на диск і безпечна віддача.

Тут живе правило «ніколи не вір шляху». Модуль верхнього рівня поруч зі
`stl_preview.py` / `mail_spool.py`, бо він — межа безпеки для роута, який
віддає сирі байти файлу.

Чому не `/static`: `app.mount("/static", ...)` віддає з бандла PyInstaller
(тека лише для читання, затирається кожним оновленням), і вона свідомо
виведена з-під ліцензійного гейту, тобто доступна без сесії взагалі.
Скріншотам робіт клієнтів там не місце.

Ім'я на диску — НАШЕ. Жоден байт клієнтського імені в шлях не потрапляє:
`NN` — порядковий номер у записці, розширення — з білого списку. Оригінальне
ім'я санітизується й живе лише в колонці для показу. Це важливо ще й тому, що
Windows-скріншот із буфера завжди приходить як `image.png` — усі вставки мали б
однакове ім'я.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.config import SHIFT_IMAGES_PATH
from app.mail_reader import safe_attachment_filename
from app.models import ShiftNote, ShiftNoteImage

logger = logging.getLogger(__name__)

# Скільки скріншотів має сенс на одну записку. Це передача зміни, а не альбом:
# «ось табло печі» плюс «ось що з верстатом» — і досить.
MAX_IMAGES_PER_NOTE = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Довга сторона після зменшення. Скріншот 4K тут ні на що не впливає — його
# однаково дивляться в картці розміром із долоню.
MAX_LONG_SIDE = 1920
_CHUNK = 64 * 1024

# Формати, які ми справді вміємо показати. SVG виключено СВІДОМО: це XML, який
# з нашого ж походження виконує скрипт (stored XSS), а не картинка.
_FORMAT_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_EXTENSION_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class ShiftImageError(Exception):
    """Скріншот не прийнято: завеликий, не картинка, понад ліміт.

    Ловиться роутом окремо від збереження тексту: провалений скріншот НЕ сміє
    забрати з собою речення «піч №2 відкрити о 9:00».
    """


def images_root() -> Path:
    return Path(SHIFT_IMAGES_PATH)


def _note_dir(note_id: int, created_at: datetime) -> Path:
    """<корінь>/<YYYY-MM>/<note_id>/ — місяць зверху, щоб піврічне прибирання
    працювало з текою, а не обходом усього дерева."""
    return images_root() / created_at.strftime("%Y-%m") / str(note_id)


def _next_index(note: ShiftNote) -> int:
    """Наступний вільний номер у записці.

    Навмисно НЕ `len(images) + 1`: після видалення першого з двох зображень
    довжина дала б 2 — номер, який уже лежить на диску, і збереження тихо
    затерло б чужий файл.
    """
    used = 0
    for row in note.images:
        stem = Path(row.saved_path).stem
        if stem.isdigit():
            used = max(used, int(stem))
    return used + 1


def save_image(
    db: Session,
    note: ShiftNote,
    *,
    stream,
    filename: str | None,
    now: datetime | None = None,
) -> ShiftNoteImage:
    """Записати один скріншот і додати рядок (без commit — транзакція за роутом).

    Порядок навмисний: спершу ліміт кількості (щоб не писати на диск даремно),
    потім потік у `.part` із лічильником байтів, і лише потім Pillow вирішує,
    що це насправді за формат. Розширення й `content_type` від браузера тут
    ні на що не впливають — їх пише клієнт.

    Будь-який збій прибирає за собою все, що встиг записати, і НЕ створює рядка.
    """
    if len(note.images) >= MAX_IMAGES_PER_NOTE:
        raise ShiftImageError(
            f"До записки можна додати максимум {MAX_IMAGES_PER_NOTE} зображення."
        )

    index = _next_index(note)
    folder = _note_dir(note.id, note.created_at)
    folder.mkdir(parents=True, exist_ok=True)
    # Файл із таким номером уже може лежати на диску (рядок прибрали, байти
    # лишились) — беремо наступний вільний, а не затираємо.
    while any(folder.glob(f"{index:02d}.*")):
        index += 1
    part = folder / f"{index:02d}.part"

    total = 0
    try:
        with open(part, "wb") as fh:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ShiftImageError("Зображення завелике (понад 10 МБ).")
                fh.write(chunk)
        if total == 0:
            raise ShiftImageError("Порожній файл.")

        # Справжнє визначення формату. verify() читає файл і закриває його,
        # тому далі потрібен новий open() — так це й задумано в Pillow.
        try:
            with Image.open(part) as probe:
                probe.verify()
                image_format = probe.format
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ShiftImageError("Це не зображення (PNG, JPEG або WEBP).") from exc

        extension = _FORMAT_EXTENSIONS.get(image_format or "")
        if extension is None:
            raise ShiftImageError("Підтримуються лише PNG, JPEG і WEBP.")

        final = folder / f"{index:02d}{extension}"
        with Image.open(part) as img:
            img = img.convert("RGB") if image_format == "JPEG" else img
            img.thumbnail((MAX_LONG_SIDE, MAX_LONG_SIDE))
            img.save(final, format=image_format)
            width, height = img.size
        part.unlink(missing_ok=True)
    except Exception:
        part.unlink(missing_ok=True)
        raise

    row = ShiftNoteImage(
        note_id=note.id,
        # Санітизоване ім'я лише для показу — у шлях воно не входить.
        filename=safe_attachment_filename(filename, index, "image/png"),
        saved_path=str(final),
        size_bytes=final.stat().st_size,
        width=width,
        height=height,
        created_at=now or datetime.now(),
    )
    db.add(row)
    note.images.append(row)
    return row


def _is_link(path: Path) -> bool:
    """І симлінк, і Windows-junction — це посилання.

    П'ять рядків навмисно продубльовано з app/stl_preview.py, а не імпортовано:
    цей модуль має читатись як самостійна межа безпеки.
    """
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def resolve_image_file(image: ShiftNoteImage) -> Path | None:
    """Перевірити шлях НАНОВО й повернути файл, або None.

    `saved_path` сьогодні пишемо ми — але відновлення з бекапу може покласти в
    цю колонку що завгодно, тож роут не має права віддавати FileResponse
    наосліп. Корінь перевиводиться з SHIFT_IMAGES_PATH, на кожному кроці
    відкидаються симлінки й junction'и, і лише потім strict-resolve усередині
    кореня.
    """
    if image.pruned_at is not None:
        return None
    try:
        lexical_root = images_root().absolute()
        if _is_link(lexical_root):
            return None
        resolved_root = lexical_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    try:
        candidate = Path(image.saved_path).absolute()
        relative = candidate.relative_to(lexical_root)
    except (ValueError, OSError, RuntimeError):
        return None

    segments = relative.parts
    if not segments:
        return None
    current = lexical_root
    for segment in segments:
        if segment in ("", ".", "..") or ":" in segment:
            return None
        current = current / segment
        try:
            if _is_link(current):
                return None
        except OSError:
            return None

    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def media_type_for(path: Path) -> str | None:
    """Тип віддається з розширення за білим списком — ніколи не з того, що
    прислав клієнт, і ніколи не вгадуванням по вмісту."""
    return _EXTENSION_MEDIA_TYPES.get(path.suffix.lower())


def delete_image(db: Session, image: ShiftNoteImage) -> None:
    """Прибрати зображення на прохання оператора: спершу байти, потім рядок.

    Порядок саме такий, бо зворотний лишав би файл-сироту без рядка, якби
    транзакція відкотилась. Файл, якого вже немає, — не помилка."""
    path = resolve_image_file(image)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("не вдалось видалити файл скріншота %s", path, exc_info=True)
    db.delete(image)
