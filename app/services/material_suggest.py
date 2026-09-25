"""Material typing suggestions for the manual «Додати роботу» form.

Two sources, one list (owner decision 15.09.26 — a shortcut is just a very
short query into the same suggestion list, not a second mechanism):

  * shortcuts — admin-entered `мл → mono`; the material WORD only, colour typed
    as-is (material_catalog.add_shortcut). A unique shortcut can Tab-expand.
  * frecency — the distinct past `material_color` spellings, canonicalised into
    clusters (match_key folds Cyrillic/Latin so `моно а3` and `mono a3` are one
    cluster) and ranked by frequency×recency, so the daily material rises to the
    top and last July's one-off sinks.

The point the operator actually sees is the RECOGNITION BADGE: a spelling the
classifier can't place (material_id NULL) shows no badge, catching a typo before
it becomes a row without a material stripe.

Raw past values are NOT suggested as-is — that would let the operator pick
blindly between two spellings of one material and tomorrow there'd be a third.
The representative of each cluster is its most-frequent spelling.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.material_catalog import (
    MaterialCatalogError,
    add_shortcut,
    ensure_seeded,
    list_shortcuts,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.material_classifier import match_key, match_keys
from app.material_match import _CYRILLIC_SHADE, _SHADE_RE, _tokens as _mm_tokens
from app.models import Material, Order

# Short badge per material category. Kept here (a display concern) rather than in
# the catalog: unknown or non-production names get no badge, which is the same
# "not recognised" signal as material_id NULL.
_BADGE_BY_NAME = {
    "Цирконій": "Zr",
    "ПММА": "PMMA",
    "СЛМ": "SLM",
    "Титан": "Ti",
    "Віск": "Wax",
}

_FRECENCY_WINDOW_DAYS = 90
_RECENT_WINDOW_DAYS = 30
_CACHE_TTL_SECONDS = 300
_DEFAULT_LIMIT = 8


@dataclass
class Suggestion:
    kind: str  # "shortcut" | "frecency"
    text: str  # canonical spelling to insert into the field
    badge: str | None  # "Zr" / "PMMA" / … or None when unrecognised
    material_id: int | None
    shortcut: str | None = None
    count: int = 0  # works in the frecency window — for «445 робіт»
    is_expand: bool = False  # unique shortcut match → Tab expands


@dataclass
class _Cluster:
    key: str
    c30: int = 0
    c90: int = 0
    spellings: Counter[str] = field(default_factory=Counter)
    mid_votes: Counter[int] = field(default_factory=Counter)


@dataclass
class _Entry:
    key: str
    text: str
    c30: int
    c90: int
    material_id: int | None
    badge: str | None


_cache_lock = threading.Lock()
_cache_entries: list[_Entry] = []
_cache_built_at: float = 0.0


def badge_for_name(name: str | None) -> str | None:
    return _BADGE_BY_NAME.get(name or "")


def _build_frecency(session: Session) -> list[_Entry]:
    """Cluster the last 90 days of colour values into frecency entries. One query,
    grouped in Python because match_key folding can't run in SQL."""
    now = utc_now()
    cutoff90 = now - timedelta(days=_FRECENCY_WINDOW_DAYS)
    cutoff30 = now - timedelta(days=_RECENT_WINDOW_DAYS)
    rows = session.execute(
        select(Order.material_color, Order.material_id, Order.created_at).where(
            Order.created_at >= cutoff90,
            Order.material_color.isnot(None),
        )
    ).all()

    clusters: dict[str, _Cluster] = {}
    for color, mid, created in rows:
        text = (color or "").strip()
        if not text:
            continue
        key = match_key(text)
        if not key:
            continue
        cluster = clusters.get(key)
        if cluster is None:
            cluster = clusters[key] = _Cluster(key=key)
        cluster.c90 += 1
        if created is not None and created >= cutoff30:
            cluster.c30 += 1
        cluster.spellings[text] += 1
        if mid is not None:
            cluster.mid_votes[mid] += 1

    name_by_id = {mid: name for mid, name in session.execute(select(Material.id, Material.name)).all()}
    entries: list[_Entry] = []
    for cluster in clusters.values():
        text = cluster.spellings.most_common(1)[0][0]
        mid = cluster.mid_votes.most_common(1)[0][0] if cluster.mid_votes else None
        entries.append(
            _Entry(
                key=cluster.key,
                text=text,
                c30=cluster.c30,
                c90=cluster.c90,
                material_id=mid,
                badge=badge_for_name(name_by_id.get(mid)) if mid is not None else None,
            )
        )
    return entries


