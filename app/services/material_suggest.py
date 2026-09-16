"""Material typing suggestions for the manual «Додати роботу» form.

Two sources, one list (owner decision 15.09.26 — a shortcut is just a very
short query into the same suggestion list, not a second mechanism):

  * shortcuts — admin-entered `мл → mono`; the material WORD only, colour typed
    as-is (material_catalog.add_shortcut). A unique shortcut can Tab-expand.
  * frecency — the distinct past `material_color` spellings, canonicalised into
    clusters (match_key folds Cyrillic/Latin so `моно а3` and `mono a3` are one
    cluster) and ranked by frequency×recency, so the daily material rises to the
    top and last July's one-off sinks.

The point the operator actually sees is the RECOGNITION BADGE: a spelling the
classifier can't place (material_id NULL) shows no badge, catching a typo before
it becomes a row without a material stripe.

Raw past values are NOT suggested as-is — that would let the operator pick
blindly between two spellings of one material and tomorrow there'd be a third.
The representative of each cluster is its most-frequent spelling.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.material_catalog import (
    MaterialCatalogError,
    add_shortcut,
    ensure_seeded,
    list_shortcuts,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.material_classifier import match_key, match_keys
from app.models import Material, Order

# Short badge per material category. Kept here (a display concern) rather than in
# the catalog: unknown or non-production names get no badge, which is the same
# "not recognised" signal as material_id NULL.
_BADGE_BY_NAME = {
    "Цирконій": "Zr",
    "ПММА": "PMMA",
    "СЛМ": "SLM",
    "Титан": "Ti",
    "Віск": "Wax",
}

_FRECENCY_WINDOW_DAYS = 90
_RECENT_WINDOW_DAYS = 30
_CACHE_TTL_SECONDS = 300
_DEFAULT_LIMIT = 8


@dataclass
class Suggestion:
    kind: str  # "shortcut" | "frecency"
    text: str  # canonical spelling to insert into the field
    badge: str | None  # "Zr" / "PMMA" / … or None when unrecognised
    material_id: int | None
    shortcut: str | None = None
    count: int = 0  # works in the frecency window — for «445 робіт»
    is_expand: bool = False  # unique shortcut match → Tab expands


@dataclass
class _Cluster:
    key: str
    c30: int = 0
    c90: int = 0
    spellings: Counter[str] = field(default_factory=Counter)
    mid_votes: Counter[int] = field(default_factory=Counter)


@dataclass
class _Entry:
    key: str
    text: str
    c30: int
    c90: int
    material_id: int | None
    badge: str | None


_cache_lock = threading.Lock()
_cache_entries: list[_Entry] = []
_cache_built_at: float = 0.0


def badge_for_name(name: str | None) -> str | None:
    return _BADGE_BY_NAME.get(name or "")


def _build_frecency(session: Session) -> list[_Entry]:
    """Cluster the last 90 days of colour values into frecency entries. One query,
    grouped in Python because match_key folding can't run in SQL."""
    now = utc_now()
    cutoff90 = now - timedelta(days=_FRECENCY_WINDOW_DAYS)
    cutoff30 = now - timedelta(days=_RECENT_WINDOW_DAYS)
    rows = session.execute(
        select(Order.material_color, Order.material_id, Order.created_at).where(
            Order.created_at >= cutoff90,
            Order.material_color.isnot(None),
        )
    ).all()

    clusters: dict[str, _Cluster] = {}
    for color, mid, created in rows:
        text = (color or "").strip()
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
        if mid is not None:
            cluster.mid_votes[mid] += 1

    name_by_id = {mid: name for mid, name in session.execute(select(Material.id, Material.name)).all()}
    entries: list[_Entry] = []
    for cluster in clusters.values():
        text = cluster.spellings.most_common(1)[0][0]
        mid = cluster.mid_votes.most_common(1)[0][0] if cluster.mid_votes else None
        entries.append(
            _Entry(
                key=cluster.key,
                text=text,
                c30=cluster.c30,
                c90=cluster.c90,
                material_id=mid,
                badge=badge_for_name(name_by_id.get(mid)) if mid is not None else None,
            )
        )
    return entries


def _frecency_entries(session: Session) -> list[_Entry]:
    """Cached frecency index. Suggestions are advisory and the data changes
    slowly, so a 5-minute TTL keeps per-keystroke calls off the whole table."""
    global _cache_entries, _cache_built_at
    now = utc_now().timestamp()
    with _cache_lock:
        if now - _cache_built_at <= _CACHE_TTL_SECONDS and _cache_built_at:
            return _cache_entries
    entries = _build_frecency(session)
    with _cache_lock:
        _cache_entries = entries
        _cache_built_at = now
    return entries


def invalidate_cache() -> None:
    """Drop the frecency cache (tests, or after a large reclassification)."""
    global _cache_built_at
    with _cache_lock:
        _cache_built_at = 0.0


def usage_for_expansion(session: Session, expansion: str) -> int:
    """Works in the frecency window whose colour starts with this expansion word
    — the «вжито» column that tells the admin which spelling earns the shortest
    shortcut."""
    ek = match_key(expansion)
    if not ek:
        return 0
    return sum(e.c90 for e in _frecency_entries(session) if e.key.startswith(ek))


