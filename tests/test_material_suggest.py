"""app/services/material_suggest.py — підказки матеріалу для ручного вводу:
скорочення (Tab-розгортання) + frecency з накопичених написань, з бейджем
розпізнавання. Плюс ключ звіряння match_key (кирилиця = латиниця)."""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from app.db import Base
from app.business_day import utc_now
from app.material_catalog import (
    MaterialCatalogError,
    add_shortcut,
    backfill_orders,
    ensure_seeded,
    list_shortcuts,
)
from app.material_classifier import match_key
from app.models import Order
from app.services.material_suggest import (
    invalidate_cache,
    suggest_materials,
    usage_for_expansion,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def add_orders(session: Session, spellings: dict[str, int], *, days_ago: int = 1) -> None:
    created = utc_now() - timedelta(days=days_ago)
    for text, count in spellings.items():
        for _ in range(count):
            session.add(
                Order(source="lab", status="нове", material_color=text, created_at=created)
            )
    session.flush()
    backfill_orders(session, only_unresolved=False)
    session.commit()


# ── match_key ────────────────────────────────────────────────────────────────


def test_match_key_folds_cyrillic_and_latin_to_one_cluster():
    assert match_key("моно а3") == match_key("mono a3")
    assert match_key("Моно А3,5") == match_key("mono a3.5")
    assert match_key("") == ""


# ── shortcuts ────────────────────────────────────────────────────────────────


def test_add_shortcut_rejects_duplicate_key_across_alphabets():
    with make_session() as session:
        add_shortcut(session, "мл", "mono")
        # `ml` folds to the same key as `мл` — the shortcut is already taken.
        with pytest.raises(MaterialCatalogError):
            add_shortcut(session, "ml", "monolit")
        assert len(list_shortcuts(session)) == 1


def test_add_shortcut_rejects_empty():
    with make_session() as session:
        with pytest.raises(MaterialCatalogError):
            add_shortcut(session, "  ", "mono")
        with pytest.raises(MaterialCatalogError):
            add_shortcut(session, "мл", "  ")


# ── suggestions ──────────────────────────────────────────────────────────────


def test_shortcut_expands_when_unique_and_carries_badge():
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        add_shortcut(session, "мл", "mono a3")
        items = suggest_materials(session, "мл")
        assert items, "скорочення має знайтись"
        top = items[0]
        assert top.kind == "shortcut"
        assert top.text == "mono a3"
        assert top.is_expand is True  # єдиний збіг → Tab розгортає
        assert top.badge == "Zr"  # `mono` розпізнається як Цирконій


def test_two_shortcuts_same_prefix_do_not_expand():
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        add_shortcut(session, "ма", "mono a3")
        add_shortcut(session, "маб", "mono a3.5")
        items = suggest_materials(session, "ма")
        assert len(items) >= 2
        assert all(it.is_expand is False for it in items), "двозначність → Tab не розгортає"


def test_frecency_ranks_by_frequency_and_recognizes_material():
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        add_orders(session, {"mono a3": 5, "моно а3": 2, "mono a3.5": 1})
        invalidate_cache()  # кеш міг наповнитись порожнім до вставки
        items = suggest_materials(session, "mo")
        texts = [it.text for it in items]
        assert texts, "мають бути підказки"
        # `mono a3` і `моно а3` — один кластер (7 робіт), представник — частіше
        # написання; окремий кластер `mono a3.5`.
        assert items[0].text == "mono a3"
        assert items[0].count == 7
        assert items[0].badge == "Zr"


def test_unrecognized_spelling_has_no_badge():
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        add_orders(session, {"zzz незрозуміле": 3})
        invalidate_cache()
        items = suggest_materials(session, "zzz")
        assert items
        assert items[0].badge is None
        assert items[0].material_id is None


def test_usage_for_expansion_counts_the_word_family():
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        add_orders(session, {"mono a3": 4, "mono a3.5": 2, "титан корея": 3})
        invalidate_cache()
        assert usage_for_expansion(session, "mono") == 6
        assert usage_for_expansion(session, "") == 0


def test_empty_query_returns_nothing():
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        assert suggest_materials(session, "") == []
        assert suggest_materials(session, "   ") == []


# ── справжній рендер фрагментів ──────────────────────────────────────────────


def _render(name: str, ctx: dict) -> str:
    from app.routers.deps import templates
    return templates.env.get_template(name).render(**ctx)


def test_suggest_list_fragment_renders_aria_options():
    from app.services.material_suggest import Suggestion

    items = [
        Suggestion(kind="shortcut", text="mono a3", badge="Zr", material_id=1,
                   shortcut="мл", is_expand=True),
        Suggestion(kind="frecency", text="mono a3.5", badge="Zr", material_id=1, count=83),
        Suggestion(kind="frecency", text="загадка", badge=None, material_id=None, count=4),
    ]
    html = _render("_suggest_list.html", {"items": items, "field": "material"})
    assert 'role="option"' in html
    assert 'data-value="mono a3"' in html
    assert 'data-expand="1"' in html          # унікальне скорочення → Tab
    assert "<kbd>Tab</kbd>" in html
    assert "83 робіт" in html
    assert 'title="Матеріал не розпізнано"' in html  # бейдж «?» на нерозпізнаному


def test_shortcuts_table_fragment_renders():
    ctx = {
        "shortcuts": [
            {"id": 1, "shortcut": "мл", "expansion": "mono", "used": 445,
             "badge": "Zr", "recognized": True},
            {"id": 2, "shortcut": "зг", "expansion": "загадка", "used": 0,
             "badge": None, "recognized": False},
        ]
    }
    html = _render("_matlib_shortcuts.html", ctx)
    assert "445" in html
    assert "/settings/materials/shortcut/1/delete" in html
    assert 'action="/settings/materials/shortcut/add"' in html
    assert "is-unknown" in html  # нерозпізнане написання підсвічене


def test_shortcut_hidden_once_material_word_typed_in_full():
    """«emo a2» — оператор уже дописав слово; скорочення `емо→emo` там нічого
    не змінить (перше слово вже «emo»). Тоді перший рядок — frecency «emo a2»,
    а не голе «emo» (власник 16.09.26)."""
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        add_shortcut(session, "емо", "emo")
        add_orders(session, {"emo a2": 5})
        invalidate_cache()
        items = suggest_materials(session, "emo a2")
        assert all(it.kind != "shortcut" for it in items), "no-op скорочення сховане"
        assert items and items[0].text == "emo a2"


def test_cyrillic_shortcut_still_shown_with_colour():
    """`емо a2` кирилицею — розгортання в латинське «emo» ЩЕ змінює поле
    (алфавіт), тож скорочення лишається з Tab."""
    invalidate_cache()
    with make_session() as session:
        ensure_seeded(session)
        add_shortcut(session, "емо", "emo")
        items = suggest_materials(session, "емо a2")
        assert any(it.kind == "shortcut" and it.text == "emo" for it in items)
