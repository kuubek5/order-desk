"""app/services/client_suggest.py — frecency-пошук клієнта для форми «Додати
роботу»: інкрементальний, з тими самими розкладко/гомогліф-ключами."""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.business_day import utc_now
from app.models import Order
from app.services.client_suggest import invalidate_cache, suggest_clients


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def add_clients(session: Session, names: dict[str, int], *, days_ago: int = 1) -> None:
    created = utc_now() - timedelta(days=days_ago)
    for name, count in names.items():
        for _ in range(count):
            session.add(
                Order(source="client", status="нове", client_name=name,
                      material_color="mono a3", created_at=created)
            )
    session.commit()


def test_ranks_by_frecency_and_matches_substring():
    invalidate_cache()
    with make_session() as session:
        add_clients(session, {"Кривовид": 6, "Євген Кривовид": 2, "Павленко": 4})
        invalidate_cache()
        items = suggest_clients(session, "крив")
        texts = [it.text for it in items]
        assert texts and texts[0] == "Кривовид"       # частіший — вище
        assert "Євген Кривовид" in texts               # підрядок теж ловиться
        assert all(it.badge is None for it in items)   # у клієнта немає категорії


def test_no_match_returns_empty():
    invalidate_cache()
    with make_session() as session:
        add_clients(session, {"Коваль": 3})
        invalidate_cache()
        assert suggest_clients(session, "щось") == []
        assert suggest_clients(session, "") == []


def test_forgotten_layout_finds_client():
    """`rhbd` на QWERTY — це `крив` на ЙЦУКЕН, має знайти «Кривовид»."""
    invalidate_cache()
    with make_session() as session:
        add_clients(session, {"Кривовид": 5})
        invalidate_cache()
        items = suggest_clients(session, "rhbd")
        assert items and items[0].text == "Кривовид"
