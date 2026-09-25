"""Поіменний доступ до закритого розділу через картку Налаштувань (25.09.26)."""

from __future__ import annotations

from sqlalchemy import select

from app.models import User
from app.services import section_gate as sg
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401


def _op_id(factory):
    with factory() as db:
        return db.scalar(select(User.id).where(User.username == OPERATOR[0]))


def test_admin_opens_mail_for_one_operator_and_banner_keeps_it(app_db):  # noqa: F811
    app, factory = app_db
    op_id = _op_id(factory)
    admin = MiniClient(app)
    admin.login(*ADMIN)
    _, _, page = admin.get("/settings")
    assert 'name="users_sent"' in page and f'name="user" value="{op_id}"' in page

    admin.post("/settings/sections/mail", {
        "state": "gauge", "audience_all": "1", "users_sent": "1", "user": str(op_id),
        "back": "/settings#sections",
    })
    with factory() as db:
        assert sg.section_users(db, "mail") == {op_id}

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    _, _, mail = operator.get("/mail")
    assert "mailv2" in mail  # сама пошта, не блокатор

    # Банер над розділом полів людей не шле — поіменний доступ не зникає.
    admin.post("/settings/sections/mail", {"variant": "shutter", "back": "/mail"})
    with factory() as db:
        assert sg.section_users(db, "mail") == {op_id}

    # Картка без жодної галочки (маркер є) — доступ знято.
    admin.post("/settings/sections/mail", {"state": "gauge", "users_sent": "1", "back": "/"})
    with factory() as db:
        assert sg.section_users(db, "mail") == set()
    _, _, blocked = operator.get("/mail")
    assert "mailv2" not in blocked
