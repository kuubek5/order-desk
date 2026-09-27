"""Прив'язка «робота ↔ тека в export» у мить, коли рядок з'явився (власник 26.09.26).

**Навіщо.** Спільного ключа «рядок таблиці ↔ тека з файлами» немає (CLAUDE.md
§2, правило 3), і видача три тижні ВГАДУВАЛА теку щоранку заново — за датою
партії, потім за «найближчим Sum3D» уночі, потім за підпартіями. Кожне
уточнення ламало сусідній випадок. Причина виявилась у самій основі: Sum3D ID
— це час ДИСКА, а не роботи. На `03-27-06` 24.09.26 прораховано п'ятьох
клієнтів, на `02-58-04` — двох; будь-яке «найближче до Sum3D» рано чи пізно
віддає теку не тій роботі (Середюк 4490: bitesplint сусідньої 4482 замість
свого зуба).

**Факт, на який спираємось** (потік оператора, власник 17.09.26): спершу він
створює теку й кладе файли, і ЛИШЕ ПОТІМ пише рядок — у Google Таблицю чи через
«Додати вручну». Тож тека рядка лежить на диску між ПОПЕРЕДНІМ рядком цього
клієнта й кольору і ним самим.

**Правило.** Для кожного клієнта й кольору рядки й теки йдуть однією
хронологією, і порядок зберігається: рядок отримує ще нічию «одиницю» (теку
кольору, підтеку-партію в ній або партію з файлами просто в ній), створену
після попереднього рядка й не пізніше за себе. Результат ЗАПИСУЄТЬСЯ в
`Order.export_folder_path`; далі видача показує саме його й нічого не вгадує,
а нова тека, що з'явиться пізніше, прив'язаний рядок не зачіпає. Sum3D тут не
використовується взагалі.

- Між рядками одна тека — вона.
- Кілька тек, а за цим рядком уже є наступний того самого кольору — старша
  цьому, решта наступним (оператор приготував теки, потім дописав рядки).
- Кілька тек і наступного рядка немає — чекаємо до 20 хв, чи не прийде він;
  не прийшов — найсвіжіша: старша тоді, найпевніше, нічия тека рядка, що
  з'явився РАНІШЕ за свої файли, і брати її означало б зсунути весь день.
- Між рядками тек немає — теку, приготовану ДО попереднього рядка, можна взяти
  лише якщо вона новіша за теку того попереднього рядка (порядок не
  перехрещується). Попередній рядок без теки — не беремо нічого.
- Пачка рядків, доданих разом («Додати вручну», одна секунда), — теки по
  порядку рядків; тек менше, ніж рядків, — жоден не прив'язується.

**Чесні межі (свідомо не вгадуємо):** рядок, написаний РАНІШЕ за теку (Кривовид
17.09.26: рядки 01:55, теки 04:48), своєї теки не отримує — лишається на
старому показі видачі. Інші кольори, інші клієнти між собою не змагаються.

Модуль без Request/Response; фоновий воркер — у `web.py`.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import utc_now, utc_to_business
from app.export_scanner import list_export_client_names_cached, scan_export_client
from app.material_match import materials_match
from app.models import Order

logger = logging.getLogger(__name__)

# ── ПАУЗА (рішення власника 27.09.26) ─────────────────────────────────────
# Механізм збудований і перевірений (див. `tests/test_folder_binding.py`), але
# в роботу поки не пускається: власник хоче спершу подивитись, чи взагалі
# болить старе вгадування. Вимикач один на ВСЕ: фоновий воркер не
# реєструється (`app/web.py`), і видача з діагностикою не змінюють поведінки —
# тобто в проді не з'являється ні нового запису в базу, ні нового правила
# показу тек. Повернути = поставити True (і зареєструвати воркер назад).
#
# Що лишається живим і без вимикача, бо це окреме виправлення, а не частина
# ідеї: `sync._reset_order_for_new_work` стирає `export_folder_path`, коли
# рядок віддано ІНШІЙ роботі — інакше під новою роботою показувались би файли
# старої (стосується й поштових робіт, які цю теку мали завжди).
ENABLED = False

# Годинник файлового сервера й ПК з CRM розходяться; теку, створену за дві
# хвилини «після» рядка, однаково вважаємо його. Поки ці хвилини не минули,
# рядок не прив'язуємо: інакше не побачили б теку, що от-от з'явиться.
BIND_SLACK = timedelta(minutes=2)
# Тека старша за добу до появи рядка — не його: чужа попередня робота того
# самого кольору (правило видачі 11.09.26).
CANDIDATE_WINDOW = timedelta(hours=24)
# Скільки пробуємо після появи рядка: перекриває рестарт чи недоступну шару.
ATTEMPT_WINDOW = timedelta(hours=6)
# Рядки, додані однією пачкою (4486 і 4490 — в одну секунду).
SIMULTANEOUS = timedelta(seconds=90)
# Скільки чекати на наступний рядок, коли тек більше, ніж рядків.
DEFER = timedelta(minutes=20)
# Партія (тека 2-го рівня) буває старшою за свій вміст на пів доби й більше:
# Середюк `Новая папка (762)` створена 10:08, остання підтека в ній — 00:24.
SCAN_LOOKBACK = timedelta(hours=48)
# Рядок без теки після проходу — наступна спроба рідше (хвилини). Кандидати —
# лише теки, старші за рядок, тож повтор допомагає тільки після збою шари.
_RETRY_MINUTES = (1, 2, 4, 8, 16, 30)
# Денний оператор рахує КОЖНУ роботу окремим проєктом Sum3D, і проєкт виникає
# за секунди після того, як файли лягли в теку. Бойові зсуви «тека → Sum3D» за
# 24.09.26: −2…−97 с (тека раніше) і +39…+78 с (файли перенесли після
# прорахунку). Уночі проєкт — цілий диск на кількох клієнтів, і там теки
# лягають хвилинами пізніше за нього — у це вікно не потрапляють.
STRONG_BEFORE = timedelta(seconds=150)
STRONG_AFTER = timedelta(seconds=90)
# Рядок ще без Sum3D (технік чи оператор допише його за хвилину-дві) —
# стільки чекаємо, перш ніж вирішувати лише за хронологією.
NO_SUM3D_WAIT = timedelta(minutes=60)

_state_lock = threading.Lock()
_next_try: dict[int, datetime] = {}
_attempts: dict[int, int] = {}


def reset_for_tests() -> None:
    with _state_lock:
        _next_try.clear()
        _attempts.clear()


@dataclass(frozen=True)
class Unit:
    """Одна прив'язувана тека: що записати в `export_folder_path`, коли її
    створено і який колір вона несе (назва теки кольору)."""

    rel: str
    created_at: datetime
    material: str


def rel_key(rel: str | None) -> str:
    """Спільна форма шляху для порівняння: Windows не розрізняє регістр і слеші."""
    return (rel or "").strip().replace("\\", "/").strip("/").casefold()


def units_of(entry) -> list[Unit]:
    """Прив'язувані одиниці однієї теки кольору.

    Кілька партій усередині (`entry.parts`) — кожна окремо: корінь теки кольору
    (3-рівневий шлях) і кожна підтека (4-рівневий). Одна партія — сама тека.
    Час кореня — не раніше за саму теку кольору: файли, ПЕРЕНЕСЕНІ зі
    «Завантажень», несуть свій давній час створення."""
    client = getattr(entry, "client_folder_name", "") or ""
    batch = getattr(entry, "batch_folder_name", "") or ""
    if not client or not batch:
        return []
    loose = (
        Path(getattr(entry, "folder_path", "") or "").name == batch
        and entry.material_color_folder_name == batch
    )
    base = f"{client}/{batch}" if loose else f"{client}/{batch}/{entry.material_color_folder_name}"
    folder_made = getattr(entry, "material_created_at", None) or entry.created_at
    parts = getattr(entry, "parts", ()) or ()
    if len(parts) < 2 or loose:
        return [Unit(base, folder_made, entry.material_color_folder_name)]
    units = []
    for part in parts:
        if "/" in part.first_stl:
            sub = part.first_stl.split("/", 1)[0]
            units.append(Unit(f"{base}/{sub}", part.created_at, entry.material_color_folder_name))
        else:
            units.append(
                Unit(base, max(part.created_at, folder_made), entry.material_color_folder_name)
            )
    return units


def entry_fully_bound(entry, bound: set[str]) -> bool:
    """Чи всі одиниці теки вже належать іншим роботам — тоді нечіткий збіг
    видачі не має пропонувати її ще комусь."""
    units = units_of(entry)
    return bool(units) and all(rel_key(u.rel) in bound for u in units)


def _family(order: Order) -> str:
    return " ".join((order.material_color or "").casefold().replace(",", ".").split())


@dataclass
class _Row:
    order: Order
    appeared: datetime  # київський час появи рядка
    family: str


def _clusters(rows: list[_Row]) -> list[list[_Row]]:
    """Рядки, що з'явились разом (пачка), — одна група."""
    groups: list[list[_Row]] = []
    for row in rows:
        if groups and row.appeared - groups[-1][0].appeared <= SIMULTANEOUS:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


