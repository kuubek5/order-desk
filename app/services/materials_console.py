"""Обчислення для консолі «Бібліотека матеріалів» (/settings/materials).

Роутер лишається тонким: тут живе вся арифметика, яка перетворює правила на
ПОКАЗАННЯ ПРИЛАДУ — скільки робіт реально ловить кожне правило, які правила
мертві й де правила конфліктують.

Чому це важливо, а не прикраса. `classify_material` при збігу ДВОХ матеріалів
повертає None (див. app/material_classifier.py) — тобто конфлікт правил не дає
«трохи неправильний матеріал», він ТИХО викидає роботу в «не розпізнано».
Раніше екран показував лише підсумкове число нерозпізнаних, і відрізнити
«немає правила» від «правила побились» було неможливо. Тепер це два різні
діагнози з різним лікуванням.

Міряємо по СПРАВЖНІХ кольорах із бази, а не теоретично: перетин шаблонів на
папері нічого не каже про те, чи такий текст узагалі трапляється в лабораторії.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.material_classifier import normalize_material
from app.models import Material, MaterialAlias, Order

#: Скільки прикладів сирого тексту показуємо біля правила/проби. Три — рівно
#: стільки, щоб побачити характер написання й не перетворити рядок на абзац.
SAMPLE_LIMIT = 3


@dataclass
class ColourRow:
    """Один унікальний сирий колір із бази + скільки робіт його мають."""

    raw: str
    normalized: str
    tokens: tuple[str, ...]
    orders: int


@dataclass
class RuleView:
    """Правило разом із його вимірами."""

    id: int
    pattern: str
    match_type: str
    orders: int = 0
    samples: list[str] = field(default_factory=list)
    #: Шаблон того ж матеріалу, який уже покриває це правило (мертве правило).
    covered_by: str | None = None


@dataclass
class MaterialView:
    id: int
    name: str
    is_production: bool
    rule_count: int
    orders: int
    dead_count: int


def load_colour_rows(session: Session) -> list[ColourRow]:
    """Унікальні сирі кольори активних робіт із кількістю робіт на кожен.

    Групуємо в SQL, а не тягнемо рядок на кожну роботу: різних написань
    кількасот, робіт — десятки тисяч. Архівовані не рахуємо: правила
    налаштовують під те, що йде в роботу зараз.
    """
    rows = session.execute(
        select(Order.material_color, func.count(Order.id))
        .where(Order.material_color.isnot(None), Order.archived_at.is_(None))
        .group_by(Order.material_color)
    ).all()
    out: list[ColourRow] = []
    for raw, count in rows:
        normalized = normalize_material(raw)
        if not normalized:
            continue
        out.append(
            ColourRow(
                raw=raw,
                normalized=normalized,
                tokens=tuple(normalized.split()),
                orders=int(count),
            )
        )
    return out


def rule_matches(colour: ColourRow, pattern: str, match_type: str) -> bool:
    """Той самий предикат, що в classify_material. Один рядок логіки, але він
    ОБОВ'ЯЗКОВО має лишатись дзеркалом класифікатора: якби консоль рахувала
    інакше, вона показувала б числа, яких у черзі немає."""
    if match_type == "token":
        return pattern in colour.tokens
    return pattern in colour.normalized


def measure_rules(
    aliases: list[MaterialAlias], colours: list[ColourRow]
) -> list[RuleView]:
    """Правила одного матеріалу з кількістю робіт, прикладами й міткою мертвих.

    Мертве правило — те, чий шаблон уже покритий КОРОТШИМ `contains`-правилом
    ТОГО САМОГО матеріалу: `mono` робить `monolith` назавжди зайвим, бо будь-що,
    що містить `monolith`, містить і `mono`. Перевіряємо лише в межах матеріалу:
    між різними матеріалами такий перетин — не надлишок, а колізія (див. модуль).
    """
    views = [
        RuleView(id=a.id, pattern=a.pattern, match_type=a.match_type) for a in aliases
    ]
    contains = [v for v in views if v.match_type == "contains"]
    for view in views:
        for other in contains:
            if other is view:
                continue
            # Коротший підрядок усередині довшого шаблону = довший недосяжний.
            if other.pattern in view.pattern and len(other.pattern) < len(view.pattern):
                view.covered_by = other.pattern
                break
        for colour in colours:
            if rule_matches(colour, view.pattern, view.match_type):
                view.orders += colour.orders
                if len(view.samples) < SAMPLE_LIMIT:
                    view.samples.append(colour.raw)
    views.sort(key=lambda v: (-v.orders, v.pattern))
    return views


def material_views(
    materials: list[Material], colours: list[ColourRow]
) -> list[MaterialView]:
    """Перелік для лівої панелі: скільки правил, скільки робіт, скільки мертвих."""
    out: list[MaterialView] = []
    for material in materials:
        rules = measure_rules(list(material.aliases), colours)
        matched = 0
        for colour in colours:
            if any(rule_matches(colour, r.pattern, r.match_type) for r in rules):
                matched += colour.orders
        out.append(
            MaterialView(
                id=material.id,
                name=material.name,
                is_production=material.is_production,
                rule_count=len(rules),
                orders=matched,
                dead_count=sum(1 for r in rules if r.covered_by),
            )
        )
    return out


def claim_map(session: Session, colours: list[ColourRow]) -> dict[str, set[str]]:
    """Для кожного сирого кольору — множина матеріалів, які на нього претендують.

    Це і є детектор колізій: два імені в множині означають, що класифікатор
    поверне None і робота піде в «не розпізнано», хоча правила ніби є.
    """
    rows = session.execute(
        select(MaterialAlias.pattern, MaterialAlias.match_type, Material.name).join(
            Material, MaterialAlias.material_id == Material.id
        )
    ).all()
    claims: dict[str, set[str]] = {}
    for colour in colours:
        hit = {
            name
            for pattern, match_type, name in rows
            if rule_matches(colour, pattern, match_type)
        }
        claims[colour.raw] = hit
    return claims


@dataclass
class UnresolvedItem:
    raw: str
    orders: int
    #: Матеріали, що б'ються за цей текст. Порожньо = правила просто немає.
    rivals: list[str] = field(default_factory=list)


def unresolved_breakdown(
    session: Session, colours: list[ColourRow]
) -> tuple[list[UnresolvedItem], int, int]:
    """Нерозпізнані написання, розділені за ПРИЧИНОЮ.

    Повертає (items, no_rule_orders, collision_orders). Розділення — головна
    цінність екрана: «немає правила» лікується одним кліком, а «колізія» означає
    зламані правила, і доти роботи тихо зникають із матеріалу.
    """
    unresolved_raw = {
        raw
        for (raw,) in session.execute(
            select(Order.material_color).where(
                Order.material_id.is_(None),
                Order.material_color.isnot(None),
                Order.archived_at.is_(None),
            )
        ).all()
    }
    claims = claim_map(session, colours)
    items: list[UnresolvedItem] = []
    no_rule = 0
    collision = 0
    for colour in colours:
        if colour.raw not in unresolved_raw:
            continue
        rivals = sorted(claims.get(colour.raw, set()))
        items.append(
            UnresolvedItem(raw=colour.raw, orders=colour.orders, rivals=rivals if len(rivals) > 1 else [])
        )
        if len(rivals) > 1:
            collision += colour.orders
        else:
            no_rule += colour.orders
    # Колізії вперед: це поламані правила, а не просто пропуск.
    items.sort(key=lambda i: (not i.rivals, -i.orders, i.raw))
    return items, no_rule, collision


@dataclass
class ProbeResult:
    """Показання приладу для написання, яке адмін ЩЕ НЕ зберіг."""

    pattern: str
    match_type: str
    valid: bool = True
    error: str | None = None
    orders: int = 0
    samples: list[str] = field(default_factory=list)
    #: Уже існує таке саме правило (у цього ж матеріалу).
    duplicate: bool = False
    #: Шаблон того ж матеріалу, який робить нове правило зайвим.
    covered_by: str | None = None
    #: Інші матеріали, чиї ПОТОЧНІ роботи це написання зачепить → колізія вже є.
    steals_from: list[str] = field(default_factory=list)
    #: Перетин на рівні САМИХ ПРАВИЛ (материал, шаблон) — колізія ще не настала,
    #: бо таких кольорів зараз немає, але настане з першою ж такою роботою.
    overlaps: list[tuple[str, str]] = field(default_factory=list)


def probe_pattern(
    session: Session, material_id: int, pattern: str, match_type: str
) -> ProbeResult:
    """Що станеться, ЯКЩО додати це правило — до того, як його додано.

    Авторський момент екрана: поле шаблону перестає бути сліпим. Три відповіді,
    усі з реальних даних — скільки робіт піймає, на яких написаннях, і чи не
    відбере воно роботи в іншого матеріалу (що, через None-при-двох-збігах,
    означає не «переїзд», а зникнення роботи з обох матеріалів).
    """
    normalized = normalize_material(pattern)
    result = ProbeResult(pattern=normalized, match_type=match_type)
    if not normalized:
        result.valid = False
        return result
    if match_type not in ("contains", "token"):
        result.valid = False
        result.error = "Невідомий тип зіставлення."
        return result

    colours = load_colour_rows(session)
    claims = claim_map(session, colours)
    material = session.get(Material, material_id)
    own_name = material.name if material else None

    if material is not None:
        for alias in material.aliases:
            if alias.pattern == normalized and alias.match_type == match_type:
                result.duplicate = True
            if (
                alias.match_type == "contains"
                and alias.pattern in normalized
                and len(alias.pattern) < len(normalized)
            ):
                result.covered_by = alias.pattern

    rivals: set[str] = set()
    for colour in colours:
        if not rule_matches(colour, normalized, match_type):
            continue
        result.orders += colour.orders
        if len(result.samples) < SAMPLE_LIMIT:
            result.samples.append(colour.raw)
        for name in claims.get(colour.raw, set()):
            if name != own_name:
                rivals.add(name)
    result.steals_from = sorted(rivals)

    # Перетин на рівні правил. Самих даних не досить: `temp` уже належить ПММА,
    # але якщо жодна поточна робота не містить «temp», вимір по даних мовчить —
    # а колізія настане з першою ж такою роботою, і тоді її не знайдуть, бо
    # шукатимуть свіжу помилку, а не правило, додане місяць тому.
    if not result.steals_from:
        overlaps: list[tuple[str, str]] = []
        rows = session.execute(
            select(MaterialAlias.pattern, MaterialAlias.match_type, Material.name)
            .join(Material, MaterialAlias.material_id == Material.id)
            .where(MaterialAlias.material_id != material_id)
        ).all()
        for pattern_other, type_other, name in rows:
            if match_type == "token" or type_other == "token":
                # Токен збігається лише сам із собою: «500» не зачіпає «1500».
                clash = pattern_other == normalized
            else:
                # Два підрядки конфліктують, щойно один вкладений в інший:
                # будь-який текст із довшим містить і коротший.
                clash = pattern_other in normalized or normalized in pattern_other
            if clash:
                overlaps.append((name, pattern_other))
        result.overlaps = sorted(set(overlaps))
    return result
