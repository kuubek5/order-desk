"""app/services/folder_merge.py — розбір дублікатів ТЕК в export з рішенням
власника по парі; групи тек складаються транзитивно."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.services.folder_merge import (
    find_candidates,
    record_merge,
    record_skip,
    folder_sibling_map,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _pairs(cands):
    return {frozenset((c.a_name, c.b_name)) for c in cands}


def test_find_candidates_over_disk_folder_names():
    with make_session() as session:
        folders = ["Ніколаєв", "Іван Ніколаєв", "Коваль"]
        cands = find_candidates(session, folders)
        assert frozenset(("Ніколаєв", "Іван Ніколаєв")) in _pairs(cands)
        assert not any("Коваль" in (c.a_name, c.b_name) for c in cands)


def test_merge_builds_sibling_map_and_hides_pair():
    with make_session() as session:
        record_merge(session, "Ніколаєв", "Іван Ніколаєв")
        session.commit()
        sib = folder_sibling_map(session)
        assert sib["Ніколаєв"] == ["Іван Ніколаєв"]
        assert sib["Іван Ніколаєв"] == ["Ніколаєв"]
        # Пара більше не пропонується.
        assert frozenset(("Ніколаєв", "Іван Ніколаєв")) not in _pairs(
            find_candidates(session, ["Ніколаєв", "Іван Ніколаєв"])
        )


def test_groups_are_transitive():
    """A-B і B-C → одна група {A,B,C}."""
    with make_session() as session:
        record_merge(session, "A", "B")
        record_merge(session, "B", "C")
        session.commit()
        sib = folder_sibling_map(session)
        assert set(sib["A"]) == {"B", "C"}
        assert set(sib["C"]) == {"A", "B"}


def test_skip_does_not_group():
    with make_session() as session:
        record_skip(session, "Ніколаєв", "Іван Ніколаєв")
        session.commit()
        assert folder_sibling_map(session) == {}
        assert frozenset(("Ніколаєв", "Іван Ніколаєв")) not in _pairs(
            find_candidates(session, ["Ніколаєв", "Іван Ніколаєв"])
        )


def test_manual_merge_and_list_groups():
    """Ручне зчеплення двох геть різних назв (rapidfuzz їх не запропонує)."""
    from app.services.folder_merge import list_groups

    with make_session() as session:
        record_merge(session, "Ніколаєв", "Стоматологія на Сонячній")
        session.commit()
        groups = list_groups(session)
        assert groups == [["Ніколаєв", "Стоматологія на Сонячній"]]
        assert folder_sibling_map(session)["Ніколаєв"] == ["Стоматологія на Сонячній"]


def test_unmerge_splits_the_group():
    from app.services.folder_merge import record_unmerge, list_groups

    with make_session() as session:
        record_merge(session, "A", "B")
        record_merge(session, "B", "C")
        session.commit()
        assert len(list_groups(session)) == 1
        record_unmerge(session, "B")  # розбити всю групу через будь-якого члена
        session.commit()
        assert list_groups(session) == []
        assert folder_sibling_map(session) == {}