_WAIT = object()


def _choose(n, eligible, prev_appear, prev_unit, later_pending, now_local, last):
    """Які одиниці віддати групі з `n` неприв'язаних рядків (див. докстрінг
    модуля). `_WAIT` — рано вирішувати, спробувати пізніше."""
    eligible = sorted(eligible, key=lambda u: u.created_at)
    if prev_appear is None:
        gap, older = eligible, []
    else:
        gap = [u for u in eligible if u.created_at > prev_appear + BIND_SLACK]
        older = [
            u for u in eligible
            if u.created_at <= prev_appear + BIND_SLACK
            and prev_unit is not None
            and u.created_at > prev_unit
        ]
    if len(gap) == n:
        return gap
    if len(gap) > n:
        if later_pending:
            return gap[:n]
        if now_local < last + DEFER:
            return _WAIT
        return gap[-n:]
    if n == 1 and not gap and older:
        return older[:1]
    return []


_SUM3D_TIME = re.compile(r"(\d{1,2})-(\d{2})-(\d{2})")


def _sum3d_moments(sum3d_id: str | None, near: datetime) -> list[datetime]:
    """Sum3D ID — лише час доби; дата — та, де цей час найближчий до появи
    рядка (прорахунок буває і до, і після неї, але не далі ніж пів доби)."""
    moments = []
    for h, m, s in _SUM3D_TIME.findall(sum3d_id or ""):
        try:
            clock = time(int(h), int(m), int(s))
        except ValueError:
            continue
        options = [
            datetime.combine(near.date() + timedelta(days=shift), clock) for shift in (-1, 0, 1)
        ]
        moments.append(min(options, key=lambda moment: abs(moment - near)))
    return moments


