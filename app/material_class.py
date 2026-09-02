"""Visual classification of the Матеріал/Колір chip in the queue (screen 1).

Цирконій (моно/емо/тисячний/числові коди типу "800") stays the default chip
style — user explicitly asked not to touch it. ПММА (multi-color plastic)
and титан get a distinct highlight so an operator scanning the queue can
tell the material family at a glance without reading the text.
"""
import re


def material_color_css_class(material_color: str | None) -> str:
    if not material_color:
        return ""
    text = material_color.strip().lower()
    if "титан" in text:
        return "chip-titan"
    if "пмма" in text:
        return "chip-pmma"
    return ""


# Трейлінговий код відтінку: «моно a3» → a3, «моноліт а 3,5» → а 3,5,
# «800» → 800, «a2 транс» → a2 транс, «пмма в1» → в1, «mono bl2» → bl2.
#
# Шкала Vita — A1–A4, B1–B4, C1–C4, D2–D4 плюс BL (bleach). Оператори
# пишуть літеру і латиницею, і кирилицею: а=A, в=B, с=C, д=D. УВАГА:
# кирилицю не можна брати діапазоном [а-д] — там а,б,в,г,д, а `с` (es)
# стоїть значно далі, тож `с2 транс` розпадалось. Тільки перелік. Спершу тут
# стояло лише [aа] — і `с2 транс` розпадалось на «с» + «2 транс», а
# `mono bl2` на «mono bl» + «2». Перевірено на реальних значеннях за 21.08.
_COLOR_CODE_RE = re.compile(
    r"^(?P<word>.*?)\s*"
    r"(?P<code>(?:bl|бл|[abcdABCDавсдАВСД])?\s?\d[\d.,]*(?:\s*транс)?)\s*$",
    re.IGNORECASE,
)


def split_material_color(material_color: str | None) -> tuple[str, str]:
    """Розділяє текст кольору на назву й код відтінку.

    Потрібно режиму «технічний код» (перемикач «Колір роботи» в панелі
    «Вигляд»): там назва матеріалу притишується, а код читається першим,
    бо `800`, `500`, `A3.5` — це маркування виробника, а не слова.

    Повертає (назва, код). Код порожній, коли цифр немає взагалі
    («титан корея») — тоді режим друкує сам текст, без спроби розділити.
    Сирий текст ніколи не втрачається: назва + код завжди дають вихідне
    значення з точністю до пробілів.
    """
    if not material_color:
        return "", ""
    text = material_color.strip()
    match = _COLOR_CODE_RE.match(text)
    if not match:
        return text, ""
    code = match.group("code").strip()
    if not code:
        return text, ""
    return match.group("word").strip(), code


# Category → (badge symbol, css class). Symbols are the element/polymer marks
# operators recognise; the css class carries the material's signature colour
# (see the .matbadge palette in base.css). Colours agreed with Roman:
# Zr ice-blue, PMMA amber, Ti emerald, SLM steel, Wax rose.
_MATERIAL_BADGES = {
    "Цирконій": ("Zr", "mat-zr"),
    "ПММА": ("PMMA", "mat-pmma"),
    "Титан": ("Ti", "mat-ti"),
    "СЛМ": ("SLM", "mat-slm"),
    "Віск": ("Wax", "mat-wax"),
}


def material_families() -> list[tuple[str, str, str]]:
    """(символ, css-клас, назва) для кожної родини — для легенди кольорів.

    Те саме джерело `_MATERIAL_BADGES`, що й маркування рядка, тож край рядка
    й легенда фарбуються з одного місця. Цирконій свідомо БЕЗ смуги на краю
    рядка (він більшість), але в легенді присутній як родина.
    """
    return [(sym, cls, name) for name, (sym, cls) in _MATERIAL_BADGES.items()]


def material_family_class(name: str | None) -> str:
    """Клас родини за НАЗВОЮ матеріалу (а не за роботою).

    `material_badge` бере роботу й тому не годиться там, де в руках сам
    матеріал — бібліотека матеріалів. Джерело те саме, `_MATERIAL_BADGES`,
    щоб клас родини не розійшовся з кольором маркування.
    """
    if not name:
        return ""
    entry = _MATERIAL_BADGES.get(name.strip())
    return entry[1] if entry else ""


# Як та сама родина матеріалу пишеться в колонці «Колір роботи». Тільки
# ІМЕНА матеріалу — брендові слова цирконію («моно», «емо», «моноліт»)
# сюди свідомо не входять: вони розрізняють вироби всередині родини, тож
# несуть інформацію, якої в символі немає.
_MATERIAL_WORD_ALIASES = {
    "Цирконій": {"цирконій", "цирконий", "zr"},
    "ПММА": {"пмма", "pmma"},
    "Титан": {"титан", "ti"},
    "СЛМ": {"слм", "slm"},
    "Віск": {"віск", "воск", "wax"},
}


def strip_material_word(
    text: str | None, material_title: str | None, allow_empty: bool = False
) -> str:
    """Прибирає ПРОВІДНУ назву матеріалу, яку символ уже промовив.

    `PMMA │ пмма A2` називає матеріал двічі, і саме в тих родинах, що й так
    найпомітніші. Символ лишається, слово йде — код відтінку виходить
    наперед, заради чого режим і робився.

    Знімається лише перший токен і лише якщо він точно збігається з
    аліасом: `титан корея` → `корея` (Ti вже сказано, «корея» — це виробник
    і його треба лишити). Якщо після зняття не лишається нічого
    (значення дорівнює самій назві, напр. `Ti`), повертаємо текст як був —
    порожній чіп гірший за надлишковий. `allow_empty=True` знімає цей
    захист там, де носієм значення лишається код: `PMMA │ пмма А2` має
    стати `PMMA │ А2`, а не лишитись із назвою.
    """
    if not text or not material_title:
        return text or ""
    aliases = _MATERIAL_WORD_ALIASES.get(material_title)
    if not aliases:
        return text
    head, _, tail = text.strip().partition(" ")
    if head.lower() not in aliases:
        return text
    return tail.strip() or ("" if allow_empty else text)


def material_badge(order) -> dict | None:
    """Compact material badge for an order, or None when no badge should show.

    - resolved production material → its symbol + signature colour;
    - unresolved (material_id NULL) → a muted "?" so it reads as "needs a rule";
    - the non-production "Не матеріал" bucket → None (stage/part rows carry no
      material badge).
    """
    material = getattr(order, "material", None)
    if material is None:
        # Only flag "?" when there IS colour text that stayed unresolved; an
        # order with no material text at all just shows no badge.
        if not (getattr(order, "material_color", None) or "").strip():
            return None
        return {"symbol": "?", "cls": "mat-unknown", "title": "матеріал не визначено"}
    if material.name == "Не матеріал":
        return None
    symbol, cls = _MATERIAL_BADGES.get(material.name, (material.name[:4], "mat-other"))
    return {"symbol": symbol, "cls": cls, "title": material.name}
