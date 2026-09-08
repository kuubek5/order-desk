"""Архів рендериться ПО-СПРАВЖНЬОМУ — через ASGI, а не викликом функції роута.

Навіщо окремий файл. Решта тестів архіву кличе роути напряму й дивиться на
контекст: це ловить логіку, але не ловить шаблон. Саме шаблон одного разу вже
коштував девʼяти днів тихих 500 (`_settings_check_result.html`, 28.08.26), а
зміна 08.09.26 додала в список дня новий вираз (`selectattr('id','in', …)`) і
нову змінну контексту. Помилка в будь-якому з них не впала б у жодному
модульному тесті — вона впала б у Роми на екрані.

httpx у venv немає, тож клієнт — свій, `tests/asgi_client.py` (той самий, що в
`test_settings_slabs_render.py`).
"""

from __future__ import annotations

from datetime import timedelta


from app.business_day import business_today, utc_now
from app.models import Order
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура


def _seed_day(session_factory) -> str:
    """День із трьома роботами: жива, зникла з таблиці й клієнтська без імені."""
    day = business_today() - timedelta(days=5)
    tab = day.strftime("%d.%m.%y")
    with session_factory() as db:
        db.add_all([
            Order(source="lab", sheet_tab=tab, row_number=1, work_order_no="29267",
                  quantity="8", material_color="моно a2", technician_name="Денис"),
            Order(source="lab", sheet_tab=tab, row_number=2, work_order_no="28393",
                  quantity="1", material_color="моно A4", archived_at=utc_now()),
            Order(source="sheet_client", sheet_tab=tab, row_number=3, quantity="6",
                  material_color="pmma a2", sum3d_id="09-26-21"),
        ])
        db.commit()
    return tab


def test_archive_day_page_renders_the_whole_day(app_db):  # noqa: F811
    app, session_factory = app_db
    tab = _seed_day(session_factory)

    client = MiniClient(app)
    status, _, _ = client.login(*ADMIN)
    assert status in (200, 302, 303), status
    status, _, html = client.get(f"/archive?date={tab}")

    assert status == 200, f"сторінка архіву впала: {status}"
    # Усі три роботи дня — включно з живою, яка ще в черзі.
    for needle in ("29267", "28393", "pmma a2"):
        assert needle in html, needle
    assert "Уся історія робіт" in html
    # Робота без імені підписана, а не показана прочерком.
    assert "без імені" in html


def test_archive_day_page_marks_only_the_vanished_row(app_db):  # noqa: F811
    """Мітка мусить стояти рівно на тій роботі, чий рядок зник із таблиці."""
    app, session_factory = app_db
    tab = _seed_day(session_factory)

    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, html = client.get(f"/archive?date={tab}")

    assert html.count("arch-gone-tag") == 1, "мітка одна — на зниклій роботі"
    assert "1</b> зникло з таблиці" in html
    # Саме рядок 28393 приглушений: беремо шматок HTML довкола нього.
    row = html[html.rfind("<tr", 0, html.find("28393")): html.find("28393")]
    assert "is-gone" in row, "приглушено не той рядок"


def test_archive_search_page_renders(app_db):  # noqa: F811
    """Пошук ділить із днем ту саму мітку — і той самий ризик шаблону."""
    app, session_factory = app_db
    _seed_day(session_factory)

    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/archive/search?q=pmma")

    assert status == 200, status
    assert "pmma a2" in html


def test_archive_opens_without_a_selected_day(app_db):  # noqa: F811
    """Голий /archive теж мусить відкриватись: це перший екран, куди клікають."""
    app, session_factory = app_db
    _seed_day(session_factory)

    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/archive")

    assert status == 200, status
    assert "Уся історія робіт" in html
