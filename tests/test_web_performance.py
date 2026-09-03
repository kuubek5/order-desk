import asyncio
import inspect
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import app.web as web
from app import perf
from app.routers import settings as settings_router_mod
from app.routers import queue as queue_router_mod
from app.routers import handout as handout_router_mod
from app.routers import stats as stats_router_mod
from app.db import Base
from app.models import Order, StatusEvent, User


def test_blocking_get_handlers_are_sync_for_fastapi_threadpool():
    assert not inspect.iscoroutinefunction(queue_router_mod.get_queue)
    assert not inspect.iscoroutinefunction(stats_router_mod.get_stats)
    assert not inspect.iscoroutinefunction(settings_router_mod.get_settings)
    assert not inspect.iscoroutinefunction(handout_router_mod.get_handout)


def test_stats_eager_loads_status_events_in_constant_query_count(monkeypatch):
    test_engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(test_engine)

    with Session(test_engine, expire_on_commit=False) as db:
        user = User(
            username="admin",
            password_hash="unused",
            full_name="Admin",
            role="адмін",
        )
        db.add(user)
        db.flush()
        now = datetime.now()
        for index in range(20):
            order = Order(source="sheet", sheet_tab=now.strftime("%d.%m.%y"))
            db.add(order)
            db.flush()
            db.add_all(
                [
                    StatusEvent(order_id=order.id, status="нове", occurred_at=now),
                    StatusEvent(
                        order_id=order.id,
                        status="відфрезеровано",
                        occurred_at=now + timedelta(hours=index + 1),
                    ),
                ]
            )
        db.commit()
        user_id = user.id
        db.expunge_all()

        queries = 0

        def count_query(*_args):
            nonlocal queries
            queries += 1

        event.listen(test_engine, "before_cursor_execute", count_query)
        monkeypatch.setattr(
            web.templates,
            "TemplateResponse",
            lambda request, template, context: context,
        )
        request = SimpleNamespace(session={"user_id": user_id})

        context = stats_router_mod.get_stats(request=request, period="all", db=db)

        event.remove(test_engine, "before_cursor_execute", count_query)
        assert context["avg_hours"] is not None
        # User, orders, select-in status events, and rework records.
        assert queries <= 4


def test_request_timing_logs_only_slow_requests(monkeypatch, caplog):
    """Гучний рядок лише для затримок, які помітно людині.

    Годинник тут — `perf_counter` через `app.perf`: та сама лінійка, що й у
    фаз розкладки. Раніше middleware мав власний `monotonic`, і сума фаз
    мірялась іншим годинником, ніж загальний час.
    """
    request = Request({"type": "http", "method": "GET", "path": "/stats", "headers": []})

    async def response(_request):
        return object()

    caplog.set_level(logging.WARNING, logger=web.__name__)

    def fake_clock(values):
        it = iter(values)
        return lambda: next(it)

    # 10.0 — старт запиту, 10.5 — його кінець: пів секунди, мовчимо.
    monkeypatch.setattr(perf.time, "perf_counter", fake_clock((10.0, 10.5)))
    asyncio.run(web.log_slow_requests(request, response))
    assert not caplog.records

    # 20.0 → 21.2 — 1.2 с, це вже видно оператору.
    monkeypatch.setattr(perf.time, "perf_counter", fake_clock((20.0, 21.2)))
    asyncio.run(web.log_slow_requests(request, response))
    assert len(caplog.records) == 1
    assert "GET /stats took 1.200s" in caplog.records[0].message


def test_request_timing_records_a_sample_with_phases():
    """Кожен запит лишає пробу з розкладкою — на цьому тримається /diag/perf."""
    perf.clear()
    request = Request({"type": "http", "method": "GET", "path": "/stats",
                       "query_string": b"", "headers": []})

    async def response(_request):
        with perf.span("sql"):
            pass
        perf.note_rows(7)
        return object()

    asyncio.run(web.log_slow_requests(request, response))
    samples = perf.samples()
    assert len(samples) == 1
    assert samples[0].path == "/stats"
    assert samples[0].phases.get("rows") == 7.0
    perf.clear()


def test_diag_and_static_paths_are_not_sampled():
    """Діагностичний екран не міряє сам себе — інакше витіснив би корисні проби."""
    perf.clear()
    for path in ("/diag/perf", "/static/js/app.js"):
        request = Request({"type": "http", "method": "GET", "path": path,
                           "query_string": b"", "headers": []})

        async def response(_request):
            return object()

        asyncio.run(web.log_slow_requests(request, response))
    assert perf.samples() == []
