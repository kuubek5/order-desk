"""Інструменти MCP про файли Sum3D: диски `.blk`, проєкти `.cam`, налаштування.
Винесено з `mcp_tools.py` без змін (12.11.26). Лише читання."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order
from app.services import sum3d_blank_shape, sum3d_deep, sum3d_files
from app.services.mcp_common import Tool, ToolError, _int_arg
from app.services.order_dates import parse_sheet_tab


def tool_sum3d_files(db: Session, args: dict) -> dict[str, Any]:
    """Формат файлів дисків `.blk` і проєктів `.cam` Sum3D — щоб знайти, де
    лежить коефіцієнт усадки (`app/services/sum3d_files.py`). Лише числа й
    назви ключів: текстових значень і назв STL не віддає."""
    from app.settings_store import get_setting, get_sum3d_projects_path

    kind = str(args.get("kind") or "both").strip().lower()
    if kind not in ("both", "blk", "cam", "settings", "cam_deep", "cam_compare", "blk_shape", "board_disc", "disc_link"):
        raise ToolError(
            "`kind` — blk, cam, both, settings, cam_deep, cam_compare, blk_shape, board_disc або disc_link (типово both)"
        )
    limit = _int_arg(args, "limit", default=2, low=1, high=5)
    blanks_root = (get_setting(db, "cam_blanks_path") or "").strip()
    projects_root = (get_sum3d_projects_path(db) or "").strip()
    if kind == "settings":
        return {
            "налаштування": sum3d_deep.scan_settings(sum3d_deep.settings_roots(blanks_root)),
            "реєстр": sum3d_deep.scan_registry(),
        }
    if kind == "blk_shape":
        return _sum3d_blank_shape(blanks_root, str(args.get("brand") or ""), args)
    if kind == "board_disc":
        return _sum3d_board_disc(db, blanks_root, projects_root)
    if kind == "disc_link":
        return _sum3d_disc_link(db, blanks_root, projects_root, args)
    if kind in ("cam_deep", "cam_compare") and not projects_root:
        raise ToolError("тека проєктів Sum3D не задана (Налаштування → Шляхи)")
    if kind == "cam_deep":
        return _sum3d_cam_deep(db, projects_root, args, limit)
    if kind == "cam_compare":
        plain, shrunk = _sum3d_cam_groups(db, projects_root, limit)
        return sum3d_deep.compare_groups(plain, shrunk)
    out: dict[str, Any] = {
        "тека_дисків": blanks_root or None,
        "тека_проєктів": projects_root or None,
    }
    if kind in ("both", "blk"):
        if not blanks_root:
            out["диски"] = "тека дисків CAM не задана («Нові диски» → нижня смуга)"
        else:
            found = sum3d_files.newest_blanks(
                blanks_root, brand=str(args.get("brand") or ""), limit=limit
            )
            out["диски"] = [_sum3d_file_entry(f) for f in found] or "файлів .blk не знайдено"
            # Схема формату — слова, спільні для КІЛЬКОХ дисків; назва STL з
            # іменем пацієнта своя в кожному диску й у перетин не потрапить.
            if len(found) >= 3:
                out["спільні_слова_дисків"] = sum3d_files.common_words(f.path for f in found)[:200]
            elif found:
                out["спільні_слова_дисків"] = "потрібно ≥3 дисків (limit=3…5)"
            # ПММА без усадки проти цирконію — де в ПММА 1.0, там коефіцієнт.
            if len(found) >= 2:
                out["порівняння_заголовків"] = sum3d_files.compare_headers(
                    [(f.name, f.path) for f in found]
                )
    if kind in ("both", "cam"):
        if not projects_root:
            out["проєкти"] = "тека проєктів Sum3D не задана (Налаштування → Шляхи)"
        else:
            found = sum3d_files.newest_projects(
                projects_root, sum3d_id=str(args.get("sum3d_id") or ""), limit=limit
            )
            out["проєкти"] = [_sum3d_file_entry(f) for f in found] or "файлів .cam не знайдено"
    return out


def _sum3d_blank_shape(root: str, brand: str, args: dict) -> dict[str, Any]:
    """Геометрія файлу диска: полілінії вирізів і службові проміжки
    (`sum3d_blank_shape`). Один диск за виклик — відповідь велика; два —
    лише на явне `limit=2`, щоб звірити новий диск з ужитим."""
    if not root:
        raise ToolError("тека дисків CAM не задана («Нові диски» → нижня смуга)")
    limit = _int_arg(args, "limit", default=1, low=1, high=2)
    found = sum3d_files.newest_blanks(root, brand=brand, limit=limit)
    return {
        "тека_дисків": root,
        "диски": [
            {
                "файл": f.name,
                "розмір": f.size,
                "змінено": datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M:%S"),
                **sum3d_blank_shape.describe_shape(f.path),
            }
            for f in found
        ] or "файлів .blk не знайдено",
    }


def _sum3d_board_disc(db: Session, blanks_root: str, projects_root: str) -> dict[str, Any]:
    """Що табло покаже як диск кожного верстата (`sum3d_disc`, `sum3d_names`):
    повна назва програми (для CORiTEC — з проєкту Sum3D, синхронно), файл
    диска, обриси, вільна частка і малюнок дисків — звірити з Sum3D оком."""

    from PIL import Image, ImageDraw

    from app.services import sum3d_disc
    from app.services.device_diag import _encode
    from app.services.machines import snapshot

    rows: list[dict[str, Any]] = []
    tiles: list[Any] = []
    for card in snapshot(db):
        if card.is_sisma_machine:
            continue
        iso, program, how, path, disc_how = _machine_disc(card, blanks_root, projects_root)
        shape = sum3d_disc.shape_of(path) if path else None
        row: dict[str, Any] = {
            "верстат": card.target.name,
            "програма_з_екрана": iso,
            "програма_для_табло": program,
            "звідки_назва": how,
            "ключ_диска": sum3d_disc.disc_key(program),
            "файл_диска": path.name if path else None,
            "звідки_диск": disc_how,
        }
        if shape is not None:
            row.update({
                "обрисів": len(shape.contours),
                "радіус_мм": shape.radius,
                "вільно_%": round(shape.free_share * 100),
                "кадр": f"д{len(tiles) + 1}",
            })
            img = Image.new("RGB", (300, 300), (24, 20, 14))
            draw = ImageDraw.Draw(img)
            k = 130 / shape.radius
            draw.ellipse([20, 20, 280, 280], outline=(255, 60, 220), width=2)
            for contour in shape.contours:
                draw.polygon([(150 + x * k, 150 - y * k) for x, y in contour], outline=(255, 212, 137))
            draw.text((8, 6), f"{row['кадр']} {card.target.name}", fill=(240, 220, 160))
            tiles.append(img)
        rows.append(row)
    result: dict[str, Any] = {"тека_дисків": blanks_root or None, "тека_проєктів": projects_root or None,
                              "верстати": rows}
    if tiles:
        cols = min(3, len(tiles))
        sheet = Image.new("RGB", (cols * 300, ((len(tiles) + cols - 1) // cols) * 300), (12, 13, 18))
        for i, tile in enumerate(tiles):
            sheet.paste(tile, ((i % cols) * 300, (i // cols) * 300))
        note: list[str] = []
        result["_image"] = _encode(sheet, note)
    return result


def _machine_disc(
    card: Any, blanks_root: str, projects_root: str
) -> tuple[Optional[str], Optional[str], str, Optional[Path], Optional[str]]:
    """(програма з екрана, програма для табло, звідки назва, файл диска,
    звідки диск) — так само, як табло (`shop_board`), але синхронно."""
    from app.services import sum3d_disc, sum3d_names

    iso = getattr(card, "iso_name", None)
    program, how = iso, "екран/заголовок"
    parts = sum3d_names.bare_parts(iso)
    if parts and projects_root:
        try:
            found = sum3d_names.find_in_project(Path(projects_root) / f"{parts[0]}_{parts[1]}.cam", *parts)
        except OSError as exc:
            found, how = None, f"проєкт не прочитано: {exc.__class__.__name__}"
        if found:
            program, how = found, "з проєкту Sum3D"
        elif how == "екран/заголовок":
            how = "у проєкті назви не знайдено"
    path = sum3d_disc.find_blank(blanks_root, program)
    disc_how = "з назви програми" if path else None
    if path is None and parts and projects_root and how == "у проєкті назви не знайдено":
        try:
            stem = sum3d_names.find_blank_in_project(Path(projects_root) / f"{parts[0]}_{parts[1]}.cam")
        except OSError:
            stem = None
        path = sum3d_disc.find_blank(blanks_root, stem) if stem else None
        disc_how = "з проєкту Sum3D (файл диска)" if path else "у проєкті диска не знайдено"
    return iso, program, how, path, disc_how


_PROGRAM_TAIL = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(?:\.iso)?$", re.I)


def _sum3d_disc_link(
    db: Session, blanks_root: str, projects_root: str, args: dict
) -> dict[str, Any]:
    """Чи видно в проєкті програми, ЯКІ обриси диска її (`sum3d_disc_link`):
    точки обрисів і зсуви матриць. Кадр: обриси зі слідом — яскраві, решта —
    тьмяні; звірити з Sum3D, що підсвітилась саме ця робота."""
    from PIL import Image, ImageDraw

    from app.services import sum3d_disc, sum3d_disc_link
    from app.services.device_diag import _encode
    from app.services.machines import snapshot

    if not projects_root:
        raise ToolError("тека проєктів Sum3D не задана (Налаштування → Шляхи)")
    wanted = str(args.get("machine") or "").strip().lower()
    rows: list[dict[str, Any]] = []
    tiles: list[Any] = []
    for card in snapshot(db):
        if card.is_sisma_machine or (wanted and wanted not in card.target.name.lower()):
            continue
        iso, program, _how, path, _disc_how = _machine_disc(card, blanks_root, projects_root)
        tail = _PROGRAM_TAIL.search(program or "")
        row: dict[str, Any] = {"верстат": card.target.name, "програма": program,
                               "файл_диска": path.name if path else None}
        rows.append(row)
        if tail is None or path is None:
            row["пропущено"] = "немає дати/Sum3D у назві" if tail is None else "диска не знайдено"
            continue
        day, sum3d_id = tail.group(1), tail.group(2)
        row["одиниць_у_черзі"] = _units_for(db, day, sum3d_id)
        shape = sum3d_disc.shape_of(path)
        project = Path(projects_root) / f"{day}_{sum3d_id}.cam"
        try:
            with open(project, "rb") as handle:
                data = handle.read(sum3d_files.MAX_READ_BYTES)
        except OSError as exc:
            row["пропущено"] = f"проєкт не прочитано: {exc.__class__.__name__}"
            continue
        if shape is None:
            row["пропущено"] = "диск не розібрано"
            continue
        row["проєкт_змінено"] = datetime.fromtimestamp(project.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        row["диск_змінено"] = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        report = sum3d_disc_link.link_report(data, shape)
        row.update(report)
        row["кадр"] = f"д{len(tiles) + 1}"
        traced = set(report["обриси_зі_слідом"])
        img = Image.new("RGB", (300, 300), (24, 20, 14))
        draw = ImageDraw.Draw(img)
        k = 130 / shape.radius
        draw.ellipse([20, 20, 280, 280], outline=(255, 60, 220), width=2)
        for i, contour in enumerate(shape.contours):
            pts = [(150 + x * k, 150 - y * k) for x, y in contour]
            if i + 1 in traced:
                draw.polygon(pts, outline=(120, 255, 160), fill=(40, 110, 60))
            else:
                draw.polygon(pts, outline=(110, 95, 70))
        draw.text((8, 6), f"{row['кадр']} {card.target.name}", fill=(240, 220, 160))
        tiles.append(img)
    result: dict[str, Any] = {"верстати": rows}
    if tiles:
        cols = min(3, len(tiles))
        sheet = Image.new("RGB", (cols * 300, ((len(tiles) + cols - 1) // cols) * 300), (12, 13, 18))
        for i, tile in enumerate(tiles):
            sheet.paste(tile, ((i % cols) * 300, (i // cols) * 300))
        note: list[str] = []
        result["_image"] = _encode(sheet, note)
    return result


def _units_for(db: Session, day: str, sum3d_id: str) -> dict[str, int]:
    """Робіт і одиниць у черзі з цим Sum3D у день проєкту (±1) — лише числа."""
    when = _cam_day(day)
    works = units = 0
    for order in db.scalars(select(Order).where(Order.sum3d_id.like(f"%{sum3d_id}%"))).all():
        tab = parse_sheet_tab(order.sheet_tab)
        if when is None or tab is None or abs((tab - when).days) > 1:
            continue
        works += 1
        digits = re.match(r"\s*(\d+)", order.quantity or "")
        units += int(digits.group(1)) if digits else 0
    return {"робіт": works, "одиниць": units}


def _sum3d_cam_deep(db: Session, root: str, args: dict, limit: int) -> dict[str, Any]:
    """Розвідка одного проєкту: масштаб у матрицях, задане число, вершини STL
    робіт цього проєкту (`sum3d_deep.probe_cam`)."""
    tail = str(args.get("sum3d_id") or "").strip()
    if not tail:
        raise ToolError("для `cam_deep` потрібен `sum3d_id` (13-12-29)")
    # Клієнт зі старою схемою інструмента шле число рядком (прод 04.11.26).
    raw = args.get("value")
    if isinstance(raw, str):
        try:
            raw = float(raw.strip().replace(",", "."))
        except ValueError:
            raise ToolError("`value` — число (1.25)") from None
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float))):
        raise ToolError("`value` — число (1.25)")
    found = sum3d_files.newest_projects(root, sum3d_id=tail, limit=limit)
    if not found:
        return {"проєкти": "файлів .cam не знайдено"}
    return {
        "проєкти": [
            {
                "файл": f.name,
                **sum3d_deep.probe_cam(
                    f.path,
                    stl_paths=_sum3d_project_stls(db, f.name, tail),
                    expected=float(raw) if raw is not None else None,
                ),
            }
            for f in found
        ]
    }


def _cam_day(name: str) -> Optional[date]:
    try:
        return datetime.strptime(name[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _sum3d_project_stls(db: Session, cam_name: str, tail: str) -> list[str]:
    """STL робіт проєкту. Sum3D ID — лише час доби, тож робота мусить бути
    з дня проєкту (±1), як і скрізь, де його зіставляють."""
    from app.services import stl_height
    from app.settings_store import get_export_folder_path, get_technician_files_path

    day = _cam_day(cam_name)
    export_root = (get_export_folder_path(db) or "").strip()
    tech_root = get_technician_files_path(db)
    paths: list[str] = []
    orders = db.scalars(select(Order).where(Order.sum3d_id.like(f"%{tail}%"))).all()
    for order in orders:
        tab = parse_sheet_tab(order.sheet_tab)
        if day is not None and (tab is None or abs((tab - day).days) > 1):
            continue
        folder = stl_height.work_folder(order, tech_root, Path(export_root) if export_root else None)
        if folder is not None:
            paths += [str(p) for p in stl_height.rotated_stls(folder)]
    return paths[:5]


def _sum3d_cam_groups(
    db: Session, root: str, limit: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Проєкти ПММА (без усадки) і цирконію — родина береться з роботи."""
    by_name = {f.name: f for f in sum3d_files.newest_projects(root, limit=10**6)}
    plain: dict[str, str] = {}
    shrunk: dict[str, str] = {}
    orders = db.scalars(
        select(Order).where(Order.sum3d_id.isnot(None)).order_by(Order.id.desc()).limit(1000)
    ).all()
    for order in orders:
        family = order.material.name if order.material is not None else None
        target = {"ПММА": plain, "Цирконій": shrunk}.get(family or "")
        day = parse_sheet_tab(order.sheet_tab)
        if target is None or day is None or len(target) >= limit:
            continue
        tail = (order.sum3d_id or "").strip()[-8:]
        for delta in (0, -1, 1):
            name = f"{day + timedelta(days=delta):%Y-%m-%d}_{tail}.cam"
            if name in by_name:
                target.setdefault(name, by_name[name].path)
                break
    return sorted(plain.items()), sorted(shrunk.items())


