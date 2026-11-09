"""Що відомо про клієнта з його листів і робіт: контакти, доставка, прохання, ритм.

Витяг ніколи не буває точним, тому факт лише ПРОПОНУЄТЬСЯ: біля нього видно, у скількох
листах він трапився, а рішення закріпити чи прибрати приймає людина (`ClientFactDecision`).
Те саме правило, що у видачі: система показує кандидатів, вибір за оператором.

Імена беруться ЛИШЕ із заголовка From. У тексті листа стоять імена пацієнтів, тому текст
не копіюється в базу й імена з нього не витягуються. Місто доставки розпізнається лише за
переліком `_CITIES`: слово з великої літери поруч із «НП» буває й прізвищем.

Роботи клієнта тут ті самі, що й у картці (нечіткий збіг імені); обсяг рахує те саме
правило, що й статистика (`stats_data.done_clause`).
"""

from __future__ import annotations

import math
import re
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.business_day import business_today
from app.client_profile import matching_client_names
from app.mail_parser import guess_client_from_forward
from app.models import (
    Client,
    ClientFactDecision,
    ClientSenderMemory,
    EmailMessage,
    Order,
    OrderEmail,
)
from app.sender_memory import _is_forwarded, sender_key_for
from app.services import stats_data as data

KINDS = ("phone", "ship", "sender", "email")
STATE_PIN = "pin"
STATE_HIDE = "hide"

HEAT_HOURS = 24
WEEKS = 26
TOP_MATERIALS = 5
PROOF_LIMIT = 3
PROOF_RADIUS = 48
# Менше кількох днів листування не дає ритму: медіана двох проміжків нічого не означає.
RHYTHM_MIN_DAYS = 5
QUIET_MIN_DAYS = 4
# Той самий номер у трьох різних відправників це не клієнт: підпис лаби в цитаті чи футер.
SHARED_PHONE_SENDERS = 3
SHARED_PHONES_TTL_SECONDS = 3600

DOW = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")

# Лише оператори й міські коди України: `00016-007` з номера роботи телефоном не стане.
_PHONE = re.compile(r"(?<![\d_/-])(?:\+?38[\s(.-]{0,2})?\(?0[\s.-]{0,1}([3-9]\d)\)?[\s.-]{0,2}(\d{3})[\s.-]{0,2}(\d{2})[\s.-]{0,2}(\d{2})(?![\d_/-])")
_QUOTE_START = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|forwarded message|пересланное|переслане|вихідне|исходное)"
    r"|(?:від|from|от|кому|to|sent|надіслано|отправлено)\s*:"
    r"|.{0,120}(?:написа(?:в|ла|л)|wrote)\s*:?\s*$"
    r"|>)",
    re.I | re.M,
)

_NP_WORD = r"(?:нов\w*\s+пошт\w*|нов\w*\s+почт\w*|nova\s*poshta|\bНП\b)"
_BRANCH_WORD = r"(?:відділен\w*|відд?\.|отделен\w*|отд\.|поштомат\w*|почтомат\w*)"
_BRANCH_AFTER = re.compile(_BRANCH_WORD + r"\s*(?:№|N|#)?\s*(\d{1,5})\b", re.I)
_BRANCH_BEFORE = re.compile(r"(?<![\d-])(\d{1,5})\s*(?:-?\s*(?:е|ге|те))?\s*" + _BRANCH_WORD, re.I)
_NP_NUMBER = re.compile(_NP_WORD + r"[^\n\d]{0,40}?(?:№|N|#)?\s*(\d{1,5})\b(?![\s-]*(?:од|шт|зуб))", re.I)
_NP_LINE = re.compile(_NP_WORD)
_UKRPOSHTA = re.compile(r"укр\s*пошт\w*|укр\s*почт\w*", re.I)
_PICKUP = re.compile(r"самовив\w*|самовыв\w*|заберу\s+сам|заберемо\s+сам|заберем\s+сам|забер[еу]\w*\s+кур.?ер", re.I)

