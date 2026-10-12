"""«Статистика»: SQL-агрегати з фільтром періоду (STATS_BRIEF.md §7).

Правило: роботи в Python НЕ вантажимо. Кожен запит групує в SQL за вкладкою
(`sheet_tab`, індекс `ix_orders_sheet_tab`) і віддає десятки рядків, а не
тисячі; бічна панель — окремий легкий запит на один день / клітинку.

День роботи — день її вкладки (`parse_sheet_tab` → `tab_day`: п'ятниця несе
й вихідні). Робота без вкладки (старі листи до появи рядка) — робоча дата її
`created_at` (UTC → Київ), як у `order_dates.order_date_of`. Для цього в
запиті є стовпчик «created лише для робіт без датованої вкладки», і саме ці
рідкі рядки групуються поштучно.

Одиниці — `quantity`, що є ЦІЛИМ числом (лише цифри). Інше («2+1», «?»,
порожньо) не підсумовується, а рахується окремо як «без кількості»
(рішення власника 12.10.26).

Кеш — 60 с за параметрами: екран відкривають кілька разів поспіль (період,
фільтри, назад), а дані змінюються синком раз на хвилини.
"""

from __future__ import annotations

import threading
import time as _clock
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import Integer, and_, case, cast, false, func, not_, or_, select
from sqlalchemy.orm import Session

from app import stats as st
from app.business_day import business_date_of, business_to_utc, get_rollover, tab_day
from app.models import Material, Order, ReworkRecord
from app.services.order_dates import order_date_of, parse_sheet_tab

CACHE_TTL_SECONDS = 60.0
_CACHE_MAX = 256
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

# Більше рядків бічна панель не показує: це список для очей, а не вивантаження.
LIST_LIMIT = 2000

MATERIAL_GROUPS = ("zr", "pmma", "ti", "oth")
MATERIAL_GROUP_LABELS = {"zr": "Цирконій", "pmma": "ПММА", "ti": "Титан", "oth": "Інше"}
_MATERIAL_GROUP_BY_NAME = {"Цирконій": "zr", "ПММА": "pmma", "Титан": "ti"}

BLAME_ORDER = ("обладнання", "технік", "адміністратор", "клієнт")


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(key: tuple, build: Callable[[], Any]) -> Any:
    now = _clock.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    value = build()
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            for old in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 4]:
                _cache.pop(old, None)
        _cache[key] = (now, value)
    return value


# ── Вирази ─────────────────────────────────────────────────────────────────
def units_expr(column):
    """Ціле число з клітинки або NULL. `GLOB '*[^0-9]*'` відсікає все, крім
    цифр: CAST('2+1') у SQLite дав би 2 — тихо неправильне число."""
    text = func.trim(column)
    return case(
        (and_(text != "", not_(text.op("GLOB")("*[^0-9]*"))), cast(text, Integer)),
        else_=None,
    )


KIND = case((Order.source == "lab", "lab"), else_="client")


def _sum3d_parts():
    raw = func.trim(Order.sum3d_id)
    two = raw.op("GLOB")("[0-9][0-9]-[0-9][0-9]-[0-9][0-9]*")
    one = raw.op("GLOB")("[0-9]-[0-9][0-9]-[0-9][0-9]*")
    hour = case(
        (two, cast(func.substr(raw, 1, 2), Integer)),
        (one, cast(func.substr(raw, 1, 1), Integer)),
        else_=None,
    )
    minute = case(
        (two, cast(func.substr(raw, 4, 2), Integer)),
        (one, cast(func.substr(raw, 3, 2), Integer)),
        else_=None,
    )
    return hour, minute


def _rollover_offset() -> str:
    roll = get_rollover()
    return f"-{roll.hour * 60 + roll.minute} minutes"


