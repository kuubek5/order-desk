"""Підказка диска при прийнятті листа (власник 12.11.26) — ЛИШЕ читання.

Під полем «Матеріал · колір» картки листа (варіант A) — один рядок: який диск
взяти, скільки в ньому вільно й чи влізе робота. Правила власника:

1. Матеріал, лінійка й колір диска = матеріал і колір листа.
2. Висота: найвища конструкція листа (STL × усадка) + запас **мінімум 1 мм**
   → найменша висота, що ФІЗИЧНО є серед дисків (14,7 → 16, бо 15 немає).
3. Одноколірні (віск…) — саме ця висота. ПММА — будь-який диск, НЕ НИЖЧИЙ за
   цю висоту (власник 22.11.26: «для 8 мм навіть 25»); серед початих першим —
   де менше вільного, незалежно від висоти; новий — найнижчий, що підходить.
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
import re
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app import material_match, perf
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
# Скільки дисків перевіряти справжньою розстановкою за одну підказку і скільки
# часу на це можна витратити. Диски йдуть від найповніших (добити початий), і
# 18.11.26 дванадцять спроб згоріли на майже повних 18 мм Monolith A1 — x06
# пройшов неперевіреним і показався «влазить» (власник: «явно не вміщаються»).
MAX_FIT_TRIES = 60
# Початий диск, де розстановка місця не знайшла, усе одно показується (з
# «розстав у Sum3D»), якщо вільно щонайменше стільки відсотків (власник 20.11.26).
DOUBTFUL_SHOW_PCT = 30
# Диск, файл якого Sum3D не переписував довше за стільки днів, не пропонується:
# його, найімовірніше, вже викинули (власник 20.11.26: x136 Emotions A3 востаннє
# брали у 2024, «99 % що його вже немає»; поріг — пів року). Sum3D переписує
# `.blk` щоразу, коли кладе на диск роботу. Копіювання й антивірус дату лише
# ОСВІЖАЮТЬ (`cam_blanks`), тож стара дата — надійна ознака «не чіпали».
STALE_DAYS = 182
# Друга ознака «вже немає» (власник 23.11.26, лист 4114: x09 Monolith A1 14 лежав
# без діла трохи менше пів року — «його вже немає», а після нього пішли x10…x19).
# Диск не чіпали `REPLACED_DAYS`, а за цей час брали щонайменше `REPLACED_BY`
# інших дисків тієї самої висоти, лінії й кольору. Рідкісний колір, де наступних
# немає, і далі живе до `STALE_DAYS`.
REPLACED_DAYS = 60
REPLACED_BY = 3
FIT_BUDGET_SECONDS = 2.5
# Початого диска потрібної висоти з місцем немає — шукаємо початий НА ОДИН крок
# вищий (власник 23.11.26: «якщо вище на один пункт, ніж ідеальний, то можна
# взяти»). Своя межа читання й часу: головний пошук свою вже витратив.
STEP_UP_READS = 20
STEP_UP_SECONDS = 1.0
# «Впритул» (власник 23.11.26): робота не вища за диск, але запас менший за
# `MARGIN_MM` (13,92 мм на 14 — цех поклав). Такий початий диск показуємо з
# попередженням — коли початого з нормальним запасом потрібної висоти немає.
TIGHT_READS = 20
TIGHT_SECONDS = 1.0
# Відповідь розстановки «диск × конструкції листа» — у пам'яті процесу:
# картку листа відкривають і перемальовують кілька разів, а диск не змінився
# (обʼєкт диска з кешу `shape_of` — той самий, доки файл не переписали).
_FIT_CACHE_MAX = 2000
_fit_cache: dict[tuple, tuple[Any, bool]] = {}
_fit_lock = threading.Lock()


def _pieces_key(pieces: list[Any], scale: float) -> tuple:
    return (round(scale, 4),) + tuple((len(p), round(float(p.sum()), 3)) for p in pieces)


def _remember_fit(key: tuple, shape: Any, ok: bool) -> None:
    with _fit_lock:
        if len(_fit_cache) >= _FIT_CACHE_MAX:
            _fit_cache.clear()
        _fit_cache[key] = (shape, ok)
# Лінійки виробника: слово з назви диска → (назва для людей, мультиколор?).
LINES = {
    "monolith": ("Monolith", True),
    "emotions": ("Emotions", True),
    "katana": ("Katana", True),
    "z-nature": ("Z-Nature", True),
    "z-smile": ("Z-Smile", True),
    "doceram": ("Doceram", False),
    # Vivid — окрема лінійка цирконію (власник 22.11.26). Без цього рядка робота
    # «Vivid 4d A3», щойно її матеріал став цирконієм, отримала б у підказці
    # будь-який диск A3 — тобто Monolith. Тепер підказка шукає лише диски зі
    # словом vivid у назві, а коли таких немає — чесно каже, що дисків немає.
    "vivid": ("Vivid", True),
    # Серії Katana (власник 23.11.26: «stml — це серія катани»). У теці CAM слова
    # katana немає зовсім — диски звуться `zr14_14-STML-a2-x01`, `…-UTML-…`. Без
    # цих рядків «katana a2» казала «дисків немає», а «stml a2» отримувала
    # Monolith і Emotions (лист 3973). Після vivid: є диски `Vivid-stml-a2`.
    "stml": ("Katana STML", True),
    "utml": ("Katana UTML", True),
}
#: Серії, які разом звуться Katana; просто «katana» в замовленні — диск STML.
KATANA_SERIES = frozenset({"stml", "utml"})
# Як лінійку пишуть у таблиці й листах.
_LINE_WORDS = {
    # Точніше слово першим: «katana utml a1» — це UTML, «vivid stml» — Vivid.
    "vivid": ("vivid", "вівід", "вивид"),
    "stml": ("stml", "стмл"),
    "utml": ("utml", "утмл"),
    "monolith": ("mono", "моно", "monolith", "моноліт", "монолит"),
    "emotions": ("emo", "емо", "emotion", "емоушен", "эмо"),
    "katana": ("katana", "катана"),
    "z-nature": ("nature", "натур", "z nat", "z-nat", "znat"),
    "z-smile": ("smile", "смайл", "z-sm", "zsm"),
    "doceram": ("doceram", "доцерам"),
}


def disc_line_fits(wanted: Optional[str], disc_line: Optional[str]) -> bool:
    """Чи можна ПРОПОНУВАТИ диск цієї лінії (`wanted` None — лінію не названо)."""
    if not wanted or wanted == disc_line:
        return True
    return wanted == "katana" and disc_line == "stml"


def lines_conflict(wanted: Optional[str], got: Optional[str]) -> bool:
    """Чи це ТОЧНО різні лінії (сторож матеріалу). Просто «katana» проти будь-якої
    її серії — не розбіжність; STML проти UTML — різні диски."""
    if not wanted or not got or wanted == got:
        return False
    pair = {wanted, got}
    return not ("katana" in pair and pair - {"katana"} <= KATANA_SERIES)


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
    hipc: bool = False

    @property
    def multilayer(self) -> bool:
        return bool(self.line and LINES.get(self.line, ("", False))[1])

    def label(self) -> str:
        name = LINES[self.line][0] if self.line else (self.family or "")
        if self.hipc:
            name = "HIPC"
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
                code=next((c for c in OPAQUE_CODES if c in found), None),
                hipc=_is_hipc(text))


# HIPC — окремий матеріал, хоч диски лежать у теках ПММА й звуться
# `pmma20_20-hipc-a3-x54`, а класифікатор відносить його до родини ПММА.
# Власник 22.11.26: HIPC замість ПММА пропонувати не можна (картка «pmma a3»
# давала запасним диск `hipc-a3`). Так само й навпаки: замовлено HIPC — лише
# диски HIPC. Слова — ті самі, що в `material_classifier` і `material_match`.
_HIPC_WORDS = ("hipc", "hips", "hipk", "хіпс", "хипс", "хіпк", "хипк")


def _is_hipc(text: Optional[str]) -> bool:
    low = (text or "").lower()
    return any(word in low for word in _HIPC_WORDS)


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
    hipc: bool = False

    @property
    def key(self) -> str:
        """Як диск зветься в назві програми без позначки верстата —
        `14-monolith-a2-x47` (той самий ключ, що `disc_check.program_disc`)."""
        return _key_of(self.stem)


_CODE_RE = re.compile(r"x\d+", re.IGNORECASE)
_SHADE_RE = re.compile(r"[a-d]\d", re.IGNORECASE)
# Z-Nature у теці CAM пишуть по-різному: `Z-Nature`, `Znature`, `ZNat`, `Nature`,
# `Nat` (23.11.26, лист 4042: «такого диска немає», хоч Znature-A3 25 мм є).
# Власник 23.11.26: версія MS — те саме, що Z-Nature; `Z-Sm` — це Z-Smile; а
# `ST_ML` — НЕ Katana (Katana лише `STML`), тож такий диск лишається без лінії.
_Z_NATURE_RE = re.compile(r"(?<![a-z])z?-?nat(?:ure)?(?![a-z])")
_Z_SMILE_RE = re.compile(r"(?<![a-z])z-?sm(?:ile)?(?![a-z])")


def _line_of_disc(brand: str, low: str) -> Optional[str]:
    """Лінія диска за назвою файла (`brand` — розібраний виробник, `low` — назва)."""
    if brand in LINES:
        return brand
    known = next((key for key in LINES if key in low), None)
    if known is not None:
        return known
    if _Z_NATURE_RE.search(low):
        return "z-nature"
    return "z-smile" if _Z_SMILE_RE.search(low) else None


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
        line = _line_of_disc(brand, low)
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
            hipc=_is_hipc(low),
        ))
    with _scan_lock:
        _scan_cache[root] = (now, out)
    return out


def _matches(disc: DiscFile, want: Want) -> bool:
    if want.family and disc.family != want.family:
        return False
    if not disc_line_fits(want.line, disc.line):
        return False
    if want.shades and not (disc.shades & want.shades):
        return False
    if want.code != disc.code:
        return False  # опак кодом — лише свої диски, і навпаки
    if want.hipc != disc.hipc:
        return False  # HIPC і ПММА — різні матеріали (власник 22.11.26)
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


def any_height_above(want: Want) -> bool:
    """ПММА: висота диска — будь-яка, не нижча за потрібну (власник 22.11.26)."""
    return want.family == PMMA


def target_heights(thickness: float, available: list[int], want: Want) -> tuple[list[int], str]:
    """Допустимі висоти диска й пояснення правила."""
    need = thickness + MARGIN_MM
    fitting = [h for h in available if h >= need]
    if not fitting:
        return [], f"потрібно ≥{need:.1f} мм — такого диска немає"
    base = fitting[0]
    above = fitting[1] if len(fitting) > 1 else None
    why = f"≈{thickness:.1f} + запас {MARGIN_MM:g} → {base} мм"
    if any_height_above(want):
        return fitting, why + (" (ПММА: годиться й будь-який вищий)" if above is not None else "")
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
    # Запас над роботою менший за `MARGIN_MM` — картка попереджає «впритул».
    tight: bool = False

    @property
    def label(self) -> tuple[str, str]:
        """Назва диска для картки (варіант A, власник 16.11.26): лінія й колір
        окремо від коду партії — `zr16_16-Monolith-A3-5-x51` → («Monolith A3.5»,
        «x51»). Повна назва лишається в підказці при наведенні."""
        tail = self.name.rsplit("_", 1)[-1]
        parts = [p for p in tail.split("-") if p]
        if parts and parts[0].isdigit():
            parts = parts[1:]
        code = parts.pop() if parts and _CODE_RE.fullmatch(parts[-1]) else ""
        words: list[str] = []
        for part in parts:
            if part == "5" and words:
                words[-1] += ".5"
            elif _SHADE_RE.fullmatch(part):
                words.append(part.upper())
            else:
                words.append(part)
        title = " ".join(words)
        if title.isdigit():
            title = f"{title} опак"  # 500/800/1000 — коди опаку Doceram
        if self.name.lower().startswith("pmma"):
            title = f"PMMA {title}".strip()
        return title or tail, code


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
    # ПММА: `heights` — усі висоти від потрібної й вище; картка пише «від N мм».
    any_higher: bool = False

    @property
    def new_tight(self) -> bool:
        """Новий диск найменшої годящої висоти — теж впритул (вищих немає)."""
        return bool(self.thickness and self.heights and self.heights[0] - self.thickness < MARGIN_MM)


def _used_heights(discs: list[DiscFile], everything: list[DiscFile], active: dict[str, datetime]) -> list[int]:
    """Висоти, які цех справді бере. 23.11.26, лист 4121: усі десять дисків 800
    16 мм — з 2018 року, а картка радила «новий 16 мм». Висота відпадає, лише
    коли за `STALE_DAYS` її не чіпали В УСІЙ лінії (будь-який колір): тоді такого
    товару в цеху немає. Відпали б усі — лишаємо як є."""
    stale_before = time.time() - STALE_DAYS * 86400

    def alive(disc: DiscFile) -> bool:
        touched = None if disc.key in active else _touched_at(disc.path)
        return touched is None or touched >= stale_before

    heights = sorted({d.height for d in discs if d.height})
    out = []
    for height in heights:
        own = [d for d in discs if d.height == height]
        if any(alive(d) for d in own):
            out.append(height)
            continue
        kinds = {(d.family, d.line, d.code, d.hipc) for d in own}
        if any(d.height == height and (d.family, d.line, d.code, d.hipc) in kinds and alive(d) for d in everything):
            out.append(height)
    return out or heights


def active_discs(db, *, until: Optional[datetime] = None, days: int = ACTIVE_DAYS,
                 machines: Optional[dict[str, str]] = None,
                 recent: Optional[set[str]] = None) -> dict[str, datetime]:
    """Диски, що ЗАРАЗ стоять у верстатах: ключ → коли бачили. Диск верстата —
    це диск його ОСТАННЬОЇ програми (з назвою) за `days` днів до `until`; диск
    попередньої програми того самого верстата вже знято (власник 18.11.26:
    «на верстаті» лише коли справді стоїть, і писати НА ЯКОМУ). `machines`
    (якщо передано) наповнюється ключ → назва верстата (кілька — через кому);
    `recent` — ключами ВСІХ дисків із програм за ці дні, і вже знятих.
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
    # Покази пишуть КЛЮЧ верстата («адреса-порт», якщо порт не типовий —
    # `MachineTarget.key`), а не голу адресу: за самою адресою назва агентних
    # верстатів (порт 8765) не знаходилась, і картка писала безіменне «на верстаті».
    names: dict[str, str] = {}
    for host, port, name in db.execute(select(Machine.host, Machine.port, Machine.name)).all():
        names[f"{host}-{port}"] = name
        names.setdefault(host, name)
    latest: dict[str, tuple[datetime, str]] = {}
    for iso, host, seen in rows:
        if seen is not None and (host not in latest or seen > latest[host][0]):
            latest[host] = (seen, iso)
    out: dict[str, datetime] = {}
    on: dict[str, list[str]] = {}
    if recent is not None:
        recent.update(disc.key for iso, _host, _seen in rows if (disc := program_disc(iso)) is not None)
    for host, (seen, iso) in latest.items():
        disc = program_disc(iso)
        if disc is None:
            continue
        if disc.key not in out or seen > out[disc.key]:
            out[disc.key] = seen
        name = names.get(host, "")
        if name and name not in on.setdefault(disc.key, []):
            on[disc.key].append(name)
    if machines is not None:
        for key in out:
            machines[key] = ", ".join(sorted(on.get(key, [])))
    return out


