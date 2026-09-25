"""Текст листа, розкладений для читання: що пише замовник — окремо від службового.

Лист замовника — це рідко лише його слова. Бойовий лист 25.09.26 (cadcamlab):
заголовок пересилання («Від кого / Кому / Тема / Дата»), три рядки по суті,
дані доставки, підпис центру з телефонами і — найдовше — список файлів ukr.net,
де кожен файл займає три рядки (ім'я, розмір, посилання з токеном на двісті
символів). У картці це читалось як стіна шуму, а оператору треба прочитати
лист ПОВНІСТЮ й нічого не пропустити.

Тут текст НЕ скорочується й нічого не губиться: лише розкладається на частини,
щоб вторинне можна було показати приглушено. Оригінал лишається поруч
(перемикач у режимі читання) — розбір лише допомагає, а не вирішує.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Початок пересланого листа в різних поштовиках.
_FORWARD_START = re.compile(
    r"^-{2,}\s*(?:повідомлення,?\s+що\s+пересилається|пересланное\s+сообщение|"
    r"исходное\s+сообщение|оригінальне\s+повідомлення|forwarded\s+message|"
    r"original\s+message)\s*-*\s*$",
    re.IGNORECASE,
)
# Поле заголовка пересилання: «Від кого: Iris <…>».
_FORWARD_FIELD = re.compile(
    r"^(від кого|кому|копія|тема|дата|от|отправлено|from|to|cc|subject|date|sent)\s*:\s*(.*)$",
    re.IGNORECASE,
)
# Маркери цитати: Proton, Thunderbird і Gmail (текстова частина) кладуть
# кожне вкладене пересилання на рівень глибше — «>», «>>», «>>>».
_QUOTE = re.compile(r"^\s*(?:>\s?)+")
# Рекламний підпис самого поштовика — не слова людини. Лист 39 (Proton, 25.09.26)
# складався з п'яти таких рядків і вкладених пересилань — і прев'ю в картці
# показувало саме їх.
_MAILER = re.compile(
    r"^(?:sent\s+(?:with|from)\s|get\s+outlook\s|надіслано\s+(?:з|із)\s|"
    r"отправлено\s+(?:с|из)\s|відправлено\s+(?:з|із)\s)",
    re.IGNORECASE,
)
# Роздільник підпису за RFC 3676 — «-- » (ukr.net кладе без пробілу).
_SIGNATURE = re.compile(r"^--\s*$")
# Службове повідомлення ukr.net про великі файли, після нього — список файлів.
_FILES_NOTICE = re.compile(r"^цей лист містить файли", re.IGNORECASE)
_SIZE = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:b|kb|mb|gb|байт|кб|мб|гб)$", re.IGNORECASE)
_URL = re.compile(r"https?://\S+")
# Markdown-посилання, яке поштовики кладуть у текстову частину:
# «[Proton Mail](https://proton.me/mail/home)» → «Proton Mail».
_MD_LINK = re.compile(r"\[([^\]]+)\]\(https?://[^)\s]+\)")
# Відтінок для підсвічування (лише підсвічування — рішень ним не приймаємо).
# Латинська a–d або кирилична «а» з цифрою («а 1», «А3.5»); кириличні в/с/д —
# лише впритул до цифри, бо «в 2 екземплярах» — це прийменник.
_SHADE = re.compile(
    r"(?<!\w)((?:[aAаА]\s?|[bcdBCD]|[вВсСдД])[1-4](?:[.,]5)?|(?:bl|BL|бліч|Бліч)\s?[1-4])(?!\w)"
)


@dataclass
class FileLine:
    name: str
    size: str = ""


@dataclass
class Segment:
    """Частина листа.

    kind:
      "text"      — те, що пише людина (показується головним);
      "forward"   — заголовок пересилання (поля `fields`);
      "files"     — файли ukr.net (`files`, `note` — рядок про видалення);
      "signature" — підпис після «--»;
      "mailer"    — рекламний рядок поштовика («Sent with Proton Mail»).
    """

    kind: str
    lines: list[str] = field(default_factory=list)
    fields: list[tuple[str, str]] = field(default_factory=list)
    files: list[FileLine] = field(default_factory=list)
    note: str = ""


def _squeeze(lines: list[str]) -> list[str]:
    """Без порожніх рядків на краях і без подвійних порожніх усередині."""
    out: list[str] = []
    for line in lines:
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line.rstrip())
    while out and out[-1] == "":
        out.pop()
    return out


def letter_segments(body: str | None) -> list[Segment]:
    """Розкласти текст листа на частини, нічого не викидаючи.

    Кожен рядок оригіналу потрапляє рівно в одну частину, крім посилань у
    списку файлів — їх дублює чіп «Скачати за посиланням», а на екрані це
    двісті символів токена на файл.
    """
    raw_lines = (body or "").replace("\r\n", "\n").split("\n")
    segments: list[Segment] = []
    current = Segment("text")
    state = "text"  # text | forward | signature | files

    def flush() -> None:
        nonlocal current
        if current.kind == "text":
            current.lines = _squeeze(current.lines)
            if current.lines:
                segments.append(current)
        elif current.kind == "signature":
            current.lines = _squeeze(current.lines)
            if current.lines:
                segments.append(current)
        elif current.fields or current.files or current.note:
            segments.append(current)

    for quoted in raw_lines:
        # Цитата — той самий лист глибше: знімаємо маркер, інакше вкладене
        # пересилання («> ------- Forwarded Message -------») не впізнати.
        raw = _QUOTE.sub("", quoted)
        line = raw.strip()
        if _MAILER.match(line) and state in ("text", "forward"):
            flush()
            segments.append(Segment("mailer", lines=[_MD_LINK.sub(r"\1", line)]))
            current, state = Segment("text"), "text"
            continue
        if _FILES_NOTICE.match(line):
            flush()
            current, state = Segment("files", note=line), "files"
            continue
        if state == "files":
            if not line or _URL.fullmatch(line):
                continue
            if _SIZE.match(line) and current.files and not current.files[-1].size:
                current.files[-1].size = line
                continue
            # Рядок «ім'я-файлу https://…» — посилання відрізаємо, ім'я лишаємо.
            name = _URL.sub("", line).strip()
            if name:
                current.files.append(FileLine(name=name))
            continue
        if _FORWARD_START.match(line):
            flush()
            current, state = Segment("forward"), "forward"
            continue
        if state == "forward":
            match = _FORWARD_FIELD.match(line)
            if match:
                current.fields.append((match.group(1), match.group(2).strip()))
                continue
            if not line:
                continue
            # Перший рядок, що не поле заголовка, — уже сам лист.
            flush()
            current, state = Segment("text"), "text"
        if _SIGNATURE.match(line) and state == "text":
            flush()
            current, state = Segment("signature"), "signature"
            continue
        current.lines.append(raw)
    flush()
    return segments


def useful_text(segments: list[Segment]) -> str:
    """Лише слова людини — для короткого прев'ю в картці (2 рядки)."""
    parts = ["\n".join(s.lines) for s in segments if s.kind == "text"]
    text = _MD_LINK.sub(r"\1", "\n".join(p for p in parts if p.strip()))
    return _URL.sub(lambda m: f"[{_host(m.group(0))}]", text)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "посилання").removeprefix("www.")


def inline_parts(line: str) -> list[tuple[str, str]]:
    """Рядок тексту → шматки для шаблону: ("text", …), ("shade", …), ("link", host).

    Відтінки підсвічуються, посилання згортаються до назви сайту — повна
    адреса лишається в «Оригіналі»."""
    parts: list[tuple[str, str]] = []
    line = _MD_LINK.sub(r"\1", line)
    pos = 0
    for match in _URL.finditer(line):
        parts.extend(_shade_parts(line[pos:match.start()]))
        parts.append(("link", _host(match.group(0))))
        pos = match.end()
    parts.extend(_shade_parts(line[pos:]))
    return parts


def _shade_parts(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    for match in _SHADE.finditer(text):
        if match.start() > pos:
            out.append(("text", text[pos:match.start()]))
        out.append(("shade", match.group(1)))
        pos = match.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out
