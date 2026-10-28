"""Літери операторів — усі разом, щоб їх можна було поміняти місцями (28.10.26).

Скарга власника: у одного оператора «Р», у другого «РР», треба поміняти —
а поодинці кожен перший крок каже «буква зайнята». Спільна форма
`/settings/users/initials` звіряє унікальність з КІНЦЕВИМ станом.
"""

from __future__ import annotations

import pytest

from app.auth import hash_password
from app.models import User
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _seed(session_factory) -> dict[str, int]:
    with session_factory() as db:
        admin = db.query(User).filter_by(username=ADMIN[0]).one()
        admin.sheet_initial = "РР"
        roma = User(username="roma", password_hash=hash_password("x" * 12), role="оператор",
                    full_name="Роман", sheet_initial="Р")
        old = User(username="old", password_hash=hash_password("x" * 12), role="оператор",
                   full_name="Колишній", sheet_initial="К", is_active=False)
        db.add_all([roma, old])
        db.commit()
        return {"admin": admin.id, "roma": roma.id, "old": old.id}


def _letters(session_factory) -> dict[str, str | None]:
    with session_factory() as db:
        return {u.username: u.sheet_initial for u in db.query(User)}


def test_swap_in_one_save(app_db):  # noqa: F811
    app, sf = app_db
    ids = _seed(sf)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, headers, _ = client.post("/settings/users/initials", {
        f"initial_{ids['admin']}": "р", f"initial_{ids['roma']}": "рр",
    })
    assert status == 303 and "saved=1" in headers["location"], headers
    letters = _letters(sf)
    assert letters[ADMIN[0]] == "Р" and letters["roma"] == "РР"
    assert letters["old"] == "К", "оператор без поля у формі лишається як був"


def test_one_by_one_swap_is_still_refused_with_a_hint(app_db):  # noqa: F811
    """Старий шлях по одній літері не послаблено — лише підказує, як зробити."""
    app, sf = app_db
    ids = _seed(sf)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, headers, _ = client.post(f"/settings/users/{ids['roma']}/initial", {"sheet_initial": "РР"})
    assert status == 303 and "error=" in headers["location"]
    assert _letters(sf)["roma"] == "Р"


def test_duplicate_in_final_state_changes_nothing(app_db):  # noqa: F811
    app, sf = app_db
    ids = _seed(sf)
    client = MiniClient(app)
    client.login(*ADMIN)
    before = _letters(sf)
    # Вимкнений «Колишній» досі тримає «К» — віддати її іншому не можна.
    status, headers, _ = client.post("/settings/users/initials", {
        f"initial_{ids['admin']}": "К", f"initial_{ids['roma']}": "Р",
    })
    assert status == 303 and "error=" in headers["location"], headers
    assert _letters(sf) == before, "помилка — обмін або цілий, або ніякий"


def test_bad_letter_and_clearing(app_db):  # noqa: F811
    app, sf = app_db
    ids = _seed(sf)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, headers, _ = client.post("/settings/users/initials", {f"initial_{ids['roma']}": "Р1"})
    assert "error=" in headers["location"]
    status, headers, _ = client.post("/settings/users/initials", {f"initial_{ids['roma']}": ""})
    assert "saved=1" in headers["location"]
    assert _letters(sf)["roma"] is None


def test_operator_cannot_save_letters(app_db):  # noqa: F811
    app, sf = app_db
    ids = _seed(sf)
    with sf() as db:
        db.query(User).filter_by(username="roma").one().password_hash = hash_password("roma-pass-12345")
        db.commit()
    client = MiniClient(app)
    client.login("roma", "roma-pass-12345")
    status, _, _ = client.post("/settings/users/initials", {f"initial_{ids['roma']}": "Ж"})
    assert status == 403
    assert _letters(sf)["roma"] == "Р"


def test_settings_page_has_one_letters_form(app_db):  # noqa: F811
    app, sf = app_db
    ids = _seed(sf)
    client = MiniClient(app)
    client.login(*ADMIN)
    html = client.get("/settings")[2]
    assert 'id="ops-letters"' in html and 'action="/settings/users/initials"' in html
    assert f'name="initial_{ids["roma"]}" form="ops-letters"' in html
    assert "/settings/users/" + str(ids["roma"]) + "/initial" not in html
