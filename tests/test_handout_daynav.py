"""Покажчик дня на видачі веде до клієнта в ОБОХ виглядах (11.09.26).

Посилання `#handout-client-N` вели лише до картки, а в режимі «рядки» карток
немає — клік нічого не робив (прохання власника). Тепер пункт покажчика й
рядки плаского списку несуть той самий `data-nav`, і handout.js веде за ним.
Тут стережемо саме розмітку, з якої JS це бере: справжній рендер /handout.
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select

from app.models import Order, User
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

DAY = date(2026, 9, 10)
TAB = DAY.strftime("%d.%m.%y")


def _seed(factory, *, flow: str) -> None:
    with factory() as db:
        user = db.scalars(select(User).where(User.username == OPERATOR[0])).one()
        user.handout_layout = "nav"
        user.handout_flow = flow
        # Роботи одного клієнта врозкид у таблиці — саме той випадок, де в
        # режимі «рядки» клієнт зустрічається кілька разів.
        for row, client in ((7, "Nicolaev"), (8, "Oleksandr"), (9, "Nicolaev")):
            db.add(Order(
                source="sheet_client", sheet_tab=TAB, row_number=row, client_name=client,
                material_color="mono a3", quantity="1", status="відфрезеровано",
            ))
        db.commit()


def _page(app) -> str:
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303)
    status, _, html = client.get(f"/handout?source=all&day={TAB}")
    assert status == 200, html[:400]
    return html


def test_rows_view_links_the_index_to_the_clients_rows(app_db):  # noqa: F811
    app, factory = app_db
    _seed(factory, flow="sheet")
    html = _page(app)
    nav = dict(re.findall(r'class="dni[^"]*"\s+href="#handout-client-(\d+)" data-nav="(\d+)"', html))
    assert nav and all(k == v for k, v in nav.items())
    rows = re.findall(r'<div class="flatrow" data-nav="(\d+)">', html)
    # Три роботи, два клієнти: у Nicolaev два рядки під одним номером.
    assert sorted(rows) == ["1", "1", "2"]
    assert 'id="handout-client-' not in html, "у режимі «рядки» карток немає — тому й потрібен data-nav"


def test_cards_view_keeps_the_card_anchor(app_db):  # noqa: F811
    app, factory = app_db
    _seed(factory, flow="")
    html = _page(app)
    assert 'id="handout-client-1"' in html and 'data-nav="1"' in html


def test_row_with_only_older_folders_says_so_and_offers_the_client_folder(app_db, monkeypatch, tmp_path):  # noqa: F811
    """Oleksandr 10.09.26: під `mono a3` за 10.09 висіла тека 09.09 — і її
    відкривали. Тепер стара тека не показується, рядок каже «за 10.09 теки
    немає» і відкриває теку клієнта."""
    from datetime import datetime
    from types import SimpleNamespace

    import app.web as web
    from app.routers import handout as handout_router_mod
    from app.services import handout as handout_service

    app, factory = app_db
    with factory() as db:
        user = db.scalars(select(User).where(User.username == OPERATOR[0])).one()
        user.handout_layout = "nav"
        db.add(Order(source="sheet_client", sheet_tab=TAB, row_number=7, client_name="Oleksandr",
                     material_color="mono a3", quantity="1", status="відфрезеровано"))
        db.commit()
    folder = tmp_path / "Oleksandr" / "Новая папка (282)" / "mono a3"
    folder.mkdir(parents=True)
    old = SimpleNamespace(client_folder_name="Oleksandr", batch_folder_name="Новая папка (282)",
                          material_color_folder_name="mono a3", created_at=datetime(2026, 9, 9, 13, 29),
                          files=["a.stl"], folder_path=folder)
    monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
    monkeypatch.setattr(web, "list_export_client_names_cached", lambda root: ["Oleksandr"])
    monkeypatch.setattr(handout_service, "scan_export_client_cached", lambda root, f, nb: [old])
    html = _page(app)
    assert "за 10.09 теки немає" in html
    assert "wfolder-miss" in html and "data-open-folder-token" in html
    assert "09.09 13:29" not in html, "стара тека як «своя» не показується"
