"""Товщина конструкції з STL — щоб диск підбирали до відкриття CAM (ідея №29,
ROADMAP_IDEAS.md; рішення власника 03.11.26).

Як рахує Sum3D (звірено 03.11.26 на мосту 25-26, наряд 37896): бере висоту
STL уздовж осі введення і множить на коефіцієнт спікання диска — «Maximum
tooth thickness: 15.02» = 12.015 мм × 1.25. exocad експортує STL уже
повернутим до осі введення (`RotateToAxisInsertion` у `.constructionInfo`),
тож ця висота — просто розмах STL по Z (12.015 = 12.015 до тисячної).

Рішення власника:
  * `.constructionInfo` джерелом не є — лабораторія скидає його не завжди,
    пошта тим паче. Рахуємо з самого STL;
  * STL, не повернутий до осі введення, НЕ рахуємо — підказки немає, не
    вгадуємо. Повернутим вважаємо лише експорт exocad (`…_cad.stl`): обидва
    звірені файли саме такі, а інші CAD (3Shape тощо) осі не гарантують;
  * коефіцієнт — цирконій ×1.25 (власник 04.11.26, замість середнього 1.2):
    `STL_To_Teeth_Scale = 1.25` у `GPSETTINGS.INI` Sum3D, не мінявся з березня,
    і рівно стільки дав наряд 37896. Брендів у матеріалах Sum3D немає, тож для
    Emotions і STML це припущення; ручну правку масштабу при імпорті ми не
    бачимо. ПММА не спікається (×1.0). Інші родини — без підказки;
  * показуємо саму товщину, як Sum3D («≈15,0 мм»), а не «який диск»: запасів
    Sum3D ми не знаємо, і на тому мосту при 15 мм поставили диск 20.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePath

from app import log_throttle

logger = logging.getLogger(__name__)

# Файл молодший за це ще можуть копіювати. STL по мережі цеху копіюється за
# секунди, а прохід іде раз на 30 с: пропуск додає затримку лише на один прохід.
SETTLE_SECONDS = 10.0
_SIGNATURE_MAX = 480  # колонка 500, ще «mixed:» попереду
# Символ чіпа матеріалу рядка пошти → родина, для якої відомий коефіцієнт.
_FAMILY_BY_BADGE = {"Zr": "Цирконій", "PMMA": "ПММА"}
_EXTENT_CACHE_MAX = 4000
_extent_cache: OrderedDict[str, tuple[tuple[int, int], float | None]] = OrderedDict()
_extent_lock = threading.Lock()

# Коефіцієнт спікання за родиною матеріалу (`Material.name`). Родини поза
# переліком підказки не мають: титан фрезерують із заготовки, СЛМ друкують.
SHRINK = {"Цирконій": 1.25, "ПММА": 1.0}

# Більше за найвищий диск (25 мм) з будь-яким збільшенням — це не коронка, а
# модель щелепи чи сміття; така «висота» гірша за відсутню.
MAX_PLAUSIBLE_MM = 40.0
# Найвищий диск у цеху — шкала ізометричного бейджа (заповнений = 25 мм).
DISC_MAX_MM = 25.0

_FACET = struct.Struct("<12fH")


def is_rotated_export(filename: str) -> bool:
    """Експорт exocad (`…_cad.stl`) — уже повернутий до осі введення."""
    name = PurePath(filename or "").name.lower()
    return name.endswith("_cad.stl")


def stl_z_extent(path: Path) -> float | None:
    """Розмах STL по Z у мм (двійковий або текстовий), або None — файл не
    читається чи порожній."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.info("Висота STL: не прочитано %s: %s", path, exc)
        return None
    zs: list[float] = []
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count and 84 + count * _FACET.size == len(data):
            for facet in _FACET.iter_unpack(memoryview(data)[84:]):
                zs.extend((facet[5], facet[8], facet[11]))
    if not zs and data[:5].lower() == b"solid":
        for line in data.decode("ascii", "ignore").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                try:
                    zs.append(float(parts[3]))
                except ValueError:
                    continue
    if not zs:
        return None
    return max(zs) - min(zs)


@dataclass
class FolderHeight:
    """Висота роботи з її теки: найвища з повернутих STL (мм, до усадки)."""

    height_mm: float | None
    files: int
    signature: str


