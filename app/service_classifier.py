"""Classify an email's free text into a service type for the triage badge.

The lab mills; it does not 3D-print (CLAUDE.md §1). A letter that sounds like a
3D-printing request is flagged «перевірити» so the operator gives it a second
look instead of the default «розпізнано». That is the ONLY thing this signals —
it never hides or drops a letter from triage (a false positive costs one glance;
a silently-missing letter is not acceptable).

Same shape as app/material_classifier.py: the service-type CATEGORIES are code,
the raw spellings that map to them are DATA (seeded into ServiceKeyword,
extended at runtime from /settings/recognition without a code change). This
module holds the seed and the pure matcher; the DB table lives in models.py and
the DB bridge in app/service_catalog.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

# Service-type category. Only one non-default type today; kept as a named
# constant so adding more later (e.g. a service the lab explicitly rejects) is a
# data/seed change, not a rewrite.
THREE_D_PRINT = "3d_print"

# match_type: "token" = exact whitespace token, "contains" = substring.
# Seeded from the spellings clients actually use (Ukrainian, surzhyk, English).
# Phrases are stored as substrings; short/ambiguous tokens use "token" so they
# match a whole word, not a fragment of an unrelated one.
SEED_SERVICE_KEYWORDS: dict[str, list[tuple[str, str]]] = {
    THREE_D_PRINT: [
        ("3d друк", "contains"), ("3д друк", "contains"),
        ("3d-друк", "contains"), ("3д-друк", "contains"),
        ("3d принтер", "contains"), ("3д принтер", "contains"),
        ("друк моделі", "contains"), ("друк модел", "contains"),
        ("надрукувати", "contains"), ("роздрукувати", "contains"),
        ("надрукуйте", "contains"),
        ("3d print", "contains"), ("3d-print", "contains"),
        ("3d printing", "contains"), ("print model", "contains"),
        ("resin print", "contains"), ("resin", "token"),
        ("sla", "token"), ("dlp", "token"),
    ],
}

_MIN_TOKEN_LEN = 2


@dataclass(frozen=True)
class ServiceKeywordRow:
    pattern: str
    match_type: str  # "token" | "contains"
    service_type: str


def seed_service_rows() -> list[ServiceKeywordRow]:
    """The seed keywords flattened — used to seed the DB and as the default
    ruleset in tests/callers before any runtime additions."""
    rows: list[ServiceKeywordRow] = []
    for service_type, keywords in SEED_SERVICE_KEYWORDS.items():
        for pattern, match_type in keywords:
            rows.append(
                ServiceKeywordRow(pattern=pattern, match_type=match_type, service_type=service_type)
            )
    return rows


def normalize_service(raw: str | None) -> str:
    """Lowercase, NFC, collapse whitespace — so "3D  Друк" and "3d друк" match
    the same keyword. Kept deliberately close to normalize_material."""
    if not raw:
        return ""
    s = unicodedata.normalize("NFC", raw).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def classify_service(raw: str | None, rows: list[ServiceKeywordRow] | None = None) -> str | None:
    """Return the service_type whose keyword matches `raw`, or None for no
    signal (which callers treat as "assume milling", the mailbox's real
    business). Pure: `rows` defaults to the seed so it works without a DB.

    If a message hits keywords for more than one service_type this still returns
    a match (the first by iteration) — a mixed letter deserves the flag, and an
    operator double-checking it is cheap; only a total absence of any keyword is
    treated as the plain milling default."""
    normalized = normalize_service(raw)
    if not normalized:
        return None
    the_rows = rows if rows is not None else seed_service_rows()
    tokens = normalized.split()
    for row in the_rows:
        if row.match_type == "token":
            if len(row.pattern) >= _MIN_TOKEN_LEN and row.pattern in tokens:
                return row.service_type
        else:  # contains
            if row.pattern and row.pattern in normalized:
                return row.service_type
    return None
