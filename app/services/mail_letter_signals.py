"""Що лист каже про роботу поза своїм текстом: назви архівів і капа (18.11.26).

Власник 18.11.26, два листи того самого дня:
  * «сплінт РММА прозорий» + `…-37-36-…-47-bitesplint_cad.stl` і «шина
    фрезерована прозора ПММА» + `Акуленко__splint-KD-46-bitesplint_cad.stl` —
    обидва фрезерована капа. Поле ставало «PMMA» (у другому чіпом ішов навіть
    `pmma a3`), хоча цех пише такі роботи як `kappa` — без кольору.
  * Матеріал і колір клієнт буває пише лише в назві архіву, а рядок архіву
    після розпакування видалявся (`mail_reader.extract_archive_attachments`) —
    тепер назва лишається при файлах (`Attachment.archive_name`, міграція 0102).

Тут лише читання ознак; що підставити в поле, вирішує `routers/mail.py`
(картка й рядок списку — одним правилом).
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Iterable, Optional, Protocol

from app.services.stl_units import exocad_type, is_model_file, is_splint_file


class _Att(Protocol):
    filename: str
    archive_name: Optional[str]


def archive_names(attachments: Iterable[_Att]) -> list[str]:
    """Назви архівів листа без розширення, `_` → пробіл, без повторів."""
    out: list[str] = []
    for att in attachments:
        raw = (getattr(att, "archive_name", None) or "").strip()
        if not raw:
            continue
        name = PurePath(raw).stem.replace("_", " ").strip()
        if name and name not in out:
            out.append(name)
    return out


# Як клієнти й цех називають фрезеровану капу. «Шина» — так само капа
# (Катеринець 17.11.26: «шина фрезерована прозора ПММА»). Окремим словом, не
# шматком іншого («шинка», «капітан»).
_SPLINT_TEXT = re.compile(
    r"(?<![^\W\d_])(?:bite\s*splint\w*|splint\w*|night\s*guard\w*|nightguard\w*|kap{1,2}a"
    r"|сплінт\w*|сплинт\w*|кап{1,2}[аиуі]|шин[аиуі]|міорелакс\w*|миорелакс\w*)(?![^\W\d_])",
    re.IGNORECASE,
)


def text_says_splint(text: str | None) -> bool:
    return bool(_SPLINT_TEXT.search(text or ""))


def is_splint_letter(text: str | None, filenames: Iterable[str]) -> bool:
    """Уся робота листа — капа.

    Моделі листа вирішують першими: є хоч одна з ІНШИМ типом Exocad (`crown`,
    `bridge`…) — у листі не лише капа, і матеріал за неї вгадувати не можна.
    Інакше капа, коли її називає назва файлу (`bitesplint_cad`, «splint») або
    слово в листі (сплінт, капа, шина) — для моделей без типу в назві.
    """
    models = [f for f in filenames if is_model_file(f)]
    splints = [f for f in models if is_splint_file(f)]
    if any(exocad_type(f) and not is_splint_file(f) for f in models):
        return False
    return bool(splints) or text_says_splint(text)
