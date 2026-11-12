"""Історія цеху для розвідки через MCP — ЛИШЕ читання (власник 12.11.26).

Власник дозволив дати Claude більше даних для аналізу, але НЕ вмикати поки
жодних нових можливостей у CRM. Тому тут лише звіти: нічого не пишеться ні в
базу, ні на диск, ні в таблицю (стереже `tests/test_program_folders.py`).

* `machine_history` — програми верстатів з показів: скільки йшла, скільки
  простояла на 100 % до наступної, повторні запуски, обриви зв'язку;
* `cam_units` — кількість у таблиці проти STL у проєкті Sum3D (`.cam`);
* `disc_usage` — скільки нових дисків беруть за тиждень по групах;
* `furnace_cycles` — цикли печей: тривалість, найвища температура, нагрів;
* `export_overview` — теки `export` і спул пошти: скільки, якого віку, чиї.

Назви STL і файлів з `export` назовні не віддаються (у них імена пацієнтів) —
лише лічильники. Імена клієнтів — так, як і в решті інструментів.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path, PurePath
from statistics import median
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CamBlank, Furnace, FurnaceReading, Machine, MachineLinkEvent, MachineReading, Order
from app.services.disc_check import program_disc
from app.services.order_dates import order_date

# Програма вважається тією самою, поки назва не змінилась; пауза в показах
# довша за це — розрив (верстат вимкнули), а не робота.
GAP_MINUTES = 20
DONE_PERCENT = 100


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


# ── Верстати ────────────────────────────────────────────────────────────────


def machine_history(db: Session, days: int, machine: str = "", examples: int = 15) -> dict[str, Any]:
    """Програми кожного верстата за `days` днів із показів (`MachineReading`)."""
    since = datetime.now() - timedelta(days=days)
    machines = db.scalars(select(Machine).order_by(Machine.sort_order, Machine.id)).all()
    wanted = machine.strip().lower()
    out: list[dict[str, Any]] = []
    for m in machines:
        if wanted and wanted not in m.name.lower():
            continue
        rows = db.execute(
            select(MachineReading.captured_at, MachineReading.iso_name, MachineReading.percent,
                   MachineReading.error)
            .where(MachineReading.host == m.host, MachineReading.captured_at >= since)
            .order_by(MachineReading.captured_at)
        ).all()
        programs: list[dict[str, Any]] = []
        current: Optional[dict[str, Any]] = None
        errors = 0
        last_at: Optional[datetime] = None
        for at, iso, pct, err in rows:
            if err:
                errors += 1
            gap = last_at is not None and at - last_at > timedelta(minutes=GAP_MINUTES)
            last_at = at
            if not iso:
                continue
            if current is None or current["iso"] != iso or gap:
                current = {"iso": iso, "start": at, "end": at, "max": pct, "done_at": None}
                programs.append(current)
            current["end"] = at
            if pct is not None:
                current["max"] = max(current["max"] or 0, pct)
                if pct >= DONE_PERCENT and current["done_at"] is None:
                    current["done_at"] = at
        # Простій на 100 %: від «готово» до початку наступної програми.
        for prev, nxt in zip(programs, programs[1:] + [None]):
            prev["wait"] = (
                _minutes((nxt["start"] if nxt else prev["end"]) - prev["done_at"])
                if prev["done_at"] else None
            )
        seen_days: dict[str, set[date]] = defaultdict(set)
        for p in programs:
            seen_days[p["iso"]].add(p["start"].date())
        repeats = [iso for iso, d in seen_days.items() if len(d) > 1
                   or sum(1 for p in programs if p["iso"] == iso) > 1]
        runs = [_minutes(p["done_at"] - p["start"]) for p in programs if p["done_at"]]
        waits = [p["wait"] for p in programs if p["wait"] is not None]
        night_waits = [p["wait"] for p in programs
                       if p["wait"] is not None and (p["done_at"].hour >= 18 or p["done_at"].hour < 8)]
        by_disc: dict[str, list[int]] = defaultdict(list)
        for p in programs:
            disc = program_disc(p["iso"])
            if p["done_at"] and disc is not None:
                by_disc[f"{disc.family or '?'} {disc.height or '?'} мм"].append(
                    _minutes(p["done_at"] - p["start"]))
        links = db.scalars(
            select(MachineLinkEvent).where(MachineLinkEvent.host == m.host,
                                           MachineLinkEvent.detected_at >= since)
        ).all()
        link_minutes = sum(
            _minutes((e.ended_at or datetime.now()) - (e.started_at or e.detected_at)) for e in links
        )
        out.append({
            "верстат": m.name,
            "показів": len(rows),
            "програм": len(programs),
            "дійшли_до_100": len(runs),
            "медіана_роботи_хв": int(median(runs)) if runs else None,
            "медіана_простою_на_100_хв": int(median(waits)) if waits else None,
            "сумарний_простій_на_100_год": round(sum(waits) / 60, 1) if waits else 0,
            "простій_уночі_18_8_год": round(sum(night_waits) / 60, 1) if night_waits else 0,
            "повторні_запуски": len(repeats),
            "повторні_приклади": repeats[:examples],
            "кадрів_з_помилкою": errors,
            "обривів_звязку": len(links),
            "обриви_хв": link_minutes,
            "робота_за_диском_медіана_хв": {k: int(median(v)) for k, v in sorted(by_disc.items())},
            "найдовші_простої": [
                {"програма": p["iso"], "готово": p["done_at"].strftime("%d.%m %H:%M"), "хв": p["wait"]}
                for p in sorted((p for p in programs if p["wait"]), key=lambda p: -p["wait"])[:examples]
            ],
        })
    return {
        "днів": days,
        "верстати": out,
        "пояснення": (
            "Програма — поки назва .iso не змінилась (розрив у показах понад "
            f"{GAP_MINUTES} хв — нова). «Робота» — від першого показу до 100 %, «простій» — від 100 % "
            "до наступної програми. Повторний запуск — та сама назва в різні дні чи після іншої "
            "програми. Стану «помилка» в історії немає — лише кадри, що не знялись."
        ),
    }


# ── Кількість: таблиця проти проєкту Sum3D ─────────────────────────────────

_STL_PATH = re.compile(rb"[^\x00-\x1f\"<>|]{1,240}?\.stl", re.I)
_STL_PATH_W = re.compile(r"[^\x00-\x1f\"<>|]{1,240}?\.stl", re.I)


def project_stl_names(path: Path, max_bytes: int = 64 * 1024 * 1024) -> list[str]:
    """Імена STL, згадані в проєкті `.cam` (ASCII й UTF-16). Лише в памʼяті."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes)
    except OSError:
        return []
    names: dict[str, None] = {}
    for raw in _STL_PATH.findall(data):
        names.setdefault(PurePath(raw.decode("latin-1").replace("\\", "/")).name.lower())
    # UTF-16 рядок може починатись і з парного, і з непарного байта.
    for shift in (0, 1):
        text = data[shift:].decode("utf-16-le", errors="ignore")
        for raw_text in _STL_PATH_W.findall(text):
            names.setdefault(PurePath(raw_text.replace("\\", "/")).name.lower())
    return [n for n in names if len(n) > 4]


