"""Чи той диск: замовлений матеріал і колір проти диска, в який прорахували.

Власник 12.11.26: технік чи клієнт замовляє, наприклад, `моно А3.5`, оператор
кладе роботу в Sum3D на диск і прораховує, а програма `.iso` несе назву диска:
`PMMA14_14-A2-X132_2026-11-12_01-20-12.ISO` — ПММА, 14 мм, A2. Хвіст `01-20-12`
— той самий Sum3D ID, що оператор вписує в таблицю. Отже кожну програму можна
звʼязати з рядками черги й звірити: матеріал, відтінок і висоту.

**Лише читання й лише підказка.** Модуль нічого не пише ні в базу, ні на
диск, ні в таблицю: тут лише розбір назв і порівняння. Розбіжність — привід
оператору глянути, а не вирок (так само як на видачі: рішення за людиною).

**Чого НЕ вважаємо розбіжністю.** Чого не вдалося розібрати з одного боку
(диск без кольору — віск; робота з кодом кольору без пари; немає висоти STL)
— це «не порівняно», окремим лічильником, а не помилка. Хибна тривога гірша
за мовчання: на неї швидко перестають дивитись.

Один Sum3D ID буває в КІЛЬКОХ робіт (кілька клієнтів на одному диску, CLAUDE.md
§14): тоді звіряється кожна з них — усі вони на тому самому диску.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import material_match
from app.material_classifier import classify_material
from app.models import MachineReading, Order
from app.services.cam_blanks import _head_code, parse_blank_name
from app.services.order_dates import order_date
from app.services.stl_height import thickness_mm

# Дата й час Sum3D у хвості назви програми.
_TAIL_RE = re.compile(
    r"_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})(?:_)?(?:\.iso)?$", re.I
)
_BARE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:\.iso)?$", re.I)
# Кодові кольори виробника (CLAUDE.md §3): 500 = A1 опак, 800 = A2, 1000 = A3.
CODE_SHADES = {"500": "a1", "800": "a2", "1000": "a3"}
# Робота від цієї товщини вище за диск — не влізе.
HEIGHT_TIGHT_MM = 0.5
# Диск вищий за роботу на стільки — дорогий високий диск під низьку роботу
# (не помилка, окремо, як підказка).
HEIGHT_WASTE_MM = 7.0
# Скільки днів до програми може стояти рядок її роботи (як у верстатів).
DAYS_BACK = 14


@dataclass(frozen=True)
class ProgramDisc:
    """Диск, розібраний із назви програми."""

    key: str
    head: str
    height: Optional[int]
    family: Optional[str]
    shades: frozenset[str]
    sum3d_id: Optional[str]
    made_on: Optional[date]


def _shade_text(shade: Optional[str]) -> str:
    """`a3-5` (CAM пише дефіс замість крапки) → `a3.5` для порівняння."""
    text = (shade or "").lower()
    return re.sub(r"\b([a-d][1-4])-5\b", r"\1.5", text)


def _norm_shades(found: Iterable[str]) -> frozenset[str]:
    return frozenset(CODE_SHADES.get(s, s) for s in found)


def program_disc(iso_name: Optional[str]) -> Optional[ProgramDisc]:
    """Назва програми → диск; None, коли в назві немає диска (`дата_час.iso`)."""
    text = (iso_name or "").strip()
    if _BARE_RE.match(text):
        return None  # гола `дата_час.iso` — диска в назві немає
    tail = _TAIL_RE.search(text)
    head = text[: tail.start()] if tail else re.sub(r"\.iso$", "", text, flags=re.I)
    if not head or "-" not in head:
        return None
    parsed = parse_blank_name(head)
    if parsed.height is None and parsed.shade is None:
        return None
    # Позначка верстата перед матеріалом (`4pmma25_25`, `13PMMA18_18`) — не
    # частина коду матеріалу.
    first = re.sub(r"^\d+(?=[a-z])", "", head.split("-", 1)[0], flags=re.I)
    code = _head_code(first) or ""
    family = classify_material(f"{parsed.brand or ''} {code}".strip()) if (parsed.brand or code) else None
    made_on: Optional[date] = None
    if tail:
        try:
            made_on = date.fromisoformat(tail.group("date"))
        except ValueError:
            made_on = None
    key = head.split("_", 1)[1].lower() if "_" in head else head.lower()
    return ProgramDisc(
        key=key,
        head=head,
        height=parsed.height,
        family=family,
        shades=_norm_shades(material_match.shades(_shade_text(parsed.shade))),
        sum3d_id=tail.group("time") if tail else None,
        made_on=made_on,
    )


@dataclass
class Verdict:
    """Звірка однієї роботи з диском її програми."""

    issues: list[str] = field(default_factory=list)  # розбіжності
    notes: list[str] = field(default_factory=list)  # не помилка, але варто знати
    skipped: list[str] = field(default_factory=list)  # чого не порівняли й чому


def order_family(order: Order) -> Optional[str]:
    material = getattr(order, "material", None)
    name = getattr(material, "name", None)
    return name or classify_material(order.material_color)


def compare(disc: ProgramDisc, order: Order) -> Verdict:
    """Матеріал, відтінок, висота. Лише те, що розібрано з ОБОХ боків."""
    verdict = Verdict()
    family = order_family(order)
    if disc.family and family:
        if disc.family != family:
            verdict.issues.append(f"матеріал: диск {disc.family}, замовлено {family}")
    else:
        verdict.skipped.append("матеріал")

    wanted = _norm_shades(material_match.shades(order.material_color))
    if disc.shades and wanted:
        if not disc.shades & wanted:
            verdict.issues.append(
                f"колір: диск {'/'.join(sorted(disc.shades)).upper()}, "
                f"замовлено {'/'.join(sorted(wanted)).upper()}"
            )
    else:
        verdict.skipped.append("колір")

    thick = thickness_mm(getattr(order, "stl_height_mm", None), family)
    if thick is not None and disc.height:
        if thick > disc.height - HEIGHT_TIGHT_MM:
            verdict.issues.append(f"висота: робота ≈{thick:.1f} мм, диск {disc.height} мм")
        elif disc.height - thick >= HEIGHT_WASTE_MM:
            verdict.notes.append(f"низька робота ≈{thick:.1f} мм на диску {disc.height} мм")
    else:
        verdict.skipped.append("висота")
    return verdict


_SUM3D_IN_TEXT = r"(?<!\d){}(?!\d)"


def orders_for(db: Session, disc: ProgramDisc) -> list[Order]:
    """Роботи з тим самим Sum3D ID, рядок яких стоїть до дня програми.

    Sum3D ID — лише час доби й повторюється між днями (CLAUDE.md §14), тому
    без дати програми звʼязку немає. Архівні беремо: перевірка заднім числом."""
    if not disc.sum3d_id or disc.made_on is None:
        return []
    rows = db.scalars(
        select(Order).where(Order.sum3d_id.like(f"%{disc.sum3d_id}%"))
    ).all()
    pattern = re.compile(_SUM3D_IN_TEXT.format(re.escape(disc.sum3d_id)))
    low = disc.made_on - timedelta(days=DAYS_BACK)
    high = disc.made_on + timedelta(days=1)
    return [
        o for o in rows
        if pattern.search(o.sum3d_id or "") and low <= order_date(o) <= high
    ]


def _order_brief(order: Order) -> dict[str, Any]:
    return {
        "робота": order.id,
        "клієнт": order.client_name,
        "колір_роботи": order.material_color,
        "вид": order.kind,
        "sum3d": order.sum3d_id,
        "в_архіві": order.archived_at is not None,
    }


def programs_seen(db: Session, since: datetime) -> list[tuple[str, str, datetime]]:
    """(хост, назва програми, коли вперше бачили) з показів верстатів."""
    rows = db.execute(
        select(MachineReading.host, MachineReading.iso_name, func.min(MachineReading.captured_at))
        .where(MachineReading.captured_at >= since, MachineReading.iso_name.is_not(None))
        .group_by(MachineReading.host, MachineReading.iso_name)
    ).all()
    return [(h, n, t) for h, n, t in rows if n]


def retro(db: Session, days: int, *, examples: int = 25) -> dict[str, Any]:
    """Що показала б звірка за останні `days` днів на програмах з верстатів."""
    from app.models import Machine

    names = {m.host: m.name for m in db.scalars(select(Machine)).all()}
    since = datetime.now() - timedelta(days=days)
    seen = programs_seen(db, since)
    counters = {
        "програм": 0, "без_диска_в_назві": 0, "без_роботи_в_черзі": 0,
        "звірено_робіт": 0, "збіг": 0, "розбіжність": 0,
    }
    kinds: dict[str, int] = {}
    skipped: dict[str, int] = {}
    notes = 0
    issues_out: list[dict[str, Any]] = []
    notes_out: list[dict[str, Any]] = []
    unlinked: list[dict[str, Any]] = []
    families: dict[str, int] = {}
    for host, iso, first_at in sorted(seen, key=lambda r: r[2]):
        counters["програм"] += 1
        disc = program_disc(iso)
        if disc is None:
            counters["без_диска_в_назві"] += 1
            continue
        families[disc.family or "—"] = families.get(disc.family or "—", 0) + 1
        orders = orders_for(db, disc)
        if not orders:
            counters["без_роботи_в_черзі"] += 1
            if len(unlinked) < examples:
                unlinked.append({"верстат": names.get(host, host), "програма": iso})
            continue
        for order in orders:
            counters["звірено_робіт"] += 1
            verdict = compare(disc, order)
            for what in verdict.skipped:
                skipped[what] = skipped.get(what, 0) + 1
            row = {"верстат": names.get(host, host), "програма": iso,
                   "на_верстаті": first_at.strftime("%Y-%m-%d %H:%M"), **_order_brief(order)}
            if verdict.issues:
                counters["розбіжність"] += 1
                for issue in verdict.issues:
                    kind = issue.split(":", 1)[0]
                    kinds[kind] = kinds.get(kind, 0) + 1
                if len(issues_out) < examples:
                    issues_out.append({**row, "розбіжності": verdict.issues})
            else:
                counters["збіг"] += 1
            if verdict.notes:
                notes += 1
                if len(notes_out) < examples:
                    notes_out.append({**row, "підказка": verdict.notes})
    return {
        "днів": days,
        **counters,
        "розбіжності_за_видом": kinds,
        "не_порівняно": skipped,
        "низька_робота_на_високому_диску": notes,
        "диски_за_матеріалом": families,
        "приклади_розбіжностей": issues_out,
        "приклади_низьких": notes_out,
        "програми_без_роботи": unlinked,
        "пояснення": (
            "Програми — з показів верстатів (назва .iso), робота — за Sum3D ID у межах "
            f"{DAYS_BACK} днів до програми, архівні теж. «не порівняно» — чого не розібрано з "
            "одного боку (це не помилка). Нічого не змінює."
        ),
    }

