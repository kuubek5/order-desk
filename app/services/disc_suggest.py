"""Підказка диска при прийнятті листа (власник 12.11.26) — ЛИШЕ читання.

Під полем «Матеріал · колір» картки листа (варіант A) — один рядок: який диск
взяти, скільки в ньому вільно й чи влізе робота. Правила власника:

1. Матеріал, лінійка й колір диска = матеріал і колір листа.
2. Висота: найвища конструкція листа (STL × усадка) + запас **мінімум 1 мм**
   → найменша висота, що ФІЗИЧНО є серед дисків (14,7 → 16, бо 15 немає).
3. Одноколірні (ПММА, віск…) — саме ця висота.
4. Мультиколор (Emotions/«емо», Katana, Monolith — знизу темний, далі світліше):
   ця висота АБО ще одна вище — щоб висота диска була близька до роботи.
5. Monolith — ЗАВЖДИ на крок вище (високий світлий шар): влазить у 14 → 16.
6. Спершу ПОЧАТИЙ диск, у який робота влізе; новий — лише коли такого немає.

**Що це не обіцяє.** «Влізе» — наближено: вільна площа диска проти суми
основ конструкцій із запасом на проміжки. Точно розставляє лише Sum3D.
Висота відома лише з експорту exocad (`…_cad.stl`) — інакше чесно «висоту не
визначено». Диск, вирізаний майже весь (<15 % вільно), найімовірніше вже
викинуто — його не пропонуємо. Нічого не змінює ні в базі, ні на диску.
"""

from __future__ import annotations

import math
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from app import material_match
from app.material_classifier import PMMA, ZIRCON, classify_material
from app.services import disc_fit, sum3d_disc
from app.services.cam_blanks import parse_blank_name, scan_blanks
from app.services.stl_height import MAX_PLAUSIBLE_MM, _cached_z_extent, is_rotated_export, thickness_mm

# Мінімальний запас між висотою роботи й диска (власник 14.11.26, бліц: 0,5;
# було 1 мм — цех ставить 13,4 мм на диск 14).
MARGIN_MM = 0.5
# Monolith іде на крок вище лише ВПРИТУЛ: коли до базової висоти лишається
# менше цього (власник 14.11.26, бліц; було «завжди» — у 15 з 32 розбіжних
# робіт цех узяв базову висоту: 9,4 мм на диск 12).
STEP_UP_WITHIN_MM = 2.0
# Майже вирізаний диск — найімовірніше вже викинуто. Було 15 %, але «влазить
# чи ні» тепер вирішує розстановка (disc_fit), а 15 % відкидали робочі диски:
# x51 (Monolith A3.5 14) 14.11.26 Sum3D узяв, підказка — ні.
NEARLY_USED = 0.05
# Основи конструкцій не лягають щільно: проміжки, конектори, краї.
PACKING = 1.8
# Скільки дисків перевіряти справжньою розстановкою за одну підказку.
MAX_FIT_TRIES = 12
# Лінійки виробника: слово з назви диска → (назва для людей, мультиколор?).
LINES = {
    "monolith": ("Monolith", True),
    "emotions": ("Emotions", True),
    "katana": ("Katana", True),
    "z-nature": ("Z-Nature", True),
    "z-smile": ("Z-Smile", True),
    "doceram": ("Doceram", False),
}
# Як лінійку пишуть у таблиці й листах.
_LINE_WORDS = {
    "monolith": ("mono", "моно", "monolith", "моноліт", "монолит"),
    "emotions": ("emo", "емо", "emotion", "емоушен", "эмо"),
    "katana": ("katana", "катана"),
    "z-nature": ("nature", "натур"),
    "z-smile": ("smile", "смайл"),
    "doceram": ("doceram", "доцерам"),
}
STEP_UP_LINES = {"monolith"}
# Опак Doceram кодом замість кольору (CLAUDE.md §3: 500 = A1, 800 = A2, 1000 = A3).
# Це ОКРЕМІ диски: «1000» — це не будь-який A3 (14.11.26: робота «1000» отримала
# підказку Monolith A3, а цех у всіх чотирьох таких роботах узяв диск «1000»).
OPAQUE_CODES = ("500", "800", "1000")
# Диск, який за ці дні був у верстаті (назва програми), — «активний»: цех кладе
# кілька робіт на диск, що вже стоїть (14.11.26: 51 збіг висоти, диск — 1 раз).
ACTIVE_DAYS = 3
SETTINGS_ENABLED_KEY = "disc_suggest_enabled"
SETTINGS_USERS_KEY = "disc_suggest_users"
_SCAN_TTL = 60.0


