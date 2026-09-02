"""Скріншоти до звернень зворотного зв'язку: запис на диск і безпечна віддача.

Свідома копія меж безпеки з app/shift_images.py, а не імпорт із нього: кожен
модуль, який віддає сирі байти файлу, має читатись як самостійна межа (той
самий принцип, що shift_images не ділиться зі stl_preview). Правило те саме —
«ніколи не вір шляху»: ім'я на диску наше, з клієнтського імені жоден байт у
шлях не потрапляє, формат вирішує Pillow, а не розширення від браузера.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.config import FEEDBACK_IMAGES_PATH
from app.mail_reader import safe_attachment_filename
from app.models import Feedback, FeedbackImage

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_FEEDBACK = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_LONG_SIDE = 1920
_CHUNK = 64 * 1024

# SVG виключено свідомо: це XML, який виконує скрипт з нашого походження
# (stored XSS), а не картинка.
_FORMAT_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_EXTENSION_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class FeedbackImageError(Exception):
    """Скріншот не прийнято: завеликий, не картинка, понад ліміт.

    Ловиться роутом окремо від збереження тексту: провалений скріншот НЕ сміє
    забрати з собою саме звернення.
    """


def images_root() -> Path:
    return Path(FEEDBACK_IMAGES_PATH)


def _feedback_dir(feedback_id: int, created_at: datetime) -> Path:
    return images_root() / created_at.strftime("%Y-%m") / str(feedback_id)


def save_image(
    db: Session,
    feedback: Feedback,
    *,
    stream,
    filename: str | None,
    now: datetime | None = None,
) -> FeedbackImage:
    """Записати один скріншот і додати рядок (без commit — транзакція за роутом)."""
    if len(feedback.images) >= MAX_IMAGES_PER_FEEDBACK:
        raise FeedbackImageError(
            f"До звернення можна додати максимум {MAX_IMAGES_PER_FEEDBACK} зображення."
        )

    folder = _feedback_dir(feedback.id, feedback.created_at)
    folder.mkdir(parents=True, exist_ok=True)
    index = 1
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
                    raise FeedbackImageError("Зображення завелике (понад 10 МБ).")
                fh.write(chunk)
        if total == 0:
            raise FeedbackImageError("Порожній файл.")

        try:
            with Image.open(part) as probe:
                probe.verify()
                image_format = probe.format
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise FeedbackImageError("Це не зображення (PNG, JPEG або WEBP).") from exc

        extension = _FORMAT_EXTENSIONS.get(image_format or "")
        if extension is None:
            raise FeedbackImageError("Підтримуються лише PNG, JPEG і WEBP.")

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

    row = FeedbackImage(
        feedback_id=feedback.id,
        filename=safe_attachment_filename(filename, index, "image/png"),
        saved_path=str(final),
        size_bytes=final.stat().st_size,
        width=width,
        height=height,
        created_at=now or datetime.now(),
    )
    db.add(row)
    feedback.images.append(row)
    return row


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def resolve_image_file(image: FeedbackImage) -> Path | None:
    """Перевірити шлях НАНОВО й повернути файл, або None (та сама межа, що в
    shift_images.resolve_image_file — відновлення з бекапу могло покласти в
    saved_path що завгодно)."""
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
    return _EXTENSION_MEDIA_TYPES.get(path.suffix.lower())
