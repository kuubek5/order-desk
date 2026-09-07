"""Operator vs admin boundary on /settings and friends.

Рішення власника 06.09.26 зсунуло межу: «Джерела робіт» (Google Таблиця,
пошта, шляхи, фільтри, матеріали) і «Обладнання» (пічки, верстати) редагує
й оператор — включно з секретами, бо в цеху за верстатом стоїть саме він.
Права описані одним реєстром `app/services/settings_nav.py` (`can_edit`).
Адмінськими лишились люди й доступ, бекап, оновлення, ліцензія та синк —
адміністрування самої машини, а не робота цеху.

The two filesystem paths (export_folder_path, technician_files_path — see
app.settings_store.OPERATOR_EDITABLE_KEYS) were open to any logged-in
operator from the start: a per-machine detail, not a secret.

Every boundary is asserted at the ROUTE level (calling the handler directly,
same convention as test_settings_routes.py), because settings.html hiding a
card is not a security control by itself — the server must refuse a
hand-crafted request the same way regardless of what the DOM shows.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers import settings as settings_router_mod
from app.routers import queue as queue_router_mod
from app.db import Base
from app.models import User
from app.settings_store import get_setting


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _admin(db: Session) -> User:
    user = User(username="admin", password_hash="unused", full_name="Адмін", role="адмін")
    db.add(user)
    db.commit()
    return user


def _operator(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Оператор", role="оператор")
    db.add(user)
    db.commit()
    return user


def _form_request(user_id: int, form: dict, host: str = "127.0.0.1", headers: dict | None = None):
    """A fake Request whose `await .form()` returns `form`, matching what
    `post_settings`/`export_backup`/`import_backup` actually call.

    `headers` defaults to empty, i.e. a plain (non-HTMX) browser POST — the
    path that still redirects. Pass {"HX-Request": "true"} to exercise the
    HTMX branch that answers 204 + toast instead.
    """
    async def _form():
        return form

    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host=host),
        form=_form,
        headers=Headers(headers or {}),
    )


# --- GET /settings: reachable by any logged-in role now -------------------


def test_get_settings_allows_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        response = settings_router_mod.get_settings(request=SimpleNamespace(session={"user_id": operator.id}), db=db)
    # A real TemplateResponse (not a redirect/exception) — status 200 by default.
    assert getattr(response, "status_code", 200) == 200


def test_get_settings_hides_operator_list_from_non_admin(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        context = settings_router_mod.get_settings(request=SimpleNamespace(session={"user_id": operator.id}), db=db)
    assert context["operators"] == []


def test_get_settings_still_requires_login():
    engine = _database()
    with Session(engine) as db:
        response = settings_router_mod.get_settings(request=SimpleNamespace(session={}), db=db)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- POST /settings: operator may only touch path fields -------------------


def test_operator_can_save_path_fields():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(
            operator.id,
            {"action": "save", "export_folder_path": r"D:\export", "technician_files_path": r"D:\tech"},
        )
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert response.status_code == 303
    assert get_setting(db, "export_folder_path") == r"D:\export"
    assert get_setting(db, "technician_files_path") == r"D:\tech"


def test_operator_cannot_set_license_key_even_when_posted():
    """Field-level enforcement: саморобний POST із чужим ключем має бути
    мовчки відкинутий, а не застосований.

    Межа зсунулась (рішення власника 06.09.26): секрети «Джерел робіт» —
    Google Sheet ID, пароль пошти — оператор тепер зберігає сам, бо цехом
    керує він. Незмінним лишилось адміністрування самої машини: ключ
    ліцензії (розділ `license`) не має розділу для оператора в реєстрі
    `app/services/settings_nav.py`, тож поле з форми не долітає до БД.
    """
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(
            operator.id,
            {
                "action": "save",
                "export_folder_path": r"D:\export",
                "google_sheet_id": "operator-supplied-sheet-id",
                "imap_password": "operator-supplied-password",
                "license_key": "attacker-supplied-license",
            },
        )
        asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert get_setting(db, "export_folder_path") == r"D:\export"
    # Джерела робіт — тепер операторські.
    assert get_setting(db, "google_sheet_id") == "operator-supplied-sheet-id"
    assert get_setting(db, "imap_password") == "operator-supplied-password"
    # Ліцензія — ні.
    assert get_setting(db, "license_key") is None


def test_operator_cannot_trigger_sync_even_when_requested():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {"action": "save_and_sync", "export_folder_path": r"D:\export"})
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    # Falls back to a plain save-and-redirect-to-/settings, never the
    # sync branch's redirect to "/".
    assert response.headers["location"] == "/settings?saved=1"


def test_admin_can_still_set_everything():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        request = _form_request(
            admin.id,
            {
                "action": "save",
                "google_sheet_id": "real-sheet-id",
                "imap_login": "lab@ukr.net",
                "export_folder_path": r"D:\export",
            },
        )
        asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert get_setting(db, "google_sheet_id") == "real-sheet-id"
    assert get_setting(db, "imap_login") == "lab@ukr.net"
    assert get_setting(db, "export_folder_path") == r"D:\export"


def test_saving_one_section_does_not_wipe_the_others():
    """Сторож бойової втрати налаштувань 03.09.26.

    Екран налаштувань має ТРИ окремі <form>, і всі три шлють POST на
    /settings: Google, IMAP, шляхи. Обробник іде по ВСІХ SETTING_FIELDS, і
    поки він читав form.get(key, ""), «поля не було в цій формі» ставало
    «поле порожнє» — а порожнє для CLEARABLE_SETTING_KEYS означає «стерти».

    Наслідок у бою: оператор зберіг шлях до проєктів Sum3D і тієї ж миті
    втратив Google Sheet ID (застосунок написав «таблиця не може
    синхронізуватися») і шлях до export (видача показала «0 тек у сховищі»).
    Один клік — три стерті налаштування, жодного попередження.
    """
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)

        # Секція «Google» — заповнюємо все, як при першому налаштуванні.
        asyncio.run(settings_router_mod.post_settings(
            request=_form_request(admin.id, {
                "action": "save",
                "google_sheet_id": "real-sheet-id",
                "export_folder_path": r"\\Systems\Export",
                "technician_files_path": r"\\Systems\Tech",
            }),
            db=db,
        ))

        # Секція «Шляхи» — окрема форма, у ній НЕМАЄ google_sheet_id.
        asyncio.run(settings_router_mod.post_settings(
            request=_form_request(admin.id, {
                "action": "save",
                "export_folder_path": r"\\Systems\Export",
                "technician_files_path": r"\\Systems\Tech",
                "sum3d_projects_path": r"D:\CAM-WORK",
            }),
            db=db,
        ))

    assert get_setting(db, "sum3d_projects_path") == r"D:\CAM-WORK"
    assert get_setting(db, "google_sheet_id") == "real-sheet-id", (
        "збереження секції шляхів стерло Google Sheet ID"
    )

    # І навпаки: збереження самої лише секції Google не має зносити шляхи.
    with Session(engine, expire_on_commit=False) as db2:
        admin2 = db2.query(User).filter_by(username="admin").one()
        asyncio.run(settings_router_mod.post_settings(
            request=_form_request(admin2.id, {
                "action": "save",
                "google_sheet_id": "real-sheet-id",
            }),
            db=db2,
        ))
    assert get_setting(db2, "export_folder_path") == r"\\Systems\Export", (
        "збереження секції Google стерло шлях до export"
    )
    assert get_setting(db2, "sum3d_projects_path") == r"D:\CAM-WORK"


def test_empty_field_that_is_present_still_clears():
    """Зворотний бік: очищення руками мусить лишитись робочим. Помилковий
    мережевий шлях вішає видачу, і зняти його треба саме порожнім полем —
    поле в формі Є, його просто стерли."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        asyncio.run(settings_router_mod.post_settings(
            request=_form_request(admin.id, {
                "action": "save", "export_folder_path": r"\\dead\share",
            }),
            db=db,
        ))
        assert get_setting(db, "export_folder_path") == r"\\dead\share"
        asyncio.run(settings_router_mod.post_settings(
            request=_form_request(admin.id, {
                "action": "save", "export_folder_path": "",
            }),
            db=db,
        ))
    assert get_setting(db, "export_folder_path") == ""


