"""Fuzzy matching of client names from sheet to export folder names.

This module provides client name matching with a confirmed-alias dictionary,
using rapidfuzz for fuzzy matching when exact matches are not found.
"""

import unicodedata
from dataclasses import dataclass
from rapidfuzz import fuzz


@dataclass
class MatchResult:
    """Result of fuzzy matching a sheet client name to folder names."""

    sheet_name: str
    """The client name as it appears in the sheet/database."""

    matched_folder_name: str | None
    """Best matching folder name from the list, or None if no confident match."""

    confidence: float
    """Fuzzy score of the best match (0-100), or 100 if from a confirmed alias."""

    is_confirmed_alias: bool
    """True if this came from a known confirmed ClientNameAlias, not a fuzzy guess."""

    candidates: list[tuple[str, float]]
    """Top up-to-3 candidate folder names with their scores, sorted best-first.

    Populated even when there is a clear winner, so a UI can show alternatives.
    Empty list if folder_names was empty.
    """


def match_client_name(
    sheet_name: str,
    folder_names: list[str],
    known_aliases: dict[str, str],
    auto_match_threshold: float = 90.0,
    ambiguous_margin: float = 5.0,
) -> MatchResult:
    """Match a sheet client name against a list of folder names.

    Performs exact lookup in known_aliases first (DB-confirmed matches),
    then falls back to fuzzy matching with threshold and ambiguity detection.

    Args:
        sheet_name: Client name from sheet/database to match.
        folder_names: List of export folder names to match against.
        known_aliases: Dict of {sheet_name: export_folder_name} for confirmed matches.
                       Caller is responsible for loading from DB (confirmed=True).
        auto_match_threshold: Minimum fuzzy score (0-100) to auto-match.
        ambiguous_margin: Minimum score difference between 1st and 2nd candidate
                          to avoid ambiguity. If top two are within this margin,
                          matched_folder_name is set to None (needs human review).

    Returns:
        MatchResult with matched_folder_name, confidence, is_confirmed_alias flag,
        and top candidates.
    """

    # Fast path: exact match in confirmed aliases
    if sheet_name in known_aliases:
        matched = known_aliases[sheet_name]
        return MatchResult(
            sheet_name=sheet_name,
            matched_folder_name=matched,
            confidence=100.0,
            is_confirmed_alias=True,
            candidates=[(matched, 100.0)],
        )

    # Empty folder list: return zero result
    if not folder_names:
        return MatchResult(
            sheet_name=sheet_name,
            matched_folder_name=None,
            confidence=0.0,
            is_confirmed_alias=False,
            candidates=[],
        )

    # Fuzzy match: normalize whitespace/case/Unicode form for comparison, keep original names.
    # NFC normalization matters because visually-identical Cyrillic text can arrive in
    # different composed forms (e.g. precomposed "й" U+0439 vs decomposed "и"+combining
    # breve U+0438 U+0306) depending on the source app/OS that produced the sheet or the
    # export folder name. Without normalizing first, rapidfuzz scores these as very
    # different strings even though they render identically, which can push a genuine
    # exact match below auto_match_threshold.
    sheet_normalized = unicodedata.normalize("NFC", sheet_name.strip().lower())

    # Score each folder name
    scores: list[tuple[str, float]] = []
    for folder_name in folder_names:
        folder_normalized = unicodedata.normalize("NFC", folder_name.strip().lower())
        score = fuzz.ratio(sheet_normalized, folder_normalized)
        scores.append((folder_name, float(score)))

    # Sort by score (descending)
    scores.sort(key=lambda x: x[1], reverse=True)

    # Get top 3 candidates
    top_candidates = scores[:3]

    # Determine if we have a confident match
    if not top_candidates:
        # Shouldn't happen if folder_names is non-empty, but be safe
        return MatchResult(
            sheet_name=sheet_name,
            matched_folder_name=None,
            confidence=0.0,
            is_confirmed_alias=False,
            candidates=[],
        )

    best_name, best_score = top_candidates[0]

    # Check auto-match conditions:
    # 1. Best score must be >= threshold
    # 2. Must beat second candidate by >= margin (if second exists)
    is_auto_match = (
        best_score >= auto_match_threshold
        and (
            len(top_candidates) < 2
            or (best_score - top_candidates[1][1]) >= ambiguous_margin
        )
    )

    if is_auto_match:
        matched_folder_name = best_name
    else:
        # Ambiguous or below threshold: return None and let caller choose
        matched_folder_name = None

    return MatchResult(
        sheet_name=sheet_name,
        matched_folder_name=matched_folder_name,
        confidence=best_score,
        is_confirmed_alias=False,
        candidates=top_candidates,
    )
