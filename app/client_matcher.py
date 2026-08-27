"""Fuzzy matching of client names from sheet to export folder names.

This module provides client name matching with a confirmed-alias dictionary,
using rapidfuzz for fuzzy matching when exact matches are not found.

Real-world folder naming the scorer has to survive (per the lab): the surname
is always there, but a folder may be surname-only while the sheet has
name+surname (or vice versa), and either side may be Cyrillic or Latin
transliteration. So scoring runs on transliterated text and treats a shared
surname token as a near-exact match.
"""

import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from rapidfuzz import fuzz, process

# Ukrainian/Russian -> Latin transliteration, two common styles: the official
# passport style (я->ia, ю->iu, ...) and the everyday "ya-style" (я->ya,
# ю->yu, ...). Folder names in the wild use either, so a name is compared in
# both variants and the best score wins.
_TRANSLIT_BASE = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ь": "", "'": "", "’": "", "ъ": "", "ы": "y", "э": "e", "ё": "o",
}
_TRANSLIT_STYLES = (
    {**_TRANSLIT_BASE, "є": "ie", "ї": "i", "й": "i", "ю": "iu", "я": "ia"},
    # everyday/Russian-influenced style: я->ya, й->y, and г->g (Шульгін is
    # written Shulgin far more often than the official Shulhin)
    {**_TRANSLIT_BASE, "г": "g", "є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"},
)


# ПРОДУКТИВНІСТЬ: ці дві функції викликались мільйони разів за один запит і
# щоразу перебудовували ті самі рядки. У бойовому логу 27.08.26 це давало
# «GET /handout took 1652s» і «GET /clients took 768s»: сторінка звіряє
# кожного клієнта з кожною текою, тобто одні й ті самі імена нормалізуються
# й транслітеруються тисячі разів. Імен обмежена кількість — кешуємо.
@lru_cache(maxsize=8192)
def _normalize(name: str) -> str:
    return unicodedata.normalize("NFC", name.strip().lower())


@lru_cache(maxsize=8192)
def _transliterations_cached(name: str) -> tuple[str, ...]:
    return tuple(_transliterations_uncached(name))


def _transliterations(name: str) -> list[str]:
    return list(_transliterations_cached(name))


def _transliterations_uncached(name: str) -> list[str]:
    """The normalized name plus its Latin transliteration variants (deduped).
    Latin input passes through unchanged, so comparing a Cyrillic sheet name
    against a Latin folder name lands on the same alphabet."""
    normalized = _normalize(name)
    variants = {normalized}
    for style in _TRANSLIT_STYLES:
        variants.add("".join(style.get(ch, ch) for ch in normalized))
    return list(variants)


def _score_pair(sheet_name: str, folder_name: str) -> float:
    """0-100 score between one sheet name and one folder name.

    Best over all transliteration-variant pairs of:
      * token_set_ratio — 100 when one side's tokens are a subset of the
        other's ("Мулик" vs "Петро Мулик": surname-only vs name+surname);
      * plain ratio — rewards whole-string closeness;
      * best per-token pair ratio (slightly damped) — catches a shared-but-
        misspelled surname across different transliteration habits.
    """
    # Точний збіг після нормалізації — 100 без жодного порівняння. Найчастіший
    # випадок у реальних даних і найдорожчий шлях, якщо його не зрізати.
    if _normalize(sheet_name) == _normalize(folder_name):
        return 100.0

    best = 0.0
    for a in _transliterations(sheet_name):
        for b in _transliterations(folder_name):
            best = max(best, float(fuzz.token_set_ratio(a, b)), float(fuzz.ratio(a, b)))
            for ta in a.split():
                if len(ta) < 4:  # short first-name bits ("др", initials) don't count
                    continue
                for tb in b.split():
                    if len(tb) < 4:
                        continue
                    best = max(best, float(fuzz.ratio(ta, tb)) * 0.97)
    return best


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


