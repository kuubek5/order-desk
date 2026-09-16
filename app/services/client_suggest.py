"""Client name suggestions for the manual «Додати роботу» form.

Same combobox as material suggestions, but clients are an OPEN list with a long
tail (≈326 distinct names), so there's no closed dictionary and no shortcuts —
just incremental frecency search over the names already in the queue. match_keys
gives the same cross-alphabet and forgotten-layout tolerance the material field
has, so `крив`, `rhbd` (Latin layout) and `Крив` all reach «Кривовид».

No category badge here — a client has no material class; the fragment hides the
badge column when field="client".
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.material_classifier import match_key, match_keys
from app.models import Order
from app.services.material_suggest import Suggestion

_FRECENCY_WINDOW_DAYS = 90
_RECENT_WINDOW_DAYS = 30
_CACHE_TTL_SECONDS = 300
_DEFAULT_LIMIT = 8


@dataclass
class _Cluster:
    key: str
    c30: int = 0
    c90: int = 0
    spellings: Counter[str] = field(default_factory=Counter)


@dataclass
class _Entry:
    key: str
    text: str
    c30: int
    c90: int


_cache_lock = threading.Lock()
_cache_entries: list[_Entry] = []
_cache_built_at: float = 0.0


def _build(session: Session) -> list[_Entry]:
    now = utc_now()
    cutoff90 = now - timedelta(days=_FRECENCY_WINDOW_DAYS)
    cutoff30 = now - timedelta(days=_RECENT_WINDOW_DAYS)
    rows = session.execute(
        select(Order.client_name, Order.created_at).where(
            Order.created_at >= cutoff90,
            Order.client_name.isnot(None),
        )
    ).all()

    clusters: dict[str, _Cluster] = {}
    for name, created in rows:
        text = (name or "").strip()
        if not text:
            continue
        key = match_key(text)
        if not key:
            continue
        cluster = clusters.get(key)
        if cluster is None:
            cluster = clusters[key] = _Cluster(key=key)
        cluster.c90 += 1
        if created is not None and created >= cutoff30:
            cluster.c30 += 1
        cluster.spellings[text] += 1

    return [
        _Entry(
            key=cluster.key,
            text=cluster.spellings.most_common(1)[0][0],
            c30=cluster.c30,
            c90=cluster.c90,
        )
        for cluster in clusters.values()
    ]


def _entries(session: Session) -> list[_Entry]:
    global _cache_entries, _cache_built_at
    now = utc_now().timestamp()
    with _cache_lock:
        if now - _cache_built_at <= _CACHE_TTL_SECONDS and _cache_built_at:
            return _cache_entries
    entries = _build(session)
    with _cache_lock:
        _cache_entries = entries
        _cache_built_at = now
    return entries


def invalidate_cache() -> None:
    global _cache_built_at
    with _cache_lock:
        _cache_built_at = 0.0


def suggest_clients(
    session: Session, q: str, *, limit: int = _DEFAULT_LIMIT
) -> list[Suggestion]:
    """Frecency-ranked client names matching `q` (substring on the fold/layout
    keys). Long tail → incremental search: the daily client rises, last July's
    one-off sinks."""
    qkeys = match_keys(q)
    if not qkeys:
        return []

    scored: list[tuple[float, _Entry]] = []
    for entry in _entries(session):
        if not any(k in entry.key for k in qkeys):
            continue
        prefix_bonus = 5 if any(entry.key.startswith(k) for k in qkeys) else 0
        scored.append((entry.c90 + 2 * entry.c30 + prefix_bonus, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].text))

    return [
        Suggestion(kind="frecency", text=entry.text, badge=None, material_id=None, count=entry.c90)
        for _score, entry in scored[:limit]
    ]
