"""Робота без свого рядка в таблиці: дії людини з банера й паспорта (22.10.26).

Привід — #6472: рядок поштової роботи свідомо видалили, CRM правильно не
фарбувала чужий рядок, але повторювала заливку вічно, і вирішити це в CRM
було нічим. Дві дії, обидві в «Журналі дій» і зі «Крок назад» (←):
«Рядок видалено свідомо — прибрати з CRM» (архів БЕЗ стирання рядка) і
«Забути про заливку» / «Не дописувати рядок». Справжні роути й рендер через
MiniClient; імена вигадані.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import ActionLog, Order, StatusEvent
from app.services import sheet_stuck_writes as stuck
from app.services import undo as undo_service
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

ROW_LOST = "рядок у таблиці не підтверджено — не записано"


@pytest.fixture(autouse=True)
def _clean_state():
    stuck.reset()
    yield
    stuck.reset()


def _client(app):
    client = MiniClient(app)
    client.login(*OPERATOR)
    return client


def _mail_order(db, **fields) -> Order:
    base = dict(
        source="email", sheet_tab="20.10.26", row_number=74, client_name="Клієнт-А",
        material_color="mono a2", quantity="2", status="знайдено при видачі",
        fill_pending="clear",
    )
    base.update(fields)
    order = Order(**base)
    db.add(order)
    db.commit()
    return order


def _toast(headers) -> dict:
    return json.loads(headers["hx-trigger"])["toast"]


def test_banner_offers_both_actions_only_when_the_row_is_lost(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        lost = _mail_order(db)
        flaky = _mail_order(db, client_name="Клієнт-Б", row_number=75)
        stuck.note_write_failed("fill", lost, ROW_LOST)
        stuck.note_write_failed("fill", flaky, "timeout")

    _, _, html = _client(app).get("/sheets/write-failures")

    assert f'hx-post="/sheets/write-failures/{lost.id}/archive"' in html
    assert f'hx-post="/sheets/write-failures/{lost.id}/forget"' in html
    assert "Рядок видалено свідомо — прибрати з CRM" in html
    assert "Забути про заливку" in html
    # Мережа впала — повтор допоможе, людині тут вирішувати нічого.
    assert f"/sheets/write-failures/{flaky.id}/" not in html
    # «Записати зараз» лишився на місці.
    assert 'hx-post="/sheets/write-failures/retry"' in html


def test_work_name_opens_the_passport(app_db):  # noqa: F811
    """Імʼя роботи в банері — паспорт (у черзі — бічна панель). Робота
    «знайдено при видачі» в черзі не стоїть, і перехід із фокусом не вів
    нікуди: до «Видалити» власник дістатись не міг (#6472)."""
    import re

    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db)
        stuck.note_write_failed("fill", order, ROW_LOST)

    client = _client(app)
    # Видача тягне той самий фрагмент поллом (/sheets/write-failures).
    for path in ("/sheets/write-failures", "/"):
        status, _, html = client.get(path)
        assert status == 200, path
        link = re.search(r'<a class="swf-link"[^>]*>', html)
        assert link, path
        assert f'href="/orders/{order.id}"' in link.group(0)
        assert f'data-order-detail="{order.id}"' in link.group(0)
        # Перехід до рядка черги лишився — окремим малим посиланням.
        assert "swf-queue-link" in html and f"focus={order.id}" in html


def test_archive_once_per_work_and_no_forget_for_sum3d(app_db):  # noqa: F811
    """Sum3D і заливка однієї роботи — два записи банера, але «прибрати» —
    один. «Забути» для Sum3D немає: без позначки синк стер би Sum3D."""
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db, sum3d_id="15-35-56", sum3d_pending="15-35-56")
        stuck.note_write_failed("sum3d", order, ROW_LOST)
        stuck.note_write_failed("fill", order, ROW_LOST)

    _, _, html = _client(app).get("/sheets/write-failures")

    assert html.count(f"/sheets/write-failures/{order.id}/archive") == 1
    assert html.count(f"/sheets/write-failures/{order.id}/forget") == 1
    assert '"kind": "fill"' in html and '"kind": "sum3d"' not in html


