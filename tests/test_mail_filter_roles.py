"""F3 (аудит 06.09.26): оператор редагує фільтри пошти нарівні з адміном.

Реєстр `app/services/settings_nav.py` оголошує `mail-filters` з
`edit_roles=None` — рішення власника 06.09.26: розділ «Джерела робіт»
(і фільтри в ньому) редагує оператор, як і адмін. Вісім POST-роутів у
`app/routers/mail.py` досі мали жорсткий `user.role != "адмін"` (частина —
взагалі без перевірки, як `dismiss-suggest`); тепер усі звіряються з
`can_edit(user, "mail-filters")`. Тут — той самий сценарій, що й
`tests/test_mail_filters.py::test_create_rule_allows_operator`, але для решти
семи роутів разом, одним файлом на назву фічі (F3).
"""

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from app.db import Base
from app.models import MailFilterCategory, MailFilterRule, User
from app.routers import mail as mail_router_mod


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session, role: str = "оператор") -> User:
    user = User(username="op", password_hash="x", full_name="Op", role=role)
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None):
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


def test_operator_can_create_toggle_and_delete_a_rule():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _user(db)

        create_resp = mail_router_mod.create_mail_filter(
            request=_request(operator.id), kind="keyword",
            pattern="реклама", category="спам", db=db,
        )
        assert create_resp.status_code == 303
        rule = db.scalar(select(MailFilterRule))
        assert rule is not None and rule.pattern == "реклама"

        toggle_resp = mail_router_mod.toggle_mail_filter(
            request=_request(operator.id), rule_id=rule.id, db=db,
        )
        assert toggle_resp.status_code == 303
        db.refresh(rule)
        assert rule.enabled is False

        delete_resp = mail_router_mod.delete_mail_filter(
            request=_request(operator.id), rule_id=rule.id, db=db,
        )
        assert delete_resp.status_code == 303
        assert db.scalars(select(MailFilterRule)).all() == []


def test_operator_can_edit_a_rule():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _user(db)
        rule = MailFilterRule(kind="keyword", pattern="друк", category="3D-друк")
        db.add(rule)
        db.commit()

        response = mail_router_mod.edit_mail_filter(
            request=_request(operator.id), rule_id=rule.id,
            kind="keyword", pattern="друк3д", category="3D-друк", db=db,
        )
        assert response.status_code == 303
        db.refresh(rule)
        assert rule.pattern == "друк3д"


def test_operator_can_manage_filter_categories():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _user(db)

        create_resp = mail_router_mod.create_filter_category(
            request=_request(operator.id), name="друкарня", db=db,
        )
        assert create_resp.status_code == 303
        cat = db.scalar(select(MailFilterCategory).where(MailFilterCategory.name == "друкарня"))
        assert cat is not None

        rename_resp = mail_router_mod.rename_filter_category(
            request=_request(operator.id), category_id=cat.id, name="3D-центр", db=db,
        )
        assert rename_resp.status_code == 303
        db.refresh(cat)
        assert cat.name == "3D-центр"

        delete_resp = mail_router_mod.delete_filter_category(
            request=_request(operator.id), category_id=cat.id, db=db,
        )
        assert delete_resp.status_code == 303
        assert db.get(MailFilterCategory, cat.id) is None


def test_operator_can_dismiss_suggest():
    """`dismiss-suggest` не мав ЖОДНОЇ перевірки ролі — F3 додає той самий
    `can_edit` гейт, що й решта семи роутів, а не робить його адмінським."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _user(db)

        response = mail_router_mod.dismiss_filter_suggest(
            request=_request(operator.id), address="spam@x.com", db=db,
        )
        assert response.status_code == 303
        rule = db.scalar(select(MailFilterRule))
        assert rule is not None
        assert rule.pattern == "spam@x.com" and rule.enabled is False


def test_anonymous_request_is_redirected_to_login_not_403():
    """Без сесії — редірект на вхід, як і завжди; не плутати з 403 «немає
    прав», який тепер стається лише для ролей поза реєстром (сьогодні таких
    нема — адмін і оператор обидва можуть)."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        response = mail_router_mod.create_mail_filter(
            request=_request(None), kind="keyword",
            pattern="x", category="y", db=db,
        )
        assert response.status_code in (302, 303)
        assert db.scalars(select(MailFilterRule)).all() == []
