"""Підтвердження, що Sum3D ID долетів у спільну Google Таблицю.

Навіщо окремий файл. Механіка запису була чесною й до цього: роут ЧЕКАЄ на
write-back, «рядок не підтверджено» не вважається успіхом, пауза синку віддає
помилку. Мовчав саме СИГНАЛ — зелений тост казав «Sum3D → 12-01-45» і не
називав таблицю, тоді як гілка помилки називала її прямо. Оператор не міг
відрізнити «портал зберіг» від «таблиця прийняла», а ставка тут найвища на
екрані: той рядок читає весь цех.

Тест іде через ASGI, а не викликом функції роута: перевіряється і заголовок
HX-Trigger, і те, що клас долітає в РОЗМІТКУ рядка (шаблон уже одного разу
коштував девʼяти днів тихих 500 — див. test_archive_render.py).

Запис у Google тут не відбувається: підмінено `await_on_writeback` — єдину
точку, через яку роут ходить у пул write-back. Підміна стоїть у просторі
`app.routers.orders`, бо саме звідти імʼя резолвиться в момент виклику (§14:
після переносу коду monkeypatch мовчки стає no-op).
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.business_day import business_tab_today
from app.models import Order
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура


def _give_operator_a_letter(session_factory, letter: str = "ЦЦ") -> None:
    """Без літери в акаунті роут НЕ ставить «Прорахував» і НЕ піднімає статус.

    Спільна фікстура створює операторів без `sheet_initial`, тож тест на відкат
    статусу без цього проходив би, нічого не перевіряючи: статус просто ніколи
    не ставав «прораховано» (спіймано мутацією 15.09.26).
    """
    from app.models import User

    with session_factory() as db:
        user = db.execute(select(User).where(User.username == ADMIN[0])).scalars().first()
        user.sheet_initial = letter
        db.commit()


def _order(session_factory, **kwargs) -> int:
    tab = business_tab_today().strftime("%d.%m.%y")
    fields = dict(
        source="lab", sheet_tab=tab, row_number=7, work_order_no="24122",
        quantity="2", material_color="моно a3", job_code="24122",
    )
    fields.update(kwargs)
    with session_factory() as db:
        order = Order(**fields)
        db.add(order)
        db.commit()
        return order.id


def _toast(headers) -> dict:
    raw = dict(headers).get("hx-trigger")
    assert raw, f"немає HX-Trigger у {headers}"
    return json.loads(raw)["toast"]


def _save_sum3d(app, order_id, sync_error=None):
    import app.routers.orders as orders

    async def fake_writeback(fn, *args):
        return sync_error

    original = orders.await_on_writeback
    orders.await_on_writeback = fake_writeback
    try:
        client = MiniClient(app)
        client.login(*ADMIN)
        return client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": "12-01-45"})
    finally:
        orders.await_on_writeback = original


def test_success_toast_names_the_sheet(app_db):  # noqa: F811
    app, session_factory = app_db
    order_id = _order(session_factory)

    status, headers, html = _save_sum3d(app, order_id)

    assert status == 200, status
    toast = _toast(headers)
    assert toast["kind"] == "success", toast
    assert "записано в таблицю" in toast["message"], toast
    assert "12-01-45" in toast["message"], toast
    assert "is-written" in html, "поле Sum3D не позначене як підтверджене"


def test_email_order_never_claims_a_sheet_row(app_db):  # noqa: F811
    """Робота з пошти рядка в таблиці не має — «записано в таблицю» було б
    неправдою. Успіх там означає лише «збережено в порталі»."""
    app, session_factory = app_db
    order_id = _order(session_factory, source="email", client_name="Vernigora")

    status, headers, html = _save_sum3d(app, order_id)

    assert status == 200, status
    toast = _toast(headers)
    assert toast["kind"] == "success", toast
    assert "таблиц" not in toast["message"].lower(), toast
    assert "is-written" not in html, "пошта не пише в таблицю — підтвердження зайве"


def test_failed_write_says_so_and_does_not_confirm(app_db):  # noqa: F811
    app, session_factory = app_db
    order_id = _order(session_factory)

    status, headers, html = _save_sum3d(
        app, order_id, sync_error="рядок у таблиці не підтверджено — не записано"
    )

    assert status == 200, status
    toast = _toast(headers)
    assert toast["kind"] != "success", toast
    assert "is-written" not in html, "невдалий запис не має підтверджуватись"


def _clear_sum3d(app, order_id):
    import app.routers.orders as orders

    seen: dict = {}

    async def fake_writeback(fn, *args):
        seen["fn"] = fn.__name__
        seen["args"] = args
        return None

    original = orders.await_on_writeback
    orders.await_on_writeback = fake_writeback
    try:
        client = MiniClient(app)
        client.login(*ADMIN)
        return (*client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": ""}), seen)
    finally:
        orders.await_on_writeback = original


def test_clearing_sum3d_does_not_flash_a_confirmation(app_db):  # noqa: F811
    """Порожнє значення — це «повернути роботу в можна брати». Підсвічувати
    порожнє поле як «записано» безглуздо: підтверджувати нічого."""
    app, session_factory = app_db
    order_id = _order(session_factory, sum3d_id="12-01-45")

    status, headers, html, _ = _clear_sum3d(app, order_id)

    assert status == 200, status
    assert "is-written" not in html
    assert "стерто в таблиці" in _toast(headers)["message"], _toast(headers)


def test_clearing_sum3d_also_erases_the_operator_letter(app_db):  # noqa: F811
    """Стерли ID — стерли й «Прорахував».

    Літера в колонці М і є позначкою «прораховано». Доки вона лишалась, у
    спільній таблиці висіло «прорахував ЦЦ» на роботі, яку щойно повернули в
    «можна брати» (знахідка живої перевірки 15.09.26).

    Перевіряється ще й `erase`: `calculated_raw` — маркерне поле, і БЕЗ цього
    набору запис прочитав би живу літеру з таблиці, зберіг її й повернув у базу,
    тобто очищення мовчки не зробило б нічого.
    """
    app, session_factory = app_db
    order_id = _order(session_factory, sum3d_id="12-01-45", calculated_raw="ЦЦ")

    status, _, _, seen = _clear_sum3d(app, order_id)

    assert status == 200, status
    assert seen["fn"] == "write_sheet_fields_warm", seen
    _, fields, erase = seen["args"]
    assert "calculated_raw" in fields, fields
    assert "calculated_raw" in erase, erase

    with session_factory() as db:
        order = db.get(Order, order_id)
        assert order.sum3d_id is None
        assert order.calculated_raw is None, "літера оператора лишилась у базі"


def _set_then_clear(app, order_id):
    """Оператор вписав ID, потім стер — як і буває, коли взяв не ту роботу."""
    import app.routers.orders as orders

    async def fake_writeback(fn, *args):
        return None

    original = orders.await_on_writeback
    orders.await_on_writeback = fake_writeback
    try:
        client = MiniClient(app)
        client.login(*ADMIN)
        client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": "12-01-45"})
        return client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": ""})
    finally:
        orders.await_on_writeback = original


def test_clearing_sum3d_returns_the_status_it_had_before(app_db):  # noqa: F811
    """Вписаний ID підняв статус до «прораховано» — стертий мусить опустити.

    Інакше лишалась робота, у якої в таблиці порожньо, фільтр каже «можна
    брати», а крапка статусу — «прораховано».
    """
    app, session_factory = app_db
    _give_operator_a_letter(session_factory)
    order_id = _order(session_factory, status="прийнято")

    status, _, _ = _set_then_clear(app, order_id)

    assert status == 200, status
    with session_factory() as db:
        order = db.get(Order, order_id)
        assert order.sum3d_id is None
        # Саме «прийнято», а не «нове»: повертаємо той статус, що БУВ.
        assert order.status == "прийнято", order.status


def test_status_is_left_alone_when_there_is_no_proof(app_db):  # noqa: F811
    """Без запису в журналі попередній статус невідомий — не вгадуємо.

    «Нове» тут було б здогадкою, а робота з пошти мала «прийнято»: така
    здогадка тихо стерла б факт. Краще лишити як є — це видно оператору.
    """
    app, session_factory = app_db
    order_id = _order(session_factory, sum3d_id="12-01-45", status="прораховано")

    status, _, _, _ = _clear_sum3d(app, order_id)

    assert status == 200, status
    with session_factory() as db:
        assert db.get(Order, order_id).status == "прораховано"


def test_clearing_does_not_undo_a_status_that_moved_on(app_db):  # noqa: F811
    """Робота вже відфрезерована — стирання ID не тягне її назад."""
    app, session_factory = app_db
    _give_operator_a_letter(session_factory)
    order_id = _order(session_factory, status="прийнято")

    import app.routers.orders as orders

    async def fake_writeback(fn, *args):
        return None

    original = orders.await_on_writeback
    orders.await_on_writeback = fake_writeback
    try:
        client = MiniClient(app)
        client.login(*ADMIN)
        client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": "12-01-45"})
        with session_factory() as db:
            order = db.get(Order, order_id)
            order.status = "відфрезеровано"
            db.commit()
        client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": ""})
    finally:
        orders.await_on_writeback = original

    with session_factory() as db:
        assert db.get(Order, order_id).status == "відфрезеровано"


def test_setting_sum3d_never_erases_a_hand_written_letter(app_db):  # noqa: F811
    """Зворотний бік: на ЗАПИС ID набір erase порожній.

    Інакше зникла б уся суть маркерних полів — «лабораторія вписала руками,
    ми не затираємо».
    """
    app, session_factory = app_db
    order_id = _order(session_factory)

    import app.routers.orders as orders

    seen: dict = {}

    async def fake_writeback(fn, *args):
        seen["args"] = args
        return None

    original = orders.await_on_writeback
    orders.await_on_writeback = fake_writeback
    try:
        client = MiniClient(app)
        client.login(*ADMIN)
        client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": "12-01-45"})
    finally:
        orders.await_on_writeback = original

    _, _, erase = seen["args"]
    assert not erase, erase


def test_editing_the_id_then_clearing_still_rolls_the_status_back(app_db):  # noqa: F811
    """Вписав ID → виправив одруківку в ньому → стер усе.

    Найчастіший реальний шлях, і саме на ньому відкат спершу не спрацьовував:
    у знімку ДРУГОЇ дії статус уже був «прораховано», тож «попередній» дорівнював
    поточному й нічого не мінялось. Робота лишалась «прораховано» при порожній
    таблиці — рівно та неправда, заради якої відкат і робили.
    """
    app, session_factory = app_db
    _give_operator_a_letter(session_factory)
    order_id = _order(session_factory, status="нове")

    import app.routers.orders as orders

    async def fake_writeback(fn, *args):
        return None

    original = orders.await_on_writeback
    orders.await_on_writeback = fake_writeback
    try:
        client = MiniClient(app)
        client.login(*ADMIN)
        client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": "12-01-45"})
        client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": "13-00-00"})
        client.post(f"/orders/{order_id}/sum3d-id", {"sum3d_id": ""})
    finally:
        orders.await_on_writeback = original

    with session_factory() as db:
        assert db.get(Order, order_id).status == "нове"