# ПОПЕРЕДНІЙ ВІДСІВ. _score_pair коштує ~30 викликів rapidfuzz на пару
# (9 комбінацій транслітерацій плюс цикли по токенах). Ганяти його по СОТНЯХ
# тек для КОЖНОГО клієнта — мільйони порівнянь і хвилини очікування: бойовий
# лог 27.08.26 показав «GET /handout took 1652s» і «GET /clients took 768s».
#
# Спершу дешевий прохід rapidfuzz по нормалізованих рядках відбирає
# правдоподібних, і лише вони йдуть у дорогий скорер. Поріг 55 із запасом:
# дорогий скорер піднімає оцінку за рахунок транслітерації й спільного
# токена, але не з нічого — усе нижче не дотягне до порога автозбігу (90).
_PREFILTER_KEEP = 40
_PREFILTER_FLOOR = 55.0


def _shortlist(sheet_name: str, folder_names: list[str]) -> list[str]:
    if len(folder_names) <= _PREFILTER_KEEP:
        return list(folder_names)
    normalized = {name: _normalize(name) for name in folder_names}
    rough = process.extract(
        _normalize(sheet_name),
        normalized,
        scorer=fuzz.token_set_ratio,
        limit=_PREFILTER_KEEP,
        score_cutoff=_PREFILTER_FLOOR,
    )
    picked = [key for _value, _score, key in rough]
    # Точні збіги мусять дійти до скорера, навіть якщо дешевий прохід їх не
    # підняв — на них тримається гілка «exact».
    target = _normalize(sheet_name)
    seen = set(picked)
    picked += [n for n in folder_names if normalized[n] == target and n not in seen]
    return picked


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

    # Exact match after normalization (case/whitespace/NFC) wins outright —
    # a folder that IS the sheet name modulo a trailing space must not be
    # dragged into the ambiguity check by a merely-similar second candidate.
    sheet_normalized = _normalize(sheet_name)
    exact = [f for f in folder_names if _normalize(f) == sheet_normalized]
    if exact:
        # Ці кандидати — суто підказка «схоже також на…» у два рядки.
        # Раніше заради них скорер проходив ПО ВСІХ теках, хоча потрібну вже
        # знайдено точним збігом: найчастіший шлях був найдорожчим.
        others = [
            (f, _score_pair(sheet_name, f))
            for f in _shortlist(sheet_name, folder_names)
            if f != exact[0]
        ]
        others.sort(key=lambda x: x[1], reverse=True)
        return MatchResult(
            sheet_name=sheet_name,
            matched_folder_name=exact[0],
            confidence=100.0,
            is_confirmed_alias=False,
            candidates=[(exact[0], 100.0)] + others[:2],
        )

    # Fuzzy match. Normalization (NFC/case/whitespace) matters because
    # visually-identical Cyrillic text can arrive in different composed forms
    # depending on the source app/OS; transliteration matters because the lab
    # mixes Cyrillic and Latin folder names for the same client. Both are
    # handled inside _score_pair.
    scores: list[tuple[str, float]] = [
        (folder_name, _score_pair(sheet_name, folder_name))
        for folder_name in _shortlist(sheet_name, folder_names)
    ]

    # A literal (normalized) whole-name match beats any fuzzy scoring — the
    # surname-token boost can put "іваненко п." within the ambiguity margin of
    # the true "іваненко петро" folder, but an exact-name folder is never
    # actually ambiguous. Two exact-equal folders still fall through to the
    # ambiguity path (genuinely needs a human).
    exact = [
        name for name in folder_names if _normalize(name) == _normalize(sheet_name)
    ]
    if len(exact) == 1:
        top = sorted(scores, key=lambda x: x[1], reverse=True)[:3]
        return MatchResult(
            sheet_name=sheet_name,
            matched_folder_name=exact[0],
            confidence=100.0,
            is_confirmed_alias=False,
            candidates=top,
        )

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
