"""app/services/materials_console.py — виміри для консолі бібліотеки матеріалів.

Ці тести стережуть три речі, кожна з яких раніше була тихою помилкою:
* мертві правила (покриті коротшим) мають бути ПОМІЧЕНІ, а не лежати в списку;
* колізія двох матеріалів = робота стає НЕРОЗПІЗНАНОЮ (classify_material при
  двох претендентах повертає None), і консоль мусить це називати;
* проба мусить ловити перетин на рівні ПРАВИЛ, навіть коли таких кольорів у
  базі ще немає — інакше конфлікт спливе через місяць без сліду причини.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.material_catalog import ensure_seeded, material_id_by_name
from app.models import Material, MaterialAlias, Order
from app.services.materials_console import (
    load_colour_rows,
    material_views,
    measure_rules,
    probe_pattern,
    unresolved_breakdown,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


_row_seq = iter(range(1, 10_000))


def add_order(session: Session, colour: str | None, *, archived=None, material_id=None):
    """Мінімальна робота з кольором. row_number унікальний у межах вкладки, тому
    береться з лічильника, а не константи."""
    order = Order(
        source="sheet",
        material_color=colour,
        material_id=material_id,
        archived_at=archived,
        sheet_tab="01.01.26",
        row_number=next(_row_seq),
    )
    session.add(order)
    return order


def test_measure_rules_counts_real_orders_and_keeps_samples():
    with make_session() as session:
        material = Material(name="Тест", is_production=True, sort_order=1)
        session.add(material)
        session.flush()
        session.add(MaterialAlias(material_id=material.id, pattern="моно", match_type="contains"))
        add_order(session, "Моно А3.5")
        add_order(session, "моно бліч")
        add_order(session, "пмма A2")
        session.flush()

        rules = measure_rules(list(material.aliases), load_colour_rows(session))

        assert len(rules) == 1
        assert rules[0].orders == 2  # «пмма A2» не рахується
        assert set(rules[0].samples) == {"Моно А3.5", "моно бліч"}


def test_shorter_contains_rule_marks_longer_one_dead():
    """`mono` робить `monolith` недосяжним: будь-що з monolith містить mono."""
    with make_session() as session:
        material = Material(name="Тест", is_production=True, sort_order=1)
        session.add(material)
        session.flush()
        session.add(MaterialAlias(material_id=material.id, pattern="mono", match_type="contains"))
        session.add(MaterialAlias(material_id=material.id, pattern="monolith", match_type="contains"))
        session.flush()

        rules = {r.pattern: r for r in measure_rules(list(material.aliases), load_colour_rows(session))}

        assert rules["monolith"].covered_by == "mono"
        assert rules["mono"].covered_by is None


def test_token_rule_is_not_killed_by_unrelated_contains():
    """Токен «500» не покривається нічим випадковим — інакше консоль радила б
    прибрати правило, на якому тримається класифікація опакових кольорів."""
    with make_session() as session:
        material = Material(name="Тест", is_production=True, sort_order=1)
        session.add(material)
        session.flush()
        session.add(MaterialAlias(material_id=material.id, pattern="500", match_type="token"))
        session.add(MaterialAlias(material_id=material.id, pattern="zr", match_type="contains"))
        session.flush()

        rules = {r.pattern: r for r in measure_rules(list(material.aliases), load_colour_rows(session))}
        assert rules["500"].covered_by is None


def test_archived_orders_do_not_count_towards_measurements():
    """Правила налаштовують під роботу, що йде зараз; архів не має роздувати
    числа, інакше «61 робота» означало б історію, а не поточний стан."""
    import datetime as dt

    with make_session() as session:
        material = Material(name="Тест", is_production=True, sort_order=1)
        session.add(material)
        session.flush()
        session.add(MaterialAlias(material_id=material.id, pattern="моно", match_type="contains"))
        add_order(session, "моно A2")
        add_order(session, "моно A3", archived=dt.datetime(2026, 1, 1))
        session.flush()

        rules = measure_rules(list(material.aliases), load_colour_rows(session))
        assert rules[0].orders == 1


def test_unresolved_split_names_collision_separately_from_missing_rule():
    """Головна цінність екрана: «немає правила» і «колізія» — різні діагнози."""
    with make_session() as session:
        first = Material(name="Перший", is_production=True, sort_order=1)
        second = Material(name="Другий", is_production=True, sort_order=2)
        session.add_all([first, second])
        session.flush()
        # Обидва матеріали претендують на «спірне».
        session.add(MaterialAlias(material_id=first.id, pattern="спірне", match_type="contains"))
        session.add(MaterialAlias(material_id=second.id, pattern="спірн", match_type="contains"))
        add_order(session, "спірне A2")   # колізія
        add_order(session, "щось нове")   # правила просто немає
        session.flush()

        items, no_rule, collision = unresolved_breakdown(session, load_colour_rows(session))

        by_raw = {i.raw: i for i in items}
        assert by_raw["спірне A2"].rivals == ["Другий", "Перший"]
        assert by_raw["щось нове"].rivals == []
        assert collision == 1
        assert no_rule == 1
        # Колізії йдуть першими — це зламані правила, а не пропуск.
        assert items[0].raw == "спірне A2"


def test_probe_reports_hits_and_samples_before_the_rule_is_saved():
    with make_session() as session:
        ensure_seeded(session)
        zircon_id = material_id_by_name(session)["Цирконій"]
        add_order(session, "кераміка люкс")
        add_order(session, "кераміка A2")
        session.flush()

        result = probe_pattern(session, zircon_id, "кераміка", "contains")

        assert result.orders == 2
        assert len(result.samples) == 2
        assert result.steals_from == []
        assert result.overlaps == []


def test_probe_flags_collision_with_another_materials_existing_orders():
    with make_session() as session:
        ensure_seeded(session)
        ids = material_id_by_name(session)
        add_order(session, "wax прозорий")  # уже належить Віску
        session.flush()

        result = probe_pattern(session, ids["Цирконій"], "wax", "contains")

        assert "Віск" in result.steals_from


def test_probe_flags_rule_overlap_even_without_matching_orders():
    """Найтихіший випадок: таких кольорів ще немає, тому вимір по даних мовчить,
    а колізія настане з першою ж такою роботою. Без цієї перевірки прилад
    показував би «жодної роботи — можна додавати» на правилі, що ламає інший
    матеріал."""
    with make_session() as session:
        ensure_seeded(session)
        ids = material_id_by_name(session)
        # Жодної роботи в базі немає взагалі.
        result = probe_pattern(session, ids["Цирконій"], "temp", "contains")

        assert result.orders == 0
        assert [name for name, _ in result.overlaps] == ["ПММА"]


def test_probe_marks_duplicate_and_covered_patterns():
    with make_session() as session:
        material = Material(name="Тест", is_production=True, sort_order=1)
        session.add(material)
        session.flush()
        session.add(MaterialAlias(material_id=material.id, pattern="моно", match_type="contains"))
        session.flush()

        same = probe_pattern(session, material.id, "моно", "contains")
        longer = probe_pattern(session, material.id, "моноліт", "contains")

        assert same.duplicate is True
        assert longer.covered_by == "моно"


def test_material_views_expose_dead_rule_count_for_the_rail():
    with make_session() as session:
        material = Material(name="Тест", is_production=True, sort_order=1)
        session.add(material)
        session.flush()
        session.add(MaterialAlias(material_id=material.id, pattern="mono", match_type="contains"))
        session.add(MaterialAlias(material_id=material.id, pattern="monolith", match_type="contains"))
        session.flush()

        view = material_views([material], load_colour_rows(session))[0]

        assert view.rule_count == 2
        assert view.dead_count == 1