def _frecency_entries(session: Session) -> list[_Entry]:
    """Cached frecency index. Suggestions are advisory and the data changes
    slowly, so a 5-minute TTL keeps per-keystroke calls off the whole table."""
    global _cache_entries, _cache_built_at
    now = utc_now().timestamp()
    with _cache_lock:
        if now - _cache_built_at <= _CACHE_TTL_SECONDS and _cache_built_at:
            return _cache_entries
    entries = _build_frecency(session)
    with _cache_lock:
        _cache_entries = entries
        _cache_built_at = now
    return entries


def invalidate_cache() -> None:
    """Drop the frecency cache (tests, or after a large reclassification)."""
    global _cache_built_at
    with _cache_lock:
        _cache_built_at = 0.0


def usage_for_expansion(session: Session, expansion: str) -> int:
    """Works in the frecency window whose colour starts with this expansion word
    — the «вжито» column that tells the admin which spelling earns the shortest
    shortcut."""
    ek = match_key(expansion)
    if not ek:
        return 0
    return sum(e.c90 for e in _frecency_entries(session) if e.key.startswith(ek))


#: Написання, що трапилось у базі рідше за це, вважаємо разовим/одруківкою й
#: авто-скорочення на нього не заводимо — інакше 309 написань дали б стіну
#: криптичних кодів. Власник дозаводить рідкісні руками.
_AUTOFILL_MIN_COUNT = 3


def _autoshort_candidates(expansion: str) -> list[str]:
    """Кандидати на авто-скорочення: перша літера слова матеріалу + колір без
    пробілів і розділювачів (`mono a3.5` → `ma35`), з розширенням початку слова
    при колізії (`mo…`, `mon…`)."""
    tokens = expansion.split()
    if not tokens:
        return []
    head = tokens[0]
    color = re.sub(r"[^0-9a-zа-яіїєґ]", "", "".join(tokens[1:]).lower())
    return [head[:n].lower() + color for n in (1, 2, 3) if len(head) >= n]


def autofill_shortcuts(
    session: Session, *, min_count: int = _AUTOFILL_MIN_COUNT
) -> tuple[int, int]:
    """Завести скорочення на всі розпізнані написання з бази (mono a1/a2/a3…,
    emo…, pmma…). Авто, не руками (власник 16.09.26). Пропускає нерозпізнане й
    нематеріал (без бейджа), уже наявні написання й колізії скорочень; на
    останні віддає розширений код або пропускає. Повертає (додано, пропущено).

    Скидає frecency-кеш кличе викликач — «вжито» на екрані має врахувати нове."""
    rows = list_shortcuts(session)
    used_keys = {row.key for row in rows}
    have_expansion = {match_key(row.expansion) for row in rows}

    added = skipped = 0
    for entry in sorted(_frecency_entries(session), key=lambda e: -e.c90):
        if entry.badge is None:  # нерозпізнане або «Не матеріал» — не скорочуємо
            continue
        if entry.c90 < min_count:
            continue
        if match_key(entry.text) in have_expansion:
            continue
        placed = False
        for cand in _autoshort_candidates(entry.text):
            key = match_key(cand)
            if not key or key in used_keys:
                continue
            try:
                add_shortcut(session, cand, entry.text)
            except MaterialCatalogError:
                continue
            used_keys.add(key)
            have_expansion.add(match_key(entry.text))
            added += 1
            placed = True
            break
        if not placed:
            skipped += 1
    return added, skipped


# ── Канонічні підказки для листа (власник 25.09.26) ───────────────────────────
# Картка листа пропонувала чіпи з СИРИХ написань лабораторії («моноліт А2»,
# «моноліт Д3», «Моноліт а 3») — кожен лист плодив би ще одне. Канон — ЛАТИНОЮ
# (`mono a2`, `emo a2`; рішення власника 16.09.26), і він уже є в даних: це
# представники frecency-кластерів без кирилиці. Тут — звести здогад листа
# («Monolith», «моноліт а 3», «емо») до слова канону й підставити відтінки листа.