# Основа слова → назва. Основа, а не слово: «у Львові», «Львів», «Львов».
_CITIES = {
    "київ": "Київ", "києв": "Київ", "киев": "Київ", "льві": "Львів", "львов": "Львів",
    "дніпр": "Дніпро", "днепр": "Дніпро", "харк": "Харків", "харьк": "Харків",
    "одес": "Одеса", "вінниц": "Вінниця", "винниц": "Вінниця", "запоріж": "Запоріжжя",
    "запорож": "Запоріжжя", "полтав": "Полтава", "черкас": "Черкаси", "чернігів": "Чернігів",
    "чернігов": "Чернігів", "чернигов": "Чернігів", "чернівц": "Чернівці", "черновц": "Чернівці",
    "суми": "Суми", "сумах": "Суми", "сумск": "Суми", "житомир": "Житомир", "рівн": "Рівне",
    "ровн": "Рівне", "луцьк": "Луцьк", "луцк": "Луцьк", "ужгород": "Ужгород",
    "тернопіл": "Тернопіль", "тернопол": "Тернопіль", "хмельниц": "Хмельницький",
    "кропивниц": "Кропивницький", "миколаїв": "Миколаїв", "миколаєв": "Миколаїв",
    "николаев": "Миколаїв", "херсон": "Херсон", "івано-франк": "Івано-Франківськ",
    "ивано-франк": "Івано-Франківськ", "франківськ": "Івано-Франківськ", "кривий ріг": "Кривий Ріг",
    "кривому роз": "Кривий Ріг", "кривой рог": "Кривий Ріг", "біла церк": "Біла Церква",
    "белая церк": "Біла Церква", "кременчу": "Кременчук", "кам'янськ": "Кам'янське",
    "павлоград": "Павлоград", "бровар": "Бровари", "бориспіл": "Бориспіль", "ірпін": "Ірпінь",
    "буча": "Буча", "обухов": "Обухів", "умань": "Умань", "умані": "Умань", "мукачев": "Мукачево",
    "дрогобич": "Дрогобич", "стрий": "Стрий", "ковель": "Ковель", "нікопол": "Нікополь",
    "никопол": "Нікополь", "мелітопол": "Мелітополь", "бердичів": "Бердичів",
}
_CITY_WORD = re.compile(r"[А-ЯІЇЄҐа-яіїєґ'’-]{2,}")

# Часті прохання: підпис на екрані → вираз. Лише те, що власник назвав або що є в цеху.
# «На швидку» прибрано (власник 09.11.26): терміновість приходить окремим листом
# адміністраторів, а в картці клієнта це слово лише шуміло.
ASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("покрити опаком", re.compile(r"покри\w*\s+опак\w*", re.I)),
    ("не фарбувати", re.compile(r"не\s+(?:роз)?фарб\w*|не\s+крас\w*|без\s+фарбув\w*", re.I)),
    ("фарбувати", re.compile(r"(?<!не\s)(?:роз)?фарбуват\w*|(?<!не\s)покрас\w*", re.I)),
    ("глазурувати", re.compile(r"глазур\w*", re.I)),
    ("на гвинтовій", re.compile(r"гвинт\w*|винт\w*", re.I)),
    ("відправити кур'єром", re.compile(r"кур.?[єеэ]р\w*", re.I)),
    ("зателефонувати", re.compile(r"зателефон\w*|передзвон\w*|перезвон\w*", re.I)),
)


@dataclass
class Fact:
    kind: str
    key: str
    value: str
    count: int = 0
    last: date | None = None
    letters: list[int] = field(default_factory=list)
    state: str | None = None
    manual: bool = False


@dataclass(frozen=True)
class Letter:
    id: int
    from_address: str | None
    from_name: str | None
    subject: str | None
    body_text: str | None
    received_at: datetime | None


# ── Витяг ────────────────────────────────────────────────────────────────────
def own_text(letter: Letter | EmailMessage) -> str:
    """Текст, написаний у цьому листі. У відповіді цитата нижче маркера містить наші ж
    слова й підпис; у пересланому навпаки: справжній лист клієнта лежить саме там."""
    body = letter.body_text or ""
    subject = letter.subject or ""
    if not _is_forwarded(letter):  # type: ignore[arg-type]
        match = _QUOTE_START.search(body)
        if match:
            body = body[: match.start()]
    return f"{subject}\n{body}"


def normalize_phone(match: re.Match[str]) -> str:
    return f"+38 0{match.group(1)} {match.group(2)} {match.group(3)} {match.group(4)}"


