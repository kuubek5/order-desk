"""app/services/client_merge.py — розбір дублікатів клієнтів з рішенням власника
по кожній парі (merge / skip), без автомату."""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.business_day import utc_now
from app.models import Order
from app.services.client_merge import (
    find_candidates,
    record_merge,
    record_skip,
    merge_map,
)
from app.services import client_suggest


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def add(session: Session, names: dict[str, int]) -> None:
    created = utc_now() - timedelta(days=1)
    for name, count in names.items():
        for _ in range(count):
            session.add(Order(source="client", status="нове", client_name=name,
                              material_color="mono a3", created_at=created))
    session.commit()


def _pair_names(candidates):
    return {frozenset((c.a_name, c.b_name)) for c in candidates}


def test_find_candidates_proposes_similar_pairs():
    with make_session() as session:
        add(session, {"Кривовид": 6, "Євген Кривовид": 2, "Павленко": 4})
        cands = find_candidates(session)
        assert frozenset(("Кривовид", "Євген Кривовид")) in _pair_names(cands)
        # Павленко ні на кого не схожий — окремої пари з ним немає
        assert not any("Павленко" in (c.a_name, c.b_name) for c in cands)


def test_merge_collapses_suggestions_and_removes_pair():
    client_suggest.invalidate_cache()
    with make_session() as session:
        add(session, {"Кривовид": 6, "Євген Кривовид": 2})
        record_merge(session, canonical_name="Кривовид", variant_name="Євген Кривовид")
        session.commit()
        assert merge_map(session)  # рядок є

        # Пара більше не пропонується.
        assert frozenset(("Кривовид", "Євген Кривовид")) not in _pair_names(find_candidates(session))

        # У підказках лишається один канон із сумою робіт (6 + 2).
        client_suggest.invalidate_cache()
        items = client_suggest.suggest_clients(session, "крив")
        krив = [it for it in items if it.text == "Кривовид"]
        assert len(krив) == 1
        assert krив[0].count == 8
        assert all(it.text != "Євген Кривовид" for it in items)


def test_skip_hides_pair_without_merging():
    with make_session() as session:
        add(session, {"Кривовид": 6, "Євген Кривовид": 2})
        record_skip(session, "Кривовид", "Євген Кривовид")
        session.commit()
        assert not merge_map(session)  # нічого не злито
        assert frozenset(("Кривовид", "Євген Кривовид")) not in _pair_names(find_candidates(session))
