"""Classify the free-text "Колір роботи" value into a material category.

The sheet's material/colour column is raw operator text — `пмма A2`,
`моно а3.5`, `800`, `титан корея`, `монліт а 3` (a typo), `моделювання`
(not even a material). To count and sort by material we map that mess onto a
small fixed catalog (Цирконій / ПММА / СЛМ / Титан, plus a non-production
"Не матеріал" bucket for stage/part rows that land in the colour column).

Categories are code; the raw spellings that map to them are DATA (seeded into
MaterialAlias, extendable at runtime without a code change) — same
accumulating-dictionary idea as the client fuzzy-matcher for handout. This
module holds the seed and the matching logic; the DB tables live in models.py.

Matching, in order:
  1. token alias — an exact whitespace-delimited token (`800`, `ti`): used for
     short/numeric codes that must not match as a substring of another word.
  2. contains alias — a substring (`пмма`, `моно`, `титан`): the normal case.
  3. fuzzy fallback — rapidfuzz against the word aliases, to absorb the typos
     the sheet is full of (`монліт`/`миноліт` → `моноліт`).
An input that still matches nothing is left unresolved (material_id NULL) for
an operator to classify; a confirmed assignment becomes a new alias.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from rapidfuzz import fuzz

# Category display names (also the Material.name seed values).
ZIRCON = "Цирконій"
PMMA = "ПММА"
SLM = "СЛМ"
TITANIUM = "Титан"
WAX = "Віск"
NON_MATERIAL = "Не матеріал"

# Seed catalog: (name, is_production, sort_order). is_production=False keeps
# stage/part rows out of material production stats while still classifying them
# so they don't sit forever in the "needs classification" bucket.
SEED_MATERIALS: list[tuple[str, bool, int]] = [
    (ZIRCON, True, 1),
    (PMMA, True, 2),
    (SLM, True, 3),
    (TITANIUM, True, 4),
    (WAX, True, 5),
    (NON_MATERIAL, False, 99),
]

# match_type: "token" = exact whitespace token, "contains" = substring.
SEED_ALIASES: dict[str, list[tuple[str, str]]] = {
    ZIRCON: [
        ("циркон", "contains"), ("zircon", "contains"), ("zirkon", "contains"),
        ("zr", "token"),
        ("моно", "contains"), ("mono", "contains"),
        ("моноліт", "contains"), ("монолит", "contains"), ("monolith", "contains"),
        ("емо", "contains"), ("emo", "contains"),
        ("katana", "contains"), ("катана", "contains"),
        ("утмл", "contains"), ("utml", "contains"),
        ("стмл", "contains"), ("stml", "contains"),
        ("nat", "token"), ("nature", "contains"), ("z nat", "contains"),
        # Super-translucent / translucent zirconia markers seen in the sheet.
        ("st", "token"), ("s1", "token"), ("tr", "token"), ("транс", "contains"),
        # Manufacturer colour codes for zirconia discs.
        ("500", "token"), ("800", "token"), ("1000", "token"),
        ("1333", "token"), ("2000", "token"),
    ],
    PMMA: [
        ("пмма", "contains"), ("pmma", "contains"),
        ("хіпс", "contains"), ("hips", "contains"), ("hipc", "contains"),
        ("каппа", "contains"), ("kappa", "contains"),
        ("trinia", "contains"),
        # Clients rarely write "ПММА" — they name the product: a temporary
        # crown ("врім'янка"/"тимчасова"/"temp") is milled from PMMA. Seeded so
        # the mail triage recogniser maps that wording to PMMA out of the box;
        # admins extend this from the material library screen.
        ("врім", "contains"), ("врем", "contains"),
        ("тимчасов", "contains"), ("временн", "contains"),
        ("temp", "contains"),
    ],
    SLM: [
        ("слм", "contains"), ("slm", "contains"),
    ],
    TITANIUM: [
        ("титан", "contains"), ("titan", "contains"), ("ti", "token"), ("tit", "token"),
    ],
    WAX: [
        ("wax", "contains"), ("віск", "contains"), ("воск", "contains"),
    ],
    NON_MATERIAL: [
        ("моделювання", "contains"), ("втулка", "contains"), ("vtulka", "contains"),
        ("анатомія", "contains"),
        ("implant", "contains"), ("імплант", "contains"), ("имплант", "contains"),
    ],
}

# Слова, які МІСТЯТЬ аліас матеріалу, але матеріалом не є.
#
# ЧОМУ це взагалі потрібно. Аліаси навмисно підрядкові ("циркон" ловить
# "цирконій", "врем" — "врем'янку"), і на короткому рядку колонки «Колір
# роботи» це безпечно. Але той самий класифікатор ганяють по ВІЛЬНОМУ тексту
# листа (app/mail_parser.guess_fields_from_text, editable-dictionary backstop),
# а там «нет времени» давало ПММА, «монополія» — цирконій, «temperature» — знову
# ПММА (аудит 05.09.26, пошта M-9). Просту праву межу слова аліасам не додаси:
# вона ж відрізала б «воскова», «цирконій», «hipsa» — тобто саме те, заради чого
# підрядковий режим і існує.
#
# Тому звужуємо з іншого боку: ці конкретні слова вирізаються ЦІЛИКОМ (межі з
# обох боків) ще до порівняння з аліасами. «Временная», «врем'янка», «temp a2»,
# «Emotions» під шаблони не підпадають і лишаються недоторканими. Список — код,
# а не дані: він однаковий на всіх інсталяціях і не потребує міграції, на
# відміну від самих аліасів (SEED_ALIASES + міграція для наявних баз).
_FALSE_FRIEND_RE = re.compile(
    r"\b(?:"
    # НЕ «времен\w*»: воно зʼїло б і «временная коронка» — а це якраз ПММА.
    # Тут перелічені форми слова «время», у яких після «времен» іде одна «н»
    # або голосна; «временн…» лишається матеріалом.
    r"врем['’]?я|времен(?:и|ем|ах|у|а|ами)"     # «нет времени», «времена» → не ПММА
    r"|монопол\w*"                               # «монополія» → не «моно»
    r"|емоц\w*|эмоц\w*|emotional\w*"             # «емоційно» → не «емо»
    r"|temperatur\w*|attempt\w*|contemporar\w*|templat\w*"  # містять «temp»
    r")\b"
)


def _strip_false_friends(normalized: str) -> str:
    """Прибрати з нормалізованого тексту слова-омоніми (див. _FALSE_FRIEND_RE)."""
    if not normalized:
        return normalized
    return re.sub(r"\s+", " ", _FALSE_FRIEND_RE.sub(" ", normalized)).strip()


_FUZZY_THRESHOLD = 84.0
_MIN_FUZZY_LEN = 4  # don't fuzzy-match very short tokens (a2, ti, 800)

# A bare tooth shade with no material word (a2, а3.5, с3, bl2, optionally with a
# translucency suffix). In this lab a colour cell that carries only a shade
# defaults to zirconia — the dominant material — so classify it as such, but
# ONLY as a last resort, after every alias/fuzzy check has failed. That ordering
# is what keeps "пмма a2" as PMMA: the пмма alias matches first, so the shade
# fallback never runs for it.
_SHADE_RE = re.compile(r"^(bl|[a-dавсд])\s?\d(\.\d)?( (tr|транс))?$")


@dataclass(frozen=True)
class AliasRow:
    pattern: str
    match_type: str  # "token" | "contains"
    material: str


def seed_alias_rows() -> list[AliasRow]:
    """The seed aliases flattened — used to seed the DB and as the default
    ruleset in tests/backfill before any runtime additions."""
    rows: list[AliasRow] = []
    for material, aliases in SEED_ALIASES.items():
        for pattern, match_type in aliases:
            rows.append(AliasRow(pattern=pattern, match_type=match_type, material=material))
    return rows


def normalize_material(raw: str | None) -> str:
    """Lowercase, NFC, decimal-comma → dot, collapse whitespace. So
    `Моноліт А 3,5` and `моноліт а 3.5` normalize identically."""
    if not raw:
        return ""
    s = unicodedata.normalize("NFC", raw).strip().lower()
    s = s.replace(",", ".")
    s = re.sub(r"\s+", " ", s)
    return s


def classify_material(
    raw: str | None,
    aliases: list[AliasRow] | None = None,
    *,
    free_text: bool = False,
) -> str | None:
    """Return the material category name for a raw colour string, or None if it
    can't be resolved confidently. Pure: `aliases` defaults to the seed so it
    works without a DB (tests, backfill dry-runs).

    ``free_text=True`` — коли на вхід іде ТЕКСТ ЛИСТА, а не клітинка «Колір
    роботи». Тоді вимикаються `token`-аліаси: вони писались під коротку
    клітинку, де окреме «500» чи «ti» справді означає матеріал, а у вільному
    тексті дають «оплата 500 грн» → Цирконій, «Nature of the request» →
    Цирконій, «Ti amo» → Титан (ревʼю 07.09.26, M.7). `contains`-аліаси
    («циркон», «пмма», «титан») лишаються — вони самі по собі однозначні.
    """
    normalized = _strip_false_friends(normalize_material(raw))
    if not normalized:
        return None
    rows = aliases if aliases is not None else seed_alias_rows()
    tokens = normalized.split()

    matched: set[str] = set()
    for row in rows:
        if row.match_type == "token":
            if free_text:
                continue
            if row.pattern in tokens:
                matched.add(row.material)
        else:  # contains
            if row.pattern in normalized:
                matched.add(row.material)

    if len(matched) == 1:
        return next(iter(matched))
    if len(matched) > 1:
        # Genuinely ambiguous (two categories claim it) — leave for a human
        # rather than guess wrong.
        return None

    # Fuzzy fallback for typos, only against alphabetic word aliases.
    word_aliases = [
        row for row in rows
        if row.match_type == "contains" and row.pattern.isalpha() and len(row.pattern) >= 3
    ]
    fuzzy_matched: set[str] = set()
    for token in tokens:
        if len(token) < _MIN_FUZZY_LEN:
            continue
        best_score = 0.0
        best_material: str | None = None
        for row in word_aliases:
            score = fuzz.ratio(token, row.pattern)
            if score > best_score:
                best_score = score
                best_material = row.material
        if best_material is not None and best_score >= _FUZZY_THRESHOLD:
            fuzzy_matched.add(best_material)

    if len(fuzzy_matched) == 1:
        return next(iter(fuzzy_matched))

    # Last resort: a colour cell that is only a shade (no material word) is
    # zirconia by lab convention.
    if _SHADE_RE.match(normalized):
        return ZIRCON
    return None
