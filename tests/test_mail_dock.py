"""Док черги на екрані пошти (MAIL_LAB_DOCK_BRIEF.md, варіант A, 07.10.26).

Що ламається тихо:
- канон (scope="") мусить лишатись рівно тим дзеркалом, що й до доку — той
  самий набір і той самий порядок (власник: «вимкнено = як зараз»);
- лаба йде в ПОРЯДКУ ТАБЛИЦІ (CLAUDE.md §2), пошта — найновіша згори; між
  ними роздільник, інакше два порядки читаються як один;
- фільтр готовності діє на лабу/табличних, НЕ на пошту;
- "mail" на дроті = "" у базі (пастка «порожнє поле = поля не було»);
- клік по доку не має скидати відступ/ширину/крок шестерні;
- межі висоти в mail.js = look_prefs.DOCK_HEIGHT; розмітка пропонує лише
  слова, які сервер приймає.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import business_tab_today
from app.db import Base
from app.models import Order, User
from app.queue_filters import READY_FILTERS
from app.routers import auth as auth_router_mod
from app.services import look_prefs
from app.services.mail_mirror import mail_dock, mail_dock_for, mail_mirror_orders
from tests.asgi_client import MiniClient

ADMIN = ("dockadmin", "D0ck-Adm-1")
OPERATOR = ("dockop", "D0ck-Op-1")


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, **kw):
    user = User(username="op", password_hash="x", full_name="Оп", role="оператор", **kw)
    db.add(user)
    db.commit()
    return user


def _order(db, **kw):
    d = dict(source="email", client_name="Клієнт", material_color="pmma a2",
             quantity="6", status="прийнято", sheet_tab=business_tab_today().strftime("%d.%m.%y"))
    d.update(kw)
    o = Order(**d)
    db.add(o)
    db.commit()
    return o


def _seed_day(db):
    """Вкладка дня: 3 поштові (створені в такому порядку), 4 лабораторні з
    різною готовністю, 1 табличний клієнт, 1 вчорашня лаба, 1 архівна."""
    m1 = _order(db, client_name="Пошта-1", row_number=60)
    m2 = _order(db, client_name="Пошта-2", row_number=58)
    m3 = _order(db, client_name="Пошта-3", row_number=None)  # ще без рядка
    # лаба: порядок рядків навмисно не збігається з порядком створення
    l_take_b = _order(db, source="lab", client_name=None, work_order_no="L-можна-2",
                      job_code="2026-10-07_00044-001", sum3d_id=None, row_number=44)
    l_take_a = _order(db, source="lab", client_name=None, work_order_no="L-можна-1",
                      job_code="2026-10-07_00041-003", sum3d_id=None, row_number=41)
    l_work = _order(db, source="lab", client_name=None, work_order_no="L-в-роботі",
                    job_code="2026-10-07_00039-002", sum3d_id="12-01-45", row_number=39)
    l_wait = _order(db, source="lab", client_name=None, work_order_no="L-не-готово",
                    job_code=None, sum3d_id=None, row_number=52)
    sheet = _order(db, source="sheet_client", client_name="Табличний",
                   job_code="Табличний/07.10.26/emo a2", sum3d_id=None, row_number=88)
    _order(db, source="lab", client_name=None, work_order_no="L-вчора",
           job_code="x", sum3d_id=None, sheet_tab="25.09.26", row_number=7)
    arch = _order(db, source="lab", client_name=None, work_order_no="L-архів",
                  job_code="x", sum3d_id=None, row_number=99)
    from app.business_day import utc_now
    arch.archived_at = utc_now()
    db.commit()
    return SimpleNamespace(m1=m1, m2=m2, m3=m3, l_take_a=l_take_a, l_take_b=l_take_b,
                           l_work=l_work, l_wait=l_wait, sheet=sheet)


def _label(o: Order) -> str:
    return o.client_name or o.work_order_no or f"#{o.id}"


# ── 1. Канон незмінний ──────────────────────────────────────────────────────


def test_canon_scope_is_exactly_the_old_mirror():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _seed_day(db)
        names = [_label(o) for o in mail_mirror_orders(db)]
        assert names == ["Пошта-3", "Пошта-2", "Пошта-1"], "пошта дня, найновіша згори, нічого іншого"
        # Явний канон і «нічого не збережено» — одне й те саме.
        assert [_label(o) for o in mail_dock(db, scope="").orders] == names
        assert [_label(o) for o in mail_dock(db, scope="mail").orders] == names
        # Фільтр готовності на канон не діє — листи оператор щойно прийняв сам.
        assert [_label(o) for o in mail_dock(db, scope="", ready="not_ready").orders] == names
        dock = mail_dock(db)
        assert dock.sep_index is None
        assert dock.title == "Прийняте з пошти"
        # Лічильники вже стоять на кнопках — щоб було видно, що вмикати.
        assert (dock.mail_count, dock.lab_count, dock.all_count) == (3, 2, 8)
        assert dock.ready_counts == {"all": 4, "not_ready": 1, "can_take": 2, "in_work": 1}


# ── 2–4. Лаба і вся вкладка ─────────────────────────────────────────────────


def test_lab_scope_appends_lab_rows_in_sheet_order_with_a_separator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _seed_day(db)
        dock = mail_dock(db, scope="lab")  # ready за замовчуванням = can_take
        names = [_label(o) for o in dock.orders]
        assert names == ["Пошта-3", "Пошта-2", "Пошта-1", "L-можна-1", "L-можна-2"], (
            "пошта найновіша згори, далі лаба за рядком таблиці (41, 44), табличних клієнтів немає"
        )
        assert dock.sep_index == 3, "роздільник перед першим лабораторним рядком"
        assert dock.title == "Черга"
        assert dock.ready == "can_take"
        assert (dock.mail_count, dock.lab_count) == (3, 2)

        # Без фільтра — уся лаба дня, і далі в порядку таблиці (39, 41, 44, 52).
        names_all = [_label(o) for o in mail_dock(db, scope="lab", ready="all").orders]
        assert names_all[3:] == ["L-в-роботі", "L-можна-1", "L-можна-2", "L-не-готово"]
        assert "L-вчора" not in names_all and "L-архів" not in names_all

        # «В роботі» — лише той, у кого стоїть Sum3D.
        assert [_label(o) for o in mail_dock(db, scope="lab", ready="in_work").orders][3:] == ["L-в-роботі"]


def test_lab_scope_without_mail_or_without_lab_has_no_separator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _order(db, source="lab", client_name=None, work_order_no="L", job_code="x", row_number=7)
        dock = mail_dock(db, scope="lab")
        assert [_label(o) for o in dock.orders] == ["L"]
        assert dock.sep_index is None, "роздільник ділить ДВА блоки; один блок межі не потребує"
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _order(db, client_name="Лише пошта", row_number=7)
        dock = mail_dock(db, scope="lab")
        assert dock.sep_index is None


def test_labonly_scope_shows_just_the_lab_rows_in_sheet_order():
    """Власник 08.10.26: «не можна включити тільки лабораторні, ті що можна
    брати» — у «+ Лабораторія» 50 листів дня стояли над 7 лабораторними."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _seed_day(db)
        dock = mail_dock(db, scope="labonly")  # can_take
        assert [_label(o) for o in dock.orders] == ["L-можна-1", "L-можна-2"]
        assert dock.sep_index is None and dock.title == "Лабораторія"
        assert [_label(o) for o in mail_dock(db, scope="labonly", ready="all").orders] == [
            "L-в-роботі", "L-можна-1", "L-можна-2", "L-не-готово"
        ]
        user = _user(db)
        look_prefs.apply_mail_look(user, dock="labonly")
        assert user.mail_dock_scope == "labonly"