# ── Доступ ──────────────────────────────────────────────────────────────────


def allowed_user_ids(db) -> set[int]:
    import json

    from app.settings_store import get_setting

    try:
        raw = json.loads(get_setting(db, SETTINGS_USERS_KEY) or "[]")
    except ValueError:
        return set()
    return {int(x) for x in raw if str(x).isdigit()} if isinstance(raw, list) else set()


def globally_enabled(db) -> bool:
    from app.settings_store import get_setting

    return (get_setting(db, SETTINGS_ENABLED_KEY) or "") == "1"


def enabled_for(db, user) -> bool:
    """Загальний вимикач увімкнено І людині підказку дав адміністратор."""
    if user is None or not globally_enabled(db):
        return False
    return getattr(user, "id", None) in allowed_user_ids(db)


# ── Що просить лист ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Want:
    family: Optional[str]
    line: Optional[str]
    shades: frozenset[str]
    code: Optional[str] = None

    @property
    def multilayer(self) -> bool:
        return bool(self.line and LINES.get(self.line, ("", False))[1])

    def label(self) -> str:
        name = LINES[self.line][0] if self.line else (self.family or "")
        if self.code:
            return f"{name} {self.code}".strip()
        shade = "/".join(sorted(s.upper() for s in self.shades))
        return f"{name} {shade}".strip()


def want_of(material_text: Optional[str]) -> Want:
    text = (material_text or "").lower()
    line = next((key for key, words in _LINE_WORDS.items()
                 if any(w in text for w in words)), None)
    family = classify_material(material_text) if material_text else None
    if line and family is None:
        family = ZIRCON
    found = material_match.shades(material_text)
    return Want(family=family, line=line,
                shades=frozenset(_norm(s) for s in found),
                code=next((c for c in OPAQUE_CODES if c in found), None))


def _norm(shade: str) -> str:
    from app.services.disc_check import CODE_SHADES

    return CODE_SHADES.get(shade, shade)


# ── Диски в теці ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiscFile:
    path: Path
    stem: str
    height: Optional[int]
    family: Optional[str]
    line: Optional[str]
    shades: frozenset[str]
    code: Optional[str] = None

    @property
    def key(self) -> str:
        """Як диск зветься в назві програми без позначки верстата —
        `14-monolith-a2-x47` (той самий ключ, що `disc_check.program_disc`)."""
        return _key_of(self.stem)


_scan_cache: dict[str, tuple[float, list[DiscFile]]] = {}
_scan_lock = threading.Lock()


def disc_files(root: Optional[str]) -> list[DiscFile]:
    """Усі диски теки CAM із розібраною назвою; кеш на хвилину."""
    root = (root or "").strip()
    if not root:
        return []
    now = time.monotonic()
    with _scan_lock:
        hit = _scan_cache.get(root)
        if hit and now - hit[0] < _SCAN_TTL:
            return hit[1]
    out = []
    for item in scan_blanks(root):
        stem = item.file_name.rsplit(".", 1)[0]
        parsed = parse_blank_name(stem)
        low = stem.lower()
        brand = (parsed.brand or "").lower()
        line = brand if brand in LINES else next((k for k in LINES if k in low), None)
        head = low.split("-", 1)[0].lstrip("0123456789")
        if head.startswith("pmma"):
            family: Optional[str] = PMMA
        elif line or head.startswith(("zr", "d98_zr")) or "_zr" in head:
            family = ZIRCON
        else:
            family = classify_material(f"{brand} {head}") if brand or head else None
        shade_text = (parsed.shade or "").replace("-5", ".5")
        out.append(DiscFile(
            path=Path(root) / item.rel_path, stem=stem, height=parsed.height,
            family=family, line=line,
            shades=frozenset(_norm(s) for s in material_match.shades(shade_text)),
            code=(parsed.shade or "") if (parsed.shade or "") in OPAQUE_CODES else None,
        ))
    with _scan_lock:
        _scan_cache[root] = (now, out)
    return out


