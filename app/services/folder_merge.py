"""Злиття дублікатів ТЕК в export — рішення власника по кожній парі, без
автомату (крок C+B, «дві теки на одного клієнта», 16.09.26).

Оператори зберігають одного клієнта під різними назвами тек («Ніколаєв» /
«Іван Ніколаєв»), і робота розсипається по обох, а видача бачила лише зіставлену
одну. Тут власник підтверджує «ці дві теки = один клієнт» (`merge`) або «не
дублі» (`skip`); видача читає теки однієї групи РАЗОМ (`folder_sibling_map`).

На відміну від client_merge (імена в черзі) канону не треба — це просто
група тек. Групи складаються транзитивно (A-B, B-C → {A,B,C}). Файли на диску
не рухаємо — лише читаємо кілька тек як одну.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.material_classifier import match_key
from app.models import FolderMerge

_CANDIDATE_THRESHOLD = 82.0
_MAX_CANDIDATES = 40


@dataclass
class Candidate:
    a_name: str
    b_name: str
    score: int


def _pair_keys(name_a: str, name_b: str) -> tuple[str, str]:
    ka, kb = match_key(name_a), match_key(name_b)
    return (ka, kb) if ka <= kb else (kb, ka)


def _pair_score(name_a: str, name_b: str) -> float:
    raw = fuzz.token_set_ratio(name_a.lower(), name_b.lower())
    folded = fuzz.token_set_ratio(match_key(name_a), match_key(name_b))
    return max(raw, folded)


def _decided_pairs(session: Session) -> set[tuple[str, str]]:
    return {(row.a_key, row.b_key) for row in session.execute(select(FolderMerge)).scalars()}


def find_candidates(
    session: Session, folder_names: list[str], *, limit: int = _MAX_CANDIDATES
) -> list[Candidate]:
    """Пари схожих назв тек, ще не розібрані власником, від найсхожіших.
    `folder_names` дає викликач (list_export_client_names) — сервіс диск не читає."""
    names = sorted({(n or "").strip() for n in folder_names if (n or "").strip()})
    decided = _decided_pairs(session)
    out: list[Candidate] = []
    for i in range(len(names)):
        key_a = match_key(names[i])
        for j in range(i + 1, len(names)):
            key_b = match_key(names[j])
            if key_a == key_b:
                continue
            if _pair_keys(names[i], names[j]) in decided:
                continue
            score = _pair_score(names[i], names[j])
            if score < _CANDIDATE_THRESHOLD:
                continue
            out.append(Candidate(a_name=names[i], b_name=names[j], score=int(round(score))))
    out.sort(key=lambda c: -c.score)
    return out[:limit]


def _upsert(session: Session, a_key: str, b_key: str) -> FolderMerge:
    row = session.scalar(
        select(FolderMerge).where(FolderMerge.a_key == a_key, FolderMerge.b_key == b_key)
    )
    if row is None:
        row = FolderMerge(a_key=a_key, b_key=b_key, kind="skip")
        session.add(row)
    return row


def record_merge(session: Session, name_a: str, name_b: str) -> None:
    """«Ці дві теки — один клієнт»."""
    name_a, name_b = (name_a or "").strip(), (name_b or "").strip()
    if not name_a or not name_b:
        return
    a_key, b_key = _pair_keys(name_a, name_b)
    row = _upsert(session, a_key, b_key)
    row.kind = "merge"
    row.a_name = name_a
    row.b_name = name_b
    session.flush()


def record_skip(session: Session, name_a: str, name_b: str) -> None:
    """«Не дублі» — пара більше не пропонується."""
    a_key, b_key = _pair_keys(name_a, name_b)
    row = _upsert(session, a_key, b_key)
    row.kind = "skip"
    session.flush()


def folder_sibling_map(session: Session) -> dict[str, list[str]]:
    """{назва теки: інші теки її групи}. Групи — звʼязні компоненти над
    підтвердженими злиттями (транзитивно). Порожньо, якщо злиттів немає."""
    edges = [
        (row.a_name.strip(), row.b_name.strip())
        for row in session.execute(select(FolderMerge).where(FolderMerge.kind == "merge")).scalars()
        if row.a_name.strip() and row.b_name.strip()
    ]
    if not edges:
        return {}

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for a, b in edges:
        parent[find(a)] = find(b)

    groups: dict[str, set[str]] = defaultdict(set)
    for name in list(parent):
        groups[find(name)].add(name)

    result: dict[str, list[str]] = {}
    for members in groups.values():
        for name in members:
            result[name] = sorted(m for m in members if m != name)
    return result