def test_post_settings_still_requires_login():
    engine = _database()
    with Session(engine) as db:
        request = _form_request(None, {})
        request.session = {}
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Admin-only routes stay admin-only (regression) -------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda request, db: queue_router_mod.sync_sheets(request=request, db=db),
        # Раніше тут стояв test_imap_connection — з 06.09.26 пошта операторська
        # (див. tests/test_settings_routes.py). Замість нього — діагностика ваги
        # таблиці: обслуговування машини, лишилось адмінським.
        lambda request, db: settings_router_mod.settings_sheet_weight(request=request, db=db),
        lambda request, db: asyncio.run(settings_router_mod.create_operator(request=request, db=db)),
        lambda request, db: settings_router_mod.export_backup(
            request=request, backup_password="pw123456", backup_password_confirm="pw123456", db=db
        ),
    ],
)
def test_admin_only_routes_still_reject_operator(call):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {})
        with pytest.raises(HTTPException) as exc:
            call(request, db)
    assert exc.value.status_code == 403


def test_operator_cannot_import_backup():
    from fastapi import UploadFile
    import io

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {})
        upload = UploadFile(filename="backup.json", file=io.BytesIO(b"{}"))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                settings_router_mod.import_backup(
                    request=request,
                    backup_password="whatever",
                    confirm_replace="on",
                    backup_file=upload,
                    db=db,
                )
            )
    assert exc.value.status_code == 403


