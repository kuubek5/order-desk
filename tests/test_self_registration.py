"""Самореєстрація оператора зі сторінки входу (рішення власника 08.09.26).

Форма прихована за Alt+Enter і створює РОБОЧИЙ акаунт одразу, без схвалення.
Власникові показано, що комбінація клавіш не є захистом — хто її знає, той
зареєструється, а після виходу в мережу (ROADMAP хід 17) сторінку входу
бачитиме кожен у цеховій мережі. Він обрав зручність свідомо.

Тому тести стережуть НЕ «чи важко зареєструватись», а те, що від того рішення
не залежить: роль, обмежувач спроб, слід у журналі, і те, що першого
користувача так створити не можна.
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.auth import verify_password
from app.models import ActionLog, User
from app.routers.auth import register_submit
from app.services import attempt_limit
from app.services.operators import validate_self_registration


def _request():
    return SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"))


def _existing_admin(db):
    from app.auth import hash_password

    db.add(User(
        username="roma", full_name="Роман",
        password_hash=hash_password("long-password"),
        role="адмін", is_active=True,
    ))
    db.commit()


@pytest.fixture(autouse=True)
def _clean_limiter():
    from app.routers.auth import login_limiter

    login_limiter._attempts.clear() if hasattr(login_limiter, "_attempts") else None
    yield


def _register(db, **kw):
    payload = {
        "username": "ivan.petrenko",
        "full_name": "Іван Петренко",
        "password": "long-password",
        "password_confirmation": "long-password",
    }
    payload.update(kw)
    return asyncio.run(register_submit(request=_request(), db=db, **payload))


class TestRules:
    def test_role_is_always_operator(self, db_session):
        """Найважливіше. Адмінські дії — шляхи, паролі пристроїв, оновлення,
        копії — мусять лишитись за наявним адміном, хоч би хто зареєструвався."""
        _existing_admin(db_session)
        _register(db_session)

        user = db_session.scalar(select(User).where(User.username == "ivan.petrenko"))
        assert user is not None
        assert user.role == "оператор"
        assert user.is_active is True
        assert verify_password("long-password", user.password_hash)

    def test_taken_login_does_not_confirm_that_it_exists(self, db_session):
        """Сторінка входу відкрита всім, і підтверджувати чужі логіни їй нема
        чого: помилка та сама, що на будь-якому іншому непридатному логіні."""
        _existing_admin(db_session)
        values, error = validate_self_registration(
            db_session, "roma", "Хтось", "long-password", "long-password"
        )
        assert values is None
        assert "не підходить" in error
        assert "існує" not in error and "зайнят" not in error

    def test_taken_login_is_case_insensitive(self, db_session):
        _existing_admin(db_session)
        values, error = validate_self_registration(
            db_session, "ROMA", "Хтось", "long-password", "long-password"
        )
        assert values is None

    def test_short_password_is_refused(self, db_session):
        _existing_admin(db_session)
        values, error = validate_self_registration(
            db_session, "новий", "Новий", "short", "short"
        )
        assert values is None
        assert error is not None

    def test_mismatched_passwords_are_refused(self, db_session):
        _existing_admin(db_session)
        values, error = validate_self_registration(
            db_session, "новий", "Новий", "long-password", "інший-пароль"
        )
        assert values is None
        assert error == "Паролі не збігаються"

    def test_name_is_required(self, db_session):
        """Імʼя показується в історії дій — без нього «хто це зробив» стає
        анонімним."""
        _existing_admin(db_session)
        values, error = validate_self_registration(
            db_session, "новий", "", "long-password", "long-password"
        )
        assert values is None


class TestRoute:
    def test_first_user_cannot_be_created_here(self, db_session):
        """Поки в базі нуль, працює /setup, який робить АДМІНА. Реєстрація не
        має створювати лабораторію без господаря."""
        response = _register(db_session)

        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
        assert db_session.scalars(select(User)).all() == []

    def test_successful_registration_signs_the_person_in(self, db_session):
        """Власник просив саме так: заповнив і одразу працюєш."""
        _existing_admin(db_session)
        request = _request()
        response = asyncio.run(register_submit(
            request=request, db=db_session,
            username="ivan.petrenko", full_name="Іван Петренко",
            password="long-password", password_confirmation="long-password",
        ))

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        user = db_session.scalar(select(User).where(User.username == "ivan.petrenko"))
        assert request.session["user_id"] == user.id

    def test_registration_leaves_a_trace_in_the_journal(self, db_session):
        """Не гальмо, а журнал: власник має бачити, хто і коли завівся, не
        риючись у базі."""
        _existing_admin(db_session)
        _register(db_session)

        rows = db_session.scalars(select(ActionLog)).all()
        assert any("зареєструвався сам" in (r.note or "") for r in rows), (
            "самореєстрація не лишила сліду — «хто завівся» стане невідомим"
        )

    def test_a_bad_attempt_keeps_what_was_typed(self, db_session):
        """Помилка не має змушувати набирати все заново."""
        _existing_admin(db_session)
        response = asyncio.run(register_submit(
            request=_request(), db=db_session,
            username="ivan.petrenko", full_name="Іван Петренко",
            password="short", password_confirmation="short",
        ))

        assert response.status_code == 400
        ctx = response.context
        assert ctx["register_open"] is True
        assert ctx["register_username"] == "ivan.petrenko"
        assert ctx["register_full_name"] == "Іван Петренко"
        assert ctx["register_error"]


class TestFrontEnd:
    def test_login_page_has_no_registration_markup(self):
        """Головна властивість фічі. Форми немає у ВИХІДНОМУ HTML — тобто її
        немає ні для ока, ні для Tab, ні для читача екрана. Атрибут hidden дав
        би те саме лише для ока."""
        from pathlib import Path

        html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "login.html").read_text(encoding="utf-8")
        for marker in ("reg-panel", "reg-submit", "reg-lcd", 'name="password_confirmation"'):
            assert marker not in html, (
                f"розмітка реєстрації ({marker}) є в шаблоні — вона мусить "
                "будуватись у login.js, інакше «прихованість» лише косметична"
            )
        assert "reg-slot" in html, "порожній слот для реєстрації зник із шаблону"

    def test_the_shortcut_is_wired(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "login.js").read_text(encoding="utf-8")
        assert "altKey" in js and 'key === "Enter"' in js
        assert "reg-slot" in js
