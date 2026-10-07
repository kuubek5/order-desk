"""Перевірка, пошук і БЕЗПЕЧНЕ відновлення знімків бази (аварійне відновлення).

Чому окремий модуль і чому лише stdlib: відновлення потрібне саме тоді, коли
застосунок не стартує — база зіпсована, ПК новий, ключ `master.key` втрачено. Тож
нічого з `app.config`/`app.db`/шифрування сюди не імпортується: інструмент не
залежить ні від ключів, ні від стану самої бази.

Що виявило навчання DR 07.10.26 (scripts/dr_drill.py):
* «відновлення» підміною лише `.db`, коли поруч лежить чужий `-wal`, дає
  «database disk image is malformed»: SQLite не звіряє WAL з файлом бази і
  накочує чужі кадри на знімок. Тому відновлення завжди відкладає убік і `.db`,
  і `-wal`, і `-shm`;
* зіпсований файл у знімках мовчки вважався знімком — тепер кожен перевіряється
  `PRAGMA quick_check` (`immutable=1`, без побічних файлів, як у дзеркалі).

Схему міграцій цей модуль НЕ чіпає: наступний звичайний старт (`ensure_schema`)
сам дожене базу до поточної версії, зробивши копію перед міграцією.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Файли, що лежать у backups/, але знімками бази не є.
_SKIP_MARKERS = (".before-restore-", ".failed-", ".restore-tmp", ".tmp")


class SnapshotError(Exception):
    """Знімок не можна використати або відновлення не можна почати. Текст —
    українською, для людини за комп'ютером, а не для логу."""


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    ok: bool
    detail: str
    orders: int | None
    revision: str | None
    modified: datetime
    size: int

    def label(self) -> str:
        when = self.modified.strftime("%d.%m.%y %H:%M")
        state = f"{self.orders} робіт" if self.ok else f"ПОШКОДЖЕНО ({self.detail})"
        return f"{when}  {self.path.name}  — {state}"


def _open_immutable(path: Path) -> sqlite3.Connection:
    # immutable=1: SQLite читає файл напряму, без блокувань і БЕЗ супутніх
    # -wal/-shm (вони лишились би поруч знімка й стали б пасткою відновлення).
    return sqlite3.connect(f"file:{path}?immutable=1", uri=True)


def inspect_snapshot(path: Path) -> SnapshotInfo:
    """Прочитати знімок, не змінюючи його. Не кидає: збій = `ok=False`."""
    path = Path(path)
    try:
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime)
        size = stat.st_size
    except OSError as exc:
        return SnapshotInfo(path, False, f"недоступний: {exc}", None, None, datetime.fromtimestamp(0), 0)
    if size == 0:
        return SnapshotInfo(path, False, "порожній файл", None, None, modified, 0)
    try:
        con = _open_immutable(path)
        try:
            quick = con.execute("PRAGMA quick_check").fetchone()
            if not quick or quick[0] != "ok":
                return SnapshotInfo(path, False, f"quick_check: {str(quick)[:80]}", None, None, modified, size)
            names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            if "orders" not in names:
                return SnapshotInfo(path, False, "це не база KuubMill (немає таблиці orders)", None, None, modified, size)
            orders = con.execute("select count(*) from orders").fetchone()[0]
            revision = None
            if "alembic_version" in names:
                row = con.execute("select version_num from alembic_version").fetchone()
                revision = row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error as exc:
        return SnapshotInfo(path, False, str(exc)[:80], None, None, modified, size)
    return SnapshotInfo(path, True, "ok", orders, revision, modified, size)


_DAMAGE_MARKERS = ("malformed", "not a database", "disk image", "encrypted")


def database_state(db_file: Path) -> tuple[str, str]:
    """Стан РОБОЧОЇ бази при старті: `(стан, деталі)`.

    * `ok` — читається, `quick_check` чистий;
    * `missing` — файла нема, він порожній або в ньому жодної таблиці;
    * `corrupt` — не база, обрізана, биті сторінки;
    * `unknown` — не вдалось зʼясувати (заблокована, немає прав). Такий стан
      НІКОЛИ не привід для відновлення: помилковий «зіпсована» затер би живу базу.

    Викликається на старті, коли застосунок не працює і нічого не пише, тож звичайне
    відкриття (з WAL) безпечне: на відміну від знімка, живу базу `immutable` читати не можна —
    вона проігнорувала б свіжі транзакції з `-wal`.
    """
    db_file = Path(db_file)
    try:
        if not db_file.is_file() or db_file.stat().st_size == 0:
            return "missing", "файла нема або він порожній"
    except OSError as exc:
        return "unknown", str(exc)[:80]
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=3)
        try:
            tables = con.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
            if tables == 0:
                return "missing", "у файлі жодної таблиці"
            quick = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        text = str(exc).lower()
        if any(m in text for m in _DAMAGE_MARKERS):
            return "corrupt", str(exc)[:100]
        return "unknown", str(exc)[:100]  # locked / unable to open: не доказ пошкодження
    except sqlite3.Error as exc:
        return "unknown", str(exc)[:100]
    if not quick or quick[0] != "ok":
        return "corrupt", f"quick_check: {str(quick)[:100]}"
    return "ok", "ok"