def test_archive_keeps_the_sheet_untouched_and_undo_returns_the_work(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db)
        order_id = order.id
        stuck.note_write_failed("fill", order, ROW_LOST)

    client = _client(app)
    with patch("app.services.sheet_writeback.clear_sheet_row_background") as clear, \
         patch.object(undo_service, "clear_sheet_row_background") as clear_undo, \
         patch.object(undo_service, "restore_sheet_row") as restore:
        status, headers, body = client.post(f"/sheets/write-failures/{order_id}/archive")
        assert status == 200
        toast = _toast(headers)
        assert toast["kind"] == "success" and "Крок назад" in toast["message"]
        assert json.loads(headers["hx-trigger"])["refresh-queue"] is True
        assert "swf-banner" not in body, "банер у відповіді вже без цієї роботи"

        with session_factory() as db:
            order = db.get(Order, order_id)
            assert order.archived_at is not None
            assert order.row_number == 74, "позицію не чіпали — стан той самий, що був"
            entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "delete"))
            assert entry.new_value == undo_service.ROWLESS_ARCHIVE_MARK
            notes = [e.note for e in db.scalars(select(StatusEvent))]
            assert any("рядка в таблиці немає" in (n or "") for n in notes)

        status, _, _ = client.post("/actions/undo-last")
        assert status in (200, 204)
        with session_factory() as db:
            assert db.get(Order, order_id).archived_at is None
        status, _, _ = client.post("/actions/redo-last")
        with session_factory() as db:
            assert db.get(Order, order_id).archived_at is not None

    clear.assert_not_called()
    clear_undo.assert_not_called()
    restore.assert_not_called()


def test_archive_of_a_sheet_client_work_never_blanks_a_row(app_db):  # noqa: F811
    """Звичайне видалення стирає рядок вписаного клієнта, а «прибрати» —
    ні: рядка вже немає, і стирання знайшло б хіба чужого двійника."""
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db, source="sheet_client")
        stuck.note_write_failed("fill", order, ROW_LOST)

    client = _client(app)
    with patch.object(undo_service, "clear_sheet_row_background") as clear_undo, \
         patch.object(undo_service, "restore_sheet_row") as restore:
        client.post(f"/sheets/write-failures/{order.id}/archive")
        client.post("/actions/undo-last")
        client.post("/actions/redo-last")
    restore.assert_not_called()
    clear_undo.assert_not_called()


def test_merged_work_says_it_goes_whole(app_db):  # noqa: F811
    from app.models import EmailMessage, OrderEmail

    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db)
        for i in (1, 2):
            email = EmailMessage(uid=f"u{i}", subject="s", status="прийнято")
            db.add(email)
            db.flush()
            db.add(OrderEmail(order_id=order.id, email_message_id=email.id, quantity=1))
        db.commit()
        stuck.note_write_failed("fill", order, ROW_LOST)

    client = _client(app)
    _, _, html = client.get("/sheets/write-failures")
    assert "зведена робота з 2 листів" in html.lower()
    _, headers, _ = client.post(f"/sheets/write-failures/{order.id}/archive")
    assert "зведена робота — 2 листів" in _toast(headers)["message"]


def test_forget_fill_drops_the_mark_and_undo_puts_it_back(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db)
        stuck.note_write_failed("fill", order, ROW_LOST)

    client = _client(app)
    status, headers, body = client.post(
        f"/sheets/write-failures/{order.id}/forget", {"kind": "fill"}
    )
    assert status == 200 and _toast(headers)["kind"] == "success"
    assert "swf-banner" not in body
    with session_factory() as db:
        fresh = db.get(Order, order.id)
        assert fresh.fill_pending is None and fresh.archived_at is None
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "forget_pending"))
        assert entry.field == "fill_pending" and entry.old_value == "clear"

    client.post("/actions/undo-last")
    with session_factory() as db:
        assert db.get(Order, order.id).fill_pending == "clear"
    client.post("/actions/redo-last")
    with session_factory() as db:
        assert db.get(Order, order.id).fill_pending is None