_CYRILLIC = re.compile(r"[а-яіїєґё]", re.IGNORECASE)
_MIN_PREFIX_WORD = 3
"""Слово канону коротше за це збігається лише ЦІЛКОМ: інакше `z` (з `z nat a3`)
ставав би «матеріалом» будь-якого здогаду на з-, наприклад «зуб»."""


def _latin_entries(session: Session) -> list[_Entry]:
    """Frecency-представники без кирилиці, найчастіші першими."""
    return sorted(
        (e for e in _frecency_entries(session) if not _CYRILLIC.search(e.text)),
        key=lambda e: (-e.c90, e.text),
    )


def _word_of(entry: _Entry) -> str:
    return entry.key.split(" ", 1)[0]


def _colour_of(entry: _Entry) -> str:
    parts = entry.key.split(" ", 1)
    return parts[1].replace(" ", "") if len(parts) == 2 else ""


def _shade_key(value: str) -> str:
    """Відтінок для звіряння, як у `material_match`: `А 3,5` / `a3.5` → `a3.5`
    (кирилична літера відтінку = латинська)."""
    return "".join(_mm_tokens(value)).lower().translate(_CYRILLIC_SHADE)


def shades_in(text: str | None) -> list[str]:
    """Відтінки шкали Vita в рядку здогаду: «моноліт а 3» → [`a3`]. Решта слів
    («корея», «репліка») відтінком не є — з них чіп не складаємо."""
    out: list[str] = []
    for token in _mm_tokens(text):
        if _SHADE_RE.match(token):
            key = _shade_key(token)
            if key not in out:
                out.append(key)
    return out


_MIN_WORD_USES = 5
"""Лінія матеріалу — слово, яким цех написав щонайменше стільки робіт за 90
днів. Одноразова описка в таблиці (`pmmma a3`, `monolith a3`) каноном не стає й
у варіанти не лізе."""
_FUZZY_WORD = 85.0
"""Схожість слова з тексту листа на слово канону («капа» → `kappa`). Лише для
слів ≥4 літер: короткі збігаються надто легко."""


def _word_weights(session: Session) -> Counter[str]:
    weight: Counter[str] = Counter()
    for entry in _latin_entries(session):
        weight[_word_of(entry)] += entry.c90
    return Counter({w: n for w, n in weight.items() if n >= _MIN_WORD_USES})


def _match_word(
    token: str, weight: Counter[str], *, allow_short: bool, fuzzy: bool = False
) -> str | None:
    """Слово канону для одного згорнутого слова: канон — його початок або воно —
    початок канону (обидва ≥3 літер; коротше — лише повний збіг, і то лише коли
    `allow_short`). Кілька кандидатів (`mono` і рідкісне `monolith`) — вирішує
    ЧАСТОТА: канон той, яким цех пише щодня."""
    if not token:
        return None
    best: tuple[int, str] | None = None
    for word, total in weight.items():
        if not word:
            continue
        if len(word) < _MIN_PREFIX_WORD or len(token) < _MIN_PREFIX_WORD:
            hit = allow_short and word == token
        else:
            hit = token.startswith(word) or word.startswith(token) or (
                fuzzy and len(token) >= 4 and len(word) >= 4
                and fuzz.ratio(token, word) >= _FUZZY_WORD
            )
        if hit and (best is None or (total, word) > best):
            best = (total, word)
    return best[1] if best else None


def canonical_material_word(
    session: Session, guess: str | None, context: str | None = None
) -> str | None:
    """Слово канону: «Monolith» / «моноліт» → `mono`, «емо» → `emo`, «титан» → `tit`.

    Спершу ПЕРШЕ слово здогаду. Здогад матеріалу не назвав («B1») — шукаємо в
    тексті замовника (`context`) перше слово, що зводиться до канону: клієнти
    пишуть криво («Колір B1, циркон, Monolight…» → `mono`, власник 25.09.26).
    У тексті пропускаємо числа, відтінки й короткі слова канону (`z`, `s1`) —
    там вони збігались би з чим завгодно. None — впізнати нема з чим."""
    weight = _word_weights(session)
    key = match_key(guess or "")
    word = _match_word(key.split(" ", 1)[0] if key else "", weight, allow_short=True)
    return word or _context_word(session, context, weight)