def test_all_scope_is_sheet_order_and_filters_everything_but_mail():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _seed_day(db)
        dock = mail_dock(db, scope="all")  # can_take
        names = [_label(o) for o in dock.orders]
        # Робота без рядка (щойно прийнята пошта) — першою; далі рядки 41, 44,
        # 58, 60, 88. Лаба «в роботі»/«не готово» відсіяна, пошта — ні.
        assert names == ["Пошта-3", "L-можна-1", "L-можна-2", "Пошта-2", "Пошта-1", "Табличний"]
        assert dock.sep_index is None, "один порядок — роздільник не потрібен"
        assert dock.all_count == 8
        assert dock.ready_counts == {"all": 5, "not_ready": 1, "can_take": 3, "in_work": 1}

        names_all = [_label(o) for o in mail_dock(db, scope="all", ready="all").orders]
        assert names_all == ["Пошта-3", "L-в-роботі", "L-можна-1", "L-можна-2", "L-не-готово",
                             "Пошта-2", "Пошта-1", "Табличний"]


def test_unknown_scope_or_ready_from_the_database_degrade_to_defaults():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _seed_day(db)
        assert mail_dock(db, scope="вигадка").scope == ""
        assert mail_dock(db, scope="lab", ready="хех").ready == "can_take"
        # Висота поза межами підтягується, 0 лишається нулем («як було»).
        assert mail_dock(db, height=99999).height == look_prefs.DOCK_HEIGHT.high
        assert mail_dock(db, height=0).height == 0