def verify_snapshot(path: Path) -> None:
    """Для ВІДНОВЛЕННЯ: кидає `SnapshotError`, якщо знімок не можна використати
    (не читається, не проходить цілісність, чи це взагалі не база KuubMill)."""
    info = inspect_snapshot(path)
    if not info.ok:
        raise SnapshotError(f"Знімок {Path(path).name} непридатний: {info.detail}")


def verify_integrity(path: Path) -> None:
    """Для СТВОРЕННЯ знімка: лише цілісність (`quick_check`), без вимоги «наша
    база» — знімок щойно знято з нашої ж бази, а тести роблять міні-бази."""
    path = Path(path)
    try:
        con = _open_immutable(path)
        try:
            quick = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise SnapshotError(f"Знімок {path.name} не читається: {exc}") from exc
    if not quick or quick[0] != "ok":
        raise SnapshotError(f"Знімок {path.name} не проходить перевірку цілісності: {str(quick)[:80]}")


def discover_snapshots(data_dir: Path, extra_dirs: Iterable[Path] = ()) -> list[SnapshotInfo]:
    """Усі знімки бази: `<data_dir>/backups/**.db` і додаткові теки (дзеркало на
    іншому диску, флешка). Найновіші першими; пошкоджені теж показуємо — людина
    має бачити, що вони є, але не обирати їх."""
    roots = [Path(data_dir) / "backups", *[Path(d) for d in extra_dirs]]
    found: dict[Path, SnapshotInfo] = {}
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for p in root.rglob("*.db"):
                if any(m in p.name for m in _SKIP_MARKERS) or not p.is_file():
                    continue
                found[p.resolve()] = inspect_snapshot(p)
        except OSError:
            logger.warning("Не вдалося прочитати теку знімків %s", root)
    return sorted(found.values(), key=lambda i: i.modified, reverse=True)


def newest_valid(snapshots: Iterable[SnapshotInfo]) -> SnapshotInfo | None:
    return next((s for s in snapshots if s.ok), None)


def app_is_running(port: int = 8000) -> bool:
    """Хтось слухає порт застосунку (на петлі) — отже база відкрита."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def restore_from_snapshot(
    db_file: Path,
    snapshot: Path,
    *,
    running: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> dict:
    """Замінити робочу базу знімком, нічого не знищуючи.

    Порядок: перевірити знімок → переконатись, що застосунок не працює →
    скопіювати знімок поруч (тимчасовий файл) і перевірити копію → відкласти
    убік `.db`, `-wal`, `-shm` (`*.before-restore-<мітка>`) → поставити копію на
    місце. Збій на будь-якому кроці повертає все, як було.
    """
    db_file, snapshot = Path(db_file), Path(snapshot)
    verify_snapshot(snapshot)
    if (running or app_is_running)():
        raise SnapshotError("KuubMill зараз працює. Закрийте програму (значок у треї → Вийти) і повторіть.")

    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    db_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_file.with_name(db_file.name + ".restore-tmp")
    tmp.unlink(missing_ok=True)
    try:
        shutil.copyfile(snapshot, tmp)
        verify_snapshot(tmp)  # копія — теж файл, і шара могла його обрізати
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    suffixes = ("", "-wal", "-shm")
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in suffixes:
            src = Path(str(db_file) + suffix)
            if src.exists():
                dst = Path(str(db_file) + suffix + f".before-restore-{stamp}")
                os.replace(src, dst)
                moved.append((src, dst))
        os.replace(tmp, db_file)
    except Exception as exc:
        for src, dst in reversed(moved):  # відкат: усе на свої місця
            try:
                os.replace(dst, src)
            except OSError:
                logger.exception("Не вдалося повернути %s", src)
        tmp.unlink(missing_ok=True)
        raise SnapshotError(f"Не вдалося замінити базу ({exc}). Чи не відкрито файл іншою програмою?") from exc

    info = inspect_snapshot(db_file)
    return {
        "restored_from": str(snapshot),
        "orders": info.orders,
        "revision": info.revision,
        "kept_aside": [str(dst) for _, dst in moved],
        "ok": info.ok,
    }