def _context_word(session: Session, context: str | None, weight: Counter[str]) -> str | None:
    """Перше слово тексту замовника, що зводиться до канону (з нечітким збігом
    для кривих написань: «Monolight» → `mono`, «капа» → `kappa`).

    Вільний текст — не здогад: у ньому звичайні слова. Коротке слово канону
    (`tit`, `emo`, `wax`) як ПОЧАТОК слова тексту приймаємо лише тоді, коли
    бібліотека матеріалів сама впізнає це слово як матеріал: «титан» → так,
    «тітка» → ні."""
    alias_rows = None
    names = None
    for token in _mm_tokens(context):
        if token.isdigit() or _SHADE_RE.match(token) or not any(ch.isalpha() for ch in token):
            continue
        key = match_key(token)
        word = _match_word(key, weight, allow_short=False, fuzzy=True)
        if not word:
            continue
        if len(word) <= _MIN_PREFIX_WORD and key != word:
            if alias_rows is None:
                ensure_seeded(session)
                alias_rows = load_alias_rows(session)
                names = material_id_by_name(session)
            if resolve_material_id(token, alias_rows, names or {}) is None:
                continue
        return word
    return None


def canonical_material(
    session: Session, guess: str | None, shade: str | None, context: str | None = None
) -> str | None:
    """Канонічний рядок «матеріал колір» для відтінку листа: наявне латинське
    написання з даних (`mono a3,5` — як цех його пише найчастіше), або, якщо
    такого ще не було, `слово відтінок`. None — слово матеріалу не впізнано."""
    word = canonical_material_word(session, guess, context)
    if not word:
        return None
    if not shade:
        return word
    want = _shade_key(shade)
    for entry in _latin_entries(session):
        if _word_of(entry) == word and _colour_of(entry) == want:
            return entry.text
    return f"{word} {want}"


def _wanted_shades(guess: str | None, shades: list[str] | None, context: str | None) -> list[str]:
    return [_shade_key(s) for s in (shades or [])] or shades_in(guess) or shades_in(context)


def best_material(session: Session, guess: str | None, context: str | None = None) -> str | None:
    """ОДНА впевнена відповідь для поля матеріалу: матеріал упізнано (у здогаді чи
    в тексті) і відтінок рівно один. Інакше None — поле лишається як було,
    вгадувати колір чи матеріал не можна."""
    wanted = _wanted_shades(guess, None, context)
    if len(wanted) != 1:
        return None
    if not canonical_material_word(session, guess, context):
        return None
    return canonical_material(session, guess, wanted[0], context)


def row_label(session: Session, guess: str | None, context: str | None = None) -> dict | None:
    """Підпис чіпа в РЯДКУ списку листів: `{"badge": "Zr", "text": "mono b1"}`
    (власник 25.09.26: «в чіпах писати Zr mono b1»). Та сама відповідь, що
    підставиться в поле картки (`best_material`); колір невідомий чи їх кілька —
    лише лінія (`mono`). Матеріал не впізнано — None (чіп тоді будує старий
    `mail_material_badge` зі здогаду, або його немає)."""
    word = canonical_material_word(session, guess, context)
    if not word:
        return None
    text = best_material(session, guess, context) or word
    badge = next((e.badge for e in _latin_entries(session) if _word_of(e) == word and e.badge), None)
    return {"badge": badge, "text": text}


