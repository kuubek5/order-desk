# -*- coding: utf-8 -*-
"""Фото конкретного верстата — для картки на екрані «Верстати» (04.09.26).

Згенеровані «портрети моделей» власник забракував: реальні верстати чорні
спереду з синім вікном, а картинка вигадала інше. Тому тепер ЙОГО фото на
КОЖЕН верстат: завантажується в Налаштуваннях, лежить одним JPEG на диску
(`<data>/machine_portraits/<id>.jpg`), без фото картка бере дефолт моделі.

Файл приходить із телефона, тож: формат визначає Pillow (не розширення),
EXIF-поворот застосовується (інакше портретне фото лягає боком), розмір
зменшується до MAX_WIDTH — картка 300–400 px, 12-мегапіксельний оригінал ні
до чого. Запис атомарний (tmp → replace): полл екрана може читати файл у
той самий момент.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import MACHINE_PORTRAITS_PATH

MAX_BYTES = 12 * 1024 * 1024
MAX_WIDTH = 1200
_CHUNK = 256 * 1024


class PortraitError(Exception):
    """Причина словами оператора."""


def portraits_root() -> Path:
    root = Path(MACHINE_PORTRAITS_PATH)
    root.mkdir(parents=True, exist_ok=True)
    return root


def portrait_path(machine_id: int) -> Path:
    # int() — пасок безпеки: у шлях іде лише число, ніяких сегментів з форми.
    return portraits_root() / f"{int(machine_id)}.jpg"


def portrait_version(machine_id: int) -> int | None:
    """mtime файлу як версія для cache-busting у URL; None — фото немає."""
    try:
        return int(portrait_path(machine_id).stat().st_mtime)
    except OSError:
        return None


def save_portrait(machine_id: int, stream) -> Path:
    """Прочитати потік, перевірити, що це зображення, повернути й зменшити,
    записати атомарно. Будь-який збій нічого не лишає на диску."""
    buf = io.BytesIO()
    total = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise PortraitError("Фото завелике (понад 12 МБ).")
        buf.write(chunk)
    if total == 0:
        raise PortraitError("Порожній файл.")

    buf.seek(0)
    try:
        with Image.open(buf) as probe:
            probe.verify()
            image_format = probe.format
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PortraitError("Це не зображення (PNG, JPEG або WEBP).") from exc
    if image_format not in {"JPEG", "PNG", "WEBP"}:
        raise PortraitError("Підтримуються лише PNG, JPEG і WEBP.")

    final = portrait_path(machine_id)
    tmp = final.with_suffix(".jpg.tmp")
    buf.seek(0)
    try:
        with Image.open(buf) as img:
            img = ImageOps.exif_transpose(img)  # фото з телефона лягає як знято
            img = img.convert("RGB")
            img.thumbnail((MAX_WIDTH, MAX_WIDTH))  # довга сторона ≤ MAX_WIDTH
            img.save(tmp, format="JPEG", quality=86, optimize=True, progressive=True)
        os.replace(tmp, final)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return final


def delete_portrait(machine_id: int) -> bool:
    path = portrait_path(machine_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
