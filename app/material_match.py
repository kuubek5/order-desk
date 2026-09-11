"""Чи описують рядок таблиці й назва теки в `export` той самий матеріал.

Задача вужча, ніж у `app/material_classifier.py`: той відповідає «це взагалі
цирконій чи ПММА», а тут треба «це та сама пара матеріал+колір», бо на видачі
під рядком роботи мають з'явитись саме її теки, а не всі теки цирконію.

Чому не звичайне порівняння рядків. У таблиці технік пише коротко — `emo a3`,
`mono a3.5`, — а теку на диску називає людина й повністю:

    таблиця   `emo a3`
    тека      `Emotions A3 опаковий всередині`

Це та сама робота (бойовий випадок Pavlenko, 26.08.26): і те, і те — Emotions
відтінку A3. Але точний збіг набору слів їх не бачить, тому робота на видачі
не знаходилась, хоча тека лежала поруч.

Правило, яке звідси випливає:

* **Відтінок вирішує.** `a3` і `a3.5` — різні диски, тож розбіжність відтінку
  це завжди «ні», хай назви скільки завгодно схожі. Тому `a 3,5`, `А-3,5`,
  `a35`, `A3_5` зводяться до `a3.5` ще до порівняння, а `бліч 2` — до `bl2`.
* **Назва матеріалу може бути скороченою.** `emo` → `Emotions`, `mono` →
  `Monolith`: слово з таблиці має бути ПОЧАТКОМ слова з теки (або навпаки).
  `emo` не є початком `mono`, тож різні лінійки не зливаються. Кирилиця й
  латиниця — одне (`емоушен` = `emo…`, `РММА` = `pmma`).
* **Тека може нести уточнення для техніка** («опаковий всередині») — зайві
  слова з боку теки не заважають. Зайві слова з боку ТАБЛИЦІ заважають:
  якщо технік написав `mono a3`, тека `Emotions A3` не підходить.

Словник випадків зібрано з реальних назв тек за 60 днів (11.09.26, 2540 тек,
514 різних назв) — див. `tests/test_material_match.py`.

Це асистент, а не точна прив'язка (CLAUDE.md §4: шлях у `export` не містить
ні наряду, ні Sum3D ID). Остаточно вирішує оператор оком по STL.
"""

import re
import unicodedata

# Слова, які тека несе як підказку техніку, а не як назву матеріалу. Вони не
# мусять заважати збігу — і в жодному разі не мусять його СТВОРЮВАТИ, тому
# викидаються з обох боків.
NOISE_WORDS = frozenset({
    "опаковий", "опакова", "опакове", "опак", "опаk",
    "всередині", "всередине", "внутри", "середина",
    "opaq", "opaque", "inside", "in",
    "шт", "од",
})

# Слова, якими тека називає ВИД РОБОТИ, а не матеріал: «Повна анатомія колір
# А3». Бойовий випадок 10.09.26 (Oleksandr): клієнт назвав теку так, у ній
# немає ні «mono», ні іншої назви матеріалу — і рядок `mono a3` її не бачив,
# а видача відкривала позавчорашню партію з `mono a3`. Якщо тека, крім
# відтінку, несе ЛИШЕ такі слова, вона матеріалу не заперечує — збіг вирішує
# відтінок. Самі по собі ці слова збігу не створюють (як і NOISE_WORDS).
WORK_KIND_WORDS = frozenset({
    "повна", "повний", "повне", "полная", "полный", "full",
    "анатомія", "анатомия", "анатомічна", "анатомічний", "анатомическая", "anatomy", "anatomic",
    "колір", "кольору", "цвет", "цвета", "color", "colour",
    "коронка", "коронки", "коронок", "crown", "crowns",
    "каркас", "каркаси", "frame",
    "міст", "мост", "bridge",
    # Що зробити, а не з чого: «Фрезернути капу», «фрезерування циркону бліч 3».
    "фрезернути", "фрезерувати", "фрезерування", "фрезеровать", "фрезерование",
    "фрезерована", "фрезерований", "фрезерованая", "фрезерованый", "фрезер", "фрез",
})