# ── Фільтри ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Filters:
    src: str = ""  # "" | "lab" | "client"
    mat: str = ""  # "" | "<id матеріалу>" | "none"
    tech: str = ""  # ключ групи техніка (`st.tech_key`)
    client: str = ""  # ключ клієнта (`st.client_key`)

    @classmethod
    def parse(cls, src=None, mat=None, tech=None, client=None) -> Filters:
        return cls(
            src=src if src in ("lab", "client") else "",
            mat=mat if (mat == "none" or (mat or "").isdigit()) else "",
            tech=(tech or "").strip(),
            client=(client or "").strip(),
        )

    @property
    def active(self) -> bool:
        return bool(self.src or self.mat or self.tech or self.client)


def tech_groups(db: Session) -> dict[str, dict]:
    """Техніки за всю історію (лаба), зведені за `st.tech_key`."""

    def build():
        rows = db.execute(
            select(Order.technician_name, func.count())
            .where(Order.source == "lab", Order.technician_name.is_not(None))
            .group_by(Order.technician_name)
        ).all()
        return st.group_spellings(((r[0], r[1]) for r in rows), st.tech_key)

    return _cached(("tech_groups",), build)


def client_groups(db: Session) -> dict[str, dict]:
    def build():
        rows = db.execute(
            select(Order.client_name, func.count())
            .where(Order.source != "lab", Order.client_name.is_not(None))
            .group_by(Order.client_name)
        ).all()
        return st.group_spellings(((r[0], r[1]) for r in rows), st.client_key)

    return _cached(("client_groups",), build)


def materials(db: Session) -> list[tuple[int, str, str]]:
    """(id, назва, група) каталогу — для фільтра й кольорів."""

    def build():
        rows = db.execute(
            select(Material.id, Material.name).order_by(Material.sort_order, Material.id)
        ).all()
        return [(r[0], r[1], _MATERIAL_GROUP_BY_NAME.get(r[1], "oth")) for r in rows]

    return _cached(("materials",), build)


def _material_group_map(db: Session) -> dict[int | None, str]:
    out: dict[int | None, str] = {mid: grp for mid, _, grp in materials(db)}
    out[None] = "oth"
    return out


def _filter_clauses(db: Session, f: Filters) -> list:
    out: list = []
    if f.src == "lab":
        out.append(Order.source == "lab")
    elif f.src == "client":
        out.append(Order.source != "lab")
    if f.mat == "none":
        out.append(Order.material_id.is_(None))
    elif f.mat.isdigit():
        out.append(Order.material_id == int(f.mat))
    if f.tech:
        spellings = tech_groups(db).get(f.tech, {}).get("spellings") or []
        out.append(Order.technician_name.in_(spellings))
    if f.client:
        spellings = client_groups(db).get(f.client, {}).get("spellings") or []
        out.append(Order.client_name.in_(spellings))
    return out


# ── Вкладки й межі ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TabIndex:
    dated: dict[str, date]  # сира назва вкладки → дата вкладки
    undated: tuple[str, ...]  # назви, що не є датою


def tab_index(db: Session) -> TabIndex:
    """Усі різні назви вкладок (десятки на місяць) → дата. Один DISTINCT по
    індексу; розбір назви — той самий `parse_sheet_tab`, що всюди."""

    def build():
        dated: dict[str, date] = {}
        undated: list[str] = []
        for raw in db.execute(select(Order.sheet_tab).distinct()).scalars():
            if raw is None:
                continue
            parsed = parse_sheet_tab(raw)
            if parsed is None:
                undated.append(raw)
            else:
                dated[raw] = parsed
        return TabIndex(dated, tuple(sorted(undated)))

    return _cached(("tab_index",), build)


@dataclass(frozen=True)
class Scope:
    days: frozenset[date]
    tabs: tuple[str, ...]
    where: Any
    # «created_at лише для робіт без датованої вкладки» — інакше NULL.
    created: Any