def find_phones(text: str) -> Iterable[tuple[str, int, int]]:
    for match in _PHONE.finditer(text or ""):
        yield normalize_phone(match), match.start(), match.end()


def _city_near(text: str) -> str | None:
    words = [w.casefold().replace("’", "'") for w in _CITY_WORD.findall(text)]
    # Пара слів для «Кривий Ріг», «Біла Церква»; окремі слова для решти.
    for word in [" ".join(pair) for pair in zip(words, words[1:])] + words:
        for stem, name in _CITIES.items():
            if word.startswith(stem):
                return name
    return None


def find_shipping(text: str) -> Iterable[tuple[str, int, int]]:
    """Куди просять відправити. Рядок за рядком: місто шукається лише в тому самому рядку,
    інакше воно приліпиться від адреси клініки в підписі."""
    offset = 0
    for line in (text or "").split("\n"):
        yield from _shipping_in_line(line, offset)
        offset += len(line) + 1


def _shipping_in_line(line: str, offset: int) -> Iterable[tuple[str, int, int]]:
    np_word = _NP_LINE.search(line)
    branch = _BRANCH_AFTER.search(line) or _BRANCH_BEFORE.search(line) or _NP_NUMBER.search(line)
    # «стоматологічне відділення» без номера й без «НП» доставкою не є: обидва вирази його не беруть.
    spot = branch or np_word
    if spot is not None:
        city = _city_near(line)
        kind = "поштомат" if branch and re.search(r"п[оа]?ш?ч?т?омат", branch.group(0), re.I) else "відділення"
        parts = ["Нова пошта"]
        if city:
            parts.append(city)
        if branch:
            parts.append(f"{kind} {branch.group(1)}")
        yield ", ".join(parts), offset + spot.start(), offset + spot.end()
    up = _UKRPOSHTA.search(line)
    if up:
        city = _city_near(line)
        yield ("Укрпошта, " + city if city else "Укрпошта"), offset + up.start(), offset + up.end()
    pickup = _PICKUP.search(line)
    if pickup:
        yield "Самовивіз або кур'єр клієнта", offset + pickup.start(), offset + pickup.end()


def clean_sender_name(name: str | None, address: str | None) -> str | None:
    value = " ".join((name or "").split()).strip(" \"'")
    if not value or "@" in value or value.casefold() == (address or "").casefold():
        return None
    return value


# ── Листи клієнта ────────────────────────────────────────────────────────────
_LETTER_COLUMNS = (
    EmailMessage.id,
    EmailMessage.from_address,
    EmailMessage.from_name,
    EmailMessage.subject,
    EmailMessage.body_text,
    EmailMessage.received_at,
)


def client_letters(db: Session, client: Client, orders: Sequence[Order]) -> list[Letter]:
    """Листи, що належать клієнту: ті, з яких прийнято його роботи, плюс ще не прийняті
    від відправника, якого памʼятає «Памʼять відправника»."""
    order_ids = [o.id for o in orders]
    ids: set[int] = {o.source_email_id for o in orders if o.source_email_id}
    if order_ids:
        ids.update(db.scalars(select(EmailMessage.id).where(EmailMessage.order_id.in_(order_ids))))
        ids.update(db.scalars(select(OrderEmail.email_message_id).where(OrderEmail.order_id.in_(order_ids))))

    memory = db.execute(select(ClientSenderMemory.sender_key, ClientSenderMemory.client_name)).all()
    wanted = set(matching_client_names(client.canonical_name, [m[1] for m in memory if m[1]]))
    keys = {m[0] for m in memory if m[1] in wanted}
    addresses = {k.split("|", 1)[0] for k in keys}

    conditions = []
    if ids:
        conditions.append(EmailMessage.id.in_(ids))
    if addresses:
        conditions.append(EmailMessage.from_address.in_(addresses))
    if not conditions:
        return []
    rows = db.execute(
        select(*_LETTER_COLUMNS)
        .where(or_(*conditions), EmailMessage.filter_category.is_(None))
        .order_by(EmailMessage.id)
    ).all()
    letters = []
    for row in rows:
        letter = Letter(*row)
        # Спільна скринька: адреса одна, клієнти різні. Такий лист рахується лише за ключем.
        if letter.id in ids or sender_key_for(letter) in keys:  # type: ignore[arg-type]
            letters.append(letter)
    return letters