# Теки, чия назва не каже нічого: Windows-«Новая папка», `attachments` з
# листа. Така тека в партії дня — кандидат для БУДЬ-ЯКОЇ роботи клієнта того
# дня (11.09.26: 38 партій із файлами просто в «Новая папка (N)», 5 тек
# `attachments`). Кандидат, не прив'язка — рішення за оператором по STL.
_GENERIC_FOLDER_RE = re.compile(
    r"^(?:(?:новая|нова)\s+папка|new\s+folder|attachments?|files?|файли)(?:\s*\(\d+\))?$"
)

# Відтінок: `a3`, `a3.5`, `b1`, відбілені `bl1`…`bl4`; кирилицею `а3`.
_SHADE_RE = re.compile(r"^(?:[a-dабвгдсб]\d(?:\.\d)?|bl[1-4])$")
# Кирилична літера відтінку, що ВИГЛЯДАЄ як латинська: людина набирає «А3» на
# українській розкладці, і для порівняння це мусить бути той самий `a3`, що в
# таблиці (Oleksandr 10.09.26: тека «…колір А3» з кириличною «А»).
_CYRILLIC_SHADE = str.maketrans({"а": "a", "в": "b", "б": "b", "с": "c", "д": "d"})
# Кодові кольори виробника (CLAUDE.md §3: `500` = A1 опак, `800` = A2 опак).
_CODE_RE = re.compile(r"^\d{3,4}$")
_NUMBER_RE = re.compile(r"^\d(?:\.\d)?$")
_SHADE_BODY = r"[a-dабвгдсб](?:[1-4]5|\d(?:\.\d)?)"
# `монос3`, `monoA35` — відтінок, склеєний зі словом.
_GLUED_SHADE_RE = re.compile(rf"^([a-zа-яіїєґ]{{3,}}?)({_SHADE_BODY})$")
# `a2tr`, `c2tr` — відтінок, до якого приклеєно позначку.
_SHADE_THEN_WORD_RE = re.compile(rf"^({_SHADE_BODY})([a-zа-яіїєґ]{{2,}})$")
# `a35` — це A3.5 (40 тек `mono a35` і стільки ж рядків таблиці): відтінку 35
# не буває, а крапку люди пропускають.
_SHADE_35_RE = re.compile(r"^([a-dабвгдсб])([1-4])5$")
# `АЗ` — кирилична «З» замість трійки.
_SHADE_Z_RE = re.compile(r"^([a-dабвгдсб])з$")
# Відбілені відтінки: `бліч 2`, `Блич-2`, `bleach 2`, `bl2`.
_BLEACH_WORDS = frozenset({"bl", "бл", "бліч", "блич", "блiч", "bleach", "блеач"})
_GLUED_BLEACH_RE = re.compile(r"^(?:bl|бл|бліч|блич|блiч|bleach)([1-4])$")

_MIN_PREFIX = 3
"""Коротше за це слово порівнюємо лише на рівність: `st`, `zr`, `s1` — це
самостійні позначення, а не скорочення чогось довшого."""


def _shade_form(token: str) -> str:
    bleach = _GLUED_BLEACH_RE.match(token)
    if bleach:
        return "bl" + bleach.group(1)
    point = _SHADE_35_RE.match(token)
    if point:
        token = f"{point.group(1)}{point.group(2)}.5"
    z_three = _SHADE_Z_RE.match(token)
    if z_three:
        token = z_three.group(1) + "3"
    if _SHADE_RE.match(token):
        return token[0].translate(_CYRILLIC_SHADE) + token[1:]
    return token