# --- HTMX save keeps the operator on the edited section -------------------
#
# The plain POST redirects to /settings?saved=1 — no #hash — which under the
# console layout lands on «Стан системи» instead of the form just saved. The
# HTMX branch must answer 204 (nothing to swap) plus a toast instead, and the
# non-HTMX branch must still redirect for the no-JS case.


def test_htmx_save_answers_204_with_toast_instead_of_redirect():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(
            operator.id,
            {"action": "save", "export_folder_path": r"C:\export"},
            headers={"HX-Request": "true"},
        )
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))

    assert response.status_code == 204
    assert "HX-Trigger" in response.headers
    payload = json.loads(response.headers["HX-Trigger"])
    assert payload["toast"]["kind"] == "success"
    # The value still had to be saved — 204 must not mean "did nothing".
    with Session(engine, expire_on_commit=False) as db:
        assert get_setting(db, "export_folder_path") == r"C:\export"


def test_plain_post_still_redirects_for_no_js():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {"action": "save", "export_folder_path": r"C:\export"})
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"


# --- K.4: секрети й керування людьми — лише за фізичним ПК -----------------


def test_post_settings_refuses_a_remote_request():
    """Ця форма пише СЕКРЕТИ (пароль IMAP, service-account JSON, шляхи до
    мережевих шар). Решта дій, що «керують самою машиною», давно під
    loopback-гейтом; ця лишалась без нього."""
    import asyncio

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(settings_router_mod.post_settings(
                request=_form_request(admin.id, {"action": "save"}, host="192.168.1.50"),
                db=db,
            ))
    assert exc.value.status_code == 403
    assert "цьому комп" in exc.value.detail


def test_post_settings_still_works_from_the_machine_itself():
    """Гейт не має ламати звичайне збереження."""
    import asyncio

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        response = asyncio.run(settings_router_mod.post_settings(
            request=_form_request(admin.id, {"action": "save"}), db=db
        ))
    assert response.status_code in (200, 204, 303)


@pytest.mark.parametrize("route", ["create_operator", "toggle_operator_active"])
def test_user_management_refuses_a_remote_request(route):
    """Створити оператора чи вимкнути його — дія над доступом до системи."""
    import asyncio

    from app.routers.settings import users as users_mod

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        request = _form_request(admin.id, {"username": "x", "password": "y"},
                                host="192.168.1.50")
        fn = getattr(users_mod, route)
        with pytest.raises(HTTPException) as exc:
            if route == "create_operator":
                asyncio.run(fn(request=request, db=db))
            else:
                asyncio.run(fn(request=request, user_id=admin.id, db=db))
    assert exc.value.status_code == 403
