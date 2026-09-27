"""Кеш екрана «Можливі дублікати» — рахунок у фоні, показ миттєвий.

Кандидати рахуються з двох квадратичних порівнянь (імена клієнтів + назви тек
export), і на цеховому ПК це 15+ секунд НА КОЖНЕ відкриття екрана (замір
27.09.26: `dup:folders 13.24с, dup:clients 2.38с` при 757 теках — прод рахує
у десятки разів повільніше за dev, причина з'ясовується окремо). Список же
змінюється рідко: нове ім'я в черзі, нова тека, рішення власника. Тому екран
віддає останній порахований знімок одразу, а перерахунок іде окремим потоком.

Той самий принцип, що кеш сканера export («протухле краще за очікування»),
але з двома відмінностями: перший знімок теж не рахується в запиті (холодний
екран каже «рахується» і сам перезавантажується), а рішення власника
(злити/не дублі) прибирає пару зі знімка ОДРАЗУ — інакше після кліку redirect
показав би її знову, і кнопка виглядала б мертвою до кінця перерахунку.

Сесію фоновий потік бере власну (`app.db.SessionLocal`) — правило пошти:
після таймауту чужу сесію з іншого потоку не чіпають, тож і не ділимось.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from sqlalchemy.orm import Session

from app.export_scanner import list_export_client_names_cached
from app.services import client_merge
from app.services import folder_merge
from app.settings_store import get_export_folder_path

logger = logging.getLogger(__name__)

#: Скільки живе знімок, перш ніж відкриття екрана запустить фоновий перерахунок.
#: Хвилини, не секунди: нове ім'я в черзі з'являється кілька разів на день, а
#: рішення власника оновлюють знімок і без перерахунку.
TTL_SECONDS = 300.0


@dataclass
class Snapshot:
    clients: list[client_merge.Candidate] = field(default_factory=list)
    folders: list[folder_merge.Candidate] = field(default_factory=list)
    folder_names: list[str] = field(default_factory=list)
    computed_at: float = 0.0  # time.monotonic()
    duration: float = 0.0


_lock = threading.Lock()
_snapshot: Snapshot | None = None
_refreshing = False


def compute(db: Session) -> Snapshot:
    """Порахувати знімок ЦІЄЮ сесією, зараз. Єдине місце самого рахунку —
    його кличуть і фоновий потік, і тести."""
    started = time.monotonic()
    clients = client_merge.find_candidates(db)
    try:
        folder_names = list_export_client_names_cached(Path(get_export_folder_path(db)))
    except OSError:
        folder_names = []
    folders = folder_merge.find_candidates(db, folder_names)
    return Snapshot(
        clients=clients,
        folders=folders,
        folder_names=sorted(folder_names),
        computed_at=time.monotonic(),
        duration=time.monotonic() - started,
    )


def refresh_now(db: Session) -> Snapshot:
    """Синхронний перерахунок + публікація. Для тестів і для фонового потоку."""
    snapshot = compute(db)
    with _lock:
        global _snapshot
        _snapshot = snapshot
    logger.info(
        "Можливі дублікати: %d пар клієнтів, %d пар тек, %d тек export, %.1fс",
        len(snapshot.clients), len(snapshot.folders),
        len(snapshot.folder_names), snapshot.duration,
    )
    return snapshot


def peek() -> Snapshot | None:
    """Останній знімок, без запуску чогось. None — ще ні разу не рахували."""
    with _lock:
        return _snapshot


def kick(reason: str) -> bool:
    """Запустити фоновий перерахунок, якщо він ще не йде. True — запустили."""
    global _refreshing
    with _lock:
        if _refreshing:
            return False
        _refreshing = True

    def _run() -> None:
        global _refreshing
        # Імпорт тут, а не нагорі: app.db тягне engine, і модульний імпорт
        # створював би його навіть у тестах, що підміняють фабрику сесій.
        from app.db import SessionLocal

        try:
            with SessionLocal() as db:
                refresh_now(db)
        except Exception:  # noqa: BLE001 — фоновий перерахунок не валить застосунок
            logger.exception("Перерахунок можливих дублікатів не вдався (%s)", reason)
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(
        target=_run, name="kmill-duplicates-refresh", daemon=True
    ).start()
    return True


def snapshot_for_screen() -> tuple[Snapshot | None, bool]:
    """(знімок, чи йде перерахунок) для відкриття екрана.

    Протухлий знімок віддається одразу — і тихо запускає перерахунок. Порожній
    (жодного разу не рахували) — теж запускає, а екран показує «рахується».
    """
    snapshot = peek()
    stale = snapshot is None or time.monotonic() - snapshot.computed_at > TTL_SECONDS
    if stale:
        kick("screen")
    with _lock:
        return snapshot, _refreshing


def drop_client_pair(name_a: str, name_b: str) -> None:
    """Прибрати пару імен зі знімка одразу після рішення власника."""
    pair = {name_a.strip(), name_b.strip()}
    with _lock:
        global _snapshot
        if _snapshot is None:
            return
        _snapshot = replace(
            _snapshot,
            clients=[
                c for c in _snapshot.clients if {c.a_name, c.b_name} != pair
            ],
        )


def drop_folder_pair(name_a: str, name_b: str) -> None:
    """Те саме для пари тек."""
    pair = {name_a.strip(), name_b.strip()}
    with _lock:
        global _snapshot
        if _snapshot is None:
            return
        _snapshot = replace(
            _snapshot,
            folders=[
                c for c in _snapshot.folders if {c.a_name, c.b_name} != pair
            ],
        )


def reset_for_tests() -> None:
    global _snapshot, _refreshing
    with _lock:
        _snapshot = None
        _refreshing = False