def scope_for(db: Session, days: Iterable[date]) -> Scope:
    day_set = frozenset(days)
    idx = tab_index(db)
    tabs = tuple(sorted(raw for raw, d in idx.dated.items() if tab_day(d) in day_set))
    clauses: list[Any] = []
    if tabs:
        clauses.append(Order.sheet_tab.in_(tabs))
    if day_set:
        roll = get_rollover()
        first, last = min(day_set), max(day_set)
        # П'ятниця несе вихідні: межа — понеділок 07:30.
        after = last + timedelta(days=3 if last.weekday() == 4 else 1)
        lo = business_to_utc(datetime.combine(first, roll))
        hi = business_to_utc(datetime.combine(after, roll))
        untabbed = or_(Order.sheet_tab.is_(None), Order.sheet_tab.in_(idx.undated))
        clauses.append(and_(untabbed, Order.created_at >= lo, Order.created_at < hi))
    where = or_(*clauses) if clauses else false()
    created = case((Order.sheet_tab.in_(tabs), None), else_=Order.created_at)
    return Scope(day_set, tabs, where, created)


def _raw_day(idx: TabIndex, tab: str | None, created: datetime | None) -> date | None:
    """Дата вкладки (або робоча дата створення для роботи без вкладки)."""
    if created is None:
        return idx.dated.get(tab) if tab is not None else None
    return order_date_of(None, created)


def _day_in(scope: Scope, raw: date | None) -> date | None:
    if raw is None:
        return None
    day = tab_day(raw)
    return day if day in scope.days else None


# ── Факти: день × джерело × матеріал ───────────────────────────────────────
@dataclass(frozen=True)
class Fact:
    day: date
    kind: str  # "lab" | "client"
    material_id: int | None
    works: int
    units: int
    noqty: int


def facts(db: Session, days: Iterable[date], f: Filters) -> list[Fact]:
    day_list = sorted(set(days))
    key = ("facts", tuple(day_list), f)

    def build():
        scope = scope_for(db, day_list)
        idx = tab_index(db)
        units = units_expr(Order.quantity)
        rows = db.execute(
            select(
                Order.sheet_tab,
                scope.created,
                KIND,
                Order.material_id,
                func.count(),
                func.coalesce(func.sum(units), 0),
                func.sum(case((units.is_(None), 1), else_=0)),
            )
            .where(scope.where, *_filter_clauses(db, f))
            .group_by(Order.sheet_tab, scope.created, KIND, Order.material_id)
        ).all()
        merged: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
        for tab, created, kind, mid, works, units_sum, noqty in rows:
            day = _day_in(scope, _raw_day(idx, tab, created))
            if day is None:
                continue
            acc = merged[(day, kind, mid)]
            acc[0] += int(works)
            acc[1] += int(units_sum or 0)
            acc[2] += int(noqty or 0)
        return [Fact(d, k, m, *v) for (d, k, m), v in sorted(merged.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0))]

    return _cached(key, build)


def summarize(facts_list: list[Fact], days: list[date], group_of: dict) -> dict:
    """Чиста згортка фактів: підсумки, стовпчики по днях, матеріали."""
    by_day = {d: {"lab": 0, "client": 0, "works": 0, "noqty": 0} for d in days}
    mats = dict.fromkeys(MATERIAL_GROUPS, 0)
    lab = client = works = noqty = 0
    for fact in facts_list:
        slot = by_day.get(fact.day)
        if slot is None:
            continue
        slot[fact.kind] += fact.units
        slot["works"] += fact.works
        slot["noqty"] += fact.noqty
        mats[group_of.get(fact.material_id, "oth")] += fact.units
        works += fact.works
        noqty += fact.noqty
        if fact.kind == "lab":
            lab += fact.units
        else:
            client += fact.units
    tab_days = sum(1 for v in by_day.values() if v["works"])
    units = lab + client
    return {
        "units": units,
        "lab": lab,
        "client": client,
        "works": works,
        "noqty": noqty,
        "tab_days": tab_days,
        "per_day": (units / tab_days) if tab_days else None,
        "by_day": by_day,
        "materials": mats,
    }


def coverage(db: Session, days: list[date]) -> list[bool]:
    """Чи є вкладка за кожен робочий день (з листингу назв, не з робіт)."""
    idx = tab_index(db)
    have = {tab_day(d) for d in idx.dated.values()}
    return [d in have for d in days]


