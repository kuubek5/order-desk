"""Злиття дублікатів клієнтів — з рішенням власника по КОЖНІЙ парі, без
автомату (крок 5 SUGGEST_BRIEF).

Різні написання одного клієнта (`Кривовид` / `Євген Кривовид`, `Pavlenko` /
`pavlenko`) зводяться до канону, який обирає власник. Канон застосовується в
підказках клієнта і на екрані клієнтів; ВИДАЧУ свідомо не чіпаємо — там ім'я є
фізичним ключем групування коронок (рішення власника 16.09.26).

Кандидатів пропонує rapidfuzz; рішення (`merge` або `skip` — «не дублі»)
зберігаються в ClientMerge за парою нормалізованих ключів, тож розібране не
вертається. Нічого не зливається саме собою.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.material_classifier import match_key
from app.models import ClientMerge, Order

#: Скільки схожості достатньо, щоб ПОКАЗАТИ пару як можливий дубль. Не для
#: автозлиття — лише для показу; вирішує власник.
_CANDIDATE_THRESHOLD = 82.0
_MAX_CANDIDATES = 40
_NAME_WINDOW_DAYS = 400  # звідки беремо активні імена (майже вся черга)


@dataclass
class Candidate:
    a_name: str
    a_count: int
    b_name: str
    b_count: int
    score: int


def _pair_keys(name_a: str, name_b: str) -> tuple[str, str]:
    """Впорядкована пара ключів — ідентичність пари незалежно від порядку."""
    ka, kb = match_key(name_a), match_key(name_b)
    return (ka, kb) if ka <= kb else (kb, ka)


def _pair_score(name_a: str, name_b: str) -> float:
    """Схожість двох імен. Беремо максимум по сирих і по згорнутих формах, щоб
    зловити і різний регістр/пробіли, і різний алфавіт (гомогліфи)."""
    raw = fuzz.token_set_ratio(name_a.lower(), name_b.lower())
    folded = fuzz.token_set_ratio(match_key(name_a), match_key(name_b))
    return max(raw, folded)


def _distinct_names(session: Session) -> list[tuple[str, int]]:
    cutoff = utc_now() - timedelta(days=_NAME_WINDOW_DAYS)
    rows = session.execute(
        select(Order.client_name, func.count())
        .where(Order.client_name.isnot(None), Order.created_at >= cutoff)
        .group_by(Order.client_name)
    ).all()
    names: dict[str, int] = {}
    for name, count in rows:
        text = (name or "").strip()
        if text:
            names[text] = names.get(text, 0) + count
    return sorted(names.items(), key=lambda kv: -kv[1])


def _decided_pairs(session: Session) -> set[tuple[str, str]]:
    return {
        (row.a_key, row.b_key)
        for row in session.execute(select(ClientMerge)).scalars()
    }


def merge_map(session: Session) -> dict[str, str]:
    """{ключ_варіанта: канонічне_ім'я} з підтверджених злиттів."""
    return {
        row.variant_key: row.canonical_name
        for row in session.execute(
            select(ClientMerge).where(ClientMerge.kind == "merge")
        ).scalars()
        if row.variant_key
    }


def find_candidates(session: Session, *, limit: int = _MAX_CANDIDATES) -> list[Candidate]:
    """Пари схожих імен, ще не розібрані власником, від найсхожіших."""
    names = _distinct_names(session)
    decided = _decided_pairs(session)
    merged_variants = set(merge_map(session))  # уже зведені — не пропонуємо знову

    out: list[Candidate] = []
    for i in range(len(names)):
        name_a, count_a = names[i]
        key_a = match_key(name_a)
        if key_a in merged_variants:
            continue
        for j in range(i + 1, len(names)):
            name_b, count_b = names[j]
            key_b = match_key(name_b)
            if key_b in merged_variants:
                continue
            if key_a == key_b:
                continue  # той самий згорнутий ключ — це вже один кластер підказок
            if _pair_keys(name_a, name_b) in decided:
                continue
            score = _pair_score(name_a, name_b)
            if score < _CANDIDATE_THRESHOLD:
                continue
            out.append(
                Candidate(
                    a_name=name_a, a_count=count_a,
                    b_name=name_b, b_count=count_b, score=int(round(score)),
                )
            )
    out.sort(key=lambda c: (-c.score, -(c.a_count + c.b_count)))
    return out[:limit]


def _upsert(session: Session, a_key: str, b_key: str) -> ClientMerge:
    row = session.scalar(
        select(ClientMerge).where(ClientMerge.a_key == a_key, ClientMerge.b_key == b_key)
    )
    if row is None:
        row = ClientMerge(a_key=a_key, b_key=b_key, kind="skip")
        session.add(row)
    return row


def record_merge(session: Session, canonical_name: str, variant_name: str) -> None:
    """Зберегти рішення «злити»: `variant_name` → `canonical_name`."""
    canonical_name = (canonical_name or "").strip()
    variant_name = (variant_name or "").strip()
    if not canonical_name or not variant_name:
        return
    a_key, b_key = _pair_keys(canonical_name, variant_name)
    row = _upsert(session, a_key, b_key)
    row.kind = "merge"
    row.variant_key = match_key(variant_name)
    row.variant_name = variant_name
    row.canonical_name = canonical_name
    session.flush()


def record_skip(session: Session, name_a: str, name_b: str) -> None:
    """Зберегти рішення «не дублі» — пара більше не пропонується."""
    a_key, b_key = _pair_keys(name_a, name_b)
    row = _upsert(session, a_key, b_key)
    row.kind = "skip"
    row.variant_key = ""
    row.variant_name = ""
    row.canonical_name = ""
    session.flush()