_shared_cache: tuple[float, frozenset[str]] | None = None
_shared_lock = Lock()


def shared_phones(db: Session) -> frozenset[str]:
    """Номери, що трапляються в листах різних відправників, разом із цитатами."""
    global _shared_cache
    with _shared_lock:
        if _shared_cache and time.monotonic() - _shared_cache[0] < SHARED_PHONES_TTL_SECONDS:
            return _shared_cache[1]
    senders: dict[str, set[str]] = {}
    rows = db.execute(select(EmailMessage.from_address, EmailMessage.body_text)).yield_per(200)
    for address, body in rows:
        for phone, _, _ in find_phones(body or ""):
            senders.setdefault(phone, set()).add((address or "").casefold())
    shared = frozenset(p for p, who in senders.items() if len(who) >= SHARED_PHONE_SENDERS)
    with _shared_lock:
        _shared_cache = (time.monotonic(), shared)
    return shared


def reset_cache_for_tests() -> None:
    global _shared_cache
    _shared_cache = None


def _add(facts: dict[tuple[str, str], Fact], kind: str, value: str, letter: Letter, seen: set) -> None:
    key = value.casefold()
    if (kind, key) in seen:
        return
    seen.add((kind, key))
    fact = facts.setdefault((kind, key), Fact(kind=kind, key=key, value=value))
    fact.count += 1
    fact.letters.append(letter.id)
    day = letter.received_at.date() if letter.received_at else None
    if day and (fact.last is None or day > fact.last):
        fact.last = day
        fact.value = value


def extract_facts(letters: Sequence[Letter], excluded_phones: frozenset[str] = frozenset()) -> tuple[list[Fact], list[tuple[str, int]]]:
    facts: dict[tuple[str, str], Fact] = {}
    asks: Counter[str] = Counter()
    for letter in letters:
        seen: set = set()
        text = own_text(letter)
        for phone, _, _ in find_phones(text):
            if phone not in excluded_phones:
                _add(facts, "phone", phone, letter, seen)
        for ship, _, _ in find_shipping(text):
            _add(facts, "ship", ship, letter, seen)
        # Пересланий лист несе в заголовку адресу того, хто переслав, а не клієнта.
        if not _is_forwarded(letter):  # type: ignore[arg-type]
            name = clean_sender_name(letter.from_name, letter.from_address)
            if name:
                _add(facts, "sender", name, letter, seen)
            address = (letter.from_address or "").strip().lower()
            if address:
                _add(facts, "email", address, letter, seen)
        else:
            original = guess_client_from_forward(letter.body_text)
            if original and "@" in original:
                _add(facts, "email", original.strip().lower(), letter, seen)
        for label, pattern in ASKS:
            if pattern.search(text):
                asks[label] += 1
    ordered = sorted(facts.values(), key=lambda f: (-f.count, -(f.last or date.min).toordinal(), f.value))
    return ordered, asks.most_common()


# ── Ритм ─────────────────────────────────────────────────────────────────────
# Циферблат «коли пише» (власник 09.11.26, макет design/client-card-motion-variants.html#B):
# 24 години по колу, 7 кілець — дні (Пн усередині). Геометрія стала, рахується раз.
DIAL_SIZE = 560
_DIAL_C = DIAL_SIZE / 2
_DIAL_R0, _DIAL_RW, _DIAL_STEP, _DIAL_GAP = 112.0, 20.0, 22.0, 2.2


def _dial_point(r: float, a: float) -> tuple[float, float]:
    return _DIAL_C + r * math.sin(a), _DIAL_C - r * math.cos(a)


def _dial_arc(day: int, hour: int) -> str:
    r1 = _DIAL_R0 + day * _DIAL_STEP
    r2 = r1 + _DIAL_RW
    a = 2 * math.pi / HEAT_HOURS
    pad = _DIAL_GAP / r2
    a1, a2 = hour * a + pad, (hour + 1) * a - pad
    (x1, y1), (x2, y2) = _dial_point(r2, a1), _dial_point(r2, a2)
    (x3, y3), (x4, y4) = _dial_point(r1, a2), _dial_point(r1, a1)
    return (f"M{x1:.1f} {y1:.1f}A{r2:.0f} {r2:.0f} 0 0 1 {x2:.1f} {y2:.1f}"
            f"L{x3:.1f} {y3:.1f}A{r1:.0f} {r1:.0f} 0 0 0 {x4:.1f} {y4:.1f}Z")