def _strong_pairs(rows: list[_Row], units: list[Unit], taken: set[str]) -> dict[int, Unit]:
    """Ступінь 1: тека, що лягла за секунди до Sum3D рядка, — його, якщо ні
    рядок, ні тека не мають іншої такої пари (див. `STRONG_BEFORE`).

    Бойовий випадок Неда 24.09.26: рядок 66 з'явився о 13:36, раніше за будь-які
    файли, а рядок 67 — о 13:43. Хронологія віддала б корінь (13:37:37) рядку
    66, але Sum3D кажуть інше: корінь ↔ `13-37-41` (рядок 67), підтека
    13:42:54 ↔ `13-42-58` (рядок 66). `rows` — усі рядки клієнта з Sum3D,
    не лише очікуючі: тека, схожа на ДВА рядки, не дістається жодному."""
    row_units: dict[int, list[Unit]] = {}
    unit_rows: dict[str, set[int]] = {}
    for row in rows:
        for moment in _sum3d_moments(row.order.sum3d_id, row.appeared):
            for unit in units:
                if (
                    rel_key(unit.rel) not in taken
                    and materials_match(row.order.material_color, unit.material)
                    and moment - STRONG_BEFORE <= unit.created_at <= moment + STRONG_AFTER
                ):
                    row_units.setdefault(row.order.id, [])
                    if unit not in row_units[row.order.id]:
                        row_units[row.order.id].append(unit)
                    unit_rows.setdefault(rel_key(unit.rel), set()).add(row.order.id)
    return {
        order_id: found[0]
        for order_id, found in row_units.items()
        if len(found) == 1 and unit_rows[rel_key(found[0].rel)] == {order_id}
    }


