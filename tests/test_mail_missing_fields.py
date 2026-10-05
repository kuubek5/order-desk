"""Запобіжник «забули кількість чи Sum3D» (власник 05.10.26).

Порожня кількість або Sum3D при прийнятті листа — не помилка (роботу можна
прийняти й до прорахунку), але майже завжди її забули. Прийняття спершу
зупиняється з попередженням, а приймає лише після свідомого «так»
(`confirm_missing`) — і в картці листа, і в Конвеєрі.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage, Order
from app.routers import mail as mail_router_mod
from app.services.mail_accept import missing_accept_fields
from tests.test_mail_conveyor_grouping import _card, _letter
from tests.test_mail_transaction_safety import _database, _user, _wire


def _htmx_request(user_id: int):
    return SimpleNamespace(
        session={"user_id": user_id}, client=SimpleNamespace(host="127.0.0.1"),
        headers={"HX-Request": "true"},
    )


@pytest.fixture
def shop(tmp_path, monkeypatch):
    engine = _database()
    _export, spool = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mail_router_mod.templates, "TemplateResponse",
        lambda request, template, context: SimpleNamespace(
            template=template, context=context, headers={}, status_code=200,
        ),
    )
    return SimpleNamespace(engine=engine, spool=spool)


def test_missing_fields_names_what_is_empty():
    assert missing_accept_fields("", "") == ["кількість", "Sum3D"]
    assert missing_accept_fields("3", "") == ["Sum3D"]
    assert missing_accept_fields(" ", "12-01-45") == ["кількість"]
    assert missing_accept_fields("3", "12-01-45") == []


def _accept_card(db, user, email, **fields):
    base = dict(
        request=_htmx_request(user.id), email_id=email.id,
        client_name="Люмі-Дент", material_color="моно а3", kind="", quantity="",
        folder_pick="", folder_new="", material_folder="", attachment_ids=[],
        accept_anyway="", sum3d_id="", opak="", confirm_missing="", db=db,
    )
    base.update(fields)
    return mail_router_mod.accept_email(**base)


def test_card_without_quantity_warns_and_keeps_the_typed_sum3d(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email = _letter(db, shop.spool, "a1")
        out = _accept_card(db, user, email, sum3d_id="12-01-45", opak="2")
        assert out.template == "_mail_detail_panel.html"
        assert out.context["missing"] == ["кількість"]
        assert "кількість" in out.context["error"]
        # Вписане не губиться.
        assert out.context["sum3d_id"] == "12-01-45" and out.context["opak"] == "2"
        db.expire_all()
        assert db.get(EmailMessage, email.id).status == "нове"
        assert db.scalars(select(Order)).all() == []


def test_card_accepts_after_conscious_yes(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email = _letter(db, shop.spool, "a1")
        out = _accept_card(db, user, email, confirm_missing="1")
        assert out.status_code == 204  # HX-Redirect — прийнято
        assert len(db.scalars(select(Order)).all()) == 1


def test_card_with_everything_filled_accepts_without_asking(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email = _letter(db, shop.spool, "a1")
        out = _accept_card(db, user, email, quantity="3", sum3d_id="12-01-45")
        assert out.status_code == 204
        (order,) = db.scalars(select(Order)).all()
        assert order.quantity == "3" and order.sum3d_id == "12-01-45"


def _batch(db, user, cards, confirm=""):
    return mail_router_mod.accept_email_batch(
        request=_htmx_request(user.id), payload=json.dumps(cards),
        confirm_missing=confirm, db=db,
    )


def test_conveyor_stops_and_lists_letters_with_missing_fields(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        b = _letter(db, shop.spool, "b1")
        out = _batch(db, user, [
            _card(a, quantity="2", sum3d_id="10-00-01"),
            _card(b, client="Інший", quantity="", sum3d_id="10-00-01"),
        ])
        assert out.template == "_mail_batch_missing.html"
        assert out.headers["HX-Retarget"] == "#mb-preflight"
        assert [(r["email_id"], r["missing"]) for r in out.context["forgot"]] == [
            (b.id, ["кількість"]),
        ]
        # Нічого не прийнято — і повний лист теж.
        assert db.scalars(select(Order)).all() == []


def test_conveyor_accepts_after_accept_anyway(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        out = _batch(db, user, [_card(a)], confirm="1")
        assert out.template == "_mail_batch_result.html"
        assert len(db.scalars(select(Order)).all()) == 1


def test_conveyor_with_everything_filled_does_not_ask(shop):
    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        a = _letter(db, shop.spool, "a1")
        out = _batch(db, user, [_card(a, quantity="2", sum3d_id="10-00-01")])
        assert out.template == "_mail_batch_result.html"