DIAL_PATHS = [[_dial_arc(d, h) for h in range(HEAT_HOURS)] for d in range(7)]
_TICK_R = _DIAL_R0 + 7 * _DIAL_STEP + 16
DIAL_TICKS = [
    {"x": round(_dial_point(_TICK_R, h * 2 * math.pi / HEAT_HOURS)[0], 1),
     "y": round(_dial_point(_TICK_R, h * 2 * math.pi / HEAT_HOURS)[1], 1),
     "label": f"{h:02d}" if h % 3 == 0 else "·", "major": h % 3 == 0}
    for h in range(HEAT_HOURS)
]
DIAL_HAND = {"x": _DIAL_C, "y1": _DIAL_C - _DIAL_R0 + 10, "y2": _DIAL_C - _DIAL_R0 - 7 * _DIAL_STEP + 4}


def rhythm(letters: Sequence[Letter], today: date) -> dict:
    moments = [letter.received_at for letter in letters if letter.received_at]
    heat = [[0] * HEAT_HOURS for _ in range(7)]
    for moment in moments:
        heat[moment.weekday()][moment.hour] += 1
    peak = max((v for row in heat for v in row), default=0)
    cells = [
        [
            {"n": v, "o": round(0.18 + 0.82 * (v / peak) ** 0.5, 2) if v else 0, "x": x, "y": y, "p": DIAL_PATHS[y][x]}
            for x, v in enumerate(row)
        ]
        for y, row in enumerate(heat)
    ]
    days = sorted({m.date() for m in moments})
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    median = int(statistics.median(gaps)) if gaps else None
    last = days[-1] if days else None
    since = (today - last).days if last else None
    enough = len(days) >= RHYTHM_MIN_DAYS and median is not None
    quiet = bool(enough and since is not None and since > max(2 * (median or 0), QUIET_MIN_DAYS))

    day_totals = [sum(row) for row in heat]
    top_days = sorted(sorted(range(7), key=lambda i: -day_totals[i])[:2]) if moments else []
    top_days = [i for i in top_days if day_totals[i]]
    hour_totals = [sum(heat[y][x] for y in range(7)) for x in range(HEAT_HOURS)]
    best_start, best = 0, -1
    for start in range(HEAT_HOURS - 2):
        window = sum(hour_totals[start:start + 3])
        if window > best:
            best_start, best = start, window
    return {
        "cells": cells,
        "total": len(moments),
        "last": last,
        "since": since,
        "median": median,
        "enough": enough,
        "quiet": quiet,
        "top_days": [DOW[i].lower() for i in top_days],
        "top_hours": f"{best_start:02d}:00–{best_start + 3:02d}:00" if moments else None,
        "top_hours_short": f"{best_start:02d}–{best_start + 3:02d}" if moments else None,
        "day_bars": [
            {"day": DOW[i], "n": n, "w": round(100 * n / max(day_totals), 1) if any(day_totals) else 0}
            for i, n in enumerate(day_totals)
        ],
        "ticks": DIAL_TICKS,
        "hand": DIAL_HAND,
        "size": DIAL_SIZE,
    }