# ── Клієнти, кольори, брак ─────────────────────────────────────────────────
def client_units(db: Session, days: list[date], f: Filters) -> dict[str, dict]:
    """Клієнтські роботи за період, зведені за `st.client_key`.
    Ключ "" — роботи без імені клієнта."""
    key = ("clients", tuple(days), f)

    def build():
        scope = scope_for(db, days)
        idx = tab_index(db)
        units = units_expr(Order.quantity)
        rows = db.execute(
            select(Order.sheet_tab, scope.created, Order.client_name, func.count(), func.coalesce(func.sum(units), 0))
            .where(scope.where, Order.source != "lab", *_filter_clauses(db, f))
            .group_by(Order.sheet_tab, scope.created, Order.client_name)
        ).all()
        groups: dict[str, dict] = {}
        spell: dict[str, Counter] = defaultdict(Counter)
        for tab, created, name, works, units_sum in rows:
            if _day_in(scope, _raw_day(idx, tab, created)) is None:
                continue
            ck = st.client_key(name) or ""
            g = groups.setdefault(ck, {"units": 0, "works": 0})
            g["units"] += int(units_sum or 0)
            g["works"] += int(works)
            if name:
                spell[ck][" ".join(name.split())] += int(works)
        for ck, g in groups.items():
            g["label"] = spell[ck].most_common(1)[0][0] if spell.get(ck) else "Без імені"
        return groups

    return _cached(key, build)


def color_units(db: Session, days: list[date], f: Filters) -> list[dict]:
    key = ("colors", tuple(days), f)

    def build():
        scope = scope_for(db, days)
        idx = tab_index(db)
        units = units_expr(Order.quantity)
        group_of = _material_group_map(db)
        rows = db.execute(
            select(Order.sheet_tab, scope.created, Order.material_color, Order.material_id, func.coalesce(func.sum(units), 0))
            .where(scope.where, *_filter_clauses(db, f))
            .group_by(Order.sheet_tab, scope.created, Order.material_color, Order.material_id)
        ).all()
        acc: dict[str, list] = {}
        for tab, created, color, mid, units_sum in rows:
            if _day_in(scope, _raw_day(idx, tab, created)) is None:
                continue
            name = st.norm_color(color)
            if not name:
                continue
            slot = acc.setdefault(name, [0, Counter()])
            slot[0] += int(units_sum or 0)
            slot[1][group_of.get(mid, "oth")] += int(units_sum or 0) or 1
        out = [
            {"color": name, "units": v[0], "group": v[1].most_common(1)[0][0]}
            for name, v in acc.items()
            if v[0] > 0
        ]
        out.sort(key=lambda r: (-r["units"], r["color"]))
        return out

    return _cached(key, build)


def rework_summary(db: Session, days: list[date], f: Filters) -> dict:
    """Брак за роботами періоду: одиниці переробки (`redo_quantity`) по винуватцях."""
    key = ("rework", tuple(days), f)

    def build():
        scope = scope_for(db, days)
        idx = tab_index(db)
        redo = units_expr(ReworkRecord.redo_quantity)
        rows = db.execute(
            select(Order.sheet_tab, scope.created, ReworkRecord.blame, func.count(), func.coalesce(func.sum(redo), 0))
            .join(Order, Order.id == ReworkRecord.order_id)
            .where(scope.where, *_filter_clauses(db, f))
            .group_by(Order.sheet_tab, scope.created, ReworkRecord.blame)
        ).all()
        by_blame: dict[str, list[int]] = {}
        for tab, created, blame, cases, units_sum in rows:
            if _day_in(scope, _raw_day(idx, tab, created)) is None:
                continue
            name = (blame or "").strip() or "не вказано"
            slot = by_blame.setdefault(name, [0, 0])
            slot[0] += int(cases)
            slot[1] += int(units_sum or 0)
        order = {b: i for i, b in enumerate(BLAME_ORDER)}
        blames = [
            {"blame": b, "cases": v[0], "units": v[1]}
            for b, v in sorted(by_blame.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))
        ]
        return {
            "cases": sum(b["cases"] for b in blames),
            "units": sum(b["units"] for b in blames),
            "blames": blames,
        }

    return _cached(key, build)