def canonical_suggestions(
    session: Session,
    guess: str | None,
    shades: list[str] | None = None,
    *,
    context: str | None = None,
    limit: int = 3,
) -> list[Suggestion]:
    """Чіпи матеріалу на картці листа — ЛИШЕ латинський канон, як у ручному
    додаванні (`mono a2`, `emo a2`).

    Спершу НАЙІМОВІРНІШЕ (`kind="best"`): упізнаний матеріал (здогад або текст
    замовника) × відтінки листа (передані з `mail_color_split`, зі здогаду чи з
    тексту). Далі ВАРІАНТИ (`kind="alt"`): той самий відтінок в інших лініях того
    ж матеріалу (`emo b1` поряд із `mono b1`), або — без відтінку — найчастіші
    написання. Матеріал словом не впізнано, але бібліотека знає його
    («Цирконій», «циркон») — лише варіанти цього матеріалу. Інакше — нічого:
    вгаданий чіп гірший за відсутній (власник 25.09.26: «тільки правильно
    підказувало, а вже потім варіанти»)."""
    entries = _latin_entries(session)
    word = canonical_material_word(session, guess, context)
    wanted = _wanted_shades(guess, shades, context)
    out: list[Suggestion] = []
    seen: set[str] = set()

    def add(text: str, entry: _Entry | None, kind: str, badge: str | None = None) -> None:
        k = match_key(text)
        if k in seen or len(out) >= limit:
            return
        seen.add(k)
        out.append(Suggestion(
            kind=kind, text=text,
            badge=entry.badge if entry else badge,
            material_id=entry.material_id if entry else None,
            count=entry.c90 if entry else 0,
        ))

    by_text = {e.text: e for e in entries}
    mid: int | None = None
    if word:
        own = [e for e in entries if _word_of(e) == word]
        badge = next((e.badge for e in own if e.badge), None)
        mids = Counter(e.material_id for e in own if e.material_id is not None)
        mid = mids.most_common(1)[0][0] if mids else None
        if wanted:
            for shade in wanted:
                text = canonical_material(session, guess, shade, context)
                if text:
                    add(text, by_text.get(text), "best", badge)
    else:
        # Лише за ЗДОГАДОМ («Цирконій»), не за всім текстом: у тексті листа
        # (службовий лист скриньки, підпис) бібліотека «знаходила» цирконій там,
        # де про матеріал не було ні слова.
        ensure_seeded(session)
        mid = resolve_material_id(guess or "", load_alias_rows(session), material_id_by_name(session))
        if mid is None:
            return []
    weight = _word_weights(session)
    # Замовник у тексті назвав ІНШУ лінію, ніж здогад («ПММА» у темі, «капа» в
    # тексті) — вона першим варіантом.
    ctx_word = _context_word(session, context, weight)
    if ctx_word and ctx_word != word:
        for entry in entries:
            if _word_of(entry) == ctx_word and (not wanted or _colour_of(entry) in wanted):
                add(entry.text, entry, "alt")
                break
    # Відтінку немає — найчастіші написання впізнаної лінії.
    if word and not wanted:
        for entry in entries:
            if _word_of(entry) == word:
                add(entry.text, entry, "alt")
    # Варіанти: той самий матеріал (Zr, PMMA…), інша РЕАЛЬНА лінія — з відтінком листа.
    for entry in entries:
        if (
            mid is not None and entry.material_id == mid and _word_of(entry) in weight
            and (not wanted or _colour_of(entry) in wanted)
        ):
            add(entry.text, entry, "alt")
    return out


