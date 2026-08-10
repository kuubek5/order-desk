"""Best-effort field guessing from free-text email subject/body.

The email text isn't structured — clients write however they want. These are
just guesses for the triage screen to pre-fill; the operator always reviews
and corrects them before the message becomes an Order (CLAUDE.md screen 2).
"""

import re
import unicodedata

from rapidfuzz import fuzz

_PATTERNS = {
    "material_color_guess": r"(?:колір|цвет|матеріал)[:\s]+[-–]?\s*([^\n,;]+)",
    "kind_guess": r"(?:вид роботи|фрезеруванн\w*|на фрезерування)[:\s]*[-–]?\s*([^\n,;]*)",
    "quantity_guess": r"(?:кількість|к-сть|шт\.?)[:\s]*[-–]?\s*(\d+)",
}

# Keyword/phrase hints that an email is about 3D printing — a service this
# mailbox's lab does NOT do (CLAUDE.md section 1: the lab mills, it doesn't
# print). Covers Ukrainian, Ukrainian-Russian surzhyk and English spellings
# clients actually use, plus common typo-adjacent variants (spaced/hyphenated
# "3d"/"3д", verb forms like "надрукувати"/"роздрукувати").
_SERVICE_TYPE_PATTERNS: dict[str, list[re.Pattern]] = {
    "3d_print": [
        re.compile(r"3\s*[dд][\s\-]*друк", re.IGNORECASE),  # "3d друк", "3д-друк", "3 d друк"
        re.compile(r"друк\w*\s+модел", re.IGNORECASE),  # "друк моделі", "надрукувати модель"
        re.compile(r"3\s*[dд][\s\-]*принтер", re.IGNORECASE),  # "3d принтер", "3д-принтер"
        re.compile(r"\b3d[\s\-]*print(?:ing)?\b", re.IGNORECASE),  # "3d print", "3d-printing"
        re.compile(r"\bprint(?:ing)?\s+(?:the\s+)?model", re.IGNORECASE),  # "print model(s)"
        re.compile(r"\bresin\s+print", re.IGNORECASE),  # "resin printing"
    ],
}


def fuzzy_match_material_color(
    candidate: str,
    known_materials: list[str],
    threshold: float = 80.0,
) -> str | None:
    """Match a raw material/color guess against the reference list of values
    actually used in the Google Sheet (Order.material_color for source=="lab" —
    see app/client_matcher.py's match_client_name for the sibling pattern on
    client names, same rapidfuzz-based approach applied to a different domain).

    Returns the closest known_materials entry if similarity >= threshold, else
    None — callers should treat None as "don't trust this guess", not "guess
    was empty".

    Uses fuzz.ratio (not WRatio) to match client_matcher.py's choice. WRatio's
    partial/token-sort boosts are built for matching a short query against a
    longer haystack (e.g. "New York" in "New York City") — helpful for
    people/company names, but a liability here: CLAUDE.md's own examples
    include bare numeric codes ("500", "800") where a partial-ratio style
    scorer could inflate the similarity between two *different* short codes
    just because one is a substring-ish fit of the other. Plain ratio scores
    the whole strings against each other, which is safer for short tokens.

    Threshold 80.0 (lower than client_matcher's 90.0 for names) was chosen by
    checking actual score distributions:
      - genuine client typos ("пмма A2" -> "пма A2", "моно а3" -> "моно а3.",
        "А3.5" -> "а35") score ~92-95 — comfortably above 80.
      - a made-up word ("емаушан а4") against real materials/colors scores
        ~28-42 — comfortably below 80.
      - two distinct short numeric codes that differ by one digit ("800" vs
        "500") score ~67 — still below 80, so it won't silently confuse one
        real code for another.
    80 sits in the gap between those two clusters. Going higher (e.g. 90)
    would start rejecting legitimate one-character typos on short strings
    like "800"; going lower would risk accepting nonsense as a near-miss.
    """
    if not candidate or not known_materials:
        return None

    candidate_normalized = unicodedata.normalize("NFC", candidate.strip().lower())
    if not candidate_normalized:
        return None

    best_name: str | None = None
    best_score = 0.0
    for known in known_materials:
        if not known:
            continue
        known_normalized = unicodedata.normalize("NFC", known.strip().lower())
        score = fuzz.ratio(candidate_normalized, known_normalized)
        if score > best_score:
            best_score = score
            best_name = known

    if best_name is not None and best_score >= threshold:
        return best_name
    return None


def guess_fields_from_text(
    text: str,
    subject: str | None = None,
    body: str | None = None,
    known_materials: list[str] | None = None,
) -> dict:
    guesses = {}
    for field, pattern in _PATTERNS.items():
        match = re.search(pattern, text, re.IGNORECASE)
        guesses[field] = match.group(1).strip() if match and match.group(1).strip() else None

    # Real клієнти often skip the "колір:"/"матеріал:" keyword entirely and
    # just put the bare material/color in the subject or body (e.g. "моно
    # а3", matching CLAUDE.md's Колір роботи examples) — treat a short,
    # otherwise unmatched subject/body as the material/color guess. Subject
    # is checked first since it's the more deliberate field when both are set.
    #
    # When known_materials is supplied (the reference values operators/techs
    # actually typed into the sheet, see app/sync.py), the bare fallback is
    # only accepted if it fuzzy-matches one of those reference values — this
    # stops made-up/garbled words (e.g. a typo'd "емаушан а4") from being
    # accepted as if they were a real material/color. Without known_materials
    # (None/empty) the old behaviour is preserved: accept the short phrase
    # as-is, so existing callers/tests that don't pass it are unaffected.
    if guesses["material_color_guess"] is None:
        for candidate in (subject, body):
            if not candidate:
                continue
            candidate = candidate.strip()
            if candidate and len(candidate.split()) <= 3:
                if known_materials:
                    matched = fuzzy_match_material_color(candidate, known_materials)
                    if matched is None:
                        continue
                    guesses["material_color_guess"] = matched
                else:
                    guesses["material_color_guess"] = candidate
                break

    return guesses


def guess_service_type(text: str) -> str | None:
    """Best-effort hint: does this email sound like it's about 3D printing —
    a service this mailbox's lab does NOT offer (CLAUDE.md section 1-2: the
    lab mills, it doesn't print) — rather than milling, which is the default
    assumption for anything landing in this inbox?

    Same philosophy as guess_fields_from_text above: a cheap keyword signal
    for the triage screen (CLAUDE.md screen 2), not a real classifier. The
    operator always reviews every message regardless of the result. This
    function MUST NEVER be used to hide or drop a message from triage — it
    only flags one for a second look. False positives/negatives are fine;
    a message silently missing from the list is not.

    Returns "3d_print" if a 3D-printing keyword/phrase is found, else None.
    None means "no signal either way", which the caller treats as "assume
    milling" since that's the mailbox's actual business.

    If a message mentions both milling and 3D printing (e.g. a client asking
    about both in one email), this still returns "3d_print" — an operator
    double-checking a mixed email that turns out to be milling-only is cheap;
    silently treating a mixed request as pure milling is not.
    """
    if not text:
        return None

    normalized = unicodedata.normalize("NFC", text)
    for service_type, patterns in _SERVICE_TYPE_PATTERNS.items():
        if any(pattern.search(normalized) for pattern in patterns):
            return service_type
    return None