def _sum3d_file_entry(found: sum3d_files.FoundFile) -> dict[str, Any]:
    return {
        "файл": found.name,
        "розмір": found.size,
        "змінено": datetime.fromtimestamp(found.mtime).strftime("%Y-%m-%d %H:%M:%S"),
        **sum3d_files.describe_file(found.path),
    }


SUM3D_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="kmill_sum3d_files",
        description=(
            "Що всередині файлів Sum3D: дисків `.blk` (тека «Нові диски») і "
            "проєктів `.cam` (Cam-work) — формат, назви ключів і ЧИСЛА, окремо ті, "
            "що біля слів shrink/scale/factor і в межах 1.10–1.40. Бінарний файл: "
            "ентропія, zlib/gzip-потоки всередині (розпаковані), слова в "
            "ASCII/UTF-16LE/BE; у малому файлі — усі «круглі» double/float (обидва "
            "порядки байтів) як коефіцієнт, обернений (0.8) чи відсотки; для ≥3 "
            "дисків — слова, спільні для всіх; для ≥2 — звірка початку файлів "
            "число-в-число (де в ПММА 1.0, а в цирконії 1.1–1.4). kind=settings — "
            "налаштування Sum3D і реєстр; kind=cam_deep (sum3d_id, value) — матриці "
            "масштабу, задане число, вершини STL у проєкті; kind=cam_compare — "
            "проєкти ПММА проти цирконію. Для пошуку, де Sum3D тримає "
            "коефіцієнт усадки диска. kind=blk_shape (brand — підрядок назви "
            "диска, limit 1–2) — геометрія диска: ряди чисел, полілінії вирізів "
            "на одній висоті (межі, площа, проріджені точки) і службові проміжки "
            "між ними шістнадцятково, літери затерто; щоб розібрати, де на диску "
            "вже вирізано. kind=board_disc — що табло покаже як диск кожного "
            "верстата: повна назва програми (CORiTEC — з проєкту Sum3D), файл "
            "диска, обриси, вільно %, малюнок дисків. kind=disc_link (machine — підрядок назви верстата) — чи видно в проєкті Sum3D програми, ЯКІ обриси диска її: точки обрисів (double/float) і зсуви матриць усередині обрису, одиниць у черзі з цим Sum3D, малюнок (обриси зі слідом зелені). Лише читання; текстових значень і назв STL "
            "(імена пацієнтів) не віддає."
        ),
        schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "blk, cam, both (типово), settings, cam_deep, cam_compare, blk_shape, board_disc, disc_link."},
                "machine": {"type": "string",
                            "description": "Для disc_link: підрядок назви верстата (boris) — лише він."},
                "value": {"type": "number",
                          "description": "Для cam_deep: очікуваний коефіцієнт (1.25) — точний пошук і вершини STL × value."},
                "brand": {"type": "string",
                          "description": "Підрядок назви диска (monolith, emo) — фільтр .blk; "
                                         "кілька через кому (pmma,monolith) — по limit кожного."},
                "sum3d_id": {"type": "string",
                             "description": "Хвіст Sum3D ID (13-12-29) — конкретний проєкт .cam."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5,
                          "description": "Скільки найновіших файлів кожного виду (типово 2; "
                                         "для blk_shape типово 1, не більше 2)."},
            },
            "additionalProperties": False,
        },
        run=tool_sum3d_files,
    ),
)
