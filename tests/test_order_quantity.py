"""Правка кількості одиниць прямо в рядку черги — ЛИШЕ для клієнтських робіт
(пошта / вписаний клієнт). Плюс дзеркало черги на екрані пошти (mail_mirror).

Кількість лабораторного рядка задає технік у таблиці — портал її не чіпає:
форма в рядку показується лише для email/sheet_client, і сервер тримає те саме
правило (пряма POST на лабораторну роботу → 400).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers import orders as orders_router_mod
from app.db import Base
from app.models import ActionLog, Order, User
from app.services.mail_mirror import mail_mirror_orders
from app.services.undo import perform_redo, perform_undo


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, role="оператор", username="op"):
    u = User(username=username, password_hash="x", full_name="Оп", role=role)
    db.add(u)
    db.commit()
    return u


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers({}),
    )


def _order(db, **kw):
    d = dict(source="email", client_name="Клієнт", material_color="pmma a2",
             quantity="6", status="прийнято")
    d.update(kw)
    o = Order(**d)
    db.add(o)
    db.commit()
    return o


def _run_quantity(db, user, order, value):
    with patch.object(orders_router_mod, "await_on_writeback",
                      new_callable=AsyncMock, return_value=None) as write, \
         patch.object(orders_router_mod, "attach_export_folder_uris"), \
         patch.object(orders_router_mod, "attach_job_code_folder_uris"), \
         patch.object(orders_router_mod, "attach_action_toast"), \
         patch.object(orders_router_mod, "attach_sync_error_toast"), \
         patch.object(web.templates, "TemplateResponse",
                      return_value=SimpleNamespace(headers={})):
        asyncio.run(orders_router_mod.set_quantity(
            request=_request(user.id), order_id=order.id, quantity=value, db=db))
    return write


def test_quantity_edit_updates_email_order_and_writes_field():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, source="email", quantity="6")
        write = _run_quantity(db, user, order, "8")
        db.refresh(order)
        assert order.quantity == "8"
        # запис у таблицю — саме поле quantity
        assert write.await_args[0][0] is orders_router_mod.write_sheet_fields_warm
        assert write.await_args[0][2] == {"quantity"}
        # дія залогована як "quantity" (для «Крок назад»)
        entry = db.scalars(select(ActionLog).where(ActionLog.action_type == "quantity")).first()
        assert entry is not None and entry.old_value == "6" and entry.new_value == "8"


def test_quantity_edit_works_for_sheet_client():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, source="sheet_client", sheet_tab="26.08.26",
                       row_number=7, quantity="2")
        write = _run_quantity(db, user, order, "3")
        db.refresh(order)
        assert order.quantity == "3"
        assert write.await_args[0][2] == {"quantity"}


def test_quantity_empty_value_clears():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, source="email", quantity="6")
        _run_quantity(db, user, order, "")
        db.refresh(order)
        assert order.quantity is None
        entry = db.scalars(select(ActionLog).where(ActionLog.action_type == "quantity")).first()
        assert entry is not None and entry.new_value == ""


def test_quantity_edit_rejected_for_lab_row():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, source="lab", client_name=None,
                       work_order_no="24122", sheet_tab="26.08.26", row_number=7)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(orders_router_mod.set_quantity(
                request=_request(user.id), order_id=order.id, quantity="9", db=db))
        assert exc.value.status_code == 400
        db.refresh(order)
        assert order.quantity == "6"  # незмінено


def test_quantity_undo_and_redo_restore_values():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, source="email", quantity="6")
        _run_quantity(db, user, order, "8")
        entry = db.scalars(select(ActionLog).where(ActionLog.action_type == "quantity")).first()
        # запис у таблицю всередині undo/redo — не чіпаємо мережу
        with patch("app.services.undo.submit_sheet_write") as sub:
            sub.return_value = SimpleNamespace(result=lambda: None)
            out = perform_undo(db, user, entry)
            assert out.kind == "success"
            db.refresh(order)
            assert order.quantity == "6"
            out2 = perform_redo(db, user, entry)
            assert out2.kind == "success"
            db.refresh(order)
            assert order.quantity == "8"


# --- дзеркало черги на екрані пошти ------------------------------------------


def test_mail_mirror_shows_only_email_orders_newest_day_first():
    engine = _database()
    from datetime import timedelta

    from app.business_day import business_today, utc_now
    # Дати відносно справжнього робочого «сьогодні», щоб не впертись у вікно
    # retention (30 днів): фіксовані дати з минулого місяця час від часу самі
    # виходили б за нього й губились.
    day_new = business_today().strftime("%d.%m.%y")
    day_old = (business_today() - timedelta(days=3)).strftime("%d.%m.%y")
    with Session(engine, expire_on_commit=False) as db:
        _user(db)
        # дві поштові роботи різних днів + чужі джерела + архівна
        _order(db, source="email", client_name="Стара", sheet_tab=day_old,
               row_number=7)
        _order(db, source="email", client_name="Нова", sheet_tab=day_new,
               row_number=7)
        _order(db, source="lab", client_name=None, work_order_no="24122",
               sheet_tab=day_new, row_number=8)
        _order(db, source="sheet_client", client_name="Табличний",
               sheet_tab=day_new, row_number=9)
        archived = _order(db, source="email", client_name="Архівна",
                          sheet_tab=day_new, row_number=10)
        archived.archived_at = utc_now()
        db.commit()

        rows = mail_mirror_orders(db)
        names = [o.client_name for o in rows]
        assert names == ["Нова", "Стара"]  # лише email, найновіший день згори
        assert "Табличний" not in names and "Архівна" not in names
