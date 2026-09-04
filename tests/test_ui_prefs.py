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
            request=_request(user.id), theme="forge", icons="neon",
            buttons="glass", loader="ring", chips="marker",
            machine_art="", machine_strip="", machine_card="", db=db,
        ))
        assert resp.status_code == 204
        db.refresh(user)
        assert user.ui_theme == "forge"
        assert user.ui_icon_style == "neon"
        assert user.ui_button_style == "glass"
        assert user.ui_loader_style == "ring"
        assert user.ui_chip_style == "marker"


def test_appearance_rejects_unknown_values():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = asyncio.run(auth_router_mod.post_account_appearance(
            request=_request(user.id), theme="hotpink", icons="", db=db,
        ))
        assert resp.status_code == 422
        db.refresh(user)
        # Дефолт теми тепер "forge" (Amber Forge) — відхилене значення його
        # не чіпає.
        assert user.ui_theme == "forge"

        resp = asyncio.run(auth_router_mod.post_account_appearance(
            request=_request(user.id), theme="", icons="comic-sans", db=db,
        ))
        assert resp.status_code == 422
        db.refresh(user)
        assert user.ui_icon_style == ""

        # Решта набору відсікається так само: значення летить атрибутом
        # у <html> кожної сторінки, тож «якось зберегти» не можна.
        for bad in ({"buttons": "neon-pink"}, {"loader": "spinner3000"}, {"chips": "blob"}):
            kwargs = {"theme": "", "icons": "", "buttons": "", "loader": "", "chips": ""}
            kwargs.update(bad)
            resp = asyncio.run(auth_router_mod.post_account_appearance(
                request=_request(user.id), db=db, **kwargs,
            ))
            assert resp.status_code == 422


def test_ui_prefs_reads_logged_in_user_and_caches(monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        forge_op = _user(db, username="forge_op")
        forge_op.ui_theme = "forge"
        forge_op.ui_icon_style = "thin"
        forge_op.ui_button_style = "glass"
        forge_op.ui_loader_style = "ring"
        forge_op.ui_chip_style = "marker"
        teal_op = _user(db, username="teal_op")
        teal_op.ui_theme = "teal"  # явний вибір бірюзового канону
        db.commit()

    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: Session(engine))

    req = _request(forge_op.id)
    prefs = deps_mod.ui_prefs(req)
    assert prefs == {"theme": "forge", "icons": "thin", "buttons": "glass",
                     "loader": "ring", "chips": "marker",
                     "mail_row_pad": 0, "mail_list_w": 0, "mail_step": 0, "queue_density": "", "queue_row_pad": 0,
                     "queue_mat_style": "", "queue_step": 0, "handout_layout": "",
                     "machine_art": "", "machine_strip": "", "machine_card": ""}
    # кеш на request.state: другий виклик не ходить у БД
    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("no cache")))
    assert deps_mod.ui_prefs(req) is prefs

    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: Session(engine))
    # Явно вибраний бірюзовий канон → "teal"; решта набору порожня.
    teal_prefs = {"theme": "teal", "icons": "", "buttons": "", "loader": "", "chips": "",
             "mail_row_pad": 0, "mail_list_w": 0, "mail_step": 0, "queue_density": "", "queue_row_pad": 0,
                     "queue_mat_style": "", "queue_step": 0, "handout_layout": "",
                     "machine_art": "", "machine_strip": "", "machine_card": ""}
    assert deps_mod.ui_prefs(_request(teal_op.id)) == teal_prefs
    # без сесії — дефолт (Amber Forge), без падіння
    assert deps_mod.ui_prefs(_request(None)) == dict(teal_prefs, theme="forge")


def test_base_html_renders_theme_attrs_on_html_tag():
    """base.html ставить data-theme/data-icons на <html> — сервер-сайд,
    без JS. Порожні значення атрибутів не додають (канон = чистий тег)."""
    tpl = deps_mod.templates.env.get_template("base.html")

    class _Req(SimpleNamespace):
        pass

    def _render(theme, icons):
        req = _Req(
            session={},
            state=SimpleNamespace(ui_prefs_cache={
                "theme": theme, "icons": icons,
                "buttons": "", "loader": "", "chips": "",
            }),
            url_for=lambda *a, **k: "/",
        )
        return tpl.render(request=req, toast_flash=None)

    html = _render("forge", "neon")
    assert '<html lang="uk" data-theme="forge" data-icons="neon">' in html
    html = _render("", "")
    assert '<html lang="uk">' in html


def test_theme_survives_a_dead_database(monkeypatch):
    """Сторінка помилки показується САМЕ ТОДІ, коли з базою погано — і колись
    саме там оператор бачив чужу тему. ui_prefs дзеркалить набір у сесію
    (підписана кука, без БД) і бере його звідти, коли БД мовчить."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        op = _user(db, username="forge_offline")
        op.ui_theme = "forge"
        op.ui_icon_style = "thin"
        db.commit()

    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: Session(engine))
    healthy = _request(op.id)
    assert deps_mod.ui_prefs(healthy)["theme"] == "forge"
    # набір осів у сесії — саме її несе браузер на наступний запит
    mirror = healthy.session[deps_mod.UI_SESSION_KEY]
    assert mirror["theme"] == "forge"

    def _dead():
        raise RuntimeError("no such column: users.ui_theme")

    monkeypatch.setattr(deps_mod, "SessionLocal", _dead)
    broken = _request(op.id)
    broken.session[deps_mod.UI_SESSION_KEY] = mirror
    prefs = deps_mod.ui_prefs(broken)
    assert prefs["theme"] == "forge", "тема мусить пережити мертву БД"
    assert prefs["icons"] == "thin"

    # Анонім із мертвою БД — дефолт (Amber Forge); дзеркала в нього немає.
    assert deps_mod.ui_prefs(_request(None))["theme"] == "forge"
