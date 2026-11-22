"""Забрати ПРОГРАМНИЙ файл Sum3D з робочого ПК на аналіз (власник 22.11.26).

Власник: «на робочому комп'ютері стоїть новіша версія… можу додати інструменти
розвідки, щоб ти міг аналізувати й копіювати файли з робочого ПК, але не
змінюючи їх без моєї згоди». Через MCP іде лише текст, а бібліотека Sum3D —
десятки мегабайт, тож інструмент `kmill_sum3d_fetch` видає ОДНОРАЗОВЕ посилання
на один файл, а самі байти віддає слухач MCP (порт 8011) окремим запитом.

Межі — усі тут, в одному місці:
  * файл лише з теки, вписаної в Налаштуваннях як тека Sum3D (`sum3d_explore.roots`),
    і лише за номером «№» з переліку розвідки — шлях у запиті не приймається;
  * лише програмні й конфігураційні розширення (`ALLOWED_EXT`); проєкти, диски,
    роботи й картинки (`.cam`, `.blk`, `.stl`, `.iso`…) — ніколи: там можуть бути
    імена пацієнтів;
  * жодної теки робіт у шляху (`sum3d_explore.SKIP_DIRS`);
  * посилання діє `TTL_SECONDS` і спрацьовує ОДИН раз; одночасно — не більше
    `MAX_LIVE`;
  * файл відкривається зі спільним доступом (`shared_read.open_shared`): Sum3D
    може ним користуватись, переписати й видалити, поки ми читаємо;
  * нічого не пише, не змінює й не видаляє — таких дій у модулі немає взагалі.
Кожна видача й кожне завантаження — рядок INFO в лозі CRM.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from sqlalchemy.orm import Session

from app.services import sum3d_explore
from app.shared_read import open_shared

logger = logging.getLogger(__name__)

ALLOWED_EXT = frozenset({
    ".exe", ".dll", ".ini", ".cfg", ".txt", ".arl", ".tab", ".par", ".mnu", ".xml", ".json",
    ".log", ".dat", ".lib", ".csv", ".eqp",
})
MAX_BYTES = 300 * 1024 * 1024
TTL_SECONDS = 300
MAX_LIVE = 4
CHUNK = 1024 * 1024
ROUTE = "/fetch/"


class FetchError(Exception):
    """Файл видати не можна; текст каже чому."""


@dataclass
class Ticket:
    path: str
    name: str
    size: int
    sha256: str
    expires: float


_lock = threading.Lock()
_tickets: dict[str, Ticket] = {}


def _inside(path: str, root: str) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    base = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([real, base]) == base and real != base
    except ValueError:  # різні диски
        return False


def check(db: Session, path: str) -> int:
    """Чи можна віддати цей файл; повертає розмір. Інакше `FetchError`."""
    roots = [root for _name, root in sum3d_explore.roots(db)]
    home = next((root for root in roots if _inside(path, root)), None)
    if home is None:
        raise FetchError("файл поза теками Sum3D з Налаштувань")
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(home))
    parts = [p.lower() for p in rel.replace("\\", "/").split("/")]
    if any(part in sum3d_explore.SKIP_DIRS for part in parts[:-1]):
        raise FetchError("файл у теці робіт — такі не віддаються")
    ext = os.path.splitext(parts[-1])[1]
    if ext not in ALLOWED_EXT:
        raise FetchError(f"розширення «{ext or 'без розширення'}» не віддається: лише програмні й "
                         "конфігураційні файли (" + ", ".join(sorted(ALLOWED_EXT)) + ")")
    try:
        if not os.path.isfile(path):
            raise FetchError("це не файл")
        size = os.path.getsize(path)
    except OSError as exc:
        raise FetchError(f"файл не читається: {exc.strerror or exc}") from exc
    if size > MAX_BYTES:
        raise FetchError(f"файл завеликий: {size // 1024**2} МБ (межа {MAX_BYTES // 1024**2} МБ)")
    return size


def _digest(path: str) -> str:
    sha = hashlib.sha256()
    with open_shared(path) as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def offer(db: Session, number: int, *, now: Optional[float] = None) -> dict[str, Any]:
    """Одноразове посилання на файл №`number` з останнього переліку розвідки."""
    now = time.time() if now is None else now
    try:
        path = sum3d_explore.listed_path(number)
    except sum3d_explore.ExploreError as exc:
        raise FetchError(str(exc)) from exc
    size = check(db, path)
    try:
        sha = _digest(path)
    except OSError as exc:
        raise FetchError(f"файл не читається: {exc.strerror or exc}") from exc
    key = secrets.token_urlsafe(32)
    name = os.path.basename(path)
    with _lock:
        for stale in [k for k, t in _tickets.items() if t.expires <= now]:
            del _tickets[stale]
        if len(_tickets) >= MAX_LIVE:
            raise FetchError(f"уже видано {MAX_LIVE} посилань — спершу завантаж їх або зачекай {TTL_SECONDS // 60} хв")
        _tickets[key] = Ticket(path=path, name=name, size=size, sha256=sha, expires=now + TTL_SECONDS)
    logger.info("Sum3D fetch: видано посилання на %s (%d байт), діє %d с", path, size, TTL_SECONDS)
    return {
        "файл": name,
        "байт": size,
        "sha256": sha,
        "посилання": f"{ROUTE}{key}",
        "діє_секунд": TTL_SECONDS,
        "як": "GET на слухач MCP (той самий хост і порт, що /mcp), без токена; спрацьовує один раз",
    }


def redeem(key: str, *, now: Optional[float] = None) -> Optional[Ticket]:
    """Квиток за ключем — один раз і лише поки діє; інакше None."""
    now = time.time() if now is None else now
    with _lock:
        ticket = _tickets.pop(key, None)
    if ticket is None or ticket.expires <= now:
        return None
    return ticket


def stream(ticket: Ticket) -> Iterator[bytes]:
    """Байти файлу шматками; рівно `ticket.size`, навіть якщо файл тим часом виріс."""
    left = ticket.size
    sent = 0
    try:
        with open_shared(ticket.path) as handle:
            while left > 0:
                block = handle.read(min(CHUNK, left))
                if not block:
                    break
                left -= len(block)
                sent += len(block)
                yield block
    finally:
        logger.info("Sum3D fetch: віддано %s — %d з %d байт", ticket.path, sent, ticket.size)


def forget_all() -> None:
    """Для тестів."""
    with _lock:
        _tickets.clear()