def test_forget_mail_row_round_trips_the_timestamp(app_db):  # noqa: F811
    app, session_factory = app_db
    stamp = datetime(2026, 10, 20, 16, 5, 7)
    with session_factory() as db:
        order = _mail_order(db, row_number=None, fill_pending=None, sheet_row_pending=stamp)
        stuck.note_write_failed("row", order, ROW_LOST)

    client = _client(app)
    _, _, html = client.get("/sheets/write-failures")
    assert "Не дописувати рядок" in html
    client.post(f"/sheets/write-failures/{order.id}/forget", {"kind": "row"})
    with session_factory() as db:
        assert db.get(Order, order.id).sheet_row_pending is None
    client.post("/actions/undo-last")
    with session_factory() as db:
        assert db.get(Order, order.id).sheet_row_pending == stamp


def test_unknown_kind_is_refused(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db, sum3d_id="1", sum3d_pending="1")
    status, _, _ = _client(app).post(
        f"/sheets/write-failures/{order.id}/forget", {"kind": "sum3d"}
    )
    assert status == 400
    with session_factory() as db:
        assert db.get(Order, order.id).sum3d_pending == "1"


def test_pause_refuses_both_actions(app_db, monkeypatch):  # noqa: F811
    from app import sync_control

    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db)
    monkeypatch.setattr(sync_control, "is_paused", lambda: True)
    client = _client(app)
    _, headers, _ = client.post(f"/sheets/write-failures/{order.id}/archive")
    assert "паузі" in _toast(headers)["message"]
    _, headers, _ = client.post(f"/sheets/write-failures/{order.id}/forget", {"kind": "fill"})
    assert "паузі" in _toast(headers)["message"]
    with session_factory() as db:
        fresh = db.get(Order, order.id)
        assert fresh.archived_at is None and fresh.fill_pending == "clear"
        assert db.scalar(select(ActionLog)) is None


def test_anonymous_gets_nothing(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db)
    status, _, _ = MiniClient(app).post(f"/sheets/write-failures/{order.id}/archive")
    assert status == 401
    with session_factory() as db:
        assert db.get(Order, order.id).archived_at is None


def test_passport_shows_the_card_with_actions(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db)
        other = _mail_order(db, client_name="Клієнт-Б", row_number=80)
        stuck.note_write_failed("fill", order, ROW_LOST)

    client = _client(app)
    status, _, html = client.get(f"/orders/{order.id}")
    assert status == 200, html[:300]
    assert "p2-sheetrow" in html and "Рядок видалено свідомо — прибрати з CRM" in html
    assert '"origin": "passport"' in html
    _, headers, _ = client.post(
        f"/sheets/write-failures/{order.id}/archive", {"origin": "passport"}
    )
    assert _toast(headers)["kind"] == "success"
    # Паспорт без збою — без картки.
    _, _, html = client.get(f"/orders/{other.id}")
    assert "p2-sheetrow" not in html


def test_plain_delete_of_a_mail_work_is_undoable_again(app_db):  # noqa: F811
    """Звичайне «Видалити з черги» для пошти рядок не стирає, а скасування
    доти відновлювало рядок, бачило його зайнятим САМОЮ роботою і відмовляло —
    поштова робота з видалення не поверталась ніколи."""
    app, session_factory = app_db
    with session_factory() as db:
        order = _mail_order(db, fill_pending=None)

    client = _client(app)
    with patch.object(undo_service, "restore_sheet_row", return_value="рядок 80 уже зайнято") as restore:
        client.post(f"/orders/{order.id}/delete", {"inline": "1"}, headers={"HX-Request": "true"})
        with session_factory() as db:
            assert db.get(Order, order.id).archived_at is not None
        client.post("/actions/undo-last")
    with session_factory() as db:
        assert db.get(Order, order.id).archived_at is None
    restore.assert_not_called()
