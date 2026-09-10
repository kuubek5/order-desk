"""«Замовлено» бере лише позначене галочками — решта лишається в списку.

До 10.09.26 кнопка позначала замовленим УВЕСЬ список, хоч галочки на екрані
стояли й буфер копіювався вибірково. Замовити частину (цирконій сьогодні,
ПММА завтра) було неможливо: зняте зникало разом із рештою. А після
замовлення кнопка відкату ховалась у згорнутій історії.

Тут стережемо три речі:
1. позначено частину — замовлено рівно її, решта в списку;
2. не позначено нічого — не замовлено нічого (порожнє ≠ «усе»);
3. відкат повертає саме цю частину, і він під рукою одразу після кліку.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import CamBlank
from app.services.cam_blanks import mark_ordered, order_lines, pending_blanks, undo_last_order
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

SEEN = datetime(2026, 9, 10, 9, 0)


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(db) -> dict[str, int]:
    db.add_all([
        CamBlank(rel_path="zr/18/a", material_dir="zr", brand="monolith", shade="a2", height=18, first_seen_at=SEEN),
        CamBlank(rel_path="zr/18/b", material_dir="zr", brand="monolith", shade="a2", height=18, first_seen_at=SEEN),
        CamBlank(rel_path="pmma/20/c", material_dir="pmma", brand="emotions", shade="a3", height=20, first_seen_at=SEEN),
    ])
    db.commit()
    return {row.rel_path: row.id for row in pending_blanks(db)}


class TestPartialOrder:
    def test_only_the_given_discs_are_ordered(self, db):
        ids = _seed(db)
        assert mark_ordered(db, ids=[ids["zr/18/a"], ids["zr/18/b"]], now=datetime(2026, 9, 10, 17, 0)) == 2
        assert [row.rel_path for row in pending_blanks(db)] == ["pmma/20/c"]

    def test_empty_selection_orders_nothing(self, db):
        """Порожній вибір — не «усе». Інакше зняті галочки замовили б список."""
        _seed(db)
        assert mark_ordered(db, ids=[]) == 0
        assert len(pending_blanks(db)) == 3

    def test_already_ordered_ids_are_not_reordered(self, db):
        """Другий оператор уже замовив — застаріла сторінка не переставляє час."""
        ids = _seed(db)
        first = datetime(2026, 9, 10, 12, 0)
        mark_ordered(db, ids=[ids["zr/18/a"]], now=first)
        assert mark_ordered(db, ids=[ids["zr/18/a"]], now=datetime(2026, 9, 10, 18, 0)) == 0
        row = db.get(CamBlank, ids["zr/18/a"])
        assert row.ordered_at == first

    def test_undo_returns_only_the_partial_batch(self, db):
        ids = _seed(db)
        mark_ordered(db, ids=[ids["pmma/20/c"]], now=datetime(2026, 9, 10, 12, 0))
        mark_ordered(db, ids=[ids["zr/18/a"]], now=datetime(2026, 9, 10, 18, 0))
        assert undo_last_order(db) == 1
        assert sorted(row.rel_path for row in pending_blanks(db)) == ["zr/18/a", "zr/18/b"]

    def test_line_carries_ids_of_its_discs(self, db):
        """Галочка стоїть на рядку «mono a2 18(2)» — він мусить знати обидва диски."""
        ids = _seed(db)
        lines = {line.item: line for line in order_lines(pending_blanks(db))}
        assert sorted(lines["mono a2 18"].ids) == sorted([ids["zr/18/a"], ids["zr/18/b"]])
        assert lines["emo a3 20"].ids == (ids["pmma/20/c"],)


def _client(app) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*ADMIN)
    assert status in (200, 302, 303), status
    return client


class TestOrderedRoute:
    """Справжній POST і рендер: зелений сервіс без роута нічого не доводить."""

    def _seed_app(self, session_factory) -> dict[str, int]:
        from app.settings_store import set_setting

        with session_factory() as db:
            set_setting(db, "cam_blanks_path", r"C:\cam-bl")
            db.commit()
            return _seed(db)

    def test_partial_post_leaves_the_rest_and_offers_undo(self, app_db):  # noqa: F811
        app, session_factory = app_db
        ids = self._seed_app(session_factory)
        client = _client(app)

        status, _, html = client.post(
            "/settings/blanks/ordered",
            {"ids": f"{ids['pmma/20/c']}"},
            headers={"HX-Request": "true"},
        )
        assert status == 200, html
        assert "Замовлено 1 диск." in html
        assert "Решта лишилась у списку." in html
        # Відкат — поруч із повідомленням, не лише в згорнутій історії.
        note = html.split("blanks-ordered-note", 1)[1].split("</p>", 1)[0]
        assert "/settings/blanks/undo-order" in note
        # Зняте лишилось у списку з тими самими id на галочці.
        assert f'data-ids="{ids["zr/18/a"]},{ids["zr/18/b"]}"' in html

        with session_factory() as db:
            assert sorted(row.rel_path for row in pending_blanks(db)) == ["zr/18/a", "zr/18/b"]

    def test_empty_post_orders_nothing(self, app_db):  # noqa: F811
        app, session_factory = app_db
        self._seed_app(session_factory)
        client = _client(app)

        status, _, html = client.post("/settings/blanks/ordered", {"ids": ""}, headers={"HX-Request": "true"})
        assert status == 200, html
        assert "Нічого не позначено" in html
        with session_factory() as db:
            assert len(pending_blanks(db)) == 3
