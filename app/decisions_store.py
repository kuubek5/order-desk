"""Сховище журналу рішень — окремий файл `decisions.db` поруч із базою CRM.

Власник 24.11.26: «щоб система фіксувала прийняття рішень… всі дрібні деталі…
і щоб могла на цих навчатися».

ЧОМУ ОКРЕМИЙ ФАЙЛ. Подій на порядок більше, ніж рядків у решті таблиць, і
жодна з них не потрібна, щоб цех працював. У спільній базі кожна ставала б у
чергу за тим самим блокуванням запису, що й галочка «знайдено» оператора.
Окремий файл цього не вміє: зіпсувався, зник, заблокований — CRM працює як
працювала, а журнал починається з чистого.

ЧОМУ ЧЕРГА. `put` не чекає диска: роут кладе подію в чергу й іде далі, пише
один потік. Черга повна (диск завис) — подія губиться з рядком у лозі; прийняття
листа не губиться ніколи. Раптове вимкнення ПК коштує щонайбільше мить подій.

Без `configure()` усе мовчить: тести й допоміжні процеси нічого не пишуть у
справжній файл. Записує лише процес застосунку (lifespan).

Чого тут НЕМАЄ: тексту листів, тем, назв файлів, коментарів, адрес — у них
стоять імена пацієнтів.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

FILE_NAME = "decisions.db"
QUEUE_MAX = 5000
RETENTION_DAYS = 3 * 365

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    step TEXT NOT NULL,
    actor TEXT NOT NULL,
    user_id INTEGER,
    email_id INTEGER,
    order_id INTEGER,
    proposed TEXT,
    chosen TEXT,
    rule TEXT,
    data TEXT,
    version TEXT,
    src TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_email ON events(email_id, at);
CREATE INDEX IF NOT EXISTS ix_events_order ON events(order_id, at);
CREATE INDEX IF NOT EXISTS ix_events_step ON events(step, at);
CREATE TABLE IF NOT EXISTS state (
    entity TEXT NOT NULL, id INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (entity, id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS disc_choices (
    project TEXT PRIMARY KEY, at TEXT NOT NULL, blank TEXT NOT NULL, machine TEXT,
    body TEXT NOT NULL, rank INTEGER
);
CREATE INDEX IF NOT EXISTS ix_disc_choices_at ON disc_choices (at);
CREATE TABLE IF NOT EXISTS disc_state (stem TEXT PRIMARY KEY, body TEXT NOT NULL);
"""

_COLUMNS = (
    "at", "step", "actor", "user_id", "email_id", "order_id",
    "proposed", "chosen", "rule", "data", "version", "src",
)


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def insert_events(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> None:
    marks = ",".join("?" * len(_COLUMNS))
    conn.executemany(
        # OR IGNORE — за `src`: подія, виведена з рядка бази (переробка №9),
        # після рестарту прийде знову й не має лягти двічі.
        f"INSERT OR IGNORE INTO events ({','.join(_COLUMNS)}) VALUES ({marks})",
        [tuple(_cell(event.get(name)) for name in _COLUMNS) for event in events],
    )


class Recorder:
    """Один потік запису з чергою. `put` ніколи не чекає й ніколи не кидає."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._queue: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="kuubmill-decisions-write", daemon=True
        )
        self._thread.start()

    def put(self, event: dict[str, Any]) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            logger.warning("Журнал рішень: черга повна, подію «%s» пропущено", event.get("step"))
            return False

    def stop(self, timeout: float = 3.0) -> None:
        if self._thread is None:
            return
        try:
            self._queue.put(None, timeout=0.5)
        except queue.Full:
            logger.warning("Журнал рішень: черга повна на вимкненні")
        self._thread.join(timeout=timeout)
        self._thread = None

    def flush(self, timeout: float = 3.0) -> None:
        """Дочекатись, поки все вже покладене ляже на диск (тести, знімок)."""
        done = threading.Event()
        try:
            self._queue.put({"_barrier": done}, timeout=timeout)
        except queue.Full:
            return
        done.wait(timeout)

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            while len(batch) < 200:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            events = [item for item in batch if item is not None and "_barrier" not in item]
            if events:
                try:
                    with closing(connect(self.path)) as conn, conn:
                        insert_events(conn, events)
                except Exception:
                    logger.exception("Журнал рішень: не записано подій: %s", len(events))
            for item in batch:
                if item is not None and "_barrier" in item:
                    item["_barrier"].set()
            if any(item is None for item in batch):
                return


_recorder: Optional[Recorder] = None
_path: Optional[Path] = None


def configure(path: str | Path) -> None:
    """Увімкнути запис (lifespan застосунку)."""
    global _recorder, _path
    shutdown()
    _path = Path(path)
    _recorder = Recorder(path)
    _recorder.start()


def shutdown() -> None:
    global _recorder, _path
    if _recorder is not None:
        _recorder.stop()
    _recorder = None
    _path = None


def enabled() -> bool:
    return _recorder is not None


def flush() -> None:
    if _recorder is not None:
        _recorder.flush()


def put(step: str, actor: str, **fields: Any) -> None:
    if _recorder is None:
        return
    from app.__version__ import VERSION

    event: dict[str, Any] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "step": step, "actor": actor, "version": VERSION,
    }
    event.update(fields)
    _recorder.put(event)


def default_path() -> Path:
    from app.db import db_file

    return db_file.with_name(FILE_NAME)


def current_path() -> Path:
    return _path or default_path()


def _json(raw: Any) -> Any:
    if not isinstance(raw, str) or not raw or raw[0] not in "{[":
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def query(
    sql: str, params: Iterable[Any] = (), *, path: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Читання. Файлу ще немає — порожньо, а не помилка: журнал необовʼязковий."""
    target = path or current_path()
    if not target.exists():
        return []
    with closing(connect(target, readonly=True)) as conn:
        rows = [dict(row) for row in conn.execute(sql, tuple(params))]
    for row in rows:
        for key in ("proposed", "chosen", "data"):
            if key in row:
                row[key] = _json(row[key])
    return rows


def prune(now: Optional[datetime] = None) -> int:
    if _path is None:
        return 0
    border = ((now or datetime.now()) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    with closing(connect(_path)) as conn, conn:
        conn.execute("DELETE FROM disc_choices WHERE at < ?", (border,))
        return conn.execute("DELETE FROM events WHERE at < ?", (border,)).rowcount


def snapshot(folder: Path, *, day: Optional[datetime] = None, keep: int = 14) -> Optional[Path]:
    """Щоденна копія (`VACUUM INTO`: WAL враховано). None — сьогоднішня вже є.

    Не `.db`: відновлення бази (`snapshot_tools`) шукає знімки CRM за цим
    розширенням по всій теці копій."""
    if _path is None or not _path.exists():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"decisions-{(day or datetime.now()).date().isoformat()}.sqlite"
    if target.exists():
        return None
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with closing(connect(_path)) as conn:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    tmp.replace(target)
    for old in sorted(folder.glob("decisions-*.sqlite"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Не вдалося прибрати стару копію журналу рішень %s", old.name)
    return target
