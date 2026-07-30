"""Best-effort field guessing from free-text email subject/body.

The email text isn't structured — clients write however they want. These are
just guesses for the triage screen to pre-fill; the operator always reviews
and corrects them before the message becomes an Order (CLAUDE.md screen 2).
"""

import re

_PATTERNS = {
    "material_color_guess": r"(?:колір|цвет|матеріал)[:\s]+([^\n,;]+)",
    "kind_guess": r"(?:вид роботи|фрезеруванн\w*|на фрезерування)[:\s]*([^\n,;]*)",
    "quantity_guess": r"(?:кількість|к-сть|шт\.?)[:\s]*(\d+)",
}


def guess_fields_from_text(text: str) -> dict:
    guesses = {}
    for field, pattern in _PATTERNS.items():
        match = re.search(pattern, text, re.IGNORECASE)
        guesses[field] = match.group(1).strip() if match and match.group(1).strip() else None
    return guesses