#: Написання, що трапилось у базі рідше за це, вважаємо разовим/одруківкою й
#: авто-скорочення на нього не заводимо — інакше 309 написань дали б стіну
#: криптичних кодів. Власник дозаводить рідкісні руками.
_AUTOFILL_MIN_COUNT = 3


def _autoshort_candidates(expansion: str) -> list[str]:
    """Кандидати на авто-скорочення: перша літера слова матеріалу + колір без
    пробілів і розділювачів (`mono a3.5` → `ma35`), з розширенням початку слова
    при колізії (`mo…`, `mon…`)."""
    tokens = expansion.split()
    if not tokens:
        return []
    head = tokens[0]
    color = re.sub(r"[^0-9a-zа-яіїєґ]", "", "".join(tokens[1:]).lower())
    return [head[:n].lower() + color for n in (1, 2, 3) if len(head) >= n]


def autofill_shortcuts(
    session: Session, *, min_count: int = _AUTOFILL_MIN_COUNT
) -> tuple[int, int]:
    """Завести скорочення на всі розпізнані написання з бази (mono a1/a2/a3…,
    emo…, pmma…). Авто, не руками (власник 16.09.26). Пропускає нерозпізнане й
    нематеріал (без бейджа), уже наявні написання й колізії скорочень; на
    останні віддає розширений код або пропускає. Повертає (додано, пропущено).

    Скидає frecency-кеш кличе викликач — «вжито» на екрані має врахувати нове."""
    rows = list_shortcuts(session)
    used_keys = {row.key for row in rows}
    have_expansion = {match_key(row.expansion) for row in rows}

    added = skipped = 0
    for entry in sorted(_frecency_entries(session), key=lambda e: -e.c90):
        if entry.badge is None:  # нерозпізнане або «Не матеріал» — не скорочуємо
            continue
        if entry.c90 < min_count:
            continue
        if match_key(entry.text) in have_expansion:
            continue
        placed = False
        for cand in _autoshort_candidates(entry.text):
            key = match_key(cand)
            if not key or key in used_keys:
                continue
            try:
                add_shortcut(session, cand, entry.text)
            except MaterialCatalogError:
                continue
            used_keys.add(key)
            have_expansion.add(match_key(entry.text))
            added += 1
            placed = True
            break
        if not placed:
            skipped += 1
    return added, skipped


def suggest_materials(
    session: Session, q: str, *, limit: int = _DEFAULT_LIMIT
) -> list[Suggestion]:
    """Rank shortcut expansions and frecency spellings for query `q`."""
    # Кілька ключів запиту: гомогліфний + розкладко-своп (забута розкладка,
    # `ьщтщ ф3` → `mono a3`). Збіг за БУДЬ-ЯКИМ.
    qkeys = match_keys(q)
    if not qkeys:
        return []

    ensure_seeded(session)
    alias_rows = load_alias_rows(session)
    name_to_id = material_id_by_name(session)
    id_to_name = {mid: name for name, mid in name_to_id.items()}

    # ── Shortcuts ──────────────────────────────────────────────────────────
    # Match when the query is a prefix of the shortcut (still typing it) or the
    # shortcut is a prefix of the query (already typed it, colour follows). Tab
    # may expand ONLY when exactly one shortcut matches — an ambiguous shortcut
    # must never rewrite the field under the operator's hand.
    #
    # A shortcut expands ONLY the first word (the material), so once the operator
    # has already typed that word out in full, the shortcut is a no-op — it would
    # replace the word with itself and just re-state what's on screen. Drop it
    # then: after «emo a2» the operator wants the frecency row «emo a2», not a
    # first suggestion offering the bare material «emo» (owner 16.09.26). The
    # test is on the RAW first token, not the fold key, so a Cyrillic shortcut
    # typed toward a Latin canon (`емо` → `emo`) still counts as a real change.
    first_token = q.strip().split()[0] if q.strip() else ""
    shortcut_rows = list_shortcuts(session)
    shortcut_matches = [
        row
        for row in shortcut_rows
        if any(row.key.startswith(k) or k.startswith(row.key) for k in qkeys)
        and row.expansion.strip().casefold() != first_token.casefold()
    ]
    unique_expand = len(shortcut_matches) == 1

    suggestions: list[Suggestion] = []
    seen: set[str] = set()
    for row in shortcut_matches:
        text_key = match_key(row.expansion)
        mid = resolve_material_id(row.expansion, alias_rows, name_to_id)
        suggestions.append(
            Suggestion(
                kind="shortcut",
                text=row.expansion,
                badge=badge_for_name(id_to_name.get(mid)) if mid is not None else None,
                material_id=mid,
                shortcut=row.shortcut,
                is_expand=unique_expand,
            )
        )
        seen.add(text_key)

    # ── Frecency ───────────────────────────────────────────────────────────
    scored: list[tuple[float, _Entry]] = []
    for entry in _frecency_entries(session):
        if not any(k in entry.key for k in qkeys):
            continue
        prefix_bonus = 5 if any(entry.key.startswith(k) for k in qkeys) else 0
        score = entry.c90 + 2 * entry.c30 + prefix_bonus
        scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].text))

    for _score, entry in scored:
        if len(suggestions) >= limit:
            break
        if entry.key in seen:
            continue
        seen.add(entry.key)
        suggestions.append(
            Suggestion(
                kind="frecency",
                text=entry.text,
                badge=entry.badge,
                material_id=entry.material_id,
                count=entry.c90,
            )
        )

    return suggestions[:limit]
