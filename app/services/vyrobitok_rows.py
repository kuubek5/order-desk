"""«Виробіток»: одне місце, що про КОЖЕН рядок вирішує — рахується він чи ні, і чому.

Досі це рішення жило в циклі `vyrobitok._live_units` і мовчало: рядок із
невпізнаним матеріалом, порожньою кількістю чи без Sum3D просто не додавався до
суми, і ніхто не бачив, що саме випало. Звірка з ручною таблицею за листопад
2026 (власник 22.11.26) впиралась у це щоразу: число є, а з чого воно — ні.

Тут рядок отримує ВЕРДИКТ: колонку й одиниці або причину. Табель будує з
вердиктів суму (`vyrobitok._live_units`), екран показує «не пораховано й чому»,
а `kmill_vyrobitok_audit` — «було → стало» по днях.

Два набори правил:

* **старі** (`v2=False`) — рівно те, що рахувалось до 22.11.26, рядок у рядок.
  Табель ними більше не рахує: лишились для звірки «було → стало» і як
  аварійний відкат (`vyrobitok.rules_v2`, налаштування `vyrobitok_rules_v2`=«0»);
* **чинні** (`v2=True`, типово) — правила, які власник назвав 22.11.26
  («аксіома, без перемикань»):
    1. *не прорахована робота не рахується* — тепер і для пошти (для
       лабораторії діяло й раніше): рядок без часу Sum3D не виконано;
    2. *переробка рахується один раз*, скільки б разів її не фрезерували.
       Колонку «Який раз фрезерується» на практиці не заповнюють (наряд 36652:
       прорахований 27.10 і знову 02.11, «який раз» порожній в обох — рахувався
       двічі), тому переробку впізнаємо самі: той самий наряд із тим самим
       видом, матеріалом і кількістю вже прорахований РАНІШОГО дня ІНШИМ
       прорахунком (інший Sum3D). Той самий Sum3D в іншому дні — не переробка,
       а той самий прорахунок: рядок перенесли вже прорахованим або він стоїть
       у двох вкладках одразу. Його не чіпаємо, лише позначаємо (`note`), і
       звірка показує обидва випадки окремо — рішення за власником.

Переробку З ЛИСТА (`mail_rework`, 03.11.26) нові правила НЕ виключають: тоді
власник окремо вирішив, що вона йде в загальну кількість. Сьогоднішнє «рахується
один раз» цьому суперечить, тож рішення — за ним; доти такий рядок рахується і
несе позначку (`note`), а звірка показує, скільки їх.

Що НЕ переробка, хоч наряд той самий: інший вид чи матеріал («2й етап» —
коронки після абатментів; 6-й і 7-й зуб окремими рядками) — це різні вироби.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.material_classifier import normalize_material
from app.models import Material, Order
from app.services.order_dates import order_date, order_date_of
from app.stats import has_sum3d_time, parse_int_safe, sum3d_ids

REASON_REWORK_MARK = "rework_mark"
REASON_NO_SUM3D = "no_sum3d"
REASON_NO_MATERIAL = "no_material"
REASON_OTHER_MATERIAL = "other_material"
REASON_NO_QUANTITY = "no_quantity"
REASON_REPEAT = "repeat"

REASON_LABELS = {
    REASON_REWORK_MARK: "переробка — у колонці «Який раз» стоїть 2 чи більше",
    REASON_NO_SUM3D: "не прораховано — немає Sum3D",
    REASON_NO_MATERIAL: "матеріал порожній або не впізнано",
    REASON_OTHER_MATERIAL: "не фрезерна робота (СЛМ, моделювання…)",
    REASON_NO_QUANTITY: "кількість порожня або не число",
    REASON_REPEAT: "переробка наряду",
}
NOTE_MAIL_REWORK = "переробка з листа — рахується за рішенням 03.11"
# Той самий прорахунок (наряд, вид, матеріал, кількість І Sum3D) у ранішому дні.
# Рядок там ЖИВИЙ — робота порахована в обох днях; рядка там уже немає —
# його перенесли. Обидва лише позначаємо: рішення за власником.
NOTE_TWIN_LIVE = "той самий прорахунок стоїть і в {day:%d.%m} — пораховано в обох днях"
NOTE_TWIN_MOVED = "прорахований рядок перенесено з {day:%d.%m}"

# ключ виробу → [(день, id роботи, Sum3D-и, рядок ще живий)]
Index = dict[tuple, list[tuple[date, int, frozenset[str], bool]]]

# Ті самі ключі, що `vyrobitok.MATERIAL_KEY_BY_NAME`/`ORDER_MATERIALS`; тут копія,
# бо `vyrobitok` імпортує цей модуль (сторож збігу — tests/test_vyrobitok_rows.py).
MATERIAL_KEY_BY_NAME = {"Цирконій": "zr", "ПММА": "pmma", "Віск": "wax", "СЛМ": "slm", "Титан": "ti"}
ORDER_MATERIALS = ("zr", "pmma", "wax", "ti")


@dataclass(frozen=True)
class Verdict:
    order_id: int
    day: date
    bucket: str                 # "lab" | "mail"
    col_key: Optional[str]      # колонка табеля, якщо рядок рахується
    units: int
    reason: Optional[str]       # None — рядок пораховано
    note: str = ""              # «уже пораховано 27.10», «той самий прорахунок і в 26.10»
    row_number: Optional[int] = None
    work_order_no: str = ""
    client_name: str = ""
    material_color: str = ""
    quantity: str = ""
    kind: str = ""
    has_sum3d: bool = False

    @property
    def counted(self) -> bool:
        return self.reason is None

    @property
    def reason_label(self) -> str:
        label = REASON_LABELS.get(self.reason or "", "")
        return f"{label} ({self.note})" if label and self.note else label


def _is_rework_mark(mill_count: str | None) -> bool:
    n = parse_int_safe(mill_count)
    return n is not None and n >= 2


def _bucket(source: str) -> str:
    """Єдине місце «Виробітку», що ділить роботи на лабораторні й решту
    (клієнтські рядки таблиці й пошта йдуть однією колонкою «пошта»)."""
    return "lab" if source == "lab" else "mail"


def _job_key(work_order_no: Any, kind: Any, material_color: Any, quantity: Any) -> Optional[tuple]:
    """Ключ «той самий виріб того самого наряду». None — порівнювати нема чим."""
    number = " ".join(str(work_order_no or "").split()).lower()
    units = parse_int_safe(quantity)
    if not number or units is None:
        return None
    return (
        number,
        " ".join(str(kind or "").split()).lower(),
        normalize_material(material_color),
        units,
    )


def _calculations(db: Session) -> Index:
    """Усі ПРОРАХОВАНІ рядки з нарядом за ключем виробу: (день, id, Sum3D-и).

    Разом з архівними: лаба чистить старі вкладки, і перший прорахунок наряду
    до часу переробки вже здебільшого в архіві — а знати про нього треба.
    Лише потрібні колонки, не цілі рядки ORM: це вся історія робіт.
    """
    index: Index = defaultdict(list)
    rows = db.execute(
        select(
            Order.id, Order.work_order_no, Order.kind, Order.material_color, Order.quantity,
            Order.sum3d_id, Order.sheet_tab, Order.created_at, Order.mill_count,
            Order.archived_at,
        ).where(Order.work_order_no.is_not(None), Order.sum3d_id.is_not(None))
    )
    for oid, number, kind, material, quantity, sum3d, tab, created, mill_count, archived in rows:
        if not has_sum3d_time(sum3d) or _is_rework_mark(mill_count):
            continue
        key = _job_key(number, kind, material, quantity)
        if key is None:
            continue
        index[key].append(
            (order_date_of(tab, created), oid, frozenset(sum3d_ids(sum3d)), archived is None)
        )
    return index


def _earlier(
    index: Index, order: Order, day: date
) -> tuple[Optional[date], Optional[date], Optional[date]]:
    """Раніші дні: (ІНШИЙ прорахунок, ТОЙ САМИЙ прорахунок у живому рядку,
    ТОЙ САМИЙ прорахунок у рядку, якого в таблиці вже немає)."""
    key = _job_key(order.work_order_no, order.kind, order.material_color, order.quantity)
    if key is None:
        return None, None, None
    mine = frozenset(sum3d_ids(order.sum3d_id))
    other: Optional[date] = None
    twin_live: Optional[date] = None
    twin_gone: Optional[date] = None
    for seen_day, oid, times, live in index.get(key, ()):
        if oid == order.id or seen_day >= day:
            continue
        if not times & mine:
            other = seen_day if other is None else min(other, seen_day)
        elif live:
            twin_live = seen_day if twin_live is None else min(twin_live, seen_day)
        else:
            twin_gone = seen_day if twin_gone is None else max(twin_gone, seen_day)
    return other, twin_live, twin_gone


def vanished_rows(db: Session, first: date, last: date, *, v2: bool) -> dict[date, int]:
    """День → скільки АРХІВНИХ рядків дали б одиниці, якби були живі.

    Закритий день тримає число, зняте в момент закриття; рядок, що зник з
    таблиці ПІСЛЯ цього, у числі лишився, а серед живих робіт його вже немає.
    Перерахунок такого дня з живих робіт мовчки зменшив би табель, і відкат
    цього не повернув би — тому `vyrobitok.recount_month` такі дні
    оминає й називає. Перевірка груба свідомо (без розпізнавання переробок):
    зайвий пропущений день дешевший за втрачені одиниці.
    """
    rows = db.execute(
        select(
            Order.source, Order.sum3d_id, Order.mill_count, Order.quantity,
            Order.sheet_tab, Order.created_at, Material.name,
        )
        .join(Material, Order.material_id == Material.id)
        .where(Order.archived_at.is_not(None))
    )
    out: dict[date, int] = defaultdict(int)
    for source, sum3d, mill_count, quantity, tab, created, material in rows:
        d = order_date_of(tab, created)
        if not (first <= d <= last) or _is_rework_mark(mill_count):
            continue
        if not has_sum3d_time(sum3d) and (_bucket(source) == "lab" or v2):
            continue
        if MATERIAL_KEY_BY_NAME.get(material) not in ORDER_MATERIALS:
            continue
        if not parse_int_safe(quantity):
            continue
        out[d] += 1
    return dict(out)


def judge(
    db: Session, first: date, last: date, *, v2: bool
) -> tuple[list[Verdict], set[tuple[int, int]]]:
    """Вердикти по ЖИВИХ роботах діапазону + місяці, у яких узагалі є роботи.

    Порядок перевірок старих правил збережено рівно таким, яким він був у
    `vyrobitok._live_units`: від нього залежить, який день вважається «живим»
    (рядок із позначкою переробки день живим НЕ робить — як і раніше).
    """
    orders = db.scalars(
        select(Order).options(selectinload(Order.material)).where(Order.archived_at.is_(None))
    ).all()

    months: set[tuple[int, int]] = set()
    in_range: list[tuple[Order, date]] = []
    for order in orders:
        d = order_date(order)
        months.add((d.year, d.month))
        if first <= d <= last:
            in_range.append((order, d))

    index: Index = {}
    mail_reworks: set[int] = set()
    if v2 and in_range:
        from app.services.mail_rework import rework_order_ids

        index = _calculations(db)
        mail_reworks = rework_order_ids(db, [o.id for o, _d in in_range if _bucket(o.source) != "lab"])

    verdicts: list[Verdict] = []
    for order, d in in_range:
        bucket = _bucket(order.source)
        calculated = has_sum3d_time(order.sum3d_id)

        def verdict(col_key: Optional[str], units: int, reason: Optional[str], note: str = "") -> Verdict:
            return Verdict(
                order_id=order.id, day=d, bucket=bucket, col_key=col_key, units=units,
                reason=reason, note=note, row_number=order.row_number,
                work_order_no=order.work_order_no or "", client_name=order.client_name or "",
                material_color=order.material_color or "", quantity=order.quantity or "",
                kind=order.kind or "", has_sum3d=calculated,
            )

        if _is_rework_mark(order.mill_count):
            verdicts.append(verdict(None, 0, REASON_REWORK_MARK))
            continue
        if not calculated and (bucket == "lab" or v2):
            verdicts.append(verdict(None, 0, REASON_NO_SUM3D))
            continue
        material = order.material
        key = MATERIAL_KEY_BY_NAME.get(material.name) if material else None
        if key is None or key not in ORDER_MATERIALS:
            reason = REASON_NO_MATERIAL if material is None else REASON_OTHER_MATERIAL
            verdicts.append(verdict(None, 0, reason))
            continue
        col_key = f"{bucket}_{key}"
        units = parse_int_safe(order.quantity)
        if units is None:
            # І раніше такий рядок давав 0 одиниць — тепер це видно.
            verdicts.append(verdict(col_key, 0, REASON_NO_QUANTITY))
            continue
        note = ""
        if v2:
            if order.id in mail_reworks:
                note = NOTE_MAIL_REWORK
            other, twin_live, twin_gone = _earlier(index, order, d)
            if other is not None:
                verdicts.append(verdict(col_key, 0, REASON_REPEAT, f"уже пораховано {other:%d.%m}"))
                continue
            if not note and twin_live is not None:
                note = NOTE_TWIN_LIVE.format(day=twin_live)
            elif not note and twin_gone is not None:
                note = NOTE_TWIN_MOVED.format(day=twin_gone)
        verdicts.append(verdict(col_key, units, None, note))
    return verdicts, months