def _matches(disc: DiscFile, want: Want) -> bool:
    if want.family and disc.family != want.family:
        return False
    if want.line and disc.line != want.line:
        return False
    if want.shades and not (disc.shades & want.shades):
        return False
    if want.code != disc.code:
        return False  # опак кодом — лише свої диски, і навпаки
    return disc.height is not None


# ── Висота ──────────────────────────────────────────────────────────────────


def letter_height(attachments: Iterable[Any]) -> Optional[float]:
    heights = []
    for att in attachments or []:
        name = getattr(att, "filename", None) or ""
        path = getattr(att, "saved_path", None) or ""
        if path and (is_rotated_export(name) or is_rotated_export(path)):
            extent = _cached_z_extent(path)
            if extent is not None and 0 < extent <= MAX_PLAUSIBLE_MM:
                heights.append(extent)
    return max(heights) if heights else None


def target_heights(thickness: float, available: list[int], want: Want) -> tuple[list[int], str]:
    """Допустимі висоти диска й пояснення правила."""
    need = thickness + MARGIN_MM
    fitting = [h for h in available if h >= need]
    if not fitting:
        return [], f"потрібно ≥{need:.1f} мм — такого диска немає"
    base = fitting[0]
    above = fitting[1] if len(fitting) > 1 else None
    why = f"≈{thickness:.1f} + запас {MARGIN_MM:g} → {base} мм"
    if want.line in STEP_UP_LINES and base - thickness < STEP_UP_WITHIN_MM:
        if above is None:
            return [base], why + f"; {LINES[want.line][0]} впритул, +1 крок — вищого немає"
        return [above], why + (f"; {LINES[want.line][0]} впритул (запас < {STEP_UP_WITHIN_MM:g} мм)"
                               f" → на крок вище, {above} мм")
    if want.multilayer and above is not None:
        return [base, above], why + f" (мультиколор: {base} або {above})"
    return [base], why


# ── Чи влізе ────────────────────────────────────────────────────────────────

_FACET = struct.Struct("<12fH")
_fp_cache: dict[str, tuple[tuple[int, int], Optional[float]]] = {}


