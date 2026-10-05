"""Зведення листів Конвеєра в один рядок (власник 05.10.26, `app/services/mail_merge.py`).

Кілька листів одного клієнта з одним кольором, що лягли в одну теку з тим
самим Sum3D, — одна робота: кількість сумується, у таблиці один рядок. Інші
клієнти лишаються окремими. Перемикач адміністратора, за замовчуванням
вимкнено; вимкнено — стара дорога. Відкат одного листа повертає лише його.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Attachment, EmailMessage, Order, OrderEmail
from app.parser import HEADER_ROWS
from app.routers import mail as mail_router_mod
from app.services import mail_accept as mail_accept_svc
from app.services.mail_merge import set_merge_enabled
from tests.test_mail_conveyor_grouping import _batch, _card, _letter
from tests.test_mail_transaction_safety import _database, _request, _user, _wire


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """База + спул + export + таблиця, що записує рядки в список."""
    engine = _database()
    # Як на проді (`app/db.py`): зовнішні ключі ввімкнені. StaticPool — одне
    # зʼєднання, тож PRAGMA тримається на весь тест. Без цього відкат, що
    # видаляє роботу раніше за її внески, проходив би тут і падав у цеху.
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    export_root, spool = _wire(monkeypatch, tmp_path)
    appended: list[dict] = []

    def _append(worksheet, client_name, quantity, material_color, **kw):
        appended.append({"tab": worksheet.title, "client": client_name,
                         "quantity": quantity, "material": material_color, **kw})
        return HEADER_ROWS + len(appended)

    monkeypatch.setattr(mail_accept_svc, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(mail_accept_svc, "latest_worksheet_on_or_before",
                        lambda *a, **k: SimpleNamespace(title="05.10.26"))
    monkeypatch.setattr(mail_accept_svc, "get_worksheet_by_name",
                        lambda spreadsheet, name: SimpleNamespace(title=name))
    monkeypatch.setattr(mail_accept_svc, "append_mail_placeholder_row", _append)

    sheet_fields: list[tuple[int, set[str]]] = []

    def _write_fields(db, order, fields, erase=frozenset()):
        sheet_fields.append((order.id, set(fields)))
        return None

    monkeypatch.setattr(mail_router_mod, "write_sheet_fields", _write_fields)
    # Стирання рядка у звичайному відкаті — таблиця тут недоступна (_wire).
    return SimpleNamespace(engine=engine, export=export_root, spool=spool,
                           appended=appended, sheet_fields=sheet_fields,
                           monkeypatch=monkeypatch)


def _enable(db: Session, on: bool = True) -> None:
    set_merge_enabled(db, on)
    db.commit()


def _restore(db, user, email):
    response = mail_router_mod.restore_email(request=_request(user.id), email_id=email.id, db=db)
    assert response.status_code == 303 and "error=" not in response.headers["location"], \
        response.headers["location"]


def test_switch_off_keeps_one_row_per_letter(shop):
    """Перемикач вимкнено (за замовчуванням) — рівно як до 05.10.26."""
    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1")
        _batch(shop.monkeypatch, db, user, [_card(a, quantity="2"), _card(b, quantity="3")])
        orders = db.scalars(select(Order)).all()
        assert sorted(o.quantity for o in orders) == ["2", "3"]
        assert db.scalars(select(OrderEmail)).all() == []
    assert [r["quantity"] for r in shop.appended] == ["2", "3"]


def test_one_client_one_colour_becomes_one_row_others_stay_apart(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        _enable(db)
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1", filename="bridge.stl")
        c = _letter(db, shop.spool, "c1")
        out = _batch(shop.monkeypatch, db, user, [
            _card(a, quantity="2", opak="1"),
            _card(c, client="Інший Клієнт", quantity="1"),
            _card(b, quantity="3", opak="2"),
        ])
        assert len(out.context["accepted"]) == 3
        # Лист, доданий до рядка клієнта, позначений — екран результату й тост
        # рахують роботи, а не листи.
        assert [r["merged"] for r in out.context["accepted"]] == [False, False, True]
        orders = {o.client_name: o for o in db.scalars(select(Order))}
        assert set(orders) == {"Люмі-Дент", "Інший Клієнт"}
        merged = orders["Люмі-Дент"]
        assert merged.quantity == "5"
        assert merged.opak_units == 3 and merged.cam_comment == "3 opaq"
        assert merged.source_email_id == a.id
        assert merged.row_number is not None
        assert orders["Інший Клієнт"].quantity == "1"
        # Обидва листи прийнято, обидва знають свою роботу.
        db.refresh(a)
        db.refresh(b)
        assert a.status == b.status == "прийнято"
        assert a.order_id == b.order_id == merged.id
        files = db.scalars(select(Attachment).where(Attachment.order_id == merged.id)).all()
        assert len(files) == 2
        links = {link.email_message_id: link.quantity for link in merged.email_links}
        assert links == {a.id: 2, b.id: 3}
    # Таблиця: два рядки, у рядку клієнта вже сума, і він записаний ПІСЛЯ
    # останнього листа групи (не append першого з переписуванням).
    assert [(r["client"], r["quantity"]) for r in shop.appended] == [
        ("Інший Клієнт", "1"), ("Люмі-Дент", "5"),
    ]
    assert shop.appended[1]["cam_comment"] == "3 opaq"


def test_different_sum3d_is_not_merged(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        _enable(db)
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1")
        _batch(shop.monkeypatch, db, user, [
            _card(a, quantity="2", sum3d_id="10-00-01"),
            _card(b, quantity="3", sum3d_id="11-00-02"),
        ])
        assert sorted(o.quantity for o in db.scalars(select(Order))) == ["2", "3"]
    assert len(shop.appended) == 2


def test_same_sum3d_is_merged(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        _enable(db)
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1")
        _batch(shop.monkeypatch, db, user, [
            _card(a, quantity="2", sum3d_id="10-00-01"),
            _card(b, quantity="3", sum3d_id="10-00-01"),
        ])
        (order,) = db.scalars(select(Order)).all()
        assert order.quantity == "5" and order.sum3d_id == "10-00-01"
    assert [r["sum3d_id"] for r in shop.appended] == ["10-00-01"]


def test_non_numeric_quantity_is_not_merged(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        _enable(db)
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1")
        _batch(shop.monkeypatch, db, user, [
            _card(a, quantity="2+1"), _card(b, quantity="3"),
        ])
        assert len(db.scalars(select(Order)).all()) == 2


def test_different_colour_is_not_merged(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        _enable(db)
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1")
        _batch(shop.monkeypatch, db, user, [
            _card(a, quantity="2"), _card(b, material="pmma a2", quantity="3"),
        ])
        assert len(db.scalars(select(Order)).all()) == 2


def _merged_pair(shop, db):
    _enable(db)
    user = _user(db)
    a = _letter(db, shop.spool, "a1")
    b = _letter(db, shop.spool, "b1", filename="bridge.stl")
    _batch(shop.monkeypatch, db, user, [_card(a, quantity="2"), _card(b, quantity="3")])
    order = db.scalar(select(Order))
    return user, a, b, order


def test_undo_second_letter_takes_only_its_files_and_quantity(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user, a, b, order = _merged_pair(shop, db)
        rel = order.export_folder_path
        _restore(db, user, b)
        db.expire_all()
        order = db.get(Order, order.id)
        assert order is not None and order.quantity == "2"
        assert order.source_email_id == a.id
        assert db.get(EmailMessage, b.id).status == "нове"
        assert db.get(EmailMessage, b.id).order_id is None
        assert db.get(EmailMessage, a.id).status == "прийнято"
        assert [link.email_message_id for link in order.email_links] == [a.id]
        assert shop.sheet_fields == [(order.id, {"quantity"})]
    folder = shop.export.joinpath(*rel.split("/"))
    assert [p.name for p in folder.iterdir()] == ["crown.stl"]
    assert (shop.spool / "b1" / "bridge.stl").is_file()


def test_undo_head_letter_passes_the_work_to_the_next(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user, a, b, order = _merged_pair(shop, db)
        _restore(db, user, a)
        db.expire_all()
        order = db.get(Order, order.id)
        assert order is not None and order.quantity == "3"
        assert order.source_email_id == b.id
        assert db.get(EmailMessage, a.id).status == "нове"
        assert db.get(EmailMessage, b.id).order_id == order.id
    assert (shop.spool / "a1" / "crown.stl").is_file()


def test_undo_both_letters_removes_the_work(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user, a, b, order = _merged_pair(shop, db)
        order_id = order.id
        _restore(db, user, b)
        _restore(db, user, a)
        db.expire_all()
        assert db.get(Order, order_id) is None
        assert db.scalars(select(OrderEmail)).all() == []
        assert {e.status for e in db.scalars(select(EmailMessage))} == {"нове"}
    assert (shop.spool / "a1" / "crown.stl").is_file()
    assert (shop.spool / "b1" / "bridge.stl").is_file()


def test_undo_still_detaches_after_switch_is_turned_off(shop):
    """Роботи, зведені при увімкненому перемикачі, відкочуються коректно й
    після вимкнення: інакше запасний `email.order_id` видалив би спільну роботу."""
    with Session(shop.engine, expire_on_commit=False) as db:
        user, a, b, order = _merged_pair(shop, db)
        _enable(db, False)
        _restore(db, user, b)
        db.expire_all()
        order = db.get(Order, order.id)
        assert order is not None and order.quantity == "2"


def test_undo_of_milled_merged_work_is_refused(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user, a, b, order = _merged_pair(shop, db)
        order.status = "відфрезеровано"
        db.commit()
        response = mail_router_mod.restore_email(request=_request(user.id), email_id=b.id, db=db)
        assert "error=" in response.headers["location"]
        db.expire_all()
        assert db.get(Order, order.id).quantity == "5"


def test_row_is_written_even_if_conveyor_breaks_midway(shop):
    """Непередбачений виняток на другому листі групи: робота першого вже в
    базі — її рядок мусить лягти в таблицю, а не лишитись без рядка."""
    real_accept = mail_router_mod.accept_letter
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real_accept(*args, **kwargs)

    shop.monkeypatch.setattr(mail_router_mod, "accept_letter", flaky)
    with Session(shop.engine, expire_on_commit=False) as db:
        _enable(db)
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1")
        with pytest.raises(RuntimeError):
            _batch(shop.monkeypatch, db, user, [_card(a, quantity="2"), _card(b, quantity="3")])
        (order,) = db.scalars(select(Order)).all()
        assert order.row_number is not None
    assert [r["quantity"] for r in shop.appended] == ["2"]