# ── Обсяг ────────────────────────────────────────────────────────────────────
def volume(db: Session, orders: Sequence[Order], today: date) -> dict:
    """Обсяг лише виконаного: те саме правило, що в статистиці."""
    ids = [o.id for o in orders]
    empty = {"weeks": [], "units": 0, "works": 0, "units_all": 0, "works_all": 0, "materials": [], "peak": 0}
    if not ids:
        return empty
    rows = db.execute(
        select(Order.sheet_tab, Order.quantity, Order.material_color, Order.material_id, data.units_expr(Order.quantity))
        .where(Order.id.in_(ids), data.done_clause(db))
    ).all()
    dated = data.tab_index(db).dated
    groups = data._material_group_map(db)
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=WEEKS - 1)
    weeks: list[dict[str, Any]] = [{"from": first + timedelta(weeks=i), "to": first + timedelta(weeks=i, days=6), "units": 0, "works": 0} for i in range(WEEKS)]
    labels: dict[str, Counter[str]] = {}
    units_by: Counter[str] = Counter()
    group_by: dict[str, str] = {}
    units_in, works_in, units_all = 0, 0, 0
    for tab, _quantity, material, material_id, units in rows:
        units = units or 0
        units_all += units
        day = dated.get(tab)
        if day is None or day < first or day > today:
            continue
        index = (day - first).days // 7
        weeks[index]["units"] += units
        weeks[index]["works"] += 1
        units_in += units
        works_in += 1
        raw = " ".join((material or "").split())
        if raw:
            key = raw.casefold()
            labels.setdefault(key, Counter())[raw] += 1
            units_by[key] += units
            group_by[key] = groups.get(material_id, "oth")
    total = sum(units_by.values()) or 1
    top = units_by.most_common(TOP_MATERIALS)
    best = top[0][1] if top and top[0][1] else 1
    materials = [
        {"label": labels[k].most_common(1)[0][0], "units": u, "pct": round(u * 100 / total), "w": round(u * 100 / best), "group": group_by[k]}
        for k, u in top
    ]
    peak = max((w["units"] for w in weeks), default=0)
    for i, week in enumerate(weeks):
        week["h"] = round(week["units"] * 100 / peak) if peak else 0
        week["x"] = i
    return {"weeks": weeks, "units": units_in, "works": works_in, "units_all": units_all, "works_all": len(rows), "materials": materials, "peak": peak, "first": first}


# ── Рішення людини ───────────────────────────────────────────────────────────
def apply_decisions(db: Session, client: Client, facts: list[Fact]) -> dict[str, list[Fact]]:
    decisions = {
        (d.kind, d.value_key): d
        for d in db.scalars(select(ClientFactDecision).where(ClientFactDecision.client_id == client.id))
    }
    grouped: dict[str, list[Fact]] = {kind: [] for kind in KINDS}
    for fact in facts:
        decision = decisions.get((fact.kind, fact.key))
        fact.state = decision.state if decision else None
        grouped[fact.kind].append(fact)
    for kind, items in grouped.items():
        items.sort(key=lambda f: (f.state != STATE_PIN, -f.count, f.value))
    return grouped


def set_decision(db: Session, client: Client, kind: str, key: str, state: str | None, actor: str | None) -> None:
    """Не комітить. Порожній стан прибирає рішення: факт знову просто пропозиція."""
    if kind not in KINDS:
        raise ValueError(f"невідомий вид факту: {kind}")
    row = db.scalar(
        select(ClientFactDecision).where(
            ClientFactDecision.client_id == client.id,
            ClientFactDecision.kind == kind,
            ClientFactDecision.value_key == key,
        )
    )
    if state is None:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        row = ClientFactDecision(client_id=client.id, kind=kind, value_key=key)
        db.add(row)
    row.state = state
    row.decided_by = actor
    row.decided_at = datetime.now()


def _ship_block(client: Client, grouped: dict[str, list[Fact]]) -> dict | None:
    """Дані для логіста: вписане вручну й закріплене важливіші за найчастіше."""

    def first(kind: str) -> str | None:
        items = [f for f in grouped[kind] if f.state != STATE_HIDE]
        return items[0].value if items else None

    who = first("sender")
    phone = (client.phone or "").strip() or first("phone")
    where = first("ship")
    if not phone and not where:
        return None
    text = "\n".join(part for part in (who or client.canonical_name, phone, where) if part)
    return {"who": who or client.canonical_name, "phone": phone, "where": where, "text": text}


def insights(db: Session, client: Client, orders: Sequence[Order], today: date | None = None) -> dict:
    today = today or business_today()
    letters = client_letters(db, client, orders)
    excluded = shared_phones(db) if letters else frozenset()
    facts, asks = extract_facts(letters, excluded)
    grouped = apply_decisions(db, client, facts)
    return {
        "letters_n": len(letters),
        "facts": grouped,
        "hidden": [f for items in grouped.values() for f in items if f.state == STATE_HIDE],
        "asks": asks,
        "ship": _ship_block(client, grouped),
        "rhythm": rhythm(letters, today),
        "volume": volume(db, orders, today),
    }