def test_mail_dock_for_reads_the_operator_prefs():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _seed_day(db)
        user = _user(db, mail_dock_scope="lab", mail_dock_ready="in_work", mail_dock_height=300)
        dock = mail_dock_for(db, user)
        assert (dock.scope, dock.ready, dock.height) == ("lab", "in_work", 300)
        assert [_label(o) for o in dock.orders][3:] == ["L-в-роботі"]
        # Порожній фільтр у базі = «можна брати».
        user.mail_dock_ready = ""
        assert mail_dock_for(db, user).ready == "can_take"
        # Анонім/без користувача — канон.
        assert mail_dock_for(db, None).scope == ""


# ── 5. Збереження через /account/look ───────────────────────────────────────


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(),
    )


def _save(request, db, **kwargs):
    # Усі необовʼязкові поля — None явно: при виклику роута НАПРЯМУ `Form(None)`
    # лишається обʼєктом fastapi.params.Form, а не None.
    payload = {
        "row_pad": None, "list_width": None, "density": "", "mat_style": "", "step": None,
        "view": None, "layout": None, "flow": None,
        "dock": None, "dock_ready": None, "dock_height": None,
    }
    payload.update(kwargs)
    return asyncio.run(auth_router_mod.post_account_look(request=request, db=db, **payload))


def test_dock_prefs_save_and_canon_has_its_own_word():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        request = _request(user.id)

        assert _save(request, db, scope="mail", dock="lab").status_code == 204
        db.refresh(user)
        assert user.mail_dock_scope == "lab"

        # "mail" на дроті — порожній рядок у базі (канон), не окреме слово.
        _save(request, db, scope="mail", dock="mail")
        db.refresh(user)
        assert user.mail_dock_scope == ""

        _save(request, db, scope="mail", dock="all")
        _save(request, db, scope="mail", dock_ready="in_work")
        db.refresh(user)
        assert (user.mail_dock_scope, user.mail_dock_ready) == ("all", "in_work")
        # "can_take" на дроті — канон "" у базі.
        _save(request, db, scope="mail", dock_ready="can_take")
        db.refresh(user)
        assert user.mail_dock_ready == ""

        _save(request, db, scope="mail", dock_height=99999)
        db.refresh(user)
        assert user.mail_dock_height == look_prefs.DOCK_HEIGHT.high
        _save(request, db, scope="mail", dock_height=0)
        db.refresh(user)
        assert user.mail_dock_height == 0

        # Невідоме — 422 і нічого не стерто.
        assert _save(request, db, scope="mail", dock="вигадка").status_code == 422
        assert _save(request, db, scope="mail", dock_ready="хех").status_code == 422
        db.refresh(user)
        assert (user.mail_dock_scope, user.mail_dock_ready) == ("all", "")


