"""«Де лежить робота» на ранковій видачі (бриф HANDOUT_PLACE_BRIEF.md, 08.10.26).

У кінці зміни оператор пише в колонку «Відфрезерував» (N, `Order.milled_raw`),
де лишився диск: `1`, `бочка`, `3`, `чіна` — печі, `250-sec` — верстат. Напарник
зранку шукає там. CRM цю колонку читала й раніше, але ніде не показувала.

Тут три речі, і кожна — ОДНЕ місце:

* `place_of` — що означає текст клітинки. Старі маркери авто-літери
  (`RR 09:12`, `D`) місцем не є: до 08.10.26 CRM сама писала туди літеру
  оператора при «відфрезеровано» (`sheet_writer.apply_status_markers`), і
  показати її піччю означало б збрехати.
* `machine_places` — верстат береться з телеметрії, а не з клітинки: поки диск
  на верстаті, мітку ставить система. Sum3D ID — лише час доби й повторюється
  між днями, тому робота мусить бути з дня поруч із датою програми (те саме
  вікно, що в `machines._queued_work_for`).
* `disc_orders` — мітка печі ставиться ДИСКУ: один Sum3D у межах однієї
  вкладки — один диск, і він цілком іде в одну піч, у всіх клієнтів.

Порядок видачі тут не чіпається ніде (правило видачі №1): смуга лише лічить і
пригашує, сортування немає.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order
from app.services.order_dates import order_date

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FurnacePlace:
    key: str          # стабільний ключ для форми, CSS і смуги
    label: str        # як піч називають у цеху
    word: str         # що пишемо в колонку N — саме те слово, яким пишуть руками
    aliases: tuple[str, ...]


# Печей ЧОТИРИ (власник 08.10.26). Текст у колонці — саме ці слова.
FURNACES: tuple[FurnacePlace, ...] = (
    FurnacePlace("p1", "Перша", "1", ("1", "перша", "піч 1", "піч1")),
    FurnacePlace("bk", "Бочка", "бочка", ("бочка",)),
    FurnacePlace("p3", "Третя", "3", ("3", "третя", "піч 3", "піч3")),
    FurnacePlace("cn", "Китайська", "чіна", ("чіна", "чина", "china", "китайська")),
)
FURNACE_BY_KEY = {f.key: f for f in FURNACES}
_FURNACE_BY_ALIAS = {alias: f for f in FURNACES for alias in f.aliases}

KIND_FURNACE = "furnace"
KIND_MACHINE = "machine"
KIND_OTHER = "other"

# Ключі смуги, крім печей. Рядок без мітки — «none».
BUCKET_MACHINE = "mc"
BUCKET_OTHER = "other"
BUCKET_NONE = "none"

# Хвіст « HH:MM». Так виглядає слід авто-літери «<оператор> HH:MM» (до
# 08.10.26 `apply_status_markers` писав його в колонку N; ім'я буває з двох
# слів — «Claude Dev 23:54» на dev-копії таблиці), а людина може дописати час
# до печі («бочка 22:00»). Тому час відрізаємо й дивимось на решту.
_TRAILING_TIME_RE = re.compile(r"\s+\d{1,2}:\d{2}$")
# Сама літера оператора без часу («D», «RR», «ЦЦ») — так пишуть «Прорахував».
_OPERATOR_LETTERS_RE = re.compile(r"^[^\W\d_]{1,2}$")

# Вікно «робота поруч із датою програми» — те саме, що в
# `machines.SCREEN_PROGRAM_DAYS_BACK` (і на 1 день вперед).
PROGRAM_DAYS_BACK = 14


@dataclass(frozen=True)
class Place:
    kind: str                 # furnace | machine | other
    key: str                  # p1/bk/p3/cn, назва верстата або «other»
    label: str
    raw: str = ""
    percent: Optional[int] = None
    # Поставила телеметрія (диск зараз на верстаті), а не клітинка. Таку мітку
    # оператор не міняє — меню печей у рядку немає.
    live: bool = False

    @property
    def bucket(self) -> str:
        if self.kind == KIND_FURNACE:
            return self.key
        if self.kind == KIND_MACHINE:
            return BUCKET_MACHINE
        return BUCKET_OTHER


def _norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "є": "e",
    "ж": "j", "з": "z", "и": "i", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ь": "", "ю": "u",
    "я": "a", "ы": "i", "э": "e", "ё": "e", "ъ": "",
})


def _machine_norm(text: str) -> str:
    """`250i-Sec` і `250-sec` — той самий верстат: без розділових знаків, без
    «i» одразу після цифр (так верстати кличуть у цеху), кирилиця — латиницею
    («Сек», «Борис»)."""
    squashed = re.sub(r"[\W_]+", "", (text or "").casefold().translate(_TRANSLIT))
    return re.sub(r"(\d)i", r"\1", squashed)


def _machine_alpha(text: str) -> str:
    """Імʼя верстата без цифр: `250i-Sec` → `sec`, `150i-Olejka` → `olejka`.
    Саме так його найчастіше й пишуть у колонці («Sec», «Olejka», «Loder»)."""
    return re.sub(r"\d+", "", _machine_norm(text))


# Нечітке зіставлення з назвою верстата: одруківка («Loder» → «350i Loader»)
# так, але лише коли кандидат ОДИН і явно кращий за другий.
_MACHINE_MIN_SCORE = 75
_MACHINE_MIN_GAP = 10


def _machine_match(text: str, machine_names: Iterable[str]) -> Optional[str]:
    from rapidfuzz import fuzz

    names = [n for n in machine_names if n]
    full = _machine_norm(text)
    alpha = _machine_alpha(text)
    if not full:
        return None
    exact = [n for n in names if _machine_norm(n) == full or (alpha and _machine_alpha(n) == full)]
    if len(exact) == 1:
        return exact[0]
    if exact or len(alpha) < 3:
        return None
    scored = sorted(
        ((fuzz.ratio(alpha, _machine_alpha(n)), n) for n in names if len(_machine_alpha(n)) >= 3),
        reverse=True,
    )
    if not scored or scored[0][0] < _MACHINE_MIN_SCORE:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < _MACHINE_MIN_GAP:
        return None
    return scored[0][1]


def place_of(raw: Optional[str], machine_names: Iterable[str] = ()) -> Optional[Place]:
    """Що означає текст колонки «Відфрезерував». None — місця немає.

    Порядок: піч → верстат (нечітко до назв у Налаштуваннях) → «невідоме місце»
    з сирим текстом (показати як є, не губити). Маркер авто-літери, сама літера
    й чиста позначка без букв і цифр («+», «✓») — НЕ місце."""
    text = (raw or "").strip()
    if not text:
        return None
    furnace = _FURNACE_BY_ALIAS.get(_norm(text))
    if furnace is not None:
        return Place(KIND_FURNACE, furnace.key, furnace.label, raw=text)
    timed = _TRAILING_TIME_RE.search(text)
    head = text[: timed.start()].strip() if timed else text
    if timed:
        furnace = _FURNACE_BY_ALIAS.get(_norm(head))
        if furnace is not None:
            return Place(KIND_FURNACE, furnace.key, furnace.label, raw=text)
    if _OPERATOR_LETTERS_RE.match(head) or not any(ch.isalnum() for ch in head):
        return None
    machine = _machine_match(head, machine_names)
    if machine is not None:
        return Place(KIND_MACHINE, machine, machine, raw=text)
    if timed:
        # «<хтось> HH:MM» без печі й верстата — слід авто-літери, не місце.
        return None
    return Place(KIND_OTHER, BUCKET_OTHER, text[:24], raw=text)


def furnace_word(key: str) -> Optional[str]:
    furnace = FURNACE_BY_KEY.get(key)
    return furnace.word if furnace else None


def disc_orders(db: Session, order: Order) -> list[Order]:
    """Усі клієнтські роботи ТОГО САМОГО диска: Sum3D + вкладка.

    Вкладка обовʼязкова: Sum3D — лише час доби, і `16-15-47` іншого дня — це
    інший диск. Лабораторні рядки не чіпаємо: їх не видають, а непорожня N у
    лабораторному рядку синк читає як «відфрезеровано» (`sync._infer_status`).
    Без Sum3D диск невідомий — мітка лише на цю роботу."""
    sum3d = (order.sum3d_id or "").strip()
    if not sum3d or not order.sheet_tab:
        return [order]
    siblings = list(
        db.scalars(
            select(Order).where(
                Order.sum3d_id == sum3d,
                Order.sheet_tab == order.sheet_tab,
                Order.archived_at.is_(None),
                Order.source != "lab",
            ).order_by(Order.id)
        )
    )
    if all(o.id != order.id for o in siblings):
        siblings.insert(0, order)
    return siblings


def set_disc_place(db: Session, user, order: Order, key: str) -> list[Order]:
    """Поставити (або зняти, `key == ""`) піч усім роботам диска.

    Повертає роботи, у яких значення справді змінилось — лише їх писати в
    таблицю. Кожна зміна — рядок журналу дій з оператором. Коміт — за
    викликачем."""
    from app.services.undo import log_action

    if key and key not in FURNACE_BY_KEY:
        raise ValueError(f"невідома піч: {key!r}")
    word = furnace_word(key) if key else None
    label = FURNACE_BY_KEY[key].label if key else "прибрано"
    changed: list[Order] = []
    for target in disc_orders(db, order):
        old = target.milled_raw or None
        if old == word:
            continue
        target.milled_raw = word
        log_action(
            db, order=target, operator=user, action_type="place",
            field="milled_raw", old=old, new=word,
            note=f"місце: {label}" + (f" (диск {target.sum3d_id})" if target.sum3d_id else ""),
        )
        changed.append(target)
    return changed


def _program_date(card) -> Optional[date]:
    from app.machine_ocr import parse_iso_title

    state = getattr(card, "state", None)
    program = parse_iso_title(getattr(state, "iso_name", None) or "")
    if program is None or program.sum3d_id != card.sum3d_id:
        return None
    try:
        return date.fromisoformat(program.date)
    except ValueError:
        return None


def machine_places(cards, orders: Iterable[Order]) -> dict[int, Place]:
    """Роботи, чий диск зараз на верстаті: {id роботи: місце-верстат}.

    Лише свіжа програма (`card.sum3d_id` уже порожній на протухлому кадрі чи
    обриві) і лише роботи з дня поруч із датою програми. Немає дати в назві
    програми — немає й прив'язки: Sum3D сам по собі повторюється між днями.
    Той самий Sum3D на двох верстатах — не вгадуємо, мовчимо."""
    by_sum3d: dict[str, list[Order]] = {}
    for order in orders:
        sid = (order.sum3d_id or "").strip()
        if sid:
            by_sum3d.setdefault(sid, []).append(order)
    found: dict[int, Place] = {}
    doubtful: set[int] = set()
    for card in cards:
        card_sid = getattr(card, "sum3d_id", None)
        if not card_sid or card_sid not in by_sum3d:
            continue
        milled_on = _program_date(card)
        if milled_on is None:
            continue
        low = milled_on - timedelta(days=PROGRAM_DAYS_BACK)
        high = milled_on + timedelta(days=1)
        name = card.target.name
        for order in by_sum3d[card_sid]:
            if not low <= order_date(order) <= high:
                continue
            if order.id in found and found[order.id].key != name:
                doubtful.add(order.id)
                continue
            found[order.id] = Place(KIND_MACHINE, name, name, percent=card.percent, live=True)
    for order_id in doubtful:
        found.pop(order_id, None)
    return found


def furnace_notes(furnace_cards) -> dict[str, str]:
    """Підпис печі в смузі з телеметрії: «ще 3:12» для тієї, що гріє.

    Підключена поки лише Бочка; назва в Налаштуваннях зіставляється зі
    словами печі. Немає свіжих даних — підпису немає (хибне число гірше)."""
    from app.furnace_ocr import format_remaining

    notes: dict[str, str] = {}
    for card in furnace_cards:
        try:
            if not card.is_running or not card.has_data or card.has_problem:
                continue
            remaining = card.state.remaining_seconds if card.state else None
        except Exception:  # noqa: BLE001 — підпис печі не має валити видачу
            continue
        if remaining is None:
            continue
        place = place_of(card.target.name)
        if place is None or place.kind != KIND_FURNACE:
            name = _norm(card.target.name)
            match = next((f for f in FURNACES if f.label.casefold() in name), None)
            if match is None:
                continue
            key = match.key
        else:
            key = place.key
        notes[key] = "ще " + format_remaining(remaining)
    return notes


@dataclass
class StripItem:
    key: str
    label: str
    count: int
    note: str = ""


@dataclass
class PlaceView:
    places: dict[int, Optional[Place]] = field(default_factory=dict)
    strip: list[StripItem] = field(default_factory=list)

    def of(self, order_id: int) -> Optional[Place]:
        return self.places.get(order_id)

    def bucket(self, order_id: int) -> str:
        place = self.places.get(order_id)
        return place.bucket if place else BUCKET_NONE


def build_place_view(
    orders: list[Order],
    *,
    machine_cards=(),
    furnace_cards=(),
    machine_names: Iterable[str] = (),
) -> PlaceView:
    """Місце кожної роботи екрана й смуга лічильників з ТОГО САМОГО набору.

    Верстат з телеметрії головніший за клітинку: диск, що зараз фрезерується,
    у печі бути не може."""
    names = list(machine_names)
    on_machine = machine_places(machine_cards, orders)
    places: dict[int, Optional[Place]] = {}
    for order in orders:
        places[order.id] = on_machine.get(order.id) or place_of(order.milled_raw, names)
    counts: dict[str, int] = {}
    for place in places.values():
        bucket = place.bucket if place else BUCKET_NONE
        counts[bucket] = counts.get(bucket, 0) + 1
    notes = furnace_notes(furnace_cards)
    strip = [
        StripItem(f.key, f.label, counts.get(f.key, 0), notes.get(f.key, ""))
        for f in FURNACES
    ]
    strip.append(StripItem(BUCKET_MACHINE, "на верстатах", counts.get(BUCKET_MACHINE, 0)))
    if counts.get(BUCKET_OTHER):
        strip.append(StripItem(BUCKET_OTHER, "інше місце", counts[BUCKET_OTHER]))
    strip.append(StripItem(BUCKET_NONE, "без мітки", counts.get(BUCKET_NONE, 0)))
    return PlaceView(places=places, strip=strip)


def place_view_for(db: Session, orders: list[Order]) -> PlaceView:
    """`build_place_view` з живою телеметрією. Телеметрія — пам'ять процесу
    плюс один-два запити; збій у ній видачу не валить: місця з клітинки
    лишаються, верстатів і таймера просто не буде."""
    machine_cards: list = []
    furnace_cards: list = []
    machine_names: list[str] = []
    try:
        from app.services import machines

        machine_cards = machines.snapshot(db)
        machine_names = [m.name for m in machines.list_machines(db) if m.name]
    except Exception:  # noqa: BLE001
        logger.exception("Видача: телеметрію верстатів для міток місця не зібрано")
    try:
        from app.services import furnace

        furnace_cards = furnace.snapshot(db)
    except Exception:  # noqa: BLE001
        logger.exception("Видача: стан печей для смуги місць не зібрано")
    return build_place_view(
        orders,
        machine_cards=machine_cards,
        furnace_cards=furnace_cards,
        machine_names=machine_names,
    )