def facts_context(db: Session, client: Client, orders: Sequence[Order]) -> dict:
    """Лише ліва колонка: збереження рішення не має перемальовувати карту й графіки."""
    letters = client_letters(db, client, orders)
    facts, asks = extract_facts(letters, shared_phones(db) if letters else frozenset())
    grouped = apply_decisions(db, client, facts)
    return {
        "letters_n": len(letters),
        "facts": grouped,
        "hidden": [f for items in grouped.values() for f in items if f.state == STATE_HIDE],
        "asks": asks,
        "ship": _ship_block(client, grouped),
    }


# ── Докази ───────────────────────────────────────────────────────────────────
def _one_line(part: str) -> str:
    """Перенос стає пробілом, а пробіл на краю лишається: «телефон 067», не «телефон067»."""
    return re.sub(r"\s+", " ", part)


def _snippet(text: str, start: int, end: int) -> dict:
    left = max(0, start - PROOF_RADIUS)
    right = min(len(text), end + PROOF_RADIUS)
    return {
        "before": ("…" if left else "") + _one_line(text[left:start]),
        "match": _one_line(text[start:end]).strip(),
        "after": _one_line(text[end:right]) + ("…" if right < len(text) else ""),
    }


def proof(letters: Sequence[Letter], kind: str, key: str) -> list[dict]:
    """Уривки читаються з листа в момент кліку: у базу текст не копіюється."""
    found: list[dict] = []
    for letter in reversed(letters):
        day = letter.received_at
        if kind in ("sender", "email"):
            value = clean_sender_name(letter.from_name, letter.from_address) if kind == "sender" else (letter.from_address or "").lower()
            if (value or "").casefold() == key and not _is_forwarded(letter):  # type: ignore[arg-type]
                found.append({"letter_id": letter.id, "at": day, "before": "Від кого: ", "match": value, "after": ""})
        else:
            text = own_text(letter)
            spots = find_phones(text) if kind == "phone" else find_shipping(text)
            for value, start, end in spots:
                if value.casefold() == key:
                    found.append({"letter_id": letter.id, "at": day, **_snippet(text, start, end)})
                    break
        if len(found) >= PROOF_LIMIT:
            break
    return found


def letters_in_slot(letters: Sequence[Letter], weekday: int, hour: int, limit: int = 5) -> list[Letter]:
    hits = [
        letter
        for letter in letters
        if letter.received_at and letter.received_at.weekday() == weekday and letter.received_at.hour == hour
    ]
    return list(reversed(hits))[:limit]


# ── Замір для MCP: без текстів листів ────────────────────────────────────────
def mask_phone(value: str) -> str:
    return value[:7] + "***" + value[-2:]


def audit(db: Session, limit: int) -> dict:
    """Наскільки витяг працює на цих даних. Текстів листів і імен тут немає."""
    from app.client_profile import find_matching_orders  # локально: потрібне лише заміру

    clients = db.scalars(select(Client)).all()
    named = db.scalars(select(Order).where(Order.client_name.isnot(None))).all()
    rows: list[dict[str, Any]] = []
    excluded = shared_phones(db)
    for client in clients:
        orders = find_matching_orders(client.canonical_name, named)
        letters = client_letters(db, client, orders)
        if not letters:
            continue
        facts, asks = extract_facts(letters, excluded)
        by_kind = Counter(f.kind for f in facts)
        top_ship = next((f for f in facts if f.kind == "ship"), None)
        top_phone = next((f for f in facts if f.kind == "phone"), None)
        rows.append(
            {
                "id": client.id,
                "листів": len(letters),
                "телефонів": by_kind["phone"],
                "адрес_доставки": by_kind["ship"],
                "імен": by_kind["sender"],
                "пошт": by_kind["email"],
                "прохань": len(asks),
                "телефон": {"значення": mask_phone(top_phone.value), "листів": top_phone.count} if top_phone else None,
                "доставка": {"значення": top_ship.value, "листів": top_ship.count} if top_ship else None,
            }
        )
    rows.sort(key=lambda r: -int(r["листів"]))
    return {
        "клієнтів_із_листами": len(rows),
        "із_телефоном": sum(1 for r in rows if r["телефонів"]),
        "із_доставкою": sum(1 for r in rows if r["адрес_доставки"]),
        "номерів_у_різних_відправників_відсіяно": len(excluded),
        "клієнти": rows[:limit],
    }