def _tokens(text: str | None) -> list[str]:
    """Слова назви, зведені до порівнюваного вигляду.

    Десятковий роздільник — крапка (`a 3,5`, `A3_5` → `a3.5`); решта
    розділових знаків, дефіс і крапка між літерами — пробіл (`А-3`,
    `mono_А3.5_циркон`, `4од.PMMA.B2`); відірвана від літери цифра
    приклеюється назад: людина пише `Emotions A 3,5`, програма має бачити
    той самий відтінок, що й у `emo a3.5`."""
    if not text:
        return []
    norm = unicodedata.normalize("NFC", text).strip().lower()
    norm = re.sub(r"(?<=\d)[,_](?=\d)", ".", norm)
    norm = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", norm)
    norm = re.sub(r"[,;:+()\[\]!?/\\_«»\"–—-]", " ", norm)
    raw = norm.split()
    # `t i t` — слово, набране окремими літерами.
    if len(raw) >= 2 and all(len(t) == 1 and t.isalpha() for t in raw):
        raw = ["".join(raw)]

    merged: list[str] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        nxt = raw[index + 1] if index + 1 < len(raw) else None
        if len(token) == 1 and token.isalpha() and nxt and _NUMBER_RE.match(nxt):
            merged.append(token + nxt)
            index += 2
            continue
        if token in _BLEACH_WORDS and nxt and nxt in ("1", "2", "3", "4"):
            merged.append("bl" + nxt)
            index += 2
            continue
        glued = _GLUED_SHADE_RE.match(token) or _SHADE_THEN_WORD_RE.match(token)
        if glued and not _GLUED_BLEACH_RE.match(token):
            # `монос3` (C3) без розділення не мав би відтінку, і збіг
            # вирішувала б сама назва — він знаходив теку `mono a3`.
            merged.extend(glued.groups())
        else:
            merged.append(token)
        index += 1
    return [_shade_form(t) for t in merged]


def _is_shade(token: str) -> bool:
    return bool(_SHADE_RE.match(token) or _CODE_RE.match(token))


def shades(text: str | None) -> set[str]:
    """Відтінки, названі в рядку (`a3`, `a3.5`, `bl2`, `500`)."""
    return {t for t in _tokens(text) if _is_shade(t)}


def words(text: str | None) -> set[str]:
    """Значущі слова назви матеріалу — без відтінків, чисел і уточнень."""
    return {
        t for t in _tokens(text)
        if not _is_shade(t) and not t.replace(".", "").isdigit() and t not in NOISE_WORDS
    }


# ── Порівняння слів ─────────────────────────────────────────────────────────

# Назву матеріалу люди пишуть і латиницею, і кирилицею: у таблиці `emo a2`,
# а тека — «Циркон емоушен a2» (Лагус, 11.09.26: тека лежала, видача її не
# бачила, бо латинське `emo` не є початком кириличного `емоушен`). Тому слова
# порівнюються в латинському записі. Лише для порівняння назв — відтінок
# вирішує окремо й раніше, тож збіг лінійки без збігу відтінку неможливий.
_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e",
    "є": "e", "э": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "і": "i",
    "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h",
    "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ы": "y", "ь": "", "ъ": "",
    "ю": "yu", "я": "ya", "'": "", "’": "", "ʼ": "",
})
# Кирилична літера, що ВИГЛЯДАЄ як латинська: «РММА» набрано кирилицею, але
# це PMMA, а не «rmma» (11.09.26: 9 тек `РММА …`, жодна не знаходилась).
_HOMOGLYPH = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ї": "i",
})

_VOWELS = "aeiouy"

# Одне поняття — різні слова. Ключ — позначка, яку отримують усі варіанти.
_SYNONYMS: dict[str, frozenset[str]] = {
    "wax": frozenset({"wax", "visk", "vosk"}),
    "tit": frozenset({"tit", "titan", "ti"}),
    "kapa": frozenset({"kapa", "splint", "miorelaks"}),
    "tr": frozenset({"tr", "trans", "translucent", "transliucent", "transliucent"}),
}
# Сімейства без відтінку: «титан корея» в таблиці й `tit` у теці — та сама
# робота з титану, хоч слова «корея» тека й не каже (41 рядок таблиці не
# знаходив жодної з 37 тек `tit`). Лише коли відтінку немає з обох боків.
_FAMILIES = ("tit", "wax", "kapa")


def _collapse(word: str) -> str:
    """Одна літера замість подвоєної: `kappa` = «капа» (Shehera, 11.09.26)."""
    out: list[str] = []
    for char in word:
        if not out or out[-1] != char:
            out.append(char)
    return "".join(out)


def _stem(word: str) -> str:
    """Слово без одного кінцевого голосного: «капа», «капу», «капи» — одне
    слово в різних відмінках. Лише для порівняння на РІВНІСТЬ основи."""
    if len(word) >= 4 and word[-1] in _VOWELS:
        return word[:-1]
    return word


def _forms(word: str) -> set[str]:
    """Латинські записи слова: транслітом і як «схожі літери»."""
    forms = {
        _collapse(word.translate(_LATIN)),
        _collapse(word.translate(_HOMOGLYPH).translate(_LATIN)),
    }
    # `капу2`, `tit1` — номер, приклеєний до слова, назви не змінює.
    return forms | {f.rstrip("0123456789") for f in forms if len(f.rstrip("0123456789")) >= _MIN_PREFIX}