# ── Карти «день тижня × година» ────────────────────────────────────────────
def sum3d_heat(db: Session, days: list[date], f: Filters) -> dict:
    """Одиниці за часом Sum3D (взяття в роботу): {kind: grid, n: {kind: робіт}}.

    Дата — день вкладки (для вихідних п'ятниці — день появи рядка, якщо він
    відомий), час — з ID; Sum3D раніше межі доби — наступний календарний
    день (нічна зміна), як у видачі."""
    key = ("heat_sum3d", tuple(days), f)

    def build():
        scope = scope_for(db, days)
        idx = tab_index(db)
        units = units_expr(Order.quantity)
        hour, minute = _sum3d_parts()
        seen = func.date(Order.row_seen_at, _rollover_offset())
        rows = db.execute(
            select(Order.sheet_tab, scope.created, KIND, hour, minute, seen, func.count(), func.coalesce(func.sum(units), 0))
            .where(scope.where, hour.is_not(None), *_filter_clauses(db, f))
            .group_by(Order.sheet_tab, scope.created, KIND, hour, minute, seen)
        ).all()
        return bucket_sum3d_rows(rows, idx, scope, get_rollover())

    return _cached(key, build)


def bucket_sum3d_rows(rows, idx: TabIndex, scope: Scope, rollover: time) -> dict:
    grids = {"lab": st.empty_grid(), "client": st.empty_grid()}
    counts = {"lab": 0, "client": 0}
    for tab, created, kind, hh, mm, seen, works, units_sum in rows:
        raw = _raw_day(idx, tab, created)
        if _day_in(scope, raw) is None or raw is None:
            continue
        if hh is None or mm is None or not (0 <= hh < 24 and 0 <= mm < 60):
            continue
        seen_day = date.fromisoformat(seen) if seen else None
        work_day = st.work_day_for_sum3d(raw, seen_day)
        moment = st.sum3d_moment(work_day, time(hh, mm), rollover)
        grids[kind][moment.weekday()][moment.hour] += int(units_sum or 0)
        counts[kind] += int(works)
    return {"grids": grids, "n": counts}


def row_seen_heat(db: Session, days: list[date], f: Filters) -> dict:
    """Одиниці за часом появи рядка (`row_seen_at`, київський)."""
    key = ("heat_row", tuple(days), f)

    def build():
        scope = scope_for(db, days)
        units = units_expr(Order.quantity)
        dow = cast(func.strftime("%w", Order.row_seen_at), Integer)
        hour = cast(func.strftime("%H", Order.row_seen_at), Integer)
        rows = db.execute(
            select(KIND, dow, hour, func.count(), func.coalesce(func.sum(units), 0))
            .where(scope.where, Order.row_seen_at.is_not(None), *_filter_clauses(db, f))
            .group_by(KIND, dow, hour)
        ).all()
        grids = {"lab": st.empty_grid(), "client": st.empty_grid()}
        counts = {"lab": 0, "client": 0}
        for kind, w, h, works, units_sum in rows:
            if w is None or h is None:
                continue
            grids[kind][(int(w) + 6) % 7][int(h)] += int(units_sum or 0)
            counts[kind] += int(works)
        return {"grids": grids, "n": counts}

    return _cached(key, build)


def row_seen_days(db: Session) -> int:
    """Скільки різних робочих днів уже мають `row_seen_at` (уся історія)."""

    def build():
        value = db.execute(
            select(func.count(func.distinct(func.date(Order.row_seen_at, _rollover_offset())))).where(
                Order.row_seen_at.is_not(None)
            )
        ).scalar()
        return int(value or 0)

    return _cached(("row_seen_days",), build)


# ── Наряд → модель ─────────────────────────────────────────────────────────
def lead_minutes(db: Session, days: list[date], f: Filters) -> list[float]:
    """Хвилини від появи рядка до першого шляху — лише для робіт з обома
    моментами (з 12.10.26). Робота, що народилась зі шляхом, тут не бере
    участі: її `job_code_at` порожній."""
    key = ("lead", tuple(days), f)

    def build():
        scope = scope_for(db, days)
        diff = (func.julianday(Order.job_code_at) - func.julianday(Order.row_seen_at)) * 1440.0
        rows = db.execute(
            select(diff).where(
                scope.where,
                Order.row_seen_at.is_not(None),
                Order.job_code_at.is_not(None),
                Order.job_code_at >= Order.row_seen_at,
                *_filter_clauses(db, f),
            )
        ).scalars()
        return [float(v) for v in rows if v is not None]

    return _cached(key, build)


