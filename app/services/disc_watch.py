"""Живі вибори диска: кожен новий проєкт Sum3D разом з усім, з чого вибирали.

Журнал Sum3D каже, який диск узяли, але не каже, скільки місця було на ньому й на
сусідніх: файл диска Sum3D переписує після кожної розстановки. Тому раз на хвилину
(воркер `kuubmill-decisions`) дивимось, чи журнал дописали, і для кожного НОВОГО
проєкту записуємо стан усіх дисків того самого матеріалу й кольору ДО нього.

Звідки «до»: стан дисків ми знімаємо після кожного проєкту групи (таблиця
`disc_state`, два останні стани на диск). Диска, стану якого ще не знали, а файл
уже переписано, чесно лишаємо без вільного місця — не вгадуємо.

Разом із вибором пишемо, на яке місце його поставила б модель ТОГО дня: це крива
навчання, яку заднім числом не підправиш.

Лише читання журналів і файлів дисків; пише тільки у `decisions.db`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import numpy as np
from sqlalchemy.orm import Session

from app import decisions_store as store
from app.services import disc_choice as dc
from app.services import disc_history, sum3d_disc
from app.services.disc_history import DiscUse, Job, Shelf
from app.services.disc_suggest import DiscFile, Want, _touched_at, describe, disc_files
from app.settings_store import get_setting

logger = logging.getLogger(__name__)

PASS_SECONDS = 6.0
PASS_JOBS = 8
READS = 60
MAX_BACKLOG_HOURS = 12
"""Старіші за стільки (CRM не працювала) стан «до» вже не мають — пропускаємо."""
BUSY_MINUTES = 25
"""Файл, переписаний так близько до проєкту, міг уже нести його розстановку."""
MARK = "disc_watch_seen"
OVERLAP = timedelta(hours=6)

_signature: Optional[tuple] = None


def forget() -> None:
    """Для тестів."""
    global _signature
    _signature = None


def _stat(paths: list[str]) -> tuple:
    out = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append((path, st.st_size, int(st.st_mtime)))
    return tuple(out)


def _meta(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


# ── Стан дисків ────────────────────────────────────────────────────────────


def _measure(disc: DiscFile) -> Optional[tuple[float, float]]:
    """(коли переписано, вільна частка) — зараз."""
    touched = _touched_at(disc.path)
    shape = sum3d_disc.shape_of(disc.path) if touched is not None else None
    if touched is None or shape is None:
        return None
    return touched, round(float(shape.usable_share), 4)


def _states(conn, stems: list[str]) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    for start in range(0, len(stems), 400):
        part = stems[start: start + 400]
        for row in conn.execute(
            f"SELECT stem, body FROM disc_state WHERE stem IN ({','.join('?' * len(part))})", part
        ):
            out[row["stem"]] = json.loads(row["body"])
    return out


def _remember(conn, measured: dict[str, tuple[float, float]], states: dict[str, list[list[float]]]) -> None:
    for stem, (touched, free) in measured.items():
        history = [s for s in states.get(stem, []) if s[0] < touched][-1:] + [[touched, free]]
        if history != states.get(stem):
            states[stem] = history
            conn.execute("INSERT INTO disc_state (stem, body) VALUES (?, ?) "
                         "ON CONFLICT(stem) DO UPDATE SET body = excluded.body", (stem, json.dumps(history)))


def free_before(history: list[list[float]], measured: Optional[tuple[float, float]], at: datetime,
                *, chosen: bool) -> Optional[float]:
    """Вільна частка диска перед проєктом `at` або None — не знаємо.

    Для ОБРАНОГО — стан, знятий раніше, ніж файл переписали під цей проєкт.
    Для сусіда — стан на той час: файл міг змінитись лише іншим проєктом."""
    moment = at.timestamp()
    border = moment - BUSY_MINUTES * 60 if chosen else moment
    known = [*history, *([list(measured)] if measured else [])]
    earlier = [free for touched, free in known if touched < border]
    return earlier[-1] if earlier else None


# ── Запис вибору ───────────────────────────────────────────────────────────


def _candidates(files: list[DiscFile], group: tuple, blank: str, at: datetime) -> list[DiscFile]:
    low = at.timestamp() - dc.ALIVE_DAYS * 86400
    out = []
    for disc in files:
        if dc.group_of(disc) != group:
            continue
        touched = _touched_at(disc.path)
        if disc_history.stem_key(disc.stem) == blank or (touched is not None and touched >= low):
            out.append((-(touched or 0.0), disc))
    return [disc for _t, disc in sorted(out, key=lambda p: p[0])[:READS]]


def _record(conn, job: Job, shelf: Shelf, files: list[DiscFile], model: dict[str, Any]) -> bool:
    disc = describe(job.blank, disc_files_path(files, job.blank))
    group = dc.group_of(disc)
    if group is None:
        return False
    found = _candidates(files, group, job.blank, job.at)
    stems = [disc_history.stem_key(f.stem) for f in found]
    states = _states(conn, stems)
    measured = {stem: m for stem, f in zip(stems, found) if (m := _measure(f)) is not None}
    rows: list[list[Any]] = []
    for stem, file in zip(stems, found):
        use = shelf.discs.get(stem)
        if use is None and stem == job.blank:
            continue  # новий диск: у виборі «з початих» його ще не було
        touched = measured.get(stem, (0.0, 0.0))[0]
        if use is None:
            seen = datetime.fromtimestamp(touched)
            use = DiscUse(first=seen, last=seen, jobs=0, items=0)
        if use.last < job.at - timedelta(days=dc.ALIVE_DAYS):
            continue
        free = free_before(states.get(stem, []), measured.get(stem), job.at, chosen=stem == job.blank)
        rows.append([stem, file.height, free, use.last.isoformat(), use.first.isoformat(), use.items, use.jobs])
    before = next((r[2] for r in rows if r[0] == job.blank), None)
    after = measured.get(job.blank, (0.0, None))
    used = round(before - after[1], 4) if before is not None and after[1] is not None and before > after[1] else None
    body = {
        "c": rows, "used": used, "units": job.items, "height": disc.height,
        "mounted": sorted(set(shelf.mounted.values())),
        "group": [disc.family, disc.line, sorted(disc.shades), disc.code, disc.hipc],
    }
    event = _event(job.at, job.blank, body, None, None)
    rank = None
    if event is not None and model.get("weights"):
        same = dc._same_height(event, strict=True)
        if same is not None:
            rank = dc._rank(same[0] @ dc._weights(model, event.bucket), same[1])
    conn.execute(
        "INSERT OR REPLACE INTO disc_choices (project, at, blank, machine, body, rank) VALUES (?, ?, ?, ?, ?, ?)",
        (job.project, job.at.isoformat(timespec="seconds"), job.blank, job.machine,
         json.dumps(body, ensure_ascii=False, separators=(",", ":")), rank),
    )
    _remember(conn, measured, states)
    return True


def disc_files_path(files: list[DiscFile], blank: str):
    from pathlib import Path

    return next((f.path for f in files if disc_history.stem_key(f.stem) == blank), Path(blank))


def _key(job: Job) -> str:
    return f"{job.project}|{job.blank}"


def _mark(jobs: list[Job]) -> str:
    last = max(j.at for j in jobs)
    return json.dumps({"at": last.isoformat(), "keys": [_key(j) for j in jobs if j.at > last - 2 * OVERLAP]})


def observe(db: Session, *, now: Optional[datetime] = None) -> int:
    """Прохід: записати нові вибори. Без змін у журналі — лише кілька `stat`."""
    global _signature
    if not store.enabled():
        return 0
    now = now or datetime.now()
    paths = dc.journals(db)
    signature = _stat(paths)
    if not signature or signature == _signature:
        return 0
    root = get_setting(db, "cam_blanks_path")
    jobs = disc_history.choices(disc_history.read_all(paths, disc_history.LIVE_BYTES))
    with closing(store.connect(store.current_path())) as conn, conn:
        seen = _meta(conn, MARK)
        if seen is None:
            # Перший прохід запам'ятовує, де кінець: минуле стану «до» не має.
            if jobs:
                _set_meta(conn, MARK, _mark(jobs))
            _signature = signature
            return 0
        mark = json.loads(seen)
        known = set(mark["keys"])
        # Дві Sum3D пишуть упереміш: блок довшого розрахунку лягає пізніше, але з
        # ранішим часом. Тому дивимось назад на `OVERLAP` і пам'ятаємо, що вже бачили.
        edge = (datetime.fromisoformat(mark["at"]) - OVERLAP).isoformat()
        fresh = [j for j in jobs if j.project and j.at.isoformat() > edge and _key(j) not in known]
        low = now - timedelta(hours=MAX_BACKLOG_HOURS)
        shelf = Shelf()
        new_keys = {_key(j) for j in fresh}
        past = [j for j in jobs if _key(j) not in new_keys]
        for job in past:
            shelf.add(job)
        files = disc_files(root)
        model = (dc._cached("model", dc.load) or {}).get("model") or {}
        done = written = 0
        started = time.monotonic()
        for job in fresh:
            if done >= PASS_JOBS or time.monotonic() - started > PASS_SECONDS:
                break
            if job.at >= low and files:
                try:
                    written += int(_record(conn, job, shelf, files, model))
                except Exception:
                    logger.exception("Вибір диска проєкту %s не записано", job.project)
            shelf.add(job)
            past.append(job)
            done += 1
        if done:
            _set_meta(conn, MARK, _mark(past))
    if done == len(fresh):
        _signature = signature
    return written


# ── Для навчання ───────────────────────────────────────────────────────────


def _event(at: datetime, blank: str, body: dict[str, Any], thickness: Optional[float],
           operator: Optional[int]) -> Optional[dc.Event]:
    rows = body["c"]
    family, line, shades, code, hipc = body["group"]
    names = [r[0] for r in rows]
    heights = [int(r[1] or 0) for r in rows]
    uses = {r[0]: DiscUse(first=datetime.fromisoformat(r[4]), last=datetime.fromisoformat(r[3]),
                          items=int(r[5]), jobs=int(r[6])) for r in rows}
    free = {r[0]: float(r[2]) for r in rows if r[2] is not None}
    need = float(body.get("used") or 0.0)
    return dc.Event(
        at=at, bucket=dc.bucket_of(family, line), names=names, heights=heights,
        x=dc._matrix(names, heights, uses, set(body["mounted"]), at, int(body["units"]), free, need),
        chosen=names.index(blank) if blank in names else -1,
        # Місця не знаємо — диск лишається у виборі: зайве не відкидаємо.
        feasible=np.array([n not in free or free[n] >= need for n in names], dtype=bool),
        thickness=thickness, available=sorted({*heights, int(body["height"])}), height=int(body["height"]),
        want=Want(family, line, frozenset(shades), code, hipc),
        operator=operator, live=True, units=int(body["units"]),
    )


def events(known: dc.Known, *, since: datetime) -> list[dc.Event]:
    out = []
    for row in store.query("SELECT project, at, blank, body FROM disc_choices WHERE at >= ? ORDER BY at",
                           (since.isoformat(timespec="seconds"),)):
        body = row["body"] if isinstance(row["body"], dict) else json.loads(row["body"])
        at = datetime.fromisoformat(row["at"])
        tail = row["project"][11:]
        event = _event(at, row["blank"], body, dc._thickness_for(tail, at, body["group"][0], known),
                       dc._operator_for(tail, at, known))
        if event is not None:
            out.append(event)
    return out


def projects() -> set[str]:
    return {row["project"] for row in store.query("SELECT project FROM disc_choices")}


def curve(weeks: int = 12, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """По тижнях: як часто модель ТОГО дня ставила взятий диск першою."""
    now = now or datetime.now()
    low = (now - timedelta(weeks=weeks)).isoformat(timespec="seconds")
    found: dict[str, list[int]] = {}
    for row in store.query("SELECT at, rank FROM disc_choices WHERE at >= ? AND rank IS NOT NULL ORDER BY at", (low,)):
        at = datetime.fromisoformat(row["at"])
        found.setdefault((at - timedelta(days=at.weekday())).strftime("%d.%m"), []).append(int(row["rank"]))
    return [_point(week, ranks) for week, ranks in found.items()]


def _point(week: str, ranks: Iterable[int]) -> dict[str, Any]:
    values = list(ranks)
    n = len(values)
    return {"тиждень": week, "виборів": n, "перший_%": round(100 * sum(r == 0 for r in values) / n),
            "у_трьох_%": round(100 * sum(r < 3 for r in values) / n)}


def state() -> dict[str, Any]:
    rows = store.query("SELECT COUNT(*) AS n, MIN(at) AS since FROM disc_choices")
    return {"виборів": rows[0]["n"] if rows else 0, "від": (rows[0]["since"] if rows else None) or ""}
