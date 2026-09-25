"""Підказка кольорів для багатокольорового листа — який файл до якого відтінку.

Бойовий випадок 25.09.26 (Роман): один лист, дев'ять файлів, чотири пацієнти
різних кольорів. Текст — «Monolith кольору денис, юшков а3.5, роман а3, лідія
а 1», а файли названі по пацієнтах: «16.09.2026-Проект денис…stl». Спільного
ключа файл↔колір у пошті немає (CLAUDE.md §2, правило 3), але ТУТ він є в
самих даних: ім'я пацієнта стоїть і в тексті біля кольору, і в імені файлу.

Модуль лише ПІДКАЗУЄ. Жодних рішень без оператора: картка показує групи
(«a3.5 · денис, юшков · 4 файли»), клік по групі позначає її файли й підставляє
матеріал, а прийняття лишається звичайним частковим прийняттям. Файл, який
не вдалося однозначно віднести, так і показується «?» — вгадане гірше за
непоказане (той самий принцип, що в печах).

Розбір тексту: ім'я (імена) ДО відтінку отримують цей відтінок. «денис, юшков
а3.5» — обидва a3.5: так люди пишуть перелік. Ім'я без відтінку після нього
(«Грунський Сергій» у даних доставки) нічого не отримує.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.material_match import _SHADE_RE, _forms, _tokens

# Слова, що стоять поруч із переліком кольорів, але пацієнтами не є. Вони
# НЕ мусять ставати «іменами»: слово «проект» є в кожному імені файлу, і
# зараховане як пацієнт воно звело б усі файли в одну групу.
_STOPWORDS = frozenset({
    "колір", "кольору", "кольор", "кольори", "кольорів", "цвет", "цвета", "цвет:",
    "color", "colour", "відтінок", "відтінку",
    "проект", "проєкт", "проэкт", "project", "роботи", "робота", "робіт",
    "для", "та", "і", "й", "и", "на", "з", "із", "по", "the", "and",
    "коронка", "коронки", "коронок", "анатомія", "каркас", "міст", "абатмент",
    "фрезерування", "фрезернути", "на фрезерування",
})

_URL_RE = re.compile(r"https?://\S+")

_MIN_NAME = 3
"""Коротше за це ім'ям не вважаємо: `а`, `із`, скорочення без сенсу."""

# Варіанти транслітерації одного імені: «денис» → denis, у файлі — denys;
# «сергій» → sergi, у файлі — serhii. Зводимо їх до одного запису й далі
# порівнюємо ТОЧНО. Нечіткий поріг тут шкідливий (виміряно 25.09.26):
# «ірина»/«ірена» мають ту саму схожість 80, що й «денис»/«denys», а збіг за
# початком слова зливав «іван» з «іванна». Різні люди в одній теці — гірше,
# ніж файл, позначений «?».
_LOOSE = str.maketrans({"y": "i", "j": "i", "g": "h", "w": "v"})


def _loose(form: str) -> str:
    form = form.translate(_LOOSE)
    out: list[str] = []
    for ch in form:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


@dataclass
class ColorGroup:
    """Одна кольорова партія листа: відтінок, чиї це роботи й які файли."""

    shade: str
    material: str
    names: list[str] = field(default_factory=list)
    attachment_ids: list[int] = field(default_factory=list)


@dataclass
class ColorPlan:
    """Підказка на весь лист. `by_attachment` — відтінок кожного віднесеного
    файлу (для позначки біля імені файлу); `unmatched_ids` — файли, яких
    однозначно віднести не вдалося."""

    groups: list[ColorGroup]
    unmatched_ids: list[int]
    by_attachment: dict[int, str]


def _is_name(token: str, material_words: set[str]) -> bool:
    return (
        len(token) >= _MIN_NAME
        and token.isalpha()
        and token not in _STOPWORDS
        and token not in material_words
    )