def cam_units(db: Session, days: int, examples: int = 25) -> dict[str, Any]:
    """Одиниці в таблиці (сума рядків одного Sum3D і дня) проти STL у проєкті."""
    from app.services.stl_units import count_units
    from app.settings_store import get_sum3d_projects_path

    root = (get_sum3d_projects_path(db) or "").strip()
    if not root:
        return {"помилка": "тека проєктів Sum3D не задана (Налаштування → Теки)"}
    since = date.today() - timedelta(days=days)
    orders = db.scalars(select(Order).where(Order.sum3d_id.is_not(None))).all()
    groups: dict[tuple[date, str], list[Order]] = defaultdict(list)
    tail_re = re.compile(r"(?<!\d)(\d{2}-\d{2}-\d{2})(?!\d)")
    for o in orders:
        day = order_date(o)
        if day < since:
            continue
        for tail in set(tail_re.findall(o.sum3d_id or "")):
            groups[(day, tail)].append(o)
    counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for (day, tail), members in sorted(groups.items(), reverse=True):
        if time.monotonic() - started > 40:
            counts["не_встигли"] += 1
            continue
        project = next(
            (Path(root) / f"{day + timedelta(days=d):%Y-%m-%d}_{tail}.cam" for d in (0, 1, -1)
             if (Path(root) / f"{day + timedelta(days=d):%Y-%m-%d}_{tail}.cam").is_file()),
            None,
        )
        if project is None:
            counts["проєкт_не_знайдено"] += 1
            continue
        names = project_stl_names(project)
        if not names:
            counts["stl_у_проєкті_не_видно"] += 1
            continue
        units = count_units(names)
        table = 0
        for o in members:
            try:
                table += int(str(o.quantity or "0").strip() or 0)
            except ValueError:
                pass
        verdict = "збіг" if units.units == table else ("без_зубів_у_назвах" if not units.teeth else "розбіжність")
        counts[verdict] += 1
        if verdict != "збіг" and len(rows) < examples:
            rows.append({
                "проєкт": project.name, "у_таблиці": table, "зубів_у_stl": units.units,
                "stl_файлів": len(names), "робіт": len(members),
                "клієнти": sorted({o.client_name or "—" for o in members}),
                "вердикт": verdict,
            })
    return {
        "днів": days, "тека_проєктів": root, "груп_sum3d": len(groups), **dict(counts),
        "приклади": rows,
        "пояснення": ("Група — усі роботи одного дня з тим самим Sum3D. Зуби рахуються з назв STL у "
                      "проєкті (як у пошті); самі назви не віддаються. Нічого не змінює."),
    }


