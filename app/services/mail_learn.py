"""Навчання на прийнятих листах: що картка підставила б проти того, що вписав
оператор (власник 18.11.26: «листи, які взяли у фрезерування, але система не
ідентифікувала правильно — зробимо з цього навчання»).

Для кожного прийнятого листа за `days` днів НИНІШНЄ правило картки
(`mail_letter_signals.card_material`) проганяється заново, і результат
звіряється з `Order.material_color` — тим, що оператор залишив у полі. Тож це
не «як було тоді», а «що схибило б сьогодні»: саме воно й показує, чого бракує.

Кожна розбіжність отримує причину й ознаку, ЗВІДКИ можна було взяти правильне:
слово й відтінок були в листі (правило не впоралось), відтінок лише в назвах
файлів, у листі нічого немає, або клієнт майже завжди бере те саме («звичне
клієнта»). З цих лічильників і видно, яке правило дасть найбільше.

Лише читання. Тексту листа, імен клієнтів і назв файлів НЕ віддає — лише
канон матеріалів (те, що пише цех у таблиці), відтінки й номери.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.material_classifier import match_key
from app.models import EmailMessage, Order
from app.services import mail_letter_signals as signals
from app.services import material_suggest as ms
from app.services.disc_suggest import lines_conflict
from app.services.stl_units import count_units

# «Звичне клієнта»: за скільки днів дивитись, скільки робіт треба щонайменше і
# яка частка одного матеріалу вважається звичкою.
HABIT_DAYS = 90
HABIT_MIN_WORKS = 3
HABIT_SHARE = 0.6

MATCH = "збіг"
NO_MATERIAL = "матеріал не впізнано"
OTHER_MATERIAL = "інший матеріал"
NO_SHADE = "колір не знайдено"
MANY_SHADES = "у листі кілька кольорів"
OTHER_SHADE = "інший колір"
OTHER_SPELLING = "те саме, інакше записано"

_NAME_SPLIT = re.compile(r"[_\-.()+,]+")
_CHUNK = 400


def _habits(db: Session, low: datetime) -> dict[str, list[tuple[datetime, int, str]]]:
    rows = db.execute(
        select(Order.id, Order.client_name, Order.material_color, Order.created_at)
        .where(Order.client_name.is_not(None), Order.created_at >= low - timedelta(days=HABIT_DAYS))
    ).all()
    out: dict[str, list[tuple[datetime, int, str]]] = defaultdict(list)
    for order_id, client, material, created in rows:
        key = match_key(material or "")
        name = (client or "").strip().casefold()
        if key and name and created is not None:
            out[name].append((created, order_id, key))
    return out


def _habit_of(history: dict[str, list[tuple[datetime, int, str]]], order: Order) -> Optional[str]:
    """Матеріал, який цей клієнт брав майже завжди ДО цієї роботи, або None."""
    name = (order.client_name or "").strip().casefold()
    if not name or order.created_at is None:
        return None
    low = order.created_at - timedelta(days=HABIT_DAYS)
    seen = Counter(
        key for created, order_id, key in history.get(name, ())
        if order_id != order.id and low <= created < order.created_at
    )
    total = sum(seen.values())
    if total < HABIT_MIN_WORKS:
        return None
    key, count = seen.most_common(1)[0]
    return key if count / total >= HABIT_SHARE else None


def _judge(db: Session, email: EmailMessage, order: Order, habit: Optional[str]) -> dict[str, Any]:
    operator = (order.material_color or "").strip()
    op_key = match_key(operator)
    guess = email.material_color_guess or ""
    context = signals.material_context(email)
    system = signals.card_material(
        db, email, context, client_name=order.client_name, before=order.created_at
    )
    splint = signals.splint_for(db, email, context)

    words = ms.canon_words_in(db, context)
    shades = ms.shades_in(context)
    names = " ".join(_NAME_SPLIT.sub(" ", a.filename or "") for a in email.attachments)
    name_shades = ms.shades_in(names)
    op_word = ms.canonical_material_word(db, operator)
    op_shades = ms.shades_in(operator)
    op_shade = op_shades[0] if len(op_shades) == 1 else None
    sys_word = splint or ms.canonical_material_word(db, guess, context)
    sys_shades = [] if splint else (ms.shades_in(guess) or shades)

    if (system and match_key(system) == op_key) or (not system and guess and match_key(guess) == op_key):
        verdict = MATCH
    elif not sys_word:
        verdict = NO_MATERIAL
    elif op_word and lines_conflict(op_word, sys_word):
        # Не `!=`: просто «katana» в листі — це диск STML (власник 23.11.26), і
        # сторож матеріалу 26.11.26 кричав «замовлено STML A1 — у листі Katana
        # A1». Правило те саме, що в підказці диска; STML проти UTML — різні.
        verdict = OTHER_MATERIAL
    elif op_shade and not sys_shades:
        verdict = NO_SHADE
    elif op_shade and len(sys_shades) > 1:
        verdict = MANY_SHADES
    elif op_shade and sys_shades and sys_shades[0] != op_shade:
        verdict = OTHER_SHADE
    else:
        verdict = OTHER_SPELLING

    if not op_shade:
        shade_where = "без відтінку"
    elif op_shade in shades or op_shade in ms.shades_in(guess):
        shade_where = "у листі"
    elif op_shade in name_shades:
        shade_where = "лише в назвах файлів"
    else:
        shade_where = "ніде"
    word_in_letter = bool(op_word) and (op_word in words or op_word == ms.canonical_material_word(db, guess))

    if verdict == MATCH:
        missing = ""
    elif (word_in_letter or not op_word) and shade_where in ("у листі", "без відтінку"):
        missing = "усе було в листі — правило не впоралось"
    elif shade_where == "лише в назвах файлів":
        missing = "відтінок лише в назвах файлів"
    elif not word_in_letter and shade_where == "у листі":
        missing = "матеріалу в листі немає, колір є"
    elif word_in_letter:
        missing = "кольору в листі немає, матеріал є"
    else:
        missing = "у листі немає ні матеріалу, ні кольору"

    if habit is None:
        habit_note = "звичного немає"
    elif habit == op_key:
        habit_note = "вгадало б"
    else:
        habit_note = "помилилось би"

    return {
        "робота": order.id,
        "лист": email.id,
        "висновок": verdict,
        "оператор": operator,
        "система": system,
        "слово_оператора": op_word,
        "слово_системи": sys_word,
        "слова_в_листі": words[:4],
        "відтінки_в_листі": shades[:4],
        "відтінок_оператора": shade_where,
        "чого_бракувало": missing,
        "звичне_клієнта": habit_note,
    }


def _quantity_verdict(email: EmailMessage, order: Order) -> str:
    result = count_units(a.filename for a in email.attachments)
    quantity = (order.quantity or "").strip()
    if not result.units:
        return "без зубів у назвах"
    return "збіг" if quantity.isdigit() and int(quantity) == result.units else "розбіжність"


def report(db: Session, days: int, limit: int = 40, *, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now()
    low = now - timedelta(days=days)
    orders = db.scalars(
        select(Order).where(Order.source_email_id.is_not(None), Order.created_at >= low)
    ).all()
    by_email: dict[int, list[Order]] = defaultdict(list)
    for order in orders:
        by_email[order.source_email_id].append(order)  # type: ignore[index]
    single = {eid: found[0] for eid, found in by_email.items() if len(found) == 1}
    history = _habits(db, low)

    rows: list[dict[str, Any]] = []
    quantity: Counter[str] = Counter()
    no_material = 0
    ids = sorted(single)
    for start in range(0, len(ids), _CHUNK):
        emails = db.scalars(
            select(EmailMessage)
            .where(EmailMessage.id.in_(ids[start: start + _CHUNK]))
            .options(selectinload(EmailMessage.attachments))
        ).all()
        for email in emails:
            order = single[email.id]
            quantity[_quantity_verdict(email, order)] += 1
            if not (order.material_color or "").strip():
                no_material += 1
                continue
            rows.append(_judge(db, email, order, _habit_of(history, order)))

    verdicts = Counter(r["висновок"] for r in rows)
    misses = [r for r in rows if r["висновок"] != MATCH]
    judged = len(rows)
    # Найчастіші причини першими, у кожній — найновіші листи.
    order_of = {name: i for i, (name, _n) in enumerate(Counter(r["висновок"] for r in misses).most_common())}
    misses.sort(key=lambda r: (order_of[r["висновок"]], -r["лист"]))
    return {
        "днів": days,
        "листів_звірено": judged,
        "лист_на_кілька_робіт": len(by_email) - len(single),
        "робота_без_матеріалу": no_material,
        "точність_%": round(100 * verdicts[MATCH] / judged, 1) if judged else None,
        "підсумок": dict(verdicts.most_common()),
        "чого_бракувало": dict(Counter(r["чого_бракувало"] for r in misses).most_common()),
        "звичне_клієнта_на_помилках": dict(Counter(r["звичне_клієнта"] for r in misses).most_common()),
        "звичне_клієнта_на_збігах": dict(
            Counter(r["звичне_клієнта"] for r in rows if r["висновок"] == MATCH).most_common()
        ),
        "інший_матеріал_пари": dict(Counter(
            f"{r['слово_системи'] or '—'} → {r['слово_оператора'] or r['оператор']}"
            for r in misses if r["висновок"] == OTHER_MATERIAL
        ).most_common(15)),
        "оператор_писав_коли_не_впізнано": dict(Counter(
            r["слово_оператора"] or r["оператор"] for r in misses if r["висновок"] == NO_MATERIAL
        ).most_common(15)),
        "кількість_з_назв_файлів": dict(quantity.most_common()),
        "приклади": misses[:limit],
        "пояснення": (
            "«система» — що картка підставила б зараз; «оператор» — що лишилось у роботі. "
            "«звичне клієнта» — матеріал, який клієнт брав у ≥60 % із ≥3 робіт за 90 днів до листа."
        ),
    }