def parse_name_shades(text: str | None, material_words: set[str]) -> dict[str, str]:
    """{ім'я: відтінок} із тексту листа.

    Лише відтінки шкали Vita (`a3`, `a3.5`, `bl2`), БЕЗ кодів `500`/`800`:
    у тексті листа «вересня 2026» дало б імені «вересня» колір «2026». Ім'я,
    назване двічі з різними кольорами, неоднозначне — його не беремо.
    """
    result: dict[str, str] = {}
    conflicts: set[str] = set()
    for line in (text or "").splitlines():
        pending: list[str] = []
        # Посилання файлообмінника несе довгий випадковий токен: його шматок
        # між дефісами на кшталт «a3» став би «відтінком» для сусідніх слів.
        line = _URL_RE.sub(" ", line)
        for token in _tokens(line):
            if _SHADE_RE.match(token):
                for name in pending:
                    if name in result and result[name] != token:
                        conflicts.add(name)
                    result[name] = token
                pending = []
            elif _is_name(token, material_words):
                pending.append(token)
    for name in conflicts:
        result.pop(name, None)
    return result


def _name_hits(filename: str, names: list[str]) -> set[str]:
    """Які імена з тексту стоять в імені файлу.

    Слово з файлу має ЗБІГТИСЬ з іменем цілком — у кирилиці або в латинській
    формі (`_forms` + `_loose`): «денис» у тексті й «denys» у файлі — одна
    людина. Початок слова не рахується: інтерфейс пошти обрізає ім'я файлу
    («Проект дені…»), але в базі воно повне, а «іван» ≠ «іванна»."""
    stem = filename.rsplit(".", 1)[0]
    tokens = [t for t in _tokens(stem) if t.isalpha() and len(t) >= _MIN_NAME]
    token_keys = [{_loose(f) for f in _forms(t)} | {t} for t in tokens]
    hits: set[str] = set()
    for name in names:
        name_key = {_loose(f) for f in _forms(name)} | {name}
        if any(name_key & key for key in token_keys):
            hits.add(name)
    return hits


def material_base(material_guess: str | None) -> str:
    """Назва матеріалу без відтінку: «Monolith a3.5» → «monolith»."""
    return " ".join(t for t in _tokens(material_guess) if not _SHADE_RE.match(t))


def suggest_color_plan(
    body_text: str | None,
    material_guess: str | None,
    attachments: list,
) -> ColorPlan | None:
    """Згрупувати файли листа за відтінками, названими в тексті.

    `attachments` — нерозібрані файли листа (обʼєкти з `id` і `filename`).
    None, якщо підказувати нема чого: у тексті менше двох різних відтінків
    (звичайний однокольоровий лист — зайвий блок лише заважав би) або жоден
    файл не впізнано.
    """
    base = material_base(material_guess)
    material_words = set(base.split())
    name_shades = parse_name_shades(body_text, material_words)
    if len(set(name_shades.values())) < 2:
        return None

    names = list(name_shades)
    by_shade: dict[str, ColorGroup] = {}
    by_attachment: dict[int, str] = {}
    unmatched: list[int] = []
    for attachment in attachments:
        hits = _name_hits(attachment.filename or "", names)
        shades_hit = {name_shades[n] for n in hits}
        if len(shades_hit) != 1:
            # Нічого або два різні кольори в одному імені — не вгадуємо.
            unmatched.append(attachment.id)
            continue
        shade = shades_hit.pop()
        group = by_shade.get(shade)
        if group is None:
            group = ColorGroup(shade=shade, material=f"{base} {shade}".strip())
            by_shade[shade] = group
        group.attachment_ids.append(attachment.id)
        for name in sorted(hits):
            if name not in group.names:
                group.names.append(name)
        by_attachment[attachment.id] = shade

    # Жодного впізнаного файлу — підказувати нема чого. Одна група — НЕ привід
    # ховати блок: після першої прийнятої партії залишок листа часто саме
    # такий («a3.5» + «?»), і клік по групі досі економить ручний вибір. Що
    # лист справді багатокольоровий, уже доведено текстом (≥2 відтінки вище).
    if not by_shade:
        return None
    # Порядок груп — як кольори йдуть у тексті листа (оператор читає згори).
    text_order = list(dict.fromkeys(name_shades.values()))
    groups = sorted(by_shade.values(), key=lambda g: text_order.index(g.shade))
    return ColorPlan(groups=groups, unmatched_ids=unmatched, by_attachment=by_attachment)
