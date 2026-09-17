"""Схожі написання одного клієнта стоять ПОРУЧ на видачі (17.09.26).

Власник побачив «Лагус» і «Дмитрий Лагус» окремими картками в різних кінцях
екрана й спитав, чому клієнт дубльований. Картки свідомо не зливаються
(рішення 15.09.26: «Ковальчук» може виявитись іншою людиною, а зліплені
автоматично картки — це чужа коронка в чужому пакеті), тож обрано третій шлях:
не зливати, але ставити поруч.

Порядок карток — це порядок таблиці, і він тут ЗБЕРІГАЄТЬСЯ: кластер стає на
місце своєї першої картки, а не в кінець і не на початок.
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select

from app.models import Order, User
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

DAY = date(2026, 9, 16)
TAB = DAY.strftime("%d.%m.%y")


def _seed(factory, rows: list[tuple[int, str]]) -> None:
    with factory() as db:
        user = db.scalars(select(User).where(User.username == OPERATOR[0])).one()
        user.handout_layout = "nav"
        user.handout_flow = "clients"
        for row, client in rows:
            db.add(Order(
                source="sheet_client", sheet_tab=TAB, row_number=row,
                client_name=client, material_color="mono a3", quantity="1",
                status="відфрезеровано",
            ))
        db.commit()


def _card_order(app) -> list[str]:
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303)
    status, _, html = client.get(f"/handout?source=all&day={TAB}")
    assert status == 200, html[:400]
    # Ім'я картки — у заголовку групи; беремо в порядку появи в розмітці.
    return re.findall(r'<span class="dni-name">([^<]+)</span>', html)


def test_two_spellings_of_one_client_stand_next_to_each_other(app_db):  # noqa: F811
    """«Лагус» у рядку 3 і «Дмитрий Лагус» у рядку 9 — між ними чужі клієнти."""
    app, factory = app_db
    _seed(factory, [
        (1, "Басараб"),
        (3, "Лагус"),
        (5, "Неда"),
        (7, "Середюк"),
        (9, "Дмитрий Лагус"),
    ])

    order = _card_order(app)
    assert "Лагус" in order and "Дмитрий Лагус" in order, order
    gap = abs(order.index("Лагус") - order.index("Дмитрий Лагус"))
    assert gap == 1, f"картки одного клієнта мають бути поруч, а стоять через {gap}: {order}"


def test_the_sheet_order_still_decides_where_the_pair_stands(app_db):  # noqa: F811
    """Пара не стрибає ні в початок, ні в кінець: вона стає на місце ПЕРШОЇ
    своєї картки, а решта списку лишається в порядку таблиці."""
    app, factory = app_db
    _seed(factory, [
        (1, "Басараб"),
        (3, "Лагус"),
        (5, "Неда"),
        (7, "Середюк"),
        (9, "Дмитрий Лагус"),
    ])

    order = _card_order(app)
    assert order[0] == "Басараб", order
    assert order[1] == "Лагус", order
    assert order[2] == "Дмитрий Лагус", order
    assert order[3:] == ["Неда", "Середюк"], order


def test_unrelated_clients_keep_the_table_order(app_db):  # noqa: F811
    """Без схожих імен порядок не міняється зовсім — перестановка не має
    чіпати звичайний день."""
    app, factory = app_db
    _seed(factory, [
        (1, "Басараб"),
        (3, "Неда"),
        (5, "Середюк"),
        (7, "Франчук"),
    ])

    assert _card_order(app) == ["Басараб", "Неда", "Середюк", "Франчук"]