def has_job_code_moments(db: Session) -> bool:
    def build():
        return db.execute(select(Order.id).where(Order.job_code_at.is_not(None)).limit(1)).first() is not None

    return _cached(("has_job_code_at",), build)


# ── Бічна панель: список робіт ─────────────────────────────────────────────
def works_list(
    db: Session,
    days: list[date],
    f: Filters,
    *,
    kind: str | None = None,
    noqty: bool = False,
    rework: bool = False,
    cell: tuple[int, int] | None = None,
    heat_mode: str = "sum3d",
) -> list[dict]:
    """Роботи для панелі: день, клітинка карти, «без кількості» чи брак.

    Легкий запит без кешу: лише вузькі колонки робіт ОДНОГО відбору."""
    scope = scope_for(db, days)
    idx = tab_index(db)
    units = units_expr(Order.quantity)
    clauses = [scope.where, *_filter_clauses(db, f)]
    if kind == "lab":
        clauses.append(Order.source == "lab")
    elif kind == "client":
        clauses.append(Order.source != "lab")
    if noqty:
        clauses.append(units.is_(None))
    if rework:
        clauses.append(Order.id.in_(select(ReworkRecord.order_id)))
    if cell is not None and heat_mode == "row":
        clauses.append(Order.row_seen_at.is_not(None))
    elif cell is not None:
        clauses.append(_sum3d_parts()[0].is_not(None))
    rows = db.execute(
        select(
            Order.id,
            Order.sheet_tab,
            scope.created,
            Order.row_number,
            Order.work_order_no,
            Order.source,
            Order.technician_name,
            Order.client_name,
            Order.material_id,
            Order.material_color,
            units,
            Order.quantity,
            Order.sum3d_id,
            Order.row_seen_at,
        ).where(*clauses)
    ).all()
    group_of = _material_group_map(db)
    rollover = get_rollover()
    out = []
    for r in rows:
        raw = _raw_day(idx, r[1], r[2])
        day = _day_in(scope, raw)
        if day is None or raw is None:
            continue
        taken = st.first_sum3d_time(r[12])
        if cell is not None:
            if heat_mode == "row":
                seen = r[13]
                bucket = (seen.weekday(), seen.hour) if seen else None
            else:
                seen_day = business_date_of(r[13]) if r[13] else None
                bucket = None
                if taken is not None:
                    moment = st.sum3d_moment(st.work_day_for_sum3d(raw, seen_day), taken, rollover)
                    bucket = (moment.weekday(), moment.hour)
            if bucket != cell:
                continue
        out.append(
            {
                "id": r[0],
                "tab": r[1] or "",
                "tab_date": raw,
                "day": day,
                "row": r[3],
                "wo": r[4],
                "kind": "lab" if r[5] == "lab" else "client",
                "tech": r[6],
                "client": r[7],
                "group": group_of.get(r[8], "oth"),
                "color": r[9],
                "units": r[10],
                "qty_raw": r[11],
                "sum3d": taken.strftime("%H-%M-%S") if taken else None,
            }
        )
    out.sort(key=lambda w: (w["tab_date"], w["row"] if w["row"] is not None else 10**6, w["id"]))
    if rework and out:
        details: dict[int, list[str]] = defaultdict(list)
        for oid, blame, redo in db.execute(
            select(ReworkRecord.order_id, ReworkRecord.blame, ReworkRecord.redo_quantity).where(
                ReworkRecord.order_id.in_([w["id"] for w in out])
            )
        ).all():
            details[oid].append(f"{(blame or 'не вказано').strip()} · {(redo or '?').strip()} од.")
        for w in out:
            w["rework"] = "; ".join(details.get(w["id"], []))
    return out[:LIST_LIMIT]