# ── Диски: темп витрати ─────────────────────────────────────────────────────


def disc_usage(db: Session, weeks: int) -> dict[str, Any]:
    """Скільки НОВИХ дисків брали за тиждень по групах (без «точки відліку»)."""
    since = datetime.now() - timedelta(weeks=weeks)
    rows = db.scalars(select(CamBlank).where(CamBlank.first_seen_at >= since)).all()
    real = [r for r in rows if not (r.ordered_at == r.first_seen_at and r.order_id is None)]
    per_group: dict[str, Counter] = defaultdict(Counter)
    for r in real:
        group = f"{r.material_dir} {r.height or r.height_dir} {r.shade or '—'}".strip()
        per_group[group][r.first_seen_at.strftime("%G-W%V")] += 1
    open_now = Counter(
        f"{r.material_dir} {r.height or r.height_dir} {r.shade or '—'}".strip()
        for r in db.scalars(select(CamBlank).where(CamBlank.gone_at.is_(None))).all()
    )
    table = sorted(
        ({"група": g, "за_період": sum(c.values()), "на_тиждень": round(sum(c.values()) / weeks, 1),
          "по_тижнях": dict(sorted(c.items())), "файлів_у_теці_зараз": open_now.get(g, 0)}
         for g, c in per_group.items()),
        key=lambda x: -x["за_період"],
    )
    return {"тижнів": weeks, "нових_дисків": len(real), "групи": table,
            "пояснення": "Новий диск = файл, якого CRM раніше не бачила (не перший прохід теки)."}


# ── Печі ────────────────────────────────────────────────────────────────────


def furnace_cycles(db: Session, days: int, examples: int = 20) -> dict[str, Any]:
    """Цикли печей (суцільний RUN): тривалість, найвища температура, нагрів."""
    since = datetime.now() - timedelta(days=days)
    names = {f.host: f.name for f in db.scalars(select(Furnace)).all()}
    out = []
    for host, name in names.items():
        rows = db.execute(
            select(FurnaceReading.captured_at, FurnaceReading.status, FurnaceReading.temp_c)
            .where(FurnaceReading.host == host, FurnaceReading.captured_at >= since)
            .order_by(FurnaceReading.captured_at)
        ).all()
        cycles: list[dict[str, Any]] = []
        cur: Optional[dict[str, Any]] = None
        for at, status, temp in rows:
            if status == "RUN":
                if cur is None:
                    cur = {"start": at, "end": at, "max": temp, "max_at": at if temp else None}
                    cycles.append(cur)
                cur["end"] = at
                if temp is not None and (cur["max"] is None or temp > cur["max"]):
                    cur["max"], cur["max_at"] = temp, at
            elif status == "WAIT":
                cur = None
        durations = [_minutes(c["end"] - c["start"]) for c in cycles]
        to_max = [_minutes(c["max_at"] - c["start"]) for c in cycles if c["max_at"]]
        out.append({
            "піч": name, "показів": len(rows), "циклів": len(cycles),
            "медіана_тривалості_хв": int(median(durations)) if durations else None,
            "медіана_до_макс_темп_хв": int(median(to_max)) if to_max else None,
            "цикли": [
                {"старт": c["start"].strftime("%d.%m %H:%M"), "хв": _minutes(c["end"] - c["start"]),
                 "макс_°C": c["max"],
                 "до_макс_хв": _minutes(c["max_at"] - c["start"]) if c["max_at"] else None}
                for c in cycles[-examples:]
            ],
        })
    return {"днів": days, "печі": out,
            "пояснення": "Цикл — суцільний статус RUN; WAIT закриває цикл. Невідомий статус не рве цикл."}


