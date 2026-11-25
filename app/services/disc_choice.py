"""Якою логікою оператори беруть диск — і порядок підказки за нею.

Вчимось на двох джерелах:

* **журнали Sum3D** (`disc_history`) — роки: який проєкт на якому диску. Чи було
  на дисках місце, вони не знають;
* **живі вибори** (`disc_watch`) — кожен новий проєкт разом з усіма дисками, які
  лежали поруч ТОДІ, і тим, скільки на кожному було вільно. Точні, але ростуть
  з дня встановлення.

Питань два, і відповіді окремі, бо доказів на них різна кількість:

1. **Яку висоту.** Товщину роботи знає CRM (`Order.stl_height_mm`), висоту диска —
   його назва: як часто беруть найнижчий, що накриває, і як часто вищий.
2. **Який диск цієї висоти.** Обраний проти всіх інших, за ознаками `FEATURES`.
   Ваги — умовний логіт «драбинкою»: цех → лінія → оператор. Мало доказів на
   лінію чи людину — її ваги лишаються біля спільних, самі собою, без порогів.
   Свіжі вибори важать більше (`HALF_LIFE_DAYS`): звичка змінилась — модель теж.

Перевірка — кілька іспитів поспіль: навчання на минулому, іспит на наступному
відрізку, якого воно не бачило. Якщо на лінії проста звичка («завжди останній
різаний») точніша за ваги, береться вона. Впевненість у картці — не вигадана
формула, а частка влучань на іспитах при такій самій оцінці.

Останнє слово за живими листами (`selection_journal.rivalry`): кожна підказка має
мовчазну пару іншого порядку, а правду дописує програма верстата. Адмін вмикає
порядок сам або один раз дозволяє «Автоматично» — тоді він вмикається, лише коли
доведено кращий, і вимикається, щойно програє.

`mine` важкий — ЛИШЕ окремим процесом (`mcp_isolated`). Імен людей тут немає:
оператор — номер користувача.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import decisions_store as store
from app.material_classifier import PMMA
from app.models import ActionLog, Order, SelectionRecord
from app.services import decision_rules as rules
from app.services import decisions as d
from app.services import disc_history
from app.services.disc_history import DiscUse, Job, Shelf, days_between
from app.services.disc_suggest import (
    LINES, MARGIN_MM, STEP_UP_WITHIN_MM, Chooser, DiscFile, Ranking, Want, describe, target_heights, want_of,
)

logger = logging.getLogger(__name__)

DAYS = 183
"""Півроку (власник 25.11.26): давніші звички цеху вже не ті."""
ALIVE_DAYS = 90
"""Диск, якого стільки днів не брали, у тодішній вибір не входить."""
HEIGHT_DAYS = 182
"""Висота, якої стільки днів не брали в групі, у виборі висоти не стоїть."""
HALF_LIFE_DAYS = 120
"""За стільки днів вибір важить удвічі менше."""
FOLDS = (0.6, 0.7, 0.8, 0.9)
"""Іспити: навчання на перших 60/70/80/90 %, іспит на наступних 10 %."""
MAX_STEP = 4
MANY_UNITS = 4
PAUSE_MIN = 20
PAUSE_GAP = 3
AUTO_MIN = 30
AUTO_Z = 1.64

KEY = "disc:order"
STATUS_AUTO = rules.STATUS_AUTO
RULE: dict[str, Any] = {
    "key": KEY, "kind": "disc", "title": "Порядок дисків у підказці — як беруть оператори", "params": {},
}
_META = "disc_model"
_META_AUTO = "disc_auto"

FEATURES = (
    "останній", "найновіший", "у верстаті", "сьогодні", "давність", "вирізано",
    "багато×найновіший", "багато×вирізано", "ніч×останній", "ніч×у верстаті",
    "вільно", "найповніший з місцем", "багато×вільно",
)
K = len(FEATURES)
_FREE, _FULLEST = 10, 11
ALL = "усі"
OTHER = "інше"

Group = tuple


def group_of(disc: DiscFile) -> Optional[Group]:
    if not disc.height or not disc.family or not (disc.shades or disc.code or disc.family == PMMA):
        return None
    return (disc.family, disc.line, tuple(sorted(disc.shades)), disc.code, disc.hipc)


def bucket_of(family: Optional[str], line: Optional[str]) -> str:
    if line:
        return LINES[line][0]
    return "ПММА" if family == PMMA else OTHER


# ── Ознаки ─────────────────────────────────────────────────────────────────


def features(uses: dict[str, DiscUse], mounted: set[str], now: datetime, *, units: int = 0,
             free: Optional[dict[str, float]] = None, need: float = 0.0) -> dict[str, list[float]]:
    """Ознаки дисків ОДНІЄЇ групи й висоти. Порядок — як у `FEATURES`.

    `free` — вільна частка дисків, де її знаємо; `need` — скільки треба роботі."""
    if not uses:
        return {}
    free = free or {}
    latest = max(uses, key=lambda n: (uses[n].last, n))
    newest = max(uses, key=lambda n: (uses[n].first, n))
    roomy = [n for n in uses if n in free and free[n] >= need]
    fullest = min(roomy, key=lambda n: (free[n], n)) if len(roomy) > 1 else None
    many = float(units >= MANY_UNITS)
    night = float(now.hour >= 20 or now.hour < 8)
    out = {}
    for name, use in uses.items():
        days = days_between(use.last, now)
        last, new, on, cut = float(name == latest), float(name == newest), float(name in mounted), math.log1p(use.items)
        space = free.get(name, 0.0)
        out[name] = [
            last, new, on, float(days < 0.5), math.log1p(days), cut,
            many * new, many * cut, night * last, night * on,
            space, float(name == fullest), many * space,
        ]
    return out


# ── Висота ─────────────────────────────────────────────────────────────────


def height_steps(thickness: float, available: Iterable[int]) -> tuple[str, dict[int, int]]:
    """(скільки запасу на найнижчому диску, висота → номер кроку)."""
    covering = sorted(h for h in set(available) if h >= thickness)
    if not covering:
        return "", {}
    slack = covering[0] - thickness
    base = "впритул" if slack < MARGIN_MM else "тісно" if slack < STEP_UP_WITHIN_MM else "вільно"
    return base, {h: min(i, MAX_STEP) for i, h in enumerate(covering)}


# Поки доказів немає: найнижчий, що накриває, далі що вище, то рідше.
_STEP_PRIOR = (0.6, 0.25, 0.1, 0.03, 0.02)


def _height_share(table: dict[str, Any], bucket: str, base: str, step: int) -> float:
    """Частка «взяли цей крок, коли він був»; мало доказів — тягнемо до спільної."""
    def cell(name: str) -> tuple[float, float]:
        row = ((table.get(name) or {}).get(base) or {}).get(str(step)) or [0, 0]
        return float(row[0]), float(row[1])

    took_all, had_all = cell(ALL)
    shared = (took_all + 2.0 * _STEP_PRIOR[min(step, MAX_STEP)]) / (had_all + 2.0)
    took, had = cell(bucket)
    return (took + 4.0 * shared) / (had + 4.0)


# ── Події: хто з чого вибрав ───────────────────────────────────────────────


@dataclass
class Event:
    at: datetime
    bucket: str
    names: list[str]
    heights: list[int]
    x: np.ndarray
    chosen: int
    """Номер обраного в `names`; −1 — узяли новий диск."""
    feasible: np.ndarray
    """Чи був диск придатний: живий вибір — за виміряним місцем; журнал — чи
    диск ще брали потім (наближення)."""
    thickness: Optional[float]
    available: list[int]
    height: int
    """Висота взятого диска (і для нового)."""
    want: Want
    operator: Optional[int] = None
    live: bool = False
    units: int = 0


@dataclass
class Known:
    """Що CRM знає про проєкт за часом Sum3D: товщина роботи й хто рахував."""

    thickness: dict[str, list[tuple[datetime, float, Optional[str]]]] = field(default_factory=dict)
    operators: dict[str, list[tuple[datetime, int]]] = field(default_factory=dict)


_TAIL_RE = re.compile(r"(?<!\d)\d{2}-\d{2}-\d{2}(?!\d)")


def known_orders(db: Session, since: datetime) -> Known:
    from app.services.stl_height import thickness_mm

    by_order: dict[int, int] = {}
    for order_id, operator in db.execute(
        select(ActionLog.order_id, ActionLog.operator_id).where(
            ActionLog.action_type == "sum3d", ActionLog.undone_at.is_(None),
            ActionLog.created_at >= since, ActionLog.operator_id.is_not(None),
        ).order_by(ActionLog.id)
    ):
        if order_id is not None and operator is not None:
            by_order[order_id] = operator
    for order_id, user in db.execute(
        select(SelectionRecord.order_id, SelectionRecord.user_id).where(
            SelectionRecord.created_at >= since, SelectionRecord.user_id.is_not(None))
    ):
        if order_id is not None and user is not None:
            by_order.setdefault(order_id, user)
    out = Known(defaultdict(list), defaultdict(list))
    for order in db.scalars(select(Order).where(Order.sum3d_id.is_not(None), Order.created_at >= since)):
        family = want_of(order.material_color).family
        value = thickness_mm(order.stl_height_mm, family) if family and order.stl_height_mm else None
        for tail in _TAIL_RE.findall(order.sum3d_id or ""):
            if value:
                out.thickness[tail].append((order.created_at, value, family))
            if order.id in by_order:
                out.operators[tail].append((order.created_at, by_order[order.id]))
    return out


def _near(created: Optional[datetime], at: datetime) -> bool:
    """Sum3D ID — лише час доби, тож ще й день поруч."""
    return created is not None and abs(created - at) <= timedelta(days=4)


def _thickness_for(sum3d: str, at: datetime, family: Optional[str], known: Known) -> Optional[float]:
    found = [v for created, v, fam in known.thickness.get(sum3d, ()) if fam == family and _near(created, at)]
    return max(found) if found else None


def _operator_for(sum3d: str, at: datetime, known: Known) -> Optional[int]:
    found = {user for created, user in known.operators.get(sum3d, ()) if _near(created, at)}
    return found.pop() if len(found) == 1 else None


def _matrix(names: list[str], heights: list[int], uses: dict[str, DiscUse], mounted: set[str], at: datetime,
            units: int, free: Optional[dict[str, float]] = None, need: float = 0.0) -> np.ndarray:
    vectors: dict[str, list[float]] = {}
    for h in set(heights):
        vectors.update(features({n: uses[n] for n, nh in zip(names, heights) if nh == h},
                                mounted, at, units=units, free=free, need=need))
    return np.array([vectors[n] for n in names], dtype=float).reshape(len(names), K)


def events_of(jobs: list[Job], known: Known, *, since: datetime, skip: Iterable[str] = ()) -> list[Event]:
    """Вибори з журналів Sum3D. `skip` — проєкти, що вже є живими виборами."""
    chosen_jobs = disc_history.choices(jobs)
    last_ever = {job.blank: job.at for job in chosen_jobs}
    passed = set(skip)
    parsed: dict[str, tuple[DiscFile, Optional[Group]]] = {}
    members: dict[Group, list[str]] = defaultdict(list)
    height_used: dict[Group, dict[int, datetime]] = defaultdict(dict)
    shelf = Shelf()
    out: list[Event] = []
    for job in chosen_jobs:
        if job.blank not in parsed:
            disc = describe(job.blank, Path(job.blank))
            parsed[job.blank] = (disc, group_of(disc))
        disc, group = parsed[job.blank]
        if group is not None and disc.height is not None:
            if job.at >= since and job.project not in passed:
                low = job.at - timedelta(days=ALIVE_DAYS)
                names = [n for n in members[group] if shelf.discs[n].last >= low]
                heights = [parsed[n][0].height or 0 for n in names]
                used = [h for h, at in height_used[group].items() if at >= job.at - timedelta(days=HEIGHT_DAYS)]
                out.append(Event(
                    at=job.at, bucket=bucket_of(disc.family, disc.line), names=names, heights=heights,
                    x=_matrix(names, heights, shelf.discs, set(shelf.mounted.values()), job.at, job.items),
                    chosen=names.index(job.blank) if job.blank in names else -1,
                    feasible=np.array([last_ever[n] >= job.at for n in names], dtype=bool),
                    thickness=_thickness_for(job.sum3d, job.at, disc.family, known),
                    available=sorted({*used, disc.height}), height=disc.height,
                    want=Want(disc.family, disc.line, disc.shades, disc.code, disc.hipc),
                    operator=_operator_for(job.sum3d, job.at, known), units=job.items,
                ))
            if job.blank not in shelf.discs:
                members[group].append(job.blank)
            height_used[group][disc.height] = job.at
        shelf.add(job)
    return out


# ── Умовний логіт ──────────────────────────────────────────────────────────

Choice = tuple[np.ndarray, int, float]
"""(ознаки сусідів, номер обраного, вага)."""


def fit_weights(found: list[Choice], *, prior: Optional[np.ndarray] = None, ridge: float = 1.0,
                steps: int = 25) -> np.ndarray:
    """Ваги ознак, за яких обраний диск найімовірніший серед сусідів (Ньютон).
    `prior` — ваги ширшої групи: без доказів лишаються вони."""
    start = np.zeros(K) if prior is None else prior
    if not found:
        return start
    x = np.vstack([c[0] for c in found])
    sizes = np.array([len(c[0]) for c in found])
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    weight = np.array([c[2] for c in found])
    segment = np.repeat(np.arange(len(sizes)), sizes)
    picked = (x[starts + np.array([c[1] for c in found])] * weight[:, None]).sum(axis=0)
    w = start.copy()
    for _ in range(steps):
        points = x @ w
        peak = np.maximum.reduceat(points, starts)
        e = np.exp(points - peak[segment])
        p = e / np.add.reduceat(e, starts)[segment]
        mean = np.add.reduceat(x * p[:, None], starts, axis=0)
        gradient = picked - (mean * weight[:, None]).sum(axis=0) - ridge * (w - start)
        hessian = ((x * (p * weight[segment])[:, None]).T @ x
                   - (mean * weight[:, None]).T @ mean + ridge * np.eye(K))
        step = np.linalg.solve(hessian, gradient)
        w = w + step
        if float(np.abs(step).max()) < 1e-6:
            break
    return w


def _same_height(event: Event, strict: bool) -> Optional[tuple[np.ndarray, int]]:
    """Вибір серед дисків тієї самої висоти. `strict` — лише придатні."""
    if event.chosen < 0:
        return None
    rows = [i for i, h in enumerate(event.heights)
            if h == event.height and (not strict or event.feasible[i] or i == event.chosen)]
    if len(rows) < 2:
        return None
    return event.x[rows], rows.index(event.chosen)


def _weight_key(bucket: str, operator: Optional[int]) -> str:
    return f"{bucket}|{operator}" if operator is not None else bucket


def _train(events: list[Event], now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or (events[-1].at if events else datetime.now())
    found: dict[str, list[Choice]] = defaultdict(list)
    table: dict[str, dict[str, dict[str, list[float]]]] = {}
    for event in events:
        weight = 0.5 ** (days_between(event.at, now) / HALF_LIFE_DAYS)
        same = _same_height(event, strict=True)
        if same is not None:
            choice = (same[0], same[1], weight)
            found[ALL].append(choice)
            found[event.bucket].append(choice)
            if event.operator is not None:
                found[_weight_key(event.bucket, event.operator)].append(choice)
        if event.thickness is None or event.height < event.thickness:
            continue
        base, steps = height_steps(event.thickness, event.available)
        took = steps.get(event.height)
        for name in (ALL, event.bucket):
            row = table.setdefault(name, {}).setdefault(base, {})
            for step in set(steps.values()):
                cell = row.setdefault(str(step), [0.0, 0.0])
                cell[1] = round(cell[1] + weight, 4)
                cell[0] = round(cell[0] + weight * (step == took), 4)
    weights: dict[str, np.ndarray] = {}
    if found:
        weights[ALL] = fit_weights(found[ALL])
        for key in sorted(found, key=lambda k: k.count("|")):
            if key == ALL:
                continue
            bucket, _, operator = key.partition("|")
            parent = weights[bucket] if operator else weights[ALL]
            weights[key] = fit_weights(found[key], prior=parent, ridge=8.0 if operator else 4.0)
    return {"weights": {k: [round(float(v), 4) for v in w] for k, w in weights.items()},
            "heights": table, "choices": {k: len(v) for k, v in found.items()}}


def _weights(model: dict[str, Any], bucket: str, operator: Optional[int] = None) -> np.ndarray:
    table = model["weights"]
    found = (table.get(_weight_key(bucket, operator)) if operator is not None else None) or \
        table.get(bucket) or table.get(ALL) or [0.0] * K
    out = np.zeros(K)
    out[: len(found)] = found[:K]
    return out


_HABITS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "останній, з якого різали": lambda x: -x[:, 4],
    "найновіший": lambda x: x[:, 1],
    "найповніший": lambda x: x[:, 5],
    "найпорожніший": lambda x: -x[:, 5],
    "у верстаті, далі останній": lambda x: x[:, 2] * 1000 - x[:, 4],
    "у верстаті, далі найповніший": lambda x: x[:, 2] * 1000 + x[:, 5],
}
LEARNED = "вивчений порядок"


def within(model: dict[str, Any], bucket: str, operator: Optional[int], x: np.ndarray) -> np.ndarray:
    """Оцінка дисків однієї висоти: ваги або, де вона точніша, проста звичка."""
    habit = (model.get("use") or {}).get(bucket)
    raw = x @ _weights(model, bucket, operator)
    if habit in _HABITS:
        # Порядок — звички, масштаб — ваг: інакше висота й диск не порівнянні.
        order = np.argsort(np.argsort(_HABITS[habit](x), kind="stable"), kind="stable")
        return order - (len(order) - 1.0) + 0.001 * raw
    return raw


def score(model: dict[str, Any], bucket: str, thickness: Optional[float], available: Iterable[int],
          heights: list[int], x: np.ndarray, operator: Optional[int] = None) -> np.ndarray:
    """Оцінка кожного диска: найкращий своєї висоти несе ймовірність висоти, решта нижче."""
    if not len(heights):
        return np.zeros(0)
    out = np.zeros(len(heights))
    base, steps = height_steps(thickness, available) if thickness is not None else ("", {})
    for h in set(heights):
        rows = np.array([i for i, other in enumerate(heights) if other == h])
        raw = within(model, bucket, operator, x[rows])
        share = _height_share(model["heights"], bucket, base, steps[h]) if h in steps else 0.02
        out[rows] = raw - raw.max() + math.log(max(share, 1e-3))
    return out


# ── Іспити ─────────────────────────────────────────────────────────────────


def _rank(values: np.ndarray, chosen: int) -> int:
    """Місце обраного; однакові оцінки — за обраним (не підсуджуємо собі)."""
    return int((values >= values[chosen]).sum()) - 1


def softmax(values: np.ndarray) -> np.ndarray:
    e = np.exp(values - values.max())
    return e / e.sum()


@dataclass
class _Tally:
    n: int = 0
    first: int = 0
    top3: int = 0

    def add(self, rank: int) -> None:
        self.n += 1
        self.first += int(rank == 0)
        self.top3 += int(rank < 3)

    def view(self) -> dict[str, Any]:
        share = (lambda v: round(100 * v / self.n, 1)) if self.n else (lambda v: None)
        # Похибка частки: скільки точності дає сама кількість виборів.
        spread = round(196 * math.sqrt(self.first / self.n * (1 - self.first / self.n) / self.n), 1) if self.n else None
        return {"виборів": self.n, "перший_%": share(self.first), "у_трьох_%": share(self.top3), "±": spread}


def _rule_scores(event: Event, rows: list[int]) -> np.ndarray:
    """Порядок нинішніх правил: висота, верстат, найповніший."""
    assert event.thickness is not None
    allowed, _why = target_heights(event.thickness, event.available, event.want)
    out = []
    for i in rows:
        h = event.heights[i]
        place = allowed.index(h) if h in allowed else len(allowed) + h
        flat = event.want.family == PMMA and h in allowed
        full = 1.0 - event.x[i, _FREE] if event.live else event.x[i, 5]
        out.append(-1e6 * (0 if flat else place) + event.x[i, 2] * 1e3 + full)
    return np.array(out)


BINS = (0.0, 0.3, 0.45, 0.6, 0.75, 1.01)


@dataclass
class _Exam:
    same: dict[str, dict[str, _Tally]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(_Tally)))
    full: dict[str, _Tally] = field(default_factory=lambda: defaultdict(_Tally))
    by_operator: dict[int, _Tally] = field(default_factory=lambda: defaultdict(_Tally))
    by_month: dict[str, _Tally] = field(default_factory=lambda: defaultdict(_Tally))
    live: _Tally = field(default_factory=_Tally)
    pairs: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    """(лінія, звичка) → скільки разів влучила лише вона / лише ваги."""
    bins: dict[int, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    def run(self, model: dict[str, Any], events: list[Event]) -> None:
        for event in events:
            found = _same_height(event, strict=True)
            if found is not None:
                x, chosen = found
                values = x @ _weights(model, event.bucket, event.operator)
                rank = _rank(values, chosen)
                self.by_month[event.at.strftime("%Y-%m")].add(rank)
                if event.live:
                    self.live.add(rank)
                if event.operator is not None:
                    self.by_operator[event.operator].add(rank)
                for bucket in (ALL, event.bucket):
                    self.same[bucket][LEARNED].add(rank)
                    for name, habit in _HABITS.items():
                        own = _rank(habit(x), chosen)
                        self.same[bucket][name].add(own)
                        if (own == 0) != (rank == 0):
                            self.pairs[f"{bucket}|{name}"]["звичка" if own == 0 else "ваги"] += 1
            self._together(model, event, found)

    def _together(self, model: dict[str, Any], event: Event, found: Optional[tuple[np.ndarray, int]]) -> None:
        """Висота й диск разом — те, що робить картка; звідси й впевненість."""
        if event.chosen < 0:
            return
        if event.thickness is not None and event.height >= event.thickness:
            rows = [i for i, h in enumerate(event.heights)
                    if h >= event.thickness and (event.feasible[i] or i == event.chosen)]
            if len(rows) < 2:
                return
            picked = rows.index(event.chosen)
            values = score(model, event.bucket, event.thickness, event.available,
                           [event.heights[i] for i in rows], event.x[rows], event.operator)
            self.full[LEARNED].add(_rank(values, picked))
            self.full["нинішні правила"].add(_rank(_rule_scores(event, rows), picked))
        elif found is not None:
            values, picked = within(model, event.bucket, event.operator, found[0]), found[1]
        else:
            return
        share = float(softmax(values).max())
        place = max(i for i, low in enumerate(BINS[:-1]) if share >= low)
        self.bins[place][0] += int(_rank(values, picked) == 0)
        self.bins[place][1] += 1

    def better_habits(self) -> dict[str, str]:
        """Лінії, де проста звичка доведено точніша за ваги."""
        out = {}
        for bucket, found in self.same.items():
            if bucket == ALL or found[LEARNED].n < 40:
                continue
            name = max(_HABITS, key=lambda h: found[h].first)
            pair = self.pairs[f"{bucket}|{name}"]
            gap, total = pair["звичка"] - pair["ваги"], pair["звичка"] + pair["ваги"]
            if total and gap / math.sqrt(total) > AUTO_Z:
                out[bucket] = name
        return out

    def view(self) -> dict[str, Any]:
        return {
            "диск_тієї_самої_висоти": {
                bucket: {name: t.view() for name, t in sorted(found.items(), key=lambda kv: -kv[1].first)}
                for bucket, found in sorted(self.same.items(), key=lambda kv: -kv[1][LEARNED].n)
                if found[LEARNED].n >= 10
            },
            "диск_і_висота_разом": {name: t.view() for name, t in self.full.items()},
            "за_оператором": {str(k): t.view() for k, t in sorted(self.by_operator.items())},
            "за_місяцем": {k: t.view() for k, t in sorted(self.by_month.items())},
            "на_живих_виборах": self.live.view(),
        }


def examine(events: list[Event]) -> _Exam:
    """Кілька іспитів: кожен — на відрізку, якого навчання не бачило."""
    exam = _Exam()
    total = len(events)
    for i, share in enumerate(FOLDS):
        start = int(total * share)
        end = int(total * FOLDS[i + 1]) if i + 1 < len(FOLDS) else total
        if start and end > start:
            exam.run(_train(events[:start]), events[start:end])
    return exam


# ── Звички словами ─────────────────────────────────────────────────────────


def _habits_of(found: list[Event]) -> tuple[int, dict[str, int]]:
    picks: Counter[str] = Counter()
    n = 0
    for event in found:
        same = _same_height(event, strict=True)
        if same is None:
            continue
        x, chosen = same
        n += 1
        for name, habit in _HABITS.items():
            picks[name] += int(_rank(habit(x), chosen) == 0)
        picks["стоїть у верстаті"] += int(x[chosen, 2] > 0)
    return n, {name: round(100 * c / n) for name, c in picks.most_common()} if n else {}


def _logic(events: list[Event], *, least: int = 20) -> list[dict[str, Any]]:
    """Звички числами, по лініях і людях — щоб людина могла з ними не погодитись."""
    groups: dict[tuple[str, Optional[int]], list[Event]] = defaultdict(list)
    for event in events:
        groups[(event.bucket, None)].append(event)
        if event.operator is not None:
            groups[(event.bucket, event.operator)].append(event)
    out = []
    for (bucket, operator), found in sorted(groups.items(), key=lambda kv: (kv[0][1] or 0, -len(kv[1]))):
        if len(found) < least:
            continue
        n, habits = _habits_of(found)
        steps: Counter[str] = Counter()
        for event in found:
            if event.thickness is None:
                continue
            if event.height < event.thickness:
                steps["нижчий за роботу"] += 1
                continue
            _base, order = height_steps(event.thickness, event.available)
            steps[str(order.get(event.height, MAX_STEP))] += 1
        out.append({
            "лінія": bucket, "оператор": operator, "проєктів": len(found),
            "новий_диск": sum(1 for e in found if e.chosen < 0),
            "виборів_серед_кількох": n, "звички_%": habits, "крок_висоти": dict(sorted(steps.items())),
        })
    return out


def mine(db: Session, days: int = DAYS, *, now: Optional[datetime] = None,
         jobs: Optional[list[Job]] = None, live: Optional[list[Event]] = None) -> dict[str, Any]:
    """Модель + звіт. Імен клієнтів, пацієнтів і назв STL тут немає й не було."""
    from app.services import disc_watch

    now = now or datetime.now()
    paths = disc_history.files(db) if jobs is None else []
    if jobs is None:
        jobs = disc_history.read_all(paths)
    since = now - timedelta(days=days)
    known = known_orders(db, since)
    if live is None:
        live = disc_watch.events(known, since=since)
    past = events_of(jobs, known, since=since, skip=disc_watch.projects())
    events = sorted([*past, *live], key=lambda e: e.at)
    exam = examine(events)
    model = _train(events, now)
    model["use"] = exam.better_habits()
    model["bins"] = {str(k): v for k, v in exam.bins.items()}
    return {
        "at": now.isoformat(timespec="seconds"),
        "days": days,
        "журналів": len(paths),
        "проєктів": len(events),
        "живих_виборів": len(live),
        "з_товщиною": sum(1 for e in events if e.thickness is not None),
        "з_оператором": sum(1 for e in events if e.operator is not None),
        "перший_запис": jobs[0].at.isoformat(timespec="minutes") if jobs else None,
        "останній_запис": jobs[-1].at.isoformat(timespec="minutes") if jobs else None,
        "logic": _logic(events),
        "exam": exam.view(),
        "пам'ятка": "У журналах Sum3D «місце було» — диск потім ще брали (заглядання наперед); на живих "
                    "виборах місце виміряне. Головна перевірка — порівняння на живих листах.",
        "model": model,
    }


# ── Де модель живе ─────────────────────────────────────────────────────────


def _put_meta(key: str, value: Any) -> None:
    from contextlib import closing

    with closing(store.connect(store.current_path())) as conn, conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def _get_meta(key: str) -> Any:
    rows = store.query("SELECT value FROM meta WHERE key = ?", (key,))
    if not rows or not rows[0]["value"]:
        return None
    try:
        return json.loads(rows[0]["value"])
    except ValueError:
        return None


CURVE_KEEP = 120


def save(found: dict[str, Any]) -> None:
    """Модель і крапка на кривій навчання (одна на день)."""
    was = load()
    curve = [c for c in (was.get("curve") or []) if c["at"][:10] != found["at"][:10]]
    learned = ((found.get("exam") or {}).get("диск_тієї_самої_висоти") or {}).get(ALL, {}).get(LEARNED)
    if learned:
        curve.append({"at": found["at"], "виборів": learned["виборів"], "перший_%": learned["перший_%"],
                      "живих": found.get("живих_виборів", 0)})
    _put_meta(_META, {**found, "curve": curve[-CURVE_KEEP:]})
    with _cache_lock:
        _cache.clear()


def load() -> dict[str, Any]:
    value = _get_meta(_META)
    return value if isinstance(value, dict) else {}


# ── Для картки листа ───────────────────────────────────────────────────────

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_CACHE_SECONDS = 300.0


def _cached(key: str, make: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = make()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def forget_cache() -> None:
    with _cache_lock:
        _cache.clear()
    disc_history.forget_live()


def journals(db: Session) -> list[str]:
    return _cached("files", lambda: disc_history.files(db))


_WHY = {0: "різали останнім", 1: "найновіший", 2: "стоїть у верстаті", 3: "різали сьогодні",
        _FULLEST: "найповніший, куди влазить"}


def _explain(model: dict[str, Any], bucket: str, operator: Optional[int], row: np.ndarray) -> str:
    habit = (model.get("use") or {}).get(bucket)
    if habit:
        return habit
    w = _weights(model, bucket, operator)
    worth = sorted(((w[i] * row[i], text) for i, text in _WHY.items() if w[i] * row[i] > 0.1), reverse=True)
    return ", ".join(text for _v, text in worth[:3])


def make_chooser(model: dict[str, Any], shelf: Shelf, *, now: Optional[datetime] = None,
                 operator: Optional[int] = None) -> Chooser:
    bins = {int(k): v for k, v in (model.get("bins") or {}).items()}

    def confidence(values: list[float]) -> Optional[float]:
        """Частка влучань на іспитах при такій самій оцінці; мало іспитів — мовчимо."""
        if len(values) < 2:
            return None
        share = float(softmax(np.array(values)).max())
        hit, n = bins.get(max(i for i, low in enumerate(BINS[:-1]) if share >= low), (0, 0))
        return hit / n if n >= 20 else None

    def choose(want: Want, discs: list[DiscFile], thickness: float, touched_at: dict[str, float],
               active: dict[str, datetime], units: int = 0,
               free: Optional[dict[str, float]] = None, need: float = 0.0) -> Ranking:
        moment = now or datetime.now()
        names = [disc.stem for disc in discs]
        heights = [disc.height or 0 for disc in discs]
        mounted = set(shelf.mounted.values())
        on = {disc.stem for disc in discs
              if disc.key in active or disc_history.stem_key(disc.stem) in mounted}
        uses: dict[str, DiscUse] = {}
        for disc in discs:
            known = shelf.discs.get(disc_history.stem_key(disc.stem))
            if known is None:
                # Журнал диска не бачив (інша Sum3D, давній): час — за файлом.
                seen = datetime.fromtimestamp(touched_at.get(str(disc.path)) or 0)
                known = DiscUse(first=seen, last=seen, jobs=1, items=0)
            uses[disc.stem] = known
        x = _matrix(names, heights, uses, on, moment, units, free, need)
        bucket = bucket_of(want.family, want.line)
        values = score(model, bucket, thickness, heights, heights, x, operator)
        return Ranking(
            score={name: float(v) for name, v in zip(names, values)},
            why={name: _explain(model, bucket, operator, row) for name, row in zip(names, x)},
            confidence=confidence,
        )

    return choose


def current(db: Session, operator: Optional[int] = None) -> Optional[Chooser]:
    """Вивчений порядок, якщо є модель і журнал Sum3D; інакше None."""
    found = _cached("model", load)
    model = found.get("model") if found else None
    if not model or not (model.get("weights") or {}).get(ALL):
        return None
    shelf = disc_history.live_shelf(journals(db))
    return make_chooser(model, shelf, operator=operator) if shelf is not None else None


# ── Увімкнено чи ні ────────────────────────────────────────────────────────


def status(db: Session) -> str:
    """Рішення адміна: `on`/`off`/`paused`/`auto`; ще не вирішував — `shadow`."""
    return (rules.load(db).get(KEY) or {}).get("status") or rules.STATUS_SHADOW


def auto_state() -> dict[str, Any]:
    value = _get_meta(_META_AUTO)
    return value if isinstance(value, dict) else {"on": False, "why": ""}


def is_on(db: Session) -> bool:
    state = status(db)
    return state == rules.STATUS_ON or (state == STATUS_AUTO and bool(_cached("auto", auto_state)["on"]))


def choosers(db: Session, operator: Optional[int] = None) -> tuple[Optional[Chooser], Optional[Chooser], bool]:
    """(для картки, для мовчазної пари, чи рахувати пару). Збій — усе за правилами."""
    try:
        if status(db) == rules.STATUS_OFF:
            return None, None, False
        learned = current(db, operator)
        on = learned is not None and is_on(db)
    except Exception:
        logger.exception("Вивчений порядок дисків недоступний — підказка за правилами")
        return None, None, False
    if learned is None:
        return None, None, False
    return (learned, None, True) if on else (None, learned, True)


def verdict(tally: dict[str, Any], *, was_on: bool) -> tuple[bool, str]:
    """Чи доведено, що вивчений порядок кращий. Увімкнений тримається, поки не програє."""
    n, learned, rule = tally["рішень"], tally["лише_вивчений"], tally["лише_правила"]
    why = f"рішень {n}: влучив лише вивчений {learned}, лише правила {rule}"
    if was_on:
        return learned >= rule, why
    total = learned + rule
    return bool(n >= AUTO_MIN and total and (learned - rule) / math.sqrt(total) >= AUTO_Z), why


def review(db: Session, *, now: Optional[datetime] = None) -> Optional[str]:
    """Щоночі: «Автоматично» — увімкнути або вимкнути за доказами; увімкнений
    людиною, що програє, — на паузу. Повертає, що сталося. Коміт — за викликачем."""
    from app.services import selection_journal

    state = status(db)
    if state not in (STATUS_AUTO, rules.STATUS_ON):
        return None
    tally = selection_journal.rivalry(db, 60, now=now)
    if state == rules.STATUS_ON:
        n, learned, rule = tally["рішень"], tally["вивчений"]["перший"], tally["правила"]["перший"]
        if n < PAUSE_MIN or learned + PAUSE_GAP > rule:
            return None
        why = f"на {n} рішеннях вивчений порядок влучив {learned}, правила {rule}"
        rules.save_status(db, RULE, rules.STATUS_PAUSED, user_id=None, now=now, note=why)
        store.put(d.STEP_RULE, d.ACTOR_SYSTEM, rule=KEY, chosen=rules.STATUS_PAUSED, data={"чому": why})
        return "пауза: " + why
    was = bool(auto_state()["on"])
    on, why = verdict(tally, was_on=was)
    _put_meta(_META_AUTO, {"on": on, "why": why, "at": (now or datetime.now()).isoformat(timespec="seconds")})
    with _cache_lock:
        _cache.pop("auto", None)
    if on == was:
        return None
    store.put(d.STEP_RULE, d.ACTOR_SYSTEM, rule=KEY, chosen="on" if on else "off", data={"чому": why, "сам": True})
    return ("увімкнено сам: " if on else "вимкнено сам: ") + why