def footprint_mm2(path: str) -> Optional[float]:
    """Площа основи STL (габарит по X×Y) у мм², з кешем за розміром і часом."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    stamp = (st.st_size, int(st.st_mtime))
    hit = _fp_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    area: Optional[float] = None
    try:
        data = Path(path).read_bytes()
    except OSError:
        data = b""
    xs: list[float] = []
    ys: list[float] = []
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count and 84 + count * _FACET.size == len(data):
            for f in _FACET.iter_unpack(memoryview(data)[84:]):
                xs += (f[3], f[6], f[9])
                ys += (f[4], f[7], f[10])
    if not xs and data[:5].lower() == b"solid":
        for line in data.decode("ascii", "ignore").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                try:
                    xs.append(float(parts[1]))
                    ys.append(float(parts[2]))
                except ValueError:
                    continue
    if xs:
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    _fp_cache[path] = (stamp, area)
    return area


def letter_footprint(attachments: Iterable[Any], family: Optional[str]) -> Optional[float]:
    """Сума основ конструкцій листа після спікання (мм²), або None."""
    total = 0.0
    found = False
    factor = (thickness_mm(1.0, family) or 1.0) ** 2
    for att in attachments or []:
        name = getattr(att, "filename", None) or ""
        path = getattr(att, "saved_path", None) or ""
        if path and (is_rotated_export(name) or is_rotated_export(path)):
            area = footprint_mm2(path)
            if area:
                total += area * factor
                found = True
    return total if found else None


def letter_scale(family: Optional[str]) -> float:
    """Усадка в площині диска (та сама, що для висоти)."""
    return thickness_mm(1.0, family) or 1.0


def letter_pieces(attachments: Iterable[Any]) -> list[Any]:
    """Точки силуету кожної конструкції листа (STL exocad), без усадки."""
    out = []
    for att in attachments or []:
        name = getattr(att, "filename", None) or ""
        path = getattr(att, "saved_path", None) or ""
        if path and (is_rotated_export(name) or is_rotated_export(path)):
            points = disc_fit.stl_points(path)
            if points is not None and len(points) >= 3:
                out.append(points)
    return out


# ── Підказка ────────────────────────────────────────────────────────────────


@dataclass
class Pick:
    name: str
    height: int
    free_pct: int
    started: bool
    paths: list[str]
    on_machine: bool = False
    # На якому верстаті стоїть (назва з Налаштувань), коли відомо.
    machine: str = ""
    # Розстановка конструкцій: True — знайшла місце, False — не знайшла (диск
    # лишається підказкою з попередженням: розстановка наближена), None — не
    # перевірялась (немає STL з exocad).
    fit: Optional[bool] = None


@dataclass
class Suggestion:
    want: Want
    thickness: Optional[float] = None
    heights: list[int] = field(default_factory=list)
    why: str = ""
    best: Optional[Pick] = None
    alts: list[Pick] = field(default_factory=list)
    new_disc: str = ""
    note: str = ""
    # Скільки дисків пройшли суму площ, але розстановка конструкцій на них не
    # вдалась (клаптики між вирізами).
    rejected: int = 0


def active_discs(db, *, until: Optional[datetime] = None, days: int = ACTIVE_DAYS,
                 machines: Optional[dict[str, str]] = None) -> dict[str, datetime]:
    """Диски з назв програм на верстатах за `days` днів до `until`: ключ → коли
    востаннє. `machines` (якщо передано) наповнюється ключ → назва верстата,
    на якому диск був востаннє (власник 14.11.26: писати, НА ЯКОМУ верстаті).
    Лише читання `MachineReading` і `Machine`."""
    from sqlalchemy import func, select

    from app.models import Machine, MachineReading
    from app.services.disc_check import program_disc

    end = until or datetime.now()
    rows = db.execute(
        select(MachineReading.iso_name, MachineReading.host, func.max(MachineReading.captured_at))
        .where(MachineReading.iso_name.is_not(None),
               MachineReading.captured_at >= end - timedelta(days=days),
               MachineReading.captured_at <= end)
        .group_by(MachineReading.iso_name, MachineReading.host)
    ).all()
    names = {host: name for host, name in db.execute(select(Machine.host, Machine.name)).all()}
    out: dict[str, datetime] = {}
    for iso, host, seen in rows:
        disc = program_disc(iso)
        if disc is not None and seen is not None and (disc.key not in out or seen > out[disc.key]):
            out[disc.key] = seen
            if machines is not None:
                machines[disc.key] = names.get(host, "")
    return out


def suggest(material_text: Optional[str], attachments: list[Any], blanks_root: Optional[str],
            *, max_reads: int = 60, active: Optional[dict[str, datetime]] = None,
            machines: Optional[dict[str, str]] = None) -> Suggestion:
    want = want_of(material_text)
    result = Suggestion(want=want)
    if not want.family or not want.shades and want.family == ZIRCON:
        result.note = "немає матеріалу й кольору — підказки немає"
        return result
    discs = [d for d in disc_files(blanks_root) if _matches(d, want)]
    if not discs:
        result.note = f"дисків {want.label()} у теці немає"
        return result
    height = letter_height(attachments)
    if height is None:
        result.note = "висоту не визначено (немає STL з exocad)"
        return result
    result.thickness = thickness_mm(height, want.family) or height
    available = sorted({d.height for d in discs if d.height})
    result.heights, result.why = target_heights(result.thickness, available, want)
    if not result.heights:
        return result
    need_area = letter_footprint(attachments, want.family)
    pieces = letter_pieces(attachments)
    shapes: dict[str, Any] = {}
    picks: list[Pick] = []
    reads = 0
    active = active or {}
    heights = result.heights

    def rank(height: int) -> int:
        return heights.index(height) if height in heights else len(heights) + height

    # Диск, що стоїть у верстаті, — і ВИЩИЙ за правило висоти, якщо робота
    # влазить (власник 14.11.26: цех кладе роботу на диск, що вже стоїть; ПММА
    # 7 мм на 18). Нижчий за правило — ні: правило Monolith +1 крок лишається.
    def allowed(disc: DiscFile) -> bool:
        h = disc.height or 0
        return h in heights or (disc.key in active and h > max(heights))

    # Диск із верстата — першим і в читанні: інакше межа `max_reads` могла б
    # його не дочитати.
    for disc in sorted((d for d in discs if allowed(d)),
                       key=lambda d: (d.key not in active, rank(d.height or 0), d.stem)):
        if reads >= max_reads:
            break
        reads += 1
        shape = sum3d_disc.shape_of(disc.path)
        if shape is None:
            continue
        free = shape.free_share
        if free < NEARLY_USED:
            continue
        if need_area is not None:
            free_area = free * math.pi * shape.radius ** 2
            if free_area < need_area * PACKING:
                continue
        picks.append(Pick(name=disc.stem, height=disc.height or 0, free_pct=round(free * 100),
                          started=bool(shape.contours), paths=sum3d_disc.svg_paths(shape),
                          on_machine=disc.key in active,
                          machine=(machines or {}).get(disc.key, "")))
        shapes[disc.stem] = shape
    # Найперше — диск, що вже стоїть у верстаті (найсвіжіший). Далі як було:
    # початий першим (власник), серед початих — де менше вільного: добити диск.
    # Серед дисків із верстата — спершу ті, що за правилом висоти, і лише
    # потім вищі (14.11.26: робота 9 мм отримувала «18 мм» лише тому, що той
    # верстат працював останнім, хоч у сусідньому стояв диск 14 мм).
    on_machine = sorted((p for p in picks if p.on_machine),
                        key=lambda p: (rank(p.height),
                                       -active[_key_of(p.name)].timestamp()
                                       if _key_of(p.name) in active else 0.0))
    picks.sort(key=lambda p: (rank(p.height), not p.started,
                              p.free_pct if p.started else 100))
    ordered = on_machine + [p for p in picks if p.started and not p.on_machine] +         [p for p in picks if not p.started and not p.on_machine]
    # Сума площ лише відсіяла явно замалі. Тепер — справжня розстановка
    # конструкцій (власник 14.11.26), у порядку пріоритету й лише поки не
    # знайдеться найкращий і два запасні: кожна перевірка — десятки мс.
    # Розстановка — наближення (власник 14.11.26: «теоретично може помилятися»),
    # тож диск, де вона місця не знайшла, не ховаємо: він іде після тих, де
    # знайшла, і підписаний «перевір у Sum3D» — коли кращого немає.
    accepted: list[Pick] = []
    doubtful: list[Pick] = []
    tries = 0
    for pick in ordered:
        if len(accepted) >= 3:
            break
        if pieces and tries < MAX_FIT_TRIES:
            tries += 1
            shape = shapes[pick.name]
            pick.fit = disc_fit.fits(shape.radius, shape.contours, pieces, letter_scale(want.family))
            if not pick.fit:
                result.rejected += 1
                doubtful.append(pick)
                continue
        accepted.append(pick)
    # Диск «місця не знайдено» — лише коли жоден не влазить: поруч із дисками,
    # де місце є, він тільки заважав би.
    ranked = accepted or doubtful
    if ranked:
        result.best = ranked[0]
        result.alts = ranked[1:3]
    result.new_disc = f"новий диск {result.heights[0]} мм {want.label()}"
    return result


def _key_of(stem: str) -> str:
    return stem.split("_", 1)[1].lower() if "_" in stem else stem.lower()


def audit(db, days: int, limit: int = 30) -> dict[str, Any]:
    """Прогін на прийнятих листах: що запропонувала б підказка проти диска,
    який реально взяли (назва програми на верстаті з тим самим Sum3D).
    Назв STL не віддає. Лише читання."""
    from sqlalchemy import select

    from app.models import EmailMessage, MachineReading, Order
    from app.services.disc_check import program_disc
    from app.settings_store import get_setting

    root = get_setting(db, "cam_blanks_path")
    since = datetime.now() - timedelta(days=days)
    orders = db.scalars(
        # Робота з листа = є `source_email_id`; окрему умову джерела не пишемо
        # (сторож `test_code_guards`: копії умови джерела розходяться).
        select(Order).where(Order.created_at >= since,
                            Order.source_email_id.is_not(None)).order_by(Order.id.desc())
    ).all()
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    # Диски з верстатів — на момент ПРИЙНЯТТЯ кожної роботи, без майбутнього:
    # інакше аудит «вгадував» би диск, на якому ця сама робота й фрезерувалась.
    readings = sorted(
        (seen, disc.key)
        for iso, seen in db.execute(
            select(MachineReading.iso_name, MachineReading.captured_at).where(
                MachineReading.iso_name.is_not(None),
                MachineReading.captured_at >= since - timedelta(days=ACTIVE_DAYS)))
        if seen is not None and (disc := program_disc(iso)) is not None
    )
    started = time.monotonic()
    for order in orders:
        if time.monotonic() - started > 50:
            counts["не_встигли"] = counts.get("не_встигли", 0) + 1
            continue
        email = db.get(EmailMessage, order.source_email_id)
        accepted = order.created_at or datetime.now()
        active: dict[str, datetime] = {}
        for seen, key in readings:
            if accepted - timedelta(days=ACTIVE_DAYS) <= seen <= accepted:
                active[key] = seen
        hint = suggest(order.material_color, list(email.attachments or []) if email else [], root,
                       active=active)
        actual = None
        tail = (order.sum3d_id or "").strip()[-8:]
        if tail:
            iso = db.scalar(select(MachineReading.iso_name).where(
                MachineReading.sum3d_id == tail, MachineReading.iso_name.is_not(None),
                MachineReading.captured_at >= since).limit(1))
            actual = program_disc(iso)
        if hint.best:
            verdict = "диск"
        elif hint.heights:
            verdict = "лише_висота"
        else:
            verdict = "без_підказки"
        counts[verdict] = counts.get(verdict, 0) + 1
        if actual is not None and actual.height and hint.heights:
            key = "висота_як_у_цеху" if actual.height in hint.heights else "висота_інша_ніж_у_цеху"
            counts[key] = counts.get(key, 0) + 1
        if actual is not None and hint.best is not None:
            key = "диск_як_у_цеху" if _key_of(hint.best.name) == actual.key else "диск_інший"
            counts[key] = counts.get(key, 0) + 1
        if len(rows) < limit:
            rows.append({
                "робота": order.id, "клієнт": order.client_name, "матеріал": order.material_color,
                "товщина_мм": round(hint.thickness, 1) if hint.thickness else None,
                "підказка_висоти": hint.heights, "чому": hint.why or hint.note,
                "підказка_диска": hint.best.name if hint.best else (hint.new_disc or None),
                "вільно_%": hint.best.free_pct if hint.best else None,
                "на_верстаті": bool(hint.best and hint.best.on_machine),
                "диск_у_програмі": actual.head if actual else None,
            })
    return {"днів": days, "робіт": len(orders), **counts, "приклади": rows,
            "пояснення": "Підказка рахується так само, як у картці листа; «диск_у_програмі» — з назви "
                         ".iso на верстаті з тим самим Sum3D. Нічого не змінює."}
