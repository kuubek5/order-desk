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
  це завжди «ні», хай назви скільки завгодно схожі. Тому `a 3,5` зводиться до
  `a3.5` ще до порівняння.
* **Назва матеріалу може бути скороченою.** `emo` → `Emotions`, `mono` →
  `Monolith`: слово з таблиці має бути ПОЧАТКОМ слова з теки (або навпаки).
  `emo` не є початком `mono`, тож різні лінійки не зливаються.
* **Тека може нести уточнення для техніка** («опаковий всередині») — зайві
  слова з боку теки не заважають. Зайві слова з боку ТАБЛИЦІ заважають:
  якщо технік написав `mono a3`, тека `Emotions A3` не підходить.

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
})

# Відтінок: `a3`, `a3.5`, `b1`, кирилицею `а3` — і те саме з пробілом (`a 3,5`),
# яке склеюється раніше.
_SHADE_RE = re.compile(r"^[a-dабвгдс](?:\d(?:\.\d)?)$")
# Кирилична літера відтінку, що ВИГЛЯДАЄ як латинська: людина набирає «А3» на
# українській розкладці, і для порівняння це мусить бути той самий `a3`, що в
# таблиці (Oleksandr 10.09.26: тека «…колір А3» з кириличною «А»).
_CYRILLIC_SHADE = str.maketrans({"а": "a", "в": "b", "с": "c", "д": "d"})
# Кодові кольори виробника (CLAUDE.md §3: `500` = A1 опак, `800` = A2 опак).
_CODE_RE = re.compile(r"^\d{3,4}$")
_NUMBER_RE = re.compile(r"^\d(?:\.\d)?$")

_MIN_PREFIX = 3
"""Коротше за це слово порівнюємо лише на рівність: `st`, `zr`, `s1` — це
самостійні позначення, а не скорочення чогось довшого."""


def _tokens(text: str | None) -> list[str]:
    """Слова назви, зведені до порівнюваного вигляду.

    Кома в десяткових — крапка (`a 3,5` → `a3.5`), розділові знаки по краях
    зрізаються (тека `Віск.` = слово `віск`), а відірвана від літери цифра
    приклеюється назад: людина пише `Emotions A 3,5`, програма має бачити
    той самий відтінок, що й у `emo a3.5`."""
    if not text:
        return []
    norm = unicodedata.normalize("NFC", text).strip().lower().replace(",", ".")
    raw = [t.strip(".,;:()[]-–—_/\\") for t in norm.split()]
    raw = [t for t in raw if t]

    merged: list[str] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        nxt = raw[index + 1] if index + 1 < len(raw) else None
        if len(token) == 1 and token.isalpha() and nxt and _NUMBER_RE.match(nxt):
            merged.append(token + nxt)
            index += 2
            continue
        merged.append(token)
        index += 1
    return [t[0].translate(_CYRILLIC_SHADE) + t[1:] if _SHADE_RE.match(t) else t for t in merged]


def shades(text: str | None) -> set[str]:
    """Відтінки, названі в рядку (`a3`, `a3.5`, `500`)."""
    return {t for t in _tokens(text) if _SHADE_RE.match(t) or _CODE_RE.match(t)}


def words(text: str | None) -> set[str]:
    """Значущі слова назви матеріалу — без відтінків і без уточнень."""
    return {
        t for t in _tokens(text)
        if not _SHADE_RE.match(t) and not _CODE_RE.match(t) and t not in NOISE_WORDS
    }


def _word_covered(needle: str, haystack: set[str]) -> bool:
    """Чи є в теці слово, яким може бути це слово з таблиці.

    Коротке позначення порівнюється лише на рівність — інакше `st` збігалось
    би з будь-чим, що з нього починається."""
    if needle in haystack:
        return True
    if len(needle) < _MIN_PREFIX:
        return False
    return any(
        other.startswith(needle) or (len(other) >= _MIN_PREFIX and needle.startswith(other))
        for other in haystack
    )


def materials_match(sheet_material: str | None, folder_name: str | None) -> bool:
    """Чи описує назва теки той самий матеріал, що й колір роботи в таблиці."""
    sheet_shades, folder_shades = shades(sheet_material), shades(folder_name)
    sheet_words, folder_words = words(sheet_material), words(folder_name)

    if not sheet_shades and not sheet_words:
        return False

    # Відтінок названо з обох боків — він і вирішує.
    if sheet_shades and folder_shades and not (sheet_shades & folder_shades):
        return False
    # Робота має відтінок, а тека — ні: підтвердити збіг нічим.
    if sheet_shades and not folder_shades:
        return False

    if not sheet_words:
        return bool(sheet_shades & folder_shades)
    # Тека називає лише вид роботи й відтінок («Повна анатомія колір А3»):
    # матеріалу вона не заперечує, тож збігається за відтінком. Це кандидат,
    # а не прив'язка — остаточно вирішує оператор по STL (CLAUDE.md §2).
    if sheet_shades and folder_words and folder_words <= WORK_KIND_WORDS:
        return True
    return all(_word_covered(word, folder_words) for word in sheet_words)
