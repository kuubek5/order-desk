"""Пауза синку тримає ВСІ записи, а не тринадцять із чотирнадцяти (S.3).

Гарантія з докстрінга `app/sync_control.py` звучить просто: на паузі система не
чіпає таблицю в ЖОДНОМУ напрямку. Насправді гейт стояв у роутах — по копії на
кожен, — і в одному з них (коментар до роботи) його не було зовсім: адмін ставив
паузу, щоб руками перебудувати вкладку, а коментар усе одно перезаписував живу
клітинку K посеред його правки (аудит 05.09.26, синк M-8).

Тепер перевірок дві, і вони про різне: роут відповідає операторові зрозумілим
повідомленням, а пул write-back не пускає до Google НІЧОГО. Друга — саме
страховка: новий роут, який забудуть загейтити, більше не проб'ється.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

import app.sync_control as sync_control
from app.db import Base
from app.models import Comment, Order, User
from app.routers import orders as orders_router_mod
from app.routers.settings import overview as settings_overview_mod
from app.services import sheet_writeback as writeback


@pytest.fixture(autouse=True)
def _resume_after_each_test():
    sync_control.resume()
    yield
    sync_control.resume()


def _user(db):
    user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(user)
    db.commit()
    return user


def _order(db):
    order = Order(source="lab", sheet_tab="26.08.26", row_number=7,
                  work_order_no="24122", status="прийнято")
    db.add(order)
    db.commit()
    return order


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers({}),
    )


class TestPoolRefusesWhilePaused:
    """Остання лінія: одна точка входу в пул, одна перевірка."""

    def test_the_job_never_runs(self):
        ran = []
        sync_control.pause()

        result = writeback.submit_sheet_write(lambda: ran.append(1)).result(timeout=5)

        assert ran == [], "на паузі задача не мала виконатись"
        assert result == writeback.SHEET_PAUSED_MESSAGE

    def test_the_job_runs_again_after_resume(self):
        ran = []
        sync_control.pause()
        writeback.submit_sheet_write(lambda: ran.append(1)).result(timeout=5)
        sync_control.resume()

        writeback.submit_sheet_write(lambda: ran.append(2)).result(timeout=5)

        assert ran == [2]

    def test_the_guard_keeps_the_function_name(self):
        """`wraps` тут не косметика: лог пише, ЯКИЙ саме запис пропущено."""
        def append_manual_rows_warm():
            return None

        guarded = writeback._guard_paused(append_manual_rows_warm)
        assert guarded.__name__ == "append_manual_rows_warm"


class TestCommentRouteAsksAboutPause:
    """Той самий роут, який єдиний не питав."""

    def test_comment_is_refused_and_nothing_is_written(self, db_session):
        user = _user(db_session)
        order = _order(db_session)
        sync_control.pause()

        import asyncio

        with patch.object(orders_router_mod, "append_comment_background") as background:
            asyncio.run(orders_router_mod.add_order_comment(
                request=_request(user.id), order_id=order.id, text="перевір край", db=db_session,
            ))

        background.assert_not_called()
        assert db_session.query(Comment).count() == 0

    def test_comment_goes_through_after_resume(self, db_session):
        import asyncio

        user = _user(db_session)
        order = _order(db_session)
        sync_control.resume()

        with patch.object(orders_router_mod, "append_comment_background") as background:
            asyncio.run(orders_router_mod.add_order_comment(
                request=_request(user.id), order_id=order.id, text="перевір край", db=db_session,
            ))

        background.assert_called_once()
        assert db_session.query(Comment).count() == 1


def _admin(db):
    user = User(username="admin", password_hash="x", full_name="Адмін", role="адмін")
    db.add(user)
    db.commit()
    return user


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


class TestSettingsSaveAndSyncAsksAboutPause:
    """F6 (аудит 06.09.26): «Зберегти й синхронізувати» на /settings ходило в
    Google в обхід паузи, тим самим шляхом, що й день-синк у Виробітку —
    `sync_google_sheets` не має викликатись, поки `sync_control.is_paused()`."""

    def test_paused_sync_is_refused_and_never_called(self):
        import asyncio

        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            admin = _admin(db)

            async def _form():
                return {"action": "save_and_sync"}

            request = SimpleNamespace(
                session={"user_id": admin.id},
                client=SimpleNamespace(host="127.0.0.1"),
                form=_form,
                headers=Headers({}),
            )
            sync_control.pause()

            def boom(*a, **kw):
                raise AssertionError("sync_google_sheets не має викликатись на паузі")

            with patch.object(settings_overview_mod, "sync_google_sheets", boom):
                response = asyncio.run(
                    settings_overview_mod.post_settings(request=request, db=db)
                )

            assert response.headers["location"] == "/"
            flash = request.session["sync_flash"]
            assert flash["kind"] == "error"
            assert "призупинено" in flash["message"]

    def test_resumed_sync_goes_through(self):
        import asyncio

        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            admin = _admin(db)

            async def _form():
                return {"action": "save_and_sync"}

            request = SimpleNamespace(
                session={"user_id": admin.id},
                client=SimpleNamespace(host="127.0.0.1"),
                form=_form,
                headers=Headers({}),
            )
            sync_control.resume()

            called = []
            fake_summary = SimpleNamespace(
                tabs_processed=0, created=0, updated=0, unchanged=0, deleted=0,
            )
            with patch.object(
                settings_overview_mod, "sync_google_sheets",
                lambda db: called.append(1) or fake_summary,
            ):
                asyncio.run(settings_overview_mod.post_settings(request=request, db=db))

            assert called == [1]
