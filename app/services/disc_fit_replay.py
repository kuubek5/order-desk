"""Звірка підказки диска з тим, що реально зробив цех (власник 16.11.26: «щоб
програма не пропонувала диски, в яких ми явно не зможемо відфрезерувати»).

Два питання, обидва — на минулих роботах, лише читання:

* **Місце.** Слід програми `.iso` (`program_tracks`) каже, на якому обрисі
  диска Sum3D поставив роботу. Обрис — це місце, яке Sum3D реально відвів
  (силует + його тримачі й проміжок). Порівнюємо з нашим силуетом STL, роздутим
  на `disc_fit.CLEARANCE_MM`: який запас насправді, і чи сказала б наша
  розстановка «влазить» на диску, яким він був ДО цієї роботи. Диск «до» — це
  нинішній `.blk` без своїх обрисів і без обрисів програм, знятих на ньому
  пізніше. Обриси без жодного знятого сліду лишаються (коли лягли — невідомо),
  тож перепрогін усе ще може бути суворішим за реальність: «не влазить» з
  невідомими обрисами — привід розібратись, а не вирок.
* **Висота.** Робота з листа (товщина з STL × усадка, як у підказці) проти
  висоти диска з назви програми на верстаті: на скільки нижчий за наш мінімум
  диск цех уже брав. Якщо брав — наш мінімум ховав придатний диск.

Назв STL і клієнтів не віддає — лише номери робіт і назви програм (назва
програми — назва диска).
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np

BUDGET_SECONDS = 50.0
MARGINS_MM = tuple(x / 2 for x in range(0, 13))  # 0 … 6 мм кроком 0,5


def _polygon_area(poly: list[tuple[float, float]]) -> float:
    if len(poly) < 3:
        return 0.0
    xs = np.array([p[0] for p in poly])
    ys = np.array([p[1] for p in poly])
    return float(abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2)


def _silhouette_areas(pieces: list[np.ndarray], scale: float) -> dict[float, float]:
    """Площа силуетів (мм²) за різного запасу навколо — щоб знайти запас, за
    якого наша площа дорівнює площі обрисів Sum3D."""
    from app.services import disc_fit

    cell = disc_fit.GRID_MM
    out = {m: 0.0 for m in MARGINS_MM}
    for points in pieces:
        pts = points * scale
        pts = pts - pts.min(axis=0)
        cells = np.floor(pts / cell).astype(np.int64)
        h, w = int(cells[:, 1].max()) + 1, int(cells[:, 0].max()) + 1
        pad = int(math.ceil(max(MARGINS_MM) / cell)) + 2
        mask = np.zeros((h + 2 * pad, w + 2 * pad), dtype=bool)
        mask[cells[:, 1] + pad, cells[:, 0] + pad] = True
        # Вершини STL розріджені: мінімальне роздування (1 клітинка) їх зшиває.
        for margin in MARGINS_MM:
            grown = disc_fit._dilate(mask, max(1, int(math.ceil(margin / cell))))
            out[margin] += float(disc_fit._fill_holes(grown).sum()) * cell * cell
    return out


def _orders_of(db, program: str) -> list[Any]:
    """Роботи з листа з тим самим Sum3D, що в назві програми (час доби
    повторюється між днями — тому лише в межах кількох днів до програми)."""
    from sqlalchemy import select

    from app.models import Order
    from app.services.program_tracks import _TAIL, track_key

    tail = _TAIL.search(track_key(program).rstrip("_"))
    if tail is None:
        return []
    day, clock = tail.group(1)[:10], tail.group(1)[11:]
    try:
        when = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return []
    rows = db.scalars(select(Order).where(
        Order.sum3d_id.is_not(None), Order.source_email_id.is_not(None),
        Order.created_at >= when - timedelta(days=5), Order.created_at <= when + timedelta(days=2),
    )).all()
    return [o for o in rows if (o.sum3d_id or "").strip().endswith(clock)]


def _program_moment(name: Optional[str]) -> Optional[datetime]:
    """Час програми з хвоста назви (`…_2026-11-16_13-03-34`); None — без часу."""
    from app.services.program_tracks import _TAIL, track_key

    tail = _TAIL.search(track_key(name).rstrip("_"))
    if tail is None:
        return None
    try:
        return datetime.strptime(tail.group(1), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def place(db, blanks_root: Optional[str], *, limit: int = 40) -> dict[str, Any]:
    """Слід кожної знятої програми проти наших силуетів і розстановки.

    Диск «до роботи» — нинішній `.blk` без її обрисів І без обрисів програм,
    знятих на тому самому диску ПІЗНІШЕ (16.11.26: 5 із 22 «не влазить» могли
    бути лише через них). Обриси, яких не ріже жоден знятий слід
    («невідомі»), лишаються: коли вони лягли — не знаємо."""
    from app.models import EmailMessage
    from app.services import disc_fit, disc_suggest, program_tracks, sum3d_disc

    started = time.monotonic()
    try:
        files = sorted(program_tracks.cache_dir().glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        files = []
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    best_margins: list[float] = []

    def skip(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    # Прохід 1: який диск і які обриси ріже кожна програма.
    blank_paths: dict[str, Any] = {}
    shapes: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, frozenset[int]]] = set()
    for cache_file in files:
        if time.monotonic() - started > BUDGET_SECONDS:
            skip("не_встигли")
            continue
        track = program_tracks._load(cache_file)
        if track is None or not track.sides:
            skip("слід_порожній")
            continue
        if track.blank and track.blank not in blank_paths:
            blank_paths[track.blank] = sum3d_disc.find_blank(blanks_root, track.blank)
        blank = blank_paths.get(track.blank) if track.blank else None
        if blank is not None and str(blank) not in shapes:
            shapes[str(blank)] = sum3d_disc.shape_of(blank)
        shape = shapes.get(str(blank)) if blank is not None else None
        if shape is None or not shape.contours:
            skip("диска_вже_немає_або_порожній")
            continue
        report = program_tracks.match(track, shape.radius, shape.contours)
        if not report["надійно"]:
            skip("слід_ненадійний")
            continue
        own = frozenset(i - 1 for i in report["свої_обриси"])
        moment = _program_moment(track.name)
        # Та сама програма під двома назвами (`1_18-…` і `+_18-…`, той самий
        # час і ті самі обриси) — одна програма, не дві.
        key = (str(blank), moment, own)
        if key in seen:
            skip("дубль_сліду")
            continue
        seen.add(key)
        entries.append({"track": track, "disc": str(blank), "shape": shape, "own": own,
                        "moment": moment})

    known: dict[str, set[int]] = {}
    for entry in entries:
        known.setdefault(entry["disc"], set()).update(entry["own"])

    # Прохід 2: розстановка на диску, яким він був до програми.
    for entry in entries:
        if time.monotonic() - started > BUDGET_SECONDS:
            skip("не_встигли")
            continue
        track, shape, own = entry["track"], entry["shape"], entry["own"]
        orders = _orders_of(db, track.name)
        if not orders:
            skip("немає_роботи_з_листа")
            continue
        pieces: list[np.ndarray] = []
        for order in orders:
            email = db.get(EmailMessage, order.source_email_id)
            pieces += disc_suggest.letter_pieces(list(email.attachments or []) if email else [])
        if not pieces:
            skip("немає_STL_exocad")
            continue
        later: set[int] = set()
        if entry["moment"] is not None:
            for other in entries:
                if (other is not entry and other["disc"] == entry["disc"] and other["moment"] is not None
                        and other["moment"] > entry["moment"]):
                    later |= other["own"]
        later -= own
        unknown = len(shape.contours) - len(known.get(entry["disc"], set()))
        scale = disc_suggest.letter_scale(disc_suggest.want_of(orders[0].material_color).family)
        if any(disc_fit.wider_than_disc(p, shape.radius, scale) for p in pieces):
            # Сітка такої «конструкції» будувалась би годинами (disc_fit.wider_than_disc).
            skip("stl_ширше_за_диск")
            continue
        own_area = sum(_polygon_area(shape.contours[i]) for i in own)
        areas = _silhouette_areas(pieces, scale)
        margin = min(MARGINS_MM, key=lambda m: abs(areas[m] - own_area))
        best_margins.append(margin)
        strict = [c for i, c in enumerate(shape.contours) if i not in own]
        before = [c for i, c in enumerate(shape.contours) if i not in own and i not in later]
        fits = disc_fit.fits(shape.radius, before, pieces, scale)
        fits_strict = fits if not later else disc_fit.fits(shape.radius, strict, pieces, scale)
        counts["звірено"] = counts.get("звірено", 0) + 1
        skip("наше_влазить" if fits else "наше_не_влазить")
        if fits and not fits_strict:
            skip("не_влазило_лише_через_пізніші")
        if not fits and unknown:
            skip("не_влазить_є_невідомі_обриси")
        if len(rows) < limit:
            rows.append({
                "програма": track.name, "роботи": [o.id for o in orders],
                "конструкцій_stl": len(pieces), "обрисів_sum3d": len(own),
                "площа_sum3d_мм2": round(own_area),
                "наша_площа_мм2": round(sum(float(disc_fit.piece_mask(p, 0, scale).sum()) for p in pieces)
                                        * disc_fit.GRID_MM ** 2),
                "запас_як_у_sum3d_мм": margin,
                "вільно_до_роботи_%": round(100 * (1 - sum(_polygon_area(c) for c in before)
                                                    / (math.pi * shape.radius ** 2))),
                "прибрано_пізніших_обрисів": len(later),
                "невідомих_обрисів_на_диску": unknown,
                "наше_влазить": fits,
                "наше_влазило_б_з_пізнішими": fits_strict,
            })
    out: dict[str, Any] = {"програм_знято": len(files), **counts, "приклади": rows}
    if best_margins:
        ordered = sorted(best_margins)
        out["запас_sum3d_мм"] = {
            "медіана": ordered[len(ordered) // 2], "мін": ordered[0], "макс": ordered[-1],
            "у_нас_зараз": disc_fit.CLEARANCE_MM,
        }
    return out


def heights(db, days: int, *, limit: int = 30) -> dict[str, Any]:
    """Товщина роботи (як у підказці) проти висоти диска, на якому цех її
    фрезерував. Від'ємний «запас» — диск нижчий за нашу оцінку товщини."""
    from sqlalchemy import select

    from app.models import EmailMessage, MachineReading, Order
    from app.services import disc_suggest
    from app.services.disc_check import program_disc
    from app.services.stl_height import thickness_mm

    started = time.monotonic()
    since = datetime.now() - timedelta(days=days)
    orders = db.scalars(select(Order).where(
        Order.created_at >= since, Order.source_email_id.is_not(None), Order.sum3d_id.is_not(None),
    ).order_by(Order.id.desc())).all()
    by_family: dict[str, list[float]] = {}
    below: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for order in orders:
        if time.monotonic() - started > BUDGET_SECONDS:
            counts["не_встигли"] = counts.get("не_встигли", 0) + 1
            continue
        tail = (order.sum3d_id or "").strip()[-8:]
        iso = db.scalar(select(MachineReading.iso_name).where(
            MachineReading.sum3d_id == tail, MachineReading.iso_name.is_not(None),
            MachineReading.captured_at >= (order.created_at or since) - timedelta(days=1),
        ).limit(1)) if tail else None
        disc = program_disc(iso)
        if disc is None or not disc.height:
            counts["диска_з_програми_немає"] = counts.get("диска_з_програми_немає", 0) + 1
            continue
        email = db.get(EmailMessage, order.source_email_id)
        raw = disc_suggest.letter_height(list(email.attachments or []) if email else [])
        want = disc_suggest.want_of(order.material_color)
        thick = thickness_mm(raw, want.family) if raw else None
        if raw is None or thick is None:
            counts["товщини_немає"] = counts.get("товщини_немає", 0) + 1
            continue
        counts["звірено"] = counts.get("звірено", 0) + 1
        spare = disc.height - thick
        by_family.setdefault(want.family or "—", []).append(spare)
        if spare < disc_suggest.MARGIN_MM:
            counts["диск_нижчий_за_наш_мінімум"] = counts.get("диск_нижчий_за_наш_мінімум", 0) + 1
            if len(below) < limit:
                below.append({"робота": order.id, "програма": iso, "товщина_мм": round(thick, 1),
                              "висота_без_усадки_мм": round(raw, 1), "диск_мм": disc.height,
                              "запас_мм": round(spare, 1)})
    families = {}
    for family, spares in by_family.items():
        ordered = sorted(spares)
        families[family] = {"робіт": len(ordered), "найменший_запас_мм": round(ordered[0], 1),
                            "медіана_запасу_мм": round(ordered[len(ordered) // 2], 1)}
    return {"днів": days, "робіт": len(orders), **counts, "за_родиною": families,
            "наш_мінімум": f"товщина + {disc_suggest.MARGIN_MM:g} мм", "нижче_за_мінімум": below}


def replay(db, days: int = 30, limit: int = 30) -> dict[str, Any]:
    from app.settings_store import get_setting

    root = get_setting(db, "cam_blanks_path")
    return {
        "місце": place(db, root, limit=limit),
        "висота": heights(db, days, limit=limit),
        "пояснення": (
            "«місце»: знята програма .iso → свій обрис на диску (як поставив Sum3D) проти нашого "
            "силуету STL. «запас_як_у_sum3d_мм» — за якого запасу наша площа дорівнює площі "
            "обрису Sum3D; «наше_влазить» — наша розстановка на диску без цієї роботи й без обрисів "
            "програм, знятих на ньому пізніше («наше_влазило_б_з_пізнішими» — як рахували до "
            "16.11.26). Обриси без знятого сліду («невідомих_обрисів_на_диску») лишаються — коли "
            "лягли, невідомо. «висота»: товщина з STL × "
            "усадка проти висоти диска з назви програми. Нічого не змінює."
        ),
    }