def folder_signature(stls: list[Path]) -> str:
    """Відбиток набору файлів (імена, розміри, час зміни): змінився — міряти заново.

    Час — бо Windows може виділити файлу кінцевий розмір ще на початку копіювання:
    за самим розміром недокопійований і докопійований файл однакові, і неправильна
    висота лишалась би назавжди. Довгий відбиток не обрізається наосліп: без хешу
    зміна файлу за межею 500 символів лишалась би непоміченою."""
    parts = []
    for path in sorted(stls, key=lambda p: p.name.lower()):
        try:
            stat = path.stat()
            parts.append(f"{path.name.lower()}:{stat.st_size}:{int(stat.st_mtime)}")
        except OSError:
            parts.append(f"{path.name.lower()}:?")
    text = "|".join(parts)
    if len(text) <= _SIGNATURE_MAX:
        return text
    return f"{text[:_SIGNATURE_MAX - 41]}#{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def still_settling(stls: list[Path], now: datetime) -> bool:
    """Чи є файл, створений чи змінений менше ніж `SETTLE_SECONDS` тому: його ще
    можуть копіювати, тож міряє наступний прохід. Час із майбутнього (годинник
    шари спішить) затримкою не вважаємо, інакше мірка стояла б, доки не наздожене."""
    moment = now.timestamp()
    for path in stls:
        try:
            stat = path.stat()
        except OSError:
            continue
        if 0 <= moment - max(stat.st_mtime, stat.st_ctime) < SETTLE_SECONDS:
            return True
    return False


def rotated_stls(folder: Path) -> list[Path]:
    """Повернуті STL у теці роботи й на рівень глибше (тека матеріалу всередині
    теки листа, підтека техніка)."""
    found: list[Path] = []
    try:
        for entry in folder.iterdir():
            if entry.is_file() and is_rotated_export(entry.name):
                found.append(entry)
            elif entry.is_dir():
                found.extend(
                    sub for sub in entry.iterdir()
                    if sub.is_file() and is_rotated_export(sub.name)
                )
    except OSError as exc:
        # Через глушник: прохід раз на 30 с по всіх свіжих роботах, і недоступна
        # шара інакше писала б рядок на кожну теку щотіку.
        skipped = log_throttle.due("stl_height.folder_unavailable")
        if skipped is not None:
            logger.info("Висота STL: тека %s недоступна: %s (ще %d подібних відтоді)",
                        folder, exc, skipped)
    return found


def measure_folder(folder: Path, stls: list[Path] | None = None) -> FolderHeight:
    stls = rotated_stls(folder) if stls is None else stls
    heights = [h for h in (stl_z_extent(p) for p in stls) if h is not None]
    plausible = [h for h in heights if 0 < h <= MAX_PLAUSIBLE_MM]
    return FolderHeight(
        height_mm=max(plausible) if plausible else None,
        files=len(plausible),
        signature=folder_signature(stls),
    )


def work_folder(order, tech_root: str | None, export_root: Path | None) -> Path | None:
    """Тека файлів роботи: закріплена тека export (пошта, прив'язаний
    клієнтський рядок) або тека техніка за «Номером роботи» (лабораторія).
    Ті самі резолвери, що видача й черга, — своя склейка розійшлася б з ними."""
    from app.export_scanner import entry_for_folder
    from app.order_folder import resolve_job_code_folder

    if order.export_folder_path and export_root is not None:
        entry = entry_for_folder(export_root, order.export_folder_path)
        if entry is not None:
            return entry.folder_path
    if order.job_code:
        return resolve_job_code_folder(tech_root, order.job_code)
    return None


# Які роботи міряти: свіжі (файли лягають до прорахунку) і ще не у
# фрезеруванні; давніші вже давно на диску, мірка їм не допоможе.
ACTIVE_STATUSES = ("нове", "прийнято", "прораховано")
RECENT_DAYS = 3


def measure_pending(db, tech_root: str | None, export_root: Path | None, *, now=None) -> int:
    """Один прохід: поміряти свіжі роботи з цирконію/ПММА, чиї файли
    змінились. Повертає, скільки робіт отримали нову мірку. Комітить сам.

    Тека, спільна для робіт РІЗНИХ родин (технік поклав у один «Номер роботи»
    титановий абатмент і цирконієві коронки), не дає підказки нікому з них:
    найвищий STL там може бути чужий."""
    from sqlalchemy import select

    from app.models import Material, Order

    moment = now or datetime.now()
    since = moment - timedelta(days=RECENT_DAYS)
    # Усі родини, не лише ті, що міряємо: інакше титан у спільній теці
    # відсіявся б запитом, і цирконій отримав би висоту його абатмента.
    rows = db.execute(
        select(Order, Material.name)
        .outerjoin(Material, Order.material_id == Material.id)
        .where(
            Order.archived_at.is_(None),
            Order.status.in_(ACTIVE_STATUSES),
            Order.created_at >= since,
        )
    ).all()

    by_folder: dict[Path, list[tuple[Order, str | None]]] = {}
    for order, family in rows:
        folder = work_folder(order, tech_root, export_root)
        if folder is not None:
            by_folder.setdefault(folder, []).append((order, family))

    changed = 0
    for folder, everyone in by_folder.items():
        works = [(order, family) for order, family in everyone if family in SHRINK]
        if not works:
            continue
        stls = rotated_stls(folder)
        if still_settling(stls, moment):
            continue
        signature = folder_signature(stls)
        families = {family for _order, family in everyone}
        if len(families) > 1:
            signature = "mixed:" + signature
        if all(order.stl_height_sig == signature for order, _family in works):
            continue
        height = measure_folder(folder, stls).height_mm if len(families) == 1 else None
        for order, _family in works:
            order.stl_height_mm = height
            order.stl_height_sig = signature
            changed += 1
        logger.info(
            "Висота STL: %s → %s (%d робіт, %d файл(ів)%s)",
            folder.name, f"{height:.3f} мм" if height is not None else "—",
            len(works), len(stls), ", різні матеріали" if len(families) > 1 else "",
        )
    if changed:
        db.commit()
    return changed


