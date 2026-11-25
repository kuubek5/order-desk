"""Мовчазна пара до показаної підказки диска: що сказав би ІНШИЙ порядок.

Картка показала диски за правилами — тут рахується вивчений порядок, і навпаки.
Відповідь лягає в запам'ятоване «що бачив оператор» (`selection_journal`), а після
фрезерування видно, хто вгадав. Оператор нічого не чекає: рахує один фоновий потік
після відповіді картки, зайве просто пропускається.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from app.services import disc_suggest

logger = logging.getLogger(__name__)

MAX_WAITING = 3
"""Більше в черзі — пропускаємо: підказка важлива, статистика про неї — ні."""
INLINE = False
"""Для тестів: рахувати одразу."""

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kuubmill-disc-shadow")
_waiting = 0
_lock = threading.Lock()


def submit(entry: dict[str, Any], material: str, attachments: Iterable[Any], blanks_root: Optional[str], *,
           active: dict[str, datetime], recent: set[str],
           chooser: Optional[disc_suggest.Chooser]) -> None:
    """`entry` — запис `selection_journal.remember_shown`; пара ляже в його `hint`."""
    global _waiting
    # Рядки бази живуть лише до кінця запиту — беремо з них саме те, що треба.
    files = [SimpleNamespace(filename=a.filename, saved_path=a.saved_path) for a in attachments]

    def work() -> None:
        global _waiting
        try:
            other = disc_suggest.suggest(material, files, blanks_root, active=active, recent=recent,
                                         chooser=chooser)
            entry["hint"]["rival"] = {
                "by": other.by,
                "discs": [p.name for p in ([other.best] if other.best else []) + other.alts],
            }
        except Exception:
            logger.exception("Мовчазна підказка диска не порахувалась")
        finally:
            with _lock:
                _waiting -= 1

    with _lock:
        if _waiting >= MAX_WAITING:
            return
        _waiting += 1
    if INLINE:
        work()
        return
    try:
        _pool.submit(work)
    except RuntimeError:  # застосунок зупиняється
        with _lock:
            _waiting -= 1