def test_dock_click_does_not_reset_the_gear_numbers():
    """Кнопки доку шлють лише своє поле. До 07.10.26 apply_mail_look читав
    відступ/ширину/крок безумовно — клік по вигляду екрана скидав їх у канон,
    а перемикач доку тиснуть десятки разів на день."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        request = _request(user.id)
        _save(request, db, scope="mail", row_pad=14, list_width=700, step=4, view="conversation")
        db.refresh(user)
        assert (user.mail_row_pad, user.mail_list_width, user.mail_ui_step) == (14, 700, 4)

        _save(request, db, scope="mail", dock="lab")
        _save(request, db, scope="mail", dock_ready="all")
        _save(request, db, scope="mail", dock_height=320)
        _save(request, db, scope="mail", view="classic")
        db.refresh(user)
        assert (user.mail_row_pad, user.mail_list_width, user.mail_ui_step) == (14, 700, 4), (
            "кнопки вигляду й доку не мають чіпати числа шестерні"
        )
        assert (user.mail_dock_scope, user.mail_dock_ready, user.mail_dock_height) == ("lab", "all", 320)
        # І навпаки: шестерня (числа) не чіпає док.
        _save(request, db, scope="mail", row_pad=6, list_width=0, step=2)
        db.refresh(user)
        assert (user.mail_dock_scope, user.mail_dock_ready, user.mail_dock_height) == ("lab", "all", 320)


def test_apply_mail_look_none_means_leave_alone():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, mail_dock_scope="lab", mail_dock_ready="in_work", mail_dock_height=300,
                     mail_row_pad=10)
        look_prefs.apply_mail_look(user)
        assert (user.mail_dock_scope, user.mail_dock_ready, user.mail_dock_height, user.mail_row_pad) == (
            "lab", "in_work", 300, 10)
        with pytest.raises(look_prefs.LookError):
            look_prefs.apply_mail_look(user, dock="xx")
        with pytest.raises(look_prefs.LookError):
            look_prefs.apply_mail_look(user, dock_ready="xx")


# ── 6. Дзеркала в JS і розмітці ─────────────────────────────────────────────


def test_js_dock_height_limits_match_the_server():
    js = Path("app/static/js/mail.js").read_text(encoding="utf-8")
    found = re.search(r"DOCK_H_LIMITS = \[(\d+), (\d+)\]", js)
    assert found, "не знайдено DOCK_H_LIMITS у mail.js — оновіть і сторожа"
    low, high = (int(g) for g in found.groups())
    assert (low, high) == (look_prefs.DOCK_HEIGHT.low, look_prefs.DOCK_HEIGHT.high)


def test_dock_markup_only_offers_values_the_server_accepts():
    html = Path("app/templates/_mail_dock_controls.html").read_text(encoding="utf-8")
    scopes = set(re.findall(r'data-dock-scope="([a-z]+)"', html))
    assert scopes == {"mail", "lab", "labonly", "all"}
    assert scopes <= set(look_prefs.MAIL_DOCK_SCOPES)
    readies = set(re.findall(r'data-dock-ready="([a-z_]+)"', html))
    assert readies == set(READY_FILTERS), "усі чотири фільтри готовності, і жодного зайвого"
    # Канон має ВЛАСНЕ слово на дроті — порожнього значення в розмітці нема.
    assert 'data-dock-scope=""' not in html


# ── 7. Рендер через справжній застосунок ────────────────────────────────────


@pytest.fixture
def app_db(monkeypatch):
    """Застосунок на базі в памʼяті + ліцензія — рецепт test_settings_slabs_render."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import app.web as web
    from app import license as license_module
    from app.auth import hash_password
    from app.routers import deps
    from app.settings_store import set_setting

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)

    def session_factory():
        return Session(engine, expire_on_commit=False)

    monkeypatch.setattr(deps, "SessionLocal", session_factory)
    monkeypatch.setattr(web, "SessionLocal", session_factory, raising=False)

    private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        license_module, "_PUBLIC_KEY_BYTES",
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    with session_factory() as db:
        set_setting(
            db, "license_key",
            license_module.encode_license_key(
                {
                    "machine_id": license_module.get_machine_id(),
                    "customer": "tests",
                    "issued_at": "2026-01-01T00:00:00",
                    "expires_at": "2099-01-01T00:00:00",
                },
                private,
            ),
        )
        db.add(User(username=OPERATOR[0], password_hash=hash_password(OPERATOR[1]),
                    full_name="Оператор", role="оператор"))
        db.commit()
    return web.app, session_factory


def _login(app) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303), status
    return client


@pytest.mark.parametrize("scope", ["", "lab", "labonly", "all"])
def test_queue_mirror_renders_for_every_scope(app_db, scope):
    app, session_factory = app_db
    with session_factory() as db:
        _seed_day(db)
        user = db.query(User).filter_by(username=OPERATOR[0]).one()
        user.mail_dock_scope = scope
        user.mail_dock_ready = ""
        db.commit()

    client = _login(app)
    status, _, html = client.get("/mail/queue-mirror")
    assert status == 200, status
    assert ("Пошта-3" in html) == (scope != "labonly"), "лише лаба — без пошти"
    assert 'id="qmir-ctl"' in html and 'hx-swap-oob="true"' in html, "шапка їде oob-свапом із поллом"
    if scope == "":
        assert "L-можна-1" not in html and "qmir-sep" not in html
        assert "Прийняте з пошти" in html
        assert "data-dock-ready" not in html, "у канона чипів готовності немає — лише перемикач"
    else:
        assert "L-можна-1" in html
        assert 'data-dock-ready="can_take" aria-pressed="true"' in html
    if scope == "lab":
        assert 'class="qmir-sep"' in html
        assert "Табличний" not in html
    if scope == "all":
        assert "Табличний" in html and "qmir-sep" not in html
    if scope == "labonly":
        assert "Табличний" not in html and "qmir-sep" not in html

    # Повна сторінка теж малюється (шапка включена без oob).
    status, _, page = client.get("/mail")
    assert status == 200, status
    assert page.count('id="qmir-ctl"') == 1
    assert 'data-dock-grip' in page


def test_queue_mirror_empty_state_names_the_scope(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        user = db.query(User).filter_by(username=OPERATOR[0]).one()
        user.mail_dock_scope = "lab"
        db.commit()
    client = _login(app)
    status, _, html = client.get("/mail/queue-mirror")
    assert status == 200
    assert "qmir-empty" in html and "лабораторних" in html