def _concepts(forms: set[str]) -> set[str]:
    found = set()
    for form in forms:
        stem = _stem(form)
        for key, variants in _SYNONYMS.items():
            if form in variants or stem in {_stem(v) for v in variants}:
                found.add(key)
        if form.startswith("trans"):
            found.add("tr")
    return found


def _is_generic_zirconia(word: str) -> bool:
    """«циркон», «мультилеєр», `zr`, `z` — кажуть «це цирконій», але не яка
    лінійка. Тека `циркон а4` не заперечує ні `mono a4`, ні `emo a4`."""
    return any(
        form in ("zr", "z", "ml") or form.startswith(("mult", "cirkon", "zircon", "zirkon", "bagatoshar"))
        for form in _forms(word)
    )


def _word_covered(needle: str, haystack: set[str]) -> bool:
    """Чи є в теці слово, яким може бути це слово з таблиці.

    Коротке позначення порівнюється лише на рівність — інакше `st` збігалось
    би з будь-чим, що з нього починається."""
    needles = _forms(needle)
    others = {form for word in haystack for form in _forms(word)}
    if needles & others or _concepts(needles) & _concepts(others):
        return True
    for form in needles:
        if len(form) < _MIN_PREFIX:
            continue
        if any(
            other.startswith(form) or (len(other) >= _MIN_PREFIX and form.startswith(other))
            for other in others
        ):
            return True
        # Відмінок: `kapa` (з `kappa`) і `kapu` (з «капу») — та сама основа
        # `kap`. Тут лише рівність основ, НЕ префікс: інакше `mono` → `mon`
        # збігалось би з будь-яким словом на «мон».
        stem = _stem(form)
        if stem != form and len(stem) >= _MIN_PREFIX and any(_stem(o) == stem for o in others):
            return True
    return False


def _family(word_set: set[str]) -> set[str]:
    concepts = _concepts({form for word in word_set for form in _forms(word)})
    return {family for family in _FAMILIES if family in concepts}


def materials_match(sheet_material: str | None, folder_name: str | None) -> bool:
    """Чи описує назва теки той самий матеріал, що й колір роботи в таблиці."""
    sheet_shades, folder_shades = shades(sheet_material), shades(folder_name)
    sheet_words, folder_words = words(sheet_material), words(folder_name)

    if not sheet_shades and not sheet_words:
        return False
    # Тека без жодної інформації («Новая папка (3)», `attachments`) — кандидат
    # для будь-якої роботи клієнта того дня.
    if folder_name and _GENERIC_FOLDER_RE.match(" ".join(folder_name.lower().split())):
        return True
    # Титан, віск, капа — без відтінку з обох боків збіг сімейства вирішує.
    if not sheet_shades and not folder_shades and _family(sheet_words) & _family(folder_words):
        return True

    # Відтінок названо з обох боків — він і вирішує.
    if sheet_shades and folder_shades and not (sheet_shades & folder_shades):
        return False
    # Робота має відтінок, а тека — ні: підтвердити збіг нічим.
    if sheet_shades and not folder_shades:
        return False

    zirconia_words = {w for w in folder_words if _is_generic_zirconia(w)}
    sheet_core = {w for w in sheet_words if w not in WORK_KIND_WORDS and not _is_generic_zirconia(w)}
    folder_core = folder_words - WORK_KIND_WORDS - zirconia_words

    if not sheet_core:
        return bool(sheet_shades & folder_shades)
    # Тека називає лише вид роботи чи «циркон» і відтінок («Повна анатомія
    # колір А3», «циркон а4», просто «А3»): матеріалу вона не заперечує, тож
    # збігається за відтінком. «Циркон» — лише для цирконієвої роботи. Це
    # кандидат, а не прив'язка — остаточно вирішує оператор по STL (CLAUDE.md §2).
    if sheet_shades and not folder_core:
        if not zirconia_words:
            return True
        from app.material_classifier import ZIRCON, classify_material

        return classify_material(sheet_material) == ZIRCON
    return all(_word_covered(word, folder_words) for word in sheet_core)
