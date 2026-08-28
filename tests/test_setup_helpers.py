import asyncio
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import verify_password
from app.db import Base
from app.models import User
from app.routers.auth import setup_submit
from app.services.operators import validate_first_admin as _validate_first_admin


def test_first_admin_validation_normalizes_values():
    values, error = _validate_first_admin(
        "  roma  ", "  Роман  ", "long-password", "long-password"
    )

    assert error is None
    assert values == {
        "username": "roma",
        "full_name": "Роман",
        "password": "long-password",
    }


def test_first_admin_requires_username_and_name():
    values, error = _validate_first_admin("", "Роман", "long-password", "long-password")

    assert values is None
    assert error == "Вкажіть логін та ім’я адміністратора"


def test_first_admin_requires_ten_character_password():
    values, error = _validate_first_admin("roma", "Роман", "short", "short")

    assert values is None
    assert error == "Пароль має містити щонайменше 10 символів"


def test_first_admin_requires_matching_passwords():
    values, error = _validate_first_admin(
        "roma", "Роман", "long-password", "other-password"
    )

    assert values is None
    assert error == "Паролі не збігаються"


def test_setup_submit_creates_only_one_active_admin():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    request = SimpleNamespace(session={})

    with Session(engine, expire_on_commit=False) as db:
        response = asyncio.run(
            setup_submit(
                request=request,
                username="roma",
                full_name="Роман",
                password="long-password",
                password_confirmation="long-password",
                db=db,
            )
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings?welcome=1"

        user = db.scalar(select(User))
        assert user is not None
        assert user.role == "адмін"
        assert user.is_active is True
        assert verify_password("long-password", user.password_hash)
        assert request.session["user_id"] == user.id

        second = asyncio.run(
            setup_submit(
                request=SimpleNamespace(session={}),
                username="second",
                full_name="Другий",
                password="other-password",
                password_confirmation="other-password",
                db=db,
            )
        )
        assert second.status_code == 303
        assert second.headers["location"] == "/login"
        assert len(db.scalars(select(User)).all()) == 1