def _cached_z_extent(path: str) -> float | None:
    """Розмах STL із запам'ятовуванням за розміром і часом зміни: список пошти
    перемальовується раз на 15 с, а читати файл треба лише раз, поки він той самий.
    Запам'ятовує й «не прочиталось», щоб битий файл не читався щотіку."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (stat.st_size, int(stat.st_mtime))
    with _extent_lock:
        hit = _extent_cache.get(path)
        if hit is not None and hit[0] == stamp:
            _extent_cache.move_to_end(path)
            return hit[1]
    extent = stl_z_extent(Path(path))
    with _extent_lock:
        _extent_cache[path] = (stamp, extent)
        _extent_cache.move_to_end(path)
        while len(_extent_cache) > _EXTENT_CACHE_MAX:
            _extent_cache.popitem(last=False)
    return extent


def letter_height_mm(attachments) -> float | None:
    """Найвищий повернутий STL серед файлів листа: те саме правило, що для теки
    роботи (`measure_folder`), але файли беруться з вкладень, бо роботи ще немає."""
    heights = []
    for attachment in attachments or []:
        name = getattr(attachment, "filename", None) or ""
        path = getattr(attachment, "saved_path", None) or ""
        if path and (is_rotated_export(name) or is_rotated_export(path)):
            extent = _cached_z_extent(path)
            if extent is not None and 0 < extent <= MAX_PLAUSIBLE_MM:
                heights.append(extent)
    return max(heights) if heights else None


def letter_chip(attachments, badge_symbol: str | None, *, mixed: bool = False) -> dict | None:
    """Бейдж рядка пошти (власник 05.11.26): товщина за матеріалом із чіпа рядка.
    Викликати лише для листа зі скачаними файлами: недокачаний файл дав би хибну
    цифру. У листі з кількома матеріалами цифра вірна лише для одного, і підказка
    це каже."""
    family = _FAMILY_BY_BADGE.get(badge_symbol or "")
    if family is None:
        return None
    chip = chip_from(letter_height_mm(attachments), family)
    if chip and mixed:
        chip["title"] += " У листі кілька матеріалів: для іншого цифра буде інша."
    return chip


def chip_for(order) -> dict | None:
    """Чіп черги для роботи (`text`, `title`) або None."""
    family = getattr(getattr(order, "material", None), "name", None)
    return chip_from(getattr(order, "stl_height_mm", None), family)


def chip_from(height: float | None, family: str | None) -> dict | None:
    """Дані бейджа за висотою STL і родиною матеріалу, або None."""
    thickness = thickness_mm(height, family)
    if thickness is None or family is None or height is None:
        return None
    factor = SHRINK[family]
    how = f"висота STL {height:.2f} мм" + (
        f" × {factor:g} (усадка: {family.lower()})" if factor != 1.0 else ""
    )
    # Ізометричний диск (власник 03.11.26): заповнений на частку найвищого
    # диска 25 мм. Геометрія тут, щоб шаблон лише підставляв числа.
    fill = min(1.0, thickness / DISC_MAX_MM)
    top, height = 2.5, 11.0
    level = top + height * (1 - fill)
    return {
        "text": chip_text(thickness),
        "title": f"Товщина ≈{chip_text(thickness)} мм, як її покаже Sum3D: {how}. Диск обирайте з запасом.",
        "fill_y": round(level, 2),
        "fill_h": round(top + height - level, 2),
    }


def thickness_mm(height_mm: float | None, family: str | None) -> float | None:
    """Товщина, яку покаже Sum3D: висота × усадка родини; None — без підказки."""
    factor = SHRINK.get(family or "")
    if height_mm is None or factor is None:
        return None
    return height_mm * factor


def chip_text(thickness: float) -> str:
    """«14,4» — десята, кома (як решта чисел у CRM). Одиниця й «≈» — у
    підказці: бейдж стоїть у рядку з маркуванням і мусить у нього влазити."""
    return f"{thickness:.1f}".replace(".", ",")
