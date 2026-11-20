"""Кнопку «Написати нам» можна вимкнути (власник 20.11.26).

Те саме правило, що в сторожа матеріалу й печі (`feature_access`), але кнопка
за замовчуванням УВІМКНЕНА: вона була в усіх і лишається, доки не вимкнуть.
"""

from __future__ import annotations

from app.models import Feedback, User
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура


def _users(factory) -> int:
    with factory() as db:
        return db.query(User).filter_by(username=OPERATOR[0]).one().id


def _feedback_count(factory) -> int:
    with factory() as db:
        return db.query(Feedback).count()


def test_beacon_is_on_by_default_and_the_operator_can_hide_it(app_db):  # noqa: F811
    app, factory = app_db
    operator = MiniClient(app)
    operator.login(*OPERATOR)
    _, _, page = operator.get("/account")
    assert 'id="fb-beacon"' in page and "Кнопка «Написати нам»" in page

    assert operator.post("/account/feedback-beacon", {"beacon": "off"})[0] == 204
    _, _, page = operator.get("/account")
    assert 'id="fb-beacon"' not in page
    # Вимкнена кнопка — звернення теж не приймається.
    operator.post("/feedback", {"kind": "bug", "text": "тест"})
    assert _feedback_count(factory) == 0

    assert operator.post("/account/feedback-beacon", {"beacon": "on"})[0] == 204
    _, _, page = operator.get("/account")
    assert 'id="fb-beacon"' in page
    assert operator.post("/account/feedback-beacon", {"beacon": "maybe"})[0] == 422


def test_admin_denies_or_forces_the_beacon(app_db):  # noqa: F811
    app, factory = app_db
    operator_id = _users(factory)
    admin = MiniClient(app)
    admin.login(*ADMIN)
    url = f"/settings/users/{operator_id}/feature-access"
    _, _, page = admin.get("/settings")
    assert 'name="feature" value="feedback"' in page

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert admin.post(url, {"feature": "feedback", "access": "deny"})[0] == 303
    _, _, page = operator.get("/account")
    assert 'id="fb-beacon"' not in page and "Адміністратор вимкнув для вас цю кнопку" in page
    assert operator.post("/account/feedback-beacon", {"beacon": "on"})[0] == 403
    operator.post("/feedback", {"kind": "bug", "text": "тест"})
    assert _feedback_count(factory) == 0

    assert admin.post(url, {"feature": "feedback", "access": "force"})[0] == 303
    with factory() as db:
        db.get(User, operator_id).feedback_beacon = False
        db.commit()
    _, _, page = operator.get("/account")
    assert 'id="fb-beacon"' in page, "«завжди» сильніше за власний перемикач"
    assert operator.post("/account/feedback-beacon", {"beacon": "off"})[0] == 403

    # Інші функції від цього не змінились.
    with factory() as db:
        user = db.get(User, operator_id)
        assert user.material_guard_access == "" and user.furnace_power_access == ""