def suggest(material_text: Optional[str], attachments: list[Any], blanks_root: Optional[str],
            *, max_reads: int = 60, active: Optional[dict[str, datetime]] = None,
            machines: Optional[dict[str, str]] = None,
            recent: Optional[set[str]] = None,
            trace: Optional[dict[str, Any]] = None) -> Suggestion:
    """`trace` (лише діагностика, `kmill_disc_hint`, 20.11.26): словник, куди
    лягає, що сталося з КОЖНИМ диском потрібного матеріалу й кольору і чому —
    висота, читання, вільне, площа, розстановка (скільки конструкцій лягло).
    Картка його не передає: розстановка з поясненням дорожча."""
    want = want_of(material_text)
    result = Suggestion(want=want)
    seen: dict[str, dict[str, Any]] = {}

    def note(disc_stem: str, **facts: Any) -> None:
        if trace is not None:
            seen.setdefault(disc_stem, {"диск": disc_stem}).update(facts)

    if trace is not None:
        trace["диски"] = []
    try:
        return _suggest(want, result, attachments, blanks_root, max_reads, active, machines,
                        recent or set(), trace, note)
    finally:
        if trace is not None:
            trace["диски"] = list(seen.values())


def _suggest(want: Want, result: Suggestion, attachments: list[Any], blanks_root: Optional[str],
             max_reads: int, active: Optional[dict[str, datetime]], machines: Optional[dict[str, str]],
             recent: set[str], trace: Optional[dict[str, Any]], note: Callable[..., None]) -> Suggestion:
    if not want.family or not want.shades and want.family == ZIRCON:
        result.note = "немає матеріалу й кольору — підказки немає"
        return result
    with perf.span("disc:scan"):
        everything = disc_files(blanks_root)
        discs = [d for d in everything if _matches(d, want)]
    if not discs:
        result.note = f"дисків {want.label()} у теці немає"
        return result
    with perf.span("disc:stl"):
        height = letter_height(attachments)
    if height is None:
        result.note = "висоту не визначено (немає STL з exocad)"
        return result
    result.thickness = thickness_mm(height, want.family) or height
    thickness = result.thickness
    with perf.span("disc:used"):
        available = _used_heights(discs, everything, active or {})
    result.heights, result.why = target_heights(thickness, available, want)

    def is_tight(height: Optional[int]) -> bool:
        return height is not None and 0 <= height - thickness < MARGIN_MM

    if not result.heights:
        # Вищих за роботу немає, але найвищий диск її ще накриває — впритул.
        result.heights = [h for h in available if is_tight(h)][-1:]
        if not result.heights:
            return result
        result.why = f"≈{thickness:.1f} мм на {result.heights[0]} мм — впритул, вищих немає"
    with perf.span("disc:stl"):
        need_area = letter_footprint(attachments, want.family)
        pieces = letter_pieces(attachments)
    if trace is not None:
        scale = letter_scale(want.family)
        trace["лист"] = {
            "товщина_мм": round(result.thickness, 2), "висоти": result.heights, "чому": result.why,
            "конструкцій_для_розстановки": len(pieces), "усадка": round(scale, 3),
            "площа_основ_мм2": round(need_area) if need_area is not None else None,
            "потрібно_вільного_мм2": round(need_area * PACKING) if need_area is not None else None,
            "розміри_мм": [f"{float(p[:, 0].max() - p[:, 0].min()) * scale:.1f}×{float(p[:, 1].max() - p[:, 1].min()) * scale:.1f}"
                           for p in pieces],
        }
        for d in discs:
            note(d.stem, висота=d.height, на_верстаті=d.key in (active or {}))
    shapes: dict[str, Any] = {}
    picks: list[Pick] = []
    reads = 0
    active = active or {}
    heights = result.heights
    # ПММА: усі допустимі висоти рівні — серед початих вирішує вільне місце
    # (добити диск), серед нових — висота (найнижчий, що підходить).
    flat = any_height_above(want)
    result.any_higher = flat and len(heights) > 1

    def rank(height: int) -> int:
        if flat and height in heights:
            return 0
        return heights.index(height) if height in heights else len(heights) + height

    # Диск, що стоїть у верстаті, — і ВИЩИЙ за правило висоти, якщо робота
    # влазить (власник 14.11.26: цех кладе роботу на диск, що вже стоїть; ПММА
    # 7 мм на 18). Нижчий за правило — ні: правило Monolith +1 крок лишається.
    def allowed(disc: DiscFile) -> bool:
        h = disc.height or 0
        return h in heights or (disc.key in active and h > max(heights))

    # Диск із верстата — першим і в читанні: інакше межа `max_reads` могла б
    # його не дочитати.
    for disc in discs:
        if not allowed(disc):
            note(disc.stem, рішення="висота не за правилом")
    # Давно не чіпаний диск — поза підказкою; диск, що стоїть у верстаті, — ні.
    now = time.time()
    times: dict[int, list[float]] = {}

    def gone(disc: DiscFile, touched: Optional[float]) -> str:
        """Чому диска, найімовірніше, вже немає; порожньо — є."""
        if disc.key in active or touched is None:
            return ""
        day = f"(файл змінено {datetime.fromtimestamp(touched):%d.%m.%y}) — не показано"
        if touched < now - STALE_DAYS * 86400:
            return f"не брали понад {STALE_DAYS} дн {day}"
        if touched >= now - REPLACED_DAYS * 86400:
            return ""
        height = disc.height or 0
        if height not in times:
            times[height] = []
            for other in discs:
                if other.height == height and (seen := _touched_at(other.path)) is not None:
                    times[height].append(seen)
        later = sum(1 for seen in times[height] if seen > touched)
        return f"не брали понад {REPLACED_DAYS} дн, після нього брали ще {later} таких {day}" if later >= REPLACED_BY else ""

    fresh: list[DiscFile] = []
    touched_at: dict[str, float] = {}
    for disc in (d for d in discs if allowed(d)):
        touched = _touched_at(disc.path)
        reason = gone(disc, touched)
        if reason:
            note(disc.stem, рішення=reason)
            continue
        touched_at[str(disc.path)] = touched or 0.0
        fresh.append(disc)
    # Межа читання відсікає НАЙДАВНІШЕ чіпані диски, а не останні за назвою
    # (22.11.26, ПММА A3: усі висоти рівні, тож читались pmma10…pmma20 за
    # абеткою, а 22 диски 25 мм — серед них учорашні x266 і x267 з купою
    # місця — лишались «не прочитано», і картка казала «новий диск»). Свіжіший
    # файл = диск, який цех брав останнім; так само сортує діалог Sum3D.
    def read(disc: DiscFile) -> Optional[Pick]:
        """Диск → кандидат; None — не прочитався чи місця явно замало."""
        with perf.span("disc:shapes"):
            shape = sum3d_disc.shape_of(disc.path)
        if shape is None:
            note(disc.stem, рішення="файл диска не прочитався")
            return None
        # Лише матеріал, з'єднаний із краєм диска: острівці між дірками в
        # диску вже випали (власник 18.11.26). Сітка ~15 мс, кеш на диск.
        with perf.span("disc:usable"):
            free = shape.usable_share
        note(disc.stem, вільно_відсотків=round(free * 100, 1), початий=bool(shape.contours),
             вільно_мм2=round(free * math.pi * shape.radius ** 2))
        if free < NEARLY_USED:
            note(disc.stem, рішення=f"майже вирізаний (< {round(NEARLY_USED * 100)} %)")
            return None
        if need_area is not None:
            free_area = free * math.pi * shape.radius ** 2
            if free_area < need_area * PACKING:
                note(disc.stem, рішення="замало вільної площі")
                return None
        shapes[disc.stem] = shape
        return Pick(name=disc.stem, height=disc.height or 0, free_pct=round(free * 100),
                    started=bool(shape.contours), paths=sum3d_disc.svg_paths(shape),
                    on_machine=disc.key in active, machine=(machines or {}).get(disc.key, ""),
                    tight=is_tight(disc.height))

    for disc in sorted(fresh,
                       key=lambda d: (d.key not in active, flat and d.key not in recent, rank(d.height or 0),
                                      -touched_at[str(d.path)], d.stem)):
        if reads >= max_reads:
            note(disc.stem, рішення=f"не прочитано: межа {max_reads} дисків")
            continue
        reads += 1
        candidate = read(disc)
        if candidate is not None:
            picks.append(candidate)
    # Найперше — диск, що вже стоїть у верстаті (найсвіжіший). Далі як було:
    # початий першим (власник), серед початих — де менше вільного: добити диск.
    # Серед дисків із верстата — спершу ті, що за правилом висоти, і лише
    # потім вищі (14.11.26: робота 9 мм отримувала «18 мм» лише тому, що той
    # верстат працював останнім, хоч у сусідньому стояв диск 14 мм).
    on_machine = sorted((p for p in picks if p.on_machine),
                        key=lambda p: (rank(p.height),
                                       -active[_key_of(p.name)].timestamp()
                                       if _key_of(p.name) in active else 0.0))
    # ПММА: усі висоти рівні, і «найповніший» виводив наперед давні залишки з
    # 19–28 % вільного — за два дні журналу 0 з 3 (23.11.26). Спершу диски, що
    # були в програмах за останні дні, серед них — як і раніше, найповніший.
    picks.sort(key=lambda p: (rank(p.height), not p.started, flat and _key_of(p.name) not in recent,
                              p.free_pct if p.started else (p.height if flat else 100)))
    # Диск із верстата, вищий БІЛЬШЕ ніж на крок, — після початих потрібної висоти
    # (23.11.26: робота 8,2 мм отримувала першим 25 мм; цех так не ставить). На
    # один крок — як було. У ПММА всі вищі висоти за правилом, її це не зачіпає.
    step = next((h for h in available if h > max(heights)), max(heights))
    far = [p for p in on_machine if p.height > step]
    ordered = ([p for p in on_machine if p.height <= step]
               + [p for p in picks if p.started and not p.on_machine] + far
               + [p for p in picks if not p.started and not p.on_machine])
    # Сума площ лише відсіяла явно замалі. Тепер — справжня розстановка
    # конструкцій (власник 14.11.26), у порядку пріоритету й лише поки не
    # знайдеться найкращий і два запасні: кожна перевірка — десятки мс.
    # Розстановка — наближення (власник 14.11.26: «теоретично може помилятися»),
    # тож диск, де вона місця не знайшла, не ховаємо: він іде після тих, де
    # знайшла, і підписаний «перевір у Sum3D» — коли кращого немає.
    accepted: list[Pick] = []
    # Не перевірені розстановкою (межа спроб чи часу): НЕ «влазить» — лише
    # після тих, де місце справді знайшлось (18.11.26, диск x06).
    unchecked: list[Pick] = []
    doubtful: list[Pick] = []
    tries = 0
    started_at = time.monotonic()
    # Силуети листа й їхні спектри — один раз на всі диски (18.11.26: інакше
    # ~150 мс на диск у цеху, і за межу часу перевірялось лише ~10 дисків).
    masks = disc_fit.Masks(pieces, letter_scale(want.family)) if pieces else None
    piece_key = _pieces_key(pieces, masks.scale) if masks else None

    def check(pick: Pick) -> bool:
        assert masks is not None  # кличеться лише коли є STL exocad
        shape = shapes[pick.name]
        hit = _fit_cache.get((pick.name, piece_key))
        if hit is not None and hit[0] is shape:
            return hit[1]
        if trace is not None:
            # Діагностика: та сама розстановка, але з тим, скільки лягло.
            report = disc_fit.fit_report(shape.radius, shape.blocked_grid(), masks)
            note(pick.name, розстановка=report)
            return bool(report["влазить"])
        with perf.span("disc:fit"):
            ok = disc_fit.fits_on(shape.radius, shape.blocked_grid(), masks)
        _remember_fit((pick.name, piece_key), shape, ok)
        return ok

    for pick in ordered:
        if len(accepted) >= 3:
            break
        if masks and tries < MAX_FIT_TRIES and time.monotonic() - started_at < FIT_BUDGET_SECONDS:
            tries += 1
            pick.fit = check(pick)
            if not pick.fit:
                result.rejected += 1
                doubtful.append(pick)
                continue
            accepted.append(pick)
        elif masks:
            unchecked.append(pick)
        else:
            # Без STL exocad розстановки немає зовсім — порядок як був.
            accepted.append(pick)
    # Жоден перевірений не підійшов, а межа вичерпалась — НОВИЙ диск потрібної
    # висоти й кольору (на порожньому місце є) перевіряємо поза межею: він
    # кращий за неперевірений майже повний (18.11.26: x06 «місце не
    # перевірено» стояв першим).
    if masks and not accepted:
        for pick in [p for p in unchecked if not p.started][:2]:
            pick.fit = check(pick)
            if pick.fit:
                accepted.append(pick)
        unchecked = [p for p in unchecked if p.fit is None]
    def step_up() -> list[Pick]:
        """Початі диски на один крок вищі за правило, куди робота влазить: до
        двох, від найповнішого. Лише коли на висотах за правилом такого немає."""
        higher = step
        if higher <= max(heights) or masks is None:
            return []
        candidates: list[tuple[float, DiscFile]] = []
        for disc in discs:
            if disc.height != higher or allowed(disc):
                continue
            touched = _touched_at(disc.path)
            if not gone(disc, touched):
                candidates.append((-(touched or 0.0), disc))
        found: list[Pick] = []
        for _touched, disc in sorted(candidates, key=lambda c: (c[0], c[1].stem))[:STEP_UP_READS]:
            pick = read(disc)
            if pick is not None and pick.started:
                found.append(pick)
        fits: list[Pick] = []
        until = time.monotonic() + STEP_UP_SECONDS
        for pick in sorted(found, key=lambda p: p.free_pct):
            if len(fits) >= 2 or time.monotonic() >= until:
                break
            pick.fit = check(pick)
            if pick.fit:
                fits.append(pick)
                note(pick.name, рішення=f"на крок вищий ({higher} мм) — початого потрібної висоти немає")
            else:
                note(pick.name, рішення="на крок вищий, розстановка не знайшла місця — не показано")
        return fits

    def tight_fit() -> list[Pick]:
        """Один початий диск «впритул», куди робота влазить: диск із верстата, далі
        найповніший. Час вийшов — ще одна спроба на найпорожнішому: серед десятка
        майже повних саме він найімовірніше й підходить (лист 4121: x34, 68 %)."""
        if masks is None:
            return []
        candidates: list[tuple[bool, float, DiscFile]] = []
        for disc in discs:
            if not is_tight(disc.height) or allowed(disc):
                continue
            touched = _touched_at(disc.path)
            if not gone(disc, touched):
                candidates.append((disc.key not in active, -(touched or 0.0), disc))
        found: list[Pick] = []
        for _off, _touched, disc in sorted(candidates, key=lambda c: (c[0], c[1], c[2].stem))[:TIGHT_READS]:
            pick = read(disc)
            if pick is not None and pick.started:
                found.append(pick)
        found.sort(key=lambda p: (not p.on_machine, p.free_pct))
        until = time.monotonic() + TIGHT_SECONDS
        while found:
            pick = found.pop(0)
            if time.monotonic() >= until:
                pick = max([pick, *found], key=lambda p: p.free_pct)
                found = []
            pick.fit = check(pick)
            if pick.fit:
                note(pick.name, рішення=f"впритул: запас {pick.height - thickness:.1f} мм — у картці з попередженням")
                return [pick]
            note(pick.name, рішення="впритул, розстановка не знайшла місця — не показано")
        return []

    # Початий вищий — перед новим потрібної висоти: добити диск (як і всюди).
    # ПММА на крок вище не потрапляє: у неї всі вищі висоти й так за правилом.
    # Впритул — після безпечних початих і теж перед новим (власник 23.11.26).
    if not any(p.started and p.height <= step for p in accepted):
        accepted = ((step_up() if not flat else []) + tight_fit() + accepted)[:3]

    # Неперевірених не показуємо (власник 18.11.26: «навіщо показувати, що
    # місце не перевірене»). «Місця не знайдено» — лише початий із багатьма
    # вільними (нижче); жодного — картка каже «новий диск» (гілка без `best`).
    # Початий диск, де розстановка місця не знайшла, але вільного багато, —
    # показуємо ПІСЛЯ тих, де знайшла, з «розстав у Sum3D» (власник 20.11.26:
    # лист Павленка — картка «новий диск», а він сам розмістив 7 зубів на x265;
    # моя розстановка лише наближення). Майже повний диск (18.11.26, x37 з 8 %)
    # і далі не показуємо.
    # Конструкція, ширша за сам диск, не влізе нікуди — тоді не показуємо.
    def placeable(pick: Pick) -> bool:
        return masks is None or not any(
            disc_fit.wider_than_disc(p, shapes[pick.name].radius, masks.scale) for p in masks.pieces)

    ranked = accepted + [p for p in doubtful
                         if p.started and p.free_pct >= DOUBTFUL_SHOW_PCT and placeable(p)]
    ranked = ranked[:3]
    for pick in ordered:
        if pick in accepted:
            note(pick.name, рішення="у картці" if accepted.index(pick) < 3 else "запасний поза картою")
        elif pick.fit is False and pick in ranked:
            note(pick.name, рішення="розстановка не знайшла місця — показано з «розстав у Sum3D»")
        elif pick.fit is False:
            note(pick.name, рішення="розстановка не знайшла місця — не показано")
        elif pick in unchecked:
            note(pick.name, рішення="не перевірено (межа спроб чи часу) — не показано")
        else:
            note(pick.name, рішення="не дійшла черга (уже є 3 кращі)")
    if ranked:
        result.best = ranked[0]
        result.alts = ranked[1:3]
    result.new_disc = f"новий диск {result.heights[0]} мм {want.label()}"
    return result


def _touched_at(path: Path) -> Optional[float]:
    """Коли Sum3D востаннє переписував файл диска; не прочиталось — None
    (тоді диск НЕ ховаємо: «не знаю» не привід прибрати)."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


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
                       active=active, recent=set(active))
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