def _due(order_id: int, now_local: datetime) -> bool:
    with _state_lock:
        when = _next_try.get(order_id)
    return when is None or when <= now_local


def _retry_later(order_id: int, now_local: datetime) -> None:
    with _state_lock:
        tries = _attempts.get(order_id, 0)
        _attempts[order_id] = tries + 1
        step = _RETRY_MINUTES[min(tries, len(_RETRY_MINUTES) - 1)]
        _next_try[order_id] = now_local + timedelta(minutes=step)


def _forget(order_id: int) -> None:
    with _state_lock:
        _next_try.pop(order_id, None)
        _attempts.pop(order_id, None)


def _prune(keep: set[int]) -> None:
    """Забути все про роботи, яких уже немає серед очікуючих."""
    with _state_lock:
        for order_id in [oid for oid in _next_try if oid not in keep]:
            _next_try.pop(order_id, None)
            _attempts.pop(order_id, None)


def _pending(db: Session, now_utc: datetime) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .where(
                Order.source == "sheet_client",
                Order.export_folder_path.is_(None),
                Order.archived_at.is_(None),
                Order.client_name.is_not(None),
                Order.client_name != "",
                Order.material_color.is_not(None),
                Order.material_color != "",
                Order.created_at >= now_utc - ATTEMPT_WINDOW,
            )
            .order_by(Order.created_at, Order.id)
        )
    )


def _history(db: Session, names: set[str], since_utc: datetime) -> list[Order]:
    """Усі рядки тих самих клієнтів за добу до найстаршого очікуючого —
    прив'язані й ні: з них видно, де в хронології стоїть «попередній рядок»."""
    return list(
        db.scalars(
            select(Order).where(
                Order.source.in_(("sheet_client", "email")),
                Order.archived_at.is_(None),
                Order.client_name.in_(names),
                Order.material_color.is_not(None),
                Order.created_at >= since_utc,
            )
        )
    )


def _taken(db: Session, now_utc: datetime) -> set[str]:
    """Шляхи, уже закріплені за роботами. Тиждень з запасом: кандидати молодші
    за добу до рядка, а рядок — за шість годин до проходу."""
    return {
        rel_key(path)
        for path in db.scalars(
            select(Order.export_folder_path).where(
                Order.export_folder_path.is_not(None),
                Order.created_at >= now_utc - timedelta(days=7),
            )
        )
        if path
    }