def suggest_materials(
    session: Session, q: str, *, limit: int = _DEFAULT_LIMIT
) -> list[Suggestion]:
    """Rank shortcut expansions and frecency spellings for query `q`."""
    # Кілька ключів запиту: гомогліфний + розкладко-своп (забута розкладка,
    # `ьщтщ ф3` → `mono a3`). Збіг за БУДЬ-ЯКИМ.
    qkeys = match_keys(q)
    if not qkeys:
        return []

    ensure_seeded(session)
    alias_rows = load_alias_rows(session)
    name_to_id = material_id_by_name(session)
    id_to_name = {mid: name for name, mid in name_to_id.items()}

    # ── Shortcuts ──────────────────────────────────────────────────────────
    # Match when the query is a prefix of the shortcut (still typing it) or the
    # shortcut is a prefix of the query (already typed it, colour follows). Tab
    # may expand ONLY when exactly one shortcut matches — an ambiguous shortcut
    # must never rewrite the field under the operator's hand.
    #
    # A shortcut expands ONLY the first word (the material), so once the operator
    # has already typed that word out in full, the shortcut is a no-op — it would
    # replace the word with itself and just re-state what's on screen. Drop it
    # then: after «emo a2» the operator wants the frecency row «emo a2», not a
    # first suggestion offering the bare material «emo» (owner 16.09.26). The
    # test is on the RAW first token, not the fold key, so a Cyrillic shortcut
    # typed toward a Latin canon (`емо` → `emo`) still counts as a real change.
    first_token = q.strip().split()[0] if q.strip() else ""
    shortcut_rows = list_shortcuts(session)
    shortcut_matches = [
        row
        for row in shortcut_rows
        if any(row.key.startswith(k) or k.startswith(row.key) for k in qkeys)
        and row.expansion.strip().casefold() != first_token.casefold()
    ]
    # Який рядок Tab розгортає. ТОЧНИЙ збіг ключа виграє навіть коли інші
    # скорочення мають його за префікс: `ma3` = `mono a3`, хоча поруч `ma35` =
    # `mono a3,5`. Без цього кожне скорочення з довшим сусідом ставало
    # «неоднозначним» і Tab не розгортав нічого (скарга 16.09.26). Інакше —
    # звичне правило: розгортаємо, лише коли збіг єдиний.
    exact = [row for row in shortcut_matches if row.key in qkeys]
    if len(exact) == 1:
        expand_row = exact[0]
    elif len(shortcut_matches) == 1:
        expand_row = shortcut_matches[0]
    else:
        expand_row = None

    suggestions: list[Suggestion] = []
    seen: set[str] = set()
    for row in shortcut_matches:
        text_key = match_key(row.expansion)
        mid = resolve_material_id(row.expansion, alias_rows, name_to_id)
        suggestions.append(
            Suggestion(
                kind="shortcut",
                text=row.expansion,
                badge=badge_for_name(id_to_name.get(mid)) if mid is not None else None,
                material_id=mid,
                shortcut=row.shortcut,
                is_expand=row is expand_row,
            )
        )
        seen.add(text_key)

    # ── Frecency ───────────────────────────────────────────────────────────
    scored: list[tuple[float, _Entry]] = []
    for entry in _frecency_entries(session):
        if not any(k in entry.key for k in qkeys):
            continue
        prefix_bonus = 5 if any(entry.key.startswith(k) for k in qkeys) else 0
        score = entry.c90 + 2 * entry.c30 + prefix_bonus
        scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].text))

    for _score, entry in scored:
        if len(suggestions) >= limit:
            break
        if entry.key in seen:
            continue
        seen.add(entry.key)
        suggestions.append(
            Suggestion(
                kind="frecency",
                text=entry.text,
                badge=entry.badge,
                material_id=entry.material_id,
                count=entry.c90,
            )
        )

    return suggestions[:limit]


def explain_material(session: Session, guess: str | None, context: str | None = None) -> dict:
    """Покроково, ЧОМУ картка листа підставила (чи ні) матеріал — для MCP
    `kmill_mail_material` (власник 25.09.26: «циркон блич 2 емоутион мульти» на
    проді не став `emo bl2`, хоча на dev ставав). Той самий ланцюг, що
    `best_material`: слово зі здогаду → слово з тексту → відтінки.

    Текст листа НЕ повертається цілком (там бувають імена пацієнтів) — лише
    слова, що зіставились із каноном, і вердикт по кожному."""
    weight = _word_weights(session)
    key = match_key(guess or "")
    first = key.split(" ", 1)[0] if key else ""
    guess_word = _match_word(first, weight, allow_short=True)
    tokens = []
    alias_rows = None
    names: dict = {}
    context_word = None
    for token in _mm_tokens(context):
        if token.isdigit() or _SHADE_RE.match(token) or not any(ch.isalpha() for ch in token):
            continue
        tkey = match_key(token)
        word = _match_word(tkey, weight, allow_short=False, fuzzy=True)
        if not word:
            continue
        verdict = "прийнято"
        if len(word) <= _MIN_PREFIX_WORD and tkey != word:
            if alias_rows is None:
                ensure_seeded(session)
                alias_rows = load_alias_rows(session)
                names = material_id_by_name(session)
            if resolve_material_id(token, alias_rows, names or {}) is None:
                verdict = "відкинуто: бібліотека не знає слово як матеріал"
        tokens.append({"слово": token, "ключ": tkey, "канон": word, "вердикт": verdict})
        if verdict == "прийнято" and context_word is None:
            context_word = word
    wanted = _wanted_shades(guess, None, context)
    return {
        "здогад": guess,
        "слово_здогаду": guess_word,
        "слова_тексту": tokens,
        "слово_матеріалу": guess_word or context_word,
        "відтінки": wanted,
        "підказки": [s.text for s in canonical_suggestions(session, guess, context=context)],
        "поле_картки": best_material(session, guess, context),
        "канон_слова": sorted(weight),
    }
