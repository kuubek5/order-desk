"""Візуальні налаштування оператора: тема + стиль іконок на акаунті.

Три речі, що ламаються тихо:
- невалідне значення з форми полетіло б атрибутом у <html> кожної сторінки —
  тому роут відсікає все поза білим списком;
- ui_prefs() мусить кешувати на request.state (base.html кличе його раз,
  але майбутні шаблони можуть кликати частіше);
- вибір одного оператора не має протікати іншому.
"""
import asyncio
from types import SimpleNamespace

from starlette.datastructures import Headers
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import User
from app.routers import auth as auth_router_mod
from app.routers import deps as deps_mod


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, username="op"):
    u = User(username=username, password_hash="x", full_name="Оп", role="оператор")
    db.add(u)
    db.commit()
    return u


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers({}),
        state=SimpleNamespace(),
    )


def test_appearance_saves_theme_and_icons():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = asyncio.run(auth_router_mod.post_account_appearance(
            request=_request(user.id), theme="forge", icons="neon", db=db,
        ))
        assert resp.status_code == 204
        db.refresh(user)
        assert user.ui_theme == "forge"
        assert user.ui_icon_style == "neon"


def test_appearance_rejects_unknown_values():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = asyncio.run(auth_router_mod.post_account_appearance(
            request=_request(user.id), theme="hotpink", icons="", db=db,
        ))
        assert resp.status_code == 422
        db.refresh(user)
        assert user.ui_theme == ""

        resp = asyncio.run(auth_router_mod.post_account_appearance(
            request=_request(user.id), theme="", icons="comic-sans", db=db,
        ))
        assert resp.status_code == 422
        db.refresh(user)
        assert user.ui_icon_style == ""


def test_ui_prefs_reads_logged_in_user_and_caches(monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        forge_op = _user(db, username="forge_op")
        forge_op.ui_theme = "forge"
        forge_op.ui_icon_style = "thin"
        teal_op = _user(db, username="teal_op")
        db.commit()

    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: Session(engine))

    req = _request(forge_op.id)
    prefs = deps_mod.ui_prefs(req)
    assert prefs == {"theme": "forge", "icons": "thin"}
    # кеш на request.state: другий виклик не ходить у БД
    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("no cache")))
    assert deps_mod.ui_prefs(req) is prefs

    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: Session(engine))
    assert deps_mod.ui_prefs(_request(teal_op.id)) == {"theme": "", "icons": ""}
    # без сесії — канон, без падіння
    assert deps_mod.ui_prefs(_request(None)) == {"theme": "", "icons": ""}


def test_base_html_renders_theme_attrs_on_html_tag():
    """base.html ставить data-theme/data-icons на <html> — сервер-сайд,
    без JS. Порожні значення атрибутів не додають (канон = чистий тег)."""
    tpl = deps_mod.templates.env.get_template("base.html")

    class _Req(SimpleNamespace):
        pass

    def _render(theme, icons):
        req = _Req(
            session={},
            state=SimpleNamespace(ui_prefs_cache={"theme": theme, "icons": icons}),
            url_for=lambda *a, **k: "/",
        )
        return tpl.render(request=req, toast_flash=None)

    html = _render("forge", "neon")
    assert '<html lang="uk" data-theme="forge" data-icons="neon">' in html
    html = _render("", "")
    assert '<html lang="uk">' in html
