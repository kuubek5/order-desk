"""Read-time fuzzy matching between Client.canonical_name and Order.client_name.

Deliberately not a stored `client_id` FK on Order (see the migration
0004_add_client_table docstring and PR description for the full reasoning):
Order.client_name is free text populated by three independent write paths
already working in production (app/sync.py's sheet import, the mail-order
accept flow in app/web.py, and sheet-side manual entry synced in) — the
same value can legitimately show up as "Вова", "вова", "Вова " across
different orders for the same person. Normalizing all of that into a hard
relational link would touch every one of those write paths for a marginal
gain right now, so instead a Client's orders are found at request time.

This reuses the same rapidfuzz-based approach and threshold philosophy as
app.client_matcher.match_client_name (sheet-name vs export-folder-name
matching for the handout screen), but that function is shaped for
one-name-against-many-folder-candidates with ambiguity detection — a
different problem shape from "does this one Order.client_name belong to
this one Client", so this module has its own small, focused helper rather
than reusing match_client_name literally.

At this project's real scale (~20-40 orders/day per CLAUDE.md section 6),
matching over the full Order table on every request is completely fine —
no caching or indexing needed for this.
"""

import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz

from app.models import Order

# Same auto-match threshold app.client_matcher.match_client_name uses for a
# confident "same name, different capitalization/whitespace/typo" match —
# not a loose semantic match across genuinely different names.
MATCH_THRESHOLD = 90.0


def _normalize(name: str) -> str:
    """NFC-normalize, strip, and lowercase — see app.client_matcher for why
    NFC matters (visually-identical Cyrillic can arrive in different
    composed forms depending on the source app/OS)."""
    return unicodedata.normalize("NFC", name.strip().lower())


def find_matching_orders(
    canonical_name: str,
    orders: list[Order],
    threshold: float = MATCH_THRESHOLD,
) -> list[Order]:
    """Return the subset of orders whose client_name fuzzy-matches canonical_name.

    Orders with no client_name are always skipped. An exact match after
    normalization (case/whitespace/Unicode form) always counts, regardless
    of the threshold value passed in.
    """
    if not canonical_name or not canonical_name.strip():
        return []

    normalized_target = _normalize(canonical_name)
    matches: list[Order] = []
    for order in orders:
        if not order.client_name:
            continue
        normalized_candidate = _normalize(order.client_name)
        if normalized_candidate == normalized_target:
            matches.append(order)
            continue
        if fuzz.ratio(normalized_target, normalized_candidate) >= threshold:
            matches.append(order)
    return matches


@dataclass
class ClientOrderSummary:
    """Aggregated view of a client's matched orders, for the client card."""

    total_count: int
    material_breakdown: list[tuple[str, int]] = field(default_factory=list)
    """(material_color, count) pairs, most common first."""
    last_order_date: datetime | None = None
    recent_orders: list[Order] = field(default_factory=list)
    """Most recent orders first, capped at recent_limit for display."""


def summarize_client_orders(orders: list[Order], recent_limit: int = 10) -> ClientOrderSummary:
    """Pure aggregation over an already-matched list of orders — no DB access."""
    ordered_by_recency = sorted(orders, key=lambda order: order.created_at, reverse=True)
    material_counts = Counter(order.material_color for order in orders if order.material_color)

    return ClientOrderSummary(
        total_count=len(orders),
        material_breakdown=material_counts.most_common(),
        last_order_date=ordered_by_recency[0].created_at if ordered_by_recency else None,
        recent_orders=ordered_by_recency[:recent_limit],
    )