def bind_pending(
    db: Session,
    export_root: Path,
    *,
    now_utc: datetime | None = None,
    scan=scan_export_client,
    client_folders=None,
) -> list[tuple[int, str]]:
    """Один прохід: прив'язати свіжі клієнтські рядки до їхніх тек.

    Повертає [(order_id, шлях)] прив'язаних; комітить сам. `scan` і
    `client_folders` — точки підміни для тестів (справжній обхід шари й
    зіставлення імен)."""
    now_utc = now_utc or utc_now()
    pending = _pending(db, now_utc)
    pending_ids = {o.id for o in pending}
    # Пам'ять про невдалі спроби — на ПРОЦЕС, а процес живе місяцями. Рядок,
    # що вийшов із вікна спроб (прив'язався або постарів за ATTEMPT_WINDOW),
    # більше ніколи сюди не повернеться, тож його запис у `_next_try` лежав
    # би до перезапуску: ~60 клієнтських рядків на день, з них невдалі —
    # назавжди. Чистимо тут, ДО раннього виходу, інакше тихі години (коли
    # черга порожня) нічого не прибирали б.
    _prune(pending_ids)
    if not pending:
        return []
    now_local = utc_to_business(now_utc)
    names = {o.client_name for o in pending if o.client_name}
    oldest = min(o.created_at for o in pending)
    history = _history(db, names, oldest - CANDIDATE_WINDOW - SIMULTANEOUS)
    by_id = {o.id: o for o in history}
    for order in pending:
        by_id.setdefault(order.id, order)

    if client_folders is None:
        from app.services.handout import handout_client_matches

        folder_names = list_export_client_names_cached(export_root)
        matches = handout_client_matches(db, names, folder_names)
        client_folders = {
            name: m.matched_folder_name
            for name, m in matches.items()
            if m is not None and m.matched_folder_name
        }

    by_folder: dict[str, list[_Row]] = {}
    for order in by_id.values():
        folder = client_folders.get(order.client_name)
        if folder:
            by_folder.setdefault(folder, []).append(
                _Row(order, utc_to_business(order.created_at), _family(order))
            )

    taken = _taken(db, now_utc)
    bound: list[tuple[int, str]] = []
    for folder, rows in by_folder.items():
        waiting = [r for r in rows if r.order.id in pending_ids]
        if not any(_due(r.order.id, now_local) for r in waiting):
            continue
        earliest = min(r.appeared for r in waiting)
        try:
            entries = scan(export_root, folder, earliest - SCAN_LOOKBACK)
        except OSError:
            logger.warning("Прив'язка тек: не вдалось прочитати теку клієнта %r", folder)
            continue
        units = [u for e in entries for u in units_of(e)]
        unit_time = {rel_key(u.rel): u.created_at for u in units}

        def settled(row: _Row) -> bool:
            """Чи можна вже вирішувати за цей рядок: минула поправка на
            годинник, Sum3D є (або чекали на нього досить) і вікно «тека за
            секунди до Sum3D» закрилось — інакше пара ще може з'явитись."""
            if now_local < row.appeared + BIND_SLACK:
                return False
            moments = _sum3d_moments(row.order.sum3d_id, row.appeared)
            if not moments:
                return now_local >= row.appeared + NO_SUM3D_WAIT
            return now_local >= max(moments) + STRONG_AFTER

        strong = _strong_pairs([r for r in rows if r.order.sum3d_id], units, taken)
        for row in waiting:
            unit = strong.get(row.order.id)
            if unit is None or row.order.export_folder_path or not settled(row):
                continue
            row.order.export_folder_path = unit.rel
            taken.add(rel_key(unit.rel))
            bound.append((row.order.id, unit.rel))
            _forget(row.order.id)

        for family in sorted({r.family for r in waiting}):
            family_rows = sorted(
                (r for r in rows if r.family == family),
                key=lambda r: (r.appeared, r.order.sheet_tab or "", r.order.row_number or 0, r.order.id),
            )
            clusters = _clusters(family_rows)
            prev_appear: datetime | None = None
            prev_unit: datetime | None = None
            for index, cluster in enumerate(clusters):
                need = [r for r in cluster if r.order.id in pending_ids and not r.order.export_folder_path]
                first = min(r.appeared for r in cluster)
                last = max(r.appeared for r in cluster)
                if need:
                    if not all(settled(r) for r in need):
                        break
                    material = need[0].order.material_color
                    eligible = [
                        u for u in units
                        if rel_key(u.rel) not in taken
                        and materials_match(material, u.material)
                        and first - CANDIDATE_WINDOW <= u.created_at <= last + BIND_SLACK
                    ]
                    later_pending = any(
                        r.order.id in pending_ids for c in clusters[index + 1:] for r in c
                    )
                    picked = _choose(
                        len(need), eligible, prev_appear, prev_unit,
                        later_pending, now_local, last,
                    )
                    if picked is _WAIT:
                        break
                    if len(picked) == len(need):
                        need.sort(key=lambda r: (r.order.sheet_tab or "", r.order.row_number or 0, r.order.id))
                        for row, unit in zip(need, picked):
                            row.order.export_folder_path = unit.rel
                            taken.add(rel_key(unit.rel))
                            bound.append((row.order.id, unit.rel))
                            _forget(row.order.id)
                    else:
                        for row in need:
                            _retry_later(row.order.id, now_local)
                prev_appear = last
                times = [
                    unit_time[key]
                    for r in cluster
                    if (key := rel_key(r.order.export_folder_path)) in unit_time
                ]
                prev_unit = max(times) if times else None
    if bound:
        db.commit()
        for order_id, rel in bound:
            logger.info("Прив'язка тек: робота %s → %s", order_id, rel)
    return bound