# ── Теки export і спул пошти ────────────────────────────────────────────────


def _folder_size(path: str, budget_end: float) -> tuple[int, int]:
    """(файлів, байтів) під текою, поки не скінчився час."""
    files = size = 0
    stack = [path]
    while stack and time.monotonic() < budget_end:
        try:
            with os.scandir(stack.pop()) as entries:
                for e in entries:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        files += 1
                        size += e.stat().st_size
        except OSError:
            continue
    return files, size


def export_overview(db: Session) -> dict[str, Any]:
    """`export/<клієнт>/<партія>/…` і спул пошти: обсяг, вік, закріплені теки."""
    from app.settings_store import get_export_folder_path, get_mail_attachments_path

    started = time.monotonic()
    budget_end = started + 40
    result: dict[str, Any] = {}
    root = (get_export_folder_path(db) or "").strip()
    bound = {
        (p or "").replace("\\", "/").rstrip("/").lower()
        for p in db.scalars(select(Order.export_folder_path).where(Order.export_folder_path.is_not(None)))
    }
    if root:
        ages: Counter[str] = Counter()
        clients = 0
        batches = unbound = 0
        files = size = 0
        now = datetime.now()
        try:
            client_dirs = [e for e in os.scandir(root) if e.is_dir()]
        except OSError as exc:
            client_dirs = []
            result["export_помилка"] = exc.__class__.__name__
        for c in client_dirs:
            clients += 1
            try:
                batch_dirs = [e for e in os.scandir(c.path) if e.is_dir()]
            except OSError:
                continue
            for b in batch_dirs:
                batches += 1
                try:
                    age = (now - datetime.fromtimestamp(b.stat().st_mtime)).days
                except OSError:
                    age = -1
                ages["до 30 дн" if 0 <= age < 30 else "30–180 дн" if 0 <= age < 180
                     else "понад 180 дн" if age >= 180 else "?"] += 1
                # `export_folder_path` — відносно кореня export (`клієнт/партія/…`).
                rel_prefix = f"{c.name}/{b.name}".lower()
                if not any(p == rel_prefix or p.startswith(rel_prefix + "/") for p in bound):
                    unbound += 1
                if time.monotonic() < budget_end:
                    n, s = _folder_size(b.path, budget_end)
                    files += n
                    size += s
        result["export"] = {
            "тека": root, "клієнтів": clients, "партій": batches, "за_віком_партій": dict(ages),
            "партій_без_закріпленої_роботи": unbound, "файлів": files,
            "гб": round(size / 1024**3, 2),
            "обрізано_за_часом": time.monotonic() >= budget_end,
        }
    spool = get_mail_attachments_path(db)
    if spool and os.path.isdir(spool):
        n, s = _folder_size(spool, time.monotonic() + 15)
        try:
            letters = sum(1 for e in os.scandir(spool) if e.is_dir())
        except OSError:
            letters = 0
        result["спул_пошти"] = {"тека": spool, "листів": letters, "файлів": n, "гб": round(s / 1024**3, 2)}
    result["прохід_с"] = round(time.monotonic() - started, 1)
    result["пояснення"] = ("Партія — тека другого рівня в export. «Без закріпленої роботи» — жодна "
                           "робота не має її в export_folder_path (видача могла знайти її нечітко). "
                           "Назв файлів немає. Нічого не змінює.")
    return result
