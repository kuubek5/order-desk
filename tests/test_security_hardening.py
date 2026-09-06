"""Сторожі базових налаштувань безпеки (аудит 05.09.26).

Кожна з цих речей — один рядок конфігурації, який легко зняти випадково і
неможливо помітити оком: кука без `SameSite=Strict` мовчки відкриває всі
HTMX-мутації будь-якій сторінці в браузері оператора, а відкритий
`/openapi.json` віддає карту роутів без входу.
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

import app.web as web
from app.services.attempt_limit import AttemptLimiter, block_message
from app.services.operators import (
    PASSWORD_MIN_LENGTH,
    validate_password,
    validate_role,
)


def _session_middleware_options() -> dict:
    for middleware in web.app.user_middleware:
        if middleware.cls is SessionMiddleware:
            return dict(middleware.kwargs)
    raise AssertionError("SessionMiddleware не підключено")


class TestSessionCookie:
    """M-4: CSRF-захисту як окремого механізму нема — усе тримається на
    `same_site="strict"`. Тест не дає зняти його непомітно."""

    def test_same_site_is_strict(self):
        assert _session_middleware_options()["same_site"] == "strict"

    def test_https_only_is_false_on_purpose(self):
        # Слухач лише на 127.0.0.1 по HTTP; https_only=True зробив би куку
        # такою, що ніколи не надсилається. Якщо колись зʼявиться TLS —
        # міняти свідомо, разом з цим тестом.
        assert _session_middleware_options()["https_only"] is False

    def test_cookie_lifetime_is_a_work_shift(self):
        assert _session_middleware_options()["max_age"] == 8 * 60 * 60


class TestSchemaClosed:
    """M-5: застосунок не має API-клієнтів, схема нікому не потрібна."""

    def test_docs_and_openapi_are_disabled(self):
        assert web.app.docs_url is None
        assert web.app.redoc_url is None
        assert web.app.openapi_url is None

    def test_no_schema_route_is_registered(self):
        paths = {getattr(route, "path", "") for route in web.app.routes}
        assert not paths & {"/docs", "/redoc", "/openapi.json"}


class TestPasswordPolicy:
    """M-3: до аудиту було три різні політики (10 / 6 / жодної)."""

    def test_short_password_rejected_with_one_message(self):
        message = validate_password("1")
        assert message and str(PASSWORD_MIN_LENGTH) in message

    def test_long_enough_password_accepted(self):
        assert validate_password("x" * PASSWORD_MIN_LENGTH) is None

    def test_role_must_be_a_known_one(self):
        assert validate_role("оператор") is None
        assert validate_role("адмін") is None
        # Латинська «a» в «aдмін»: акаунт виглядав би адміном у списку, але
        # жоден гейт його не пускав би.
        assert validate_role("aдмін") is not None
        assert validate_role("") is not None


class TestAttemptLimiter:
    """M-2/M-6: перебір ПІНа й пароля. Лічильник у памʼяті, скидається сам —
    оператор не має лишитись замкненим після кількох одруківок."""

    def test_free_attempts_are_not_blocked(self):
        limiter = AttemptLimiter(free_attempts=3, block_seconds=60)
        for _ in range(3):
            assert limiter.register_failure("k") == 0
            assert limiter.retry_after("k") == 0

    def test_block_kicks_in_after_the_free_attempts(self):
        limiter = AttemptLimiter(free_attempts=3, block_seconds=60)
        for _ in range(3):
            limiter.register_failure("k")
        assert limiter.register_failure("k") == 60
        assert 0 < limiter.retry_after("k") <= 61

    def test_success_clears_the_counter(self):
        limiter = AttemptLimiter(free_attempts=1, block_seconds=60)
        limiter.register_failure("k")
        limiter.reset("k")
        assert limiter.retry_after("k") == 0
        assert limiter.register_failure("k") == 0

    def test_keys_do_not_leak_into_each_other(self):
        limiter = AttemptLimiter(free_attempts=1, block_seconds=60)
        limiter.register_failure("a")
        limiter.register_failure("a")
        assert limiter.retry_after("a") > 0
        assert limiter.retry_after("b") == 0

    def test_block_expires_on_its_own(self):
        limiter = AttemptLimiter(free_attempts=1, block_seconds=0.01)
        limiter.register_failure("k")
        limiter.register_failure("k")
        import time

        time.sleep(0.05)
        assert limiter.retry_after("k") == 0

    def test_block_message_is_in_minutes(self):
        assert "5 хв" in block_message(300)


class TestCheckPathWriteProbe:
    """M-1: створення й видалення файлу-маркера в будь-якій вказаній теці —
    це write-anywhere оракул по всіх шарах, до яких дотягується служба."""

    def test_operator_probe_never_writes(self, tmp_path, monkeypatch):
        from app.routers.settings import check_path_status

        result = check_path_status(str(tmp_path), write_probe=False)
        assert result["state"] == "success"
        assert list(tmp_path.iterdir()) == []

    def test_admin_probe_still_verifies_writability(self, tmp_path):
        from app.routers.settings import check_path_status

        result = check_path_status(str(tmp_path))
        assert result["state"] == "success"
        assert "запис" in result["message"]
        # Маркер прибрано за собою.
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("write_probe", [True, False])
    def test_missing_path_reported_either_way(self, tmp_path, write_probe):
        from app.routers.settings import check_path_status

        result = check_path_status(str(tmp_path / "nope"), write_probe=write_probe)
        assert result["state"] == "error"


class TestLoginRateLimit:
    """M-6: `POST /login` не мав ані лічильника, ані паузи. Реальний сценарій —
    не мережа (слухач на loopback), а хвилина біля покинутого ПК."""

    def _db(self):
        from app.db import Base

        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        return engine

    def _request(self):
        return SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"))

    def _login(self, db, username, password):
        from app.routers import auth as auth_router

        return asyncio.run(
            auth_router.login_submit(
                request=self._request(), username=username, password=password, db=db
            )
        )

    @pytest.fixture(autouse=True)
    def _clean_limiter(self):
        from app.services.attempt_limit import login_limiter

        login_limiter.clear()
        yield
        login_limiter.clear()

    def _seed(self, db):
        from app.auth import hash_password
        from app.models import User

        db.add(
            User(
                username="roma",
                password_hash=hash_password("parol-na-desyat"),
                full_name="Рома",
                role="адмін",
            )
        )
        db.commit()

    def test_repeated_failures_end_in_a_pause(self, monkeypatch):
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context, status_code=200: SimpleNamespace(
                context=context, status_code=status_code
            ),
        )
        engine = self._db()
        with Session(engine, expire_on_commit=False) as db:
            self._seed(db)
            for _ in range(6):
                self._login(db, "roma", "wrong-password")
            blocked = self._login(db, "roma", "wrong-password")

        assert blocked.status_code == 429
        assert "Забагато невдалих спроб" in blocked.context["error"]

    def test_correct_password_still_works_and_clears_the_counter(self, monkeypatch):
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context, status_code=200: SimpleNamespace(
                context=context, status_code=status_code
            ),
        )
        engine = self._db()
        with Session(engine, expire_on_commit=False) as db:
            self._seed(db)
            self._login(db, "roma", "wrong-password")
            response = self._login(db, "roma", "parol-na-desyat")
            assert response.status_code == 303

            # Лічильник скинуто: наступні одруківки знову мають вільні спроби.
            for _ in range(3):
                assert self._login(db, "roma", "wrong-password").status_code == 200
