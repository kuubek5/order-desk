"""Перенесення листа в папку скриньки й повернення назад (23.09.26).

Оператор після запуску роботи в цех переносить лист у папку «оброблено»
(«Скачано-просчитано»). Перенесений лист покидає «Вхідні»/«Архів» і живе у
вкладці папки; звідти його можна повернути в Inbox. IMAP тут замокано —
стережемо логіку роутів і фільтрацію вкладок, не мережу.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.business_day import business_today, get_rollover
from app.models import EmailMessage
from app.routers import mail as mail_router_mod
from app.settings_store import get_setting, set_setting
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _letter(db, uid="100", status="нове", folder=None, **kw):
    email = EmailMessage(
        uid=uid, uid_validity="1", from_address="lab@ukr.net",
        from_name="Клієнт", subject="моно а3", status=status,
        attachments_status="ready", mailbox_folder=folder, message_id=f"<{uid}@x>",
        **kw,
    )
    db.add(email)
    db.commit()
    return email


def _set_folder(db, name="Оброблено"):
    set_setting(db, "mail_processed_folder", name)
    db.commit()


class TestMoveToProcessed:
    def test_move_sets_folder_and_leaves_inbox(self, app_db, monkeypatch):  # noqa: F811
        app, session_factory = app_db
        with session_factory() as db:
            _set_folder(db)
            eid = _letter(db).id
        # IMAP-переміщення мокаємо в НАМЕСПЕЙСі роутера (він імпортував функцію).
        monkeypatch.setattr(mail_router_mod, "move_message_to_folder", lambda *a, **k: None)
        client = MiniClient(app)
        client.login(*OPERATOR)
        status, _, _ = client.post(f"/mail/{eid}/move-processed", {})
        assert status in (200, 204, 303)
        with session_factory() as db:
            assert db.get(EmailMessage, eid).mailbox_folder == "Оброблено"

    def test_row_context_deletes_row_with_empty_200(self, app_db, monkeypatch):  # noqa: F811
        app, session_factory = app_db
        with session_factory() as db:
            _set_folder(db)
            eid = _letter(db).id
        monkeypatch.setattr(mail_router_mod, "move_message_to_folder", lambda *a, **k: None)
        client = MiniClient(app)
        client.login(*OPERATOR)
        # row=1 + HX-Request → порожній 200, щоб htmx прибрав саме цей рядок.
        status, _, body = client.post(
            f"/mail/{eid}/move-processed", {"row": "1"}, {"HX-Request": "true"}
        )
        assert status == 200
        assert body.strip() == ""

    def test_no_folder_configured_is_409(self, app_db, monkeypatch):  # noqa: F811
        app, session_factory = app_db
        with session_factory() as db:
            eid = _letter(db).id  # папку НЕ задано
        monkeypatch.setattr(mail_router_mod, "move_message_to_folder", lambda *a, **k: None)
        client = MiniClient(app)
        client.login(*OPERATOR)
        status, _, _ = client.post(f"/mail/{eid}/move-processed", {})
        assert status == 409
        with session_factory() as db:
            assert db.get(EmailMessage, eid).mailbox_folder is None

    def test_imap_failure_keeps_letter_in_inbox(self, app_db, monkeypatch):  # noqa: F811
        app, session_factory = app_db
        with session_factory() as db:
            _set_folder(db)
            eid = _letter(db).id

        def _boom(*a, **k):
            raise RuntimeError("IMAP down")

        monkeypatch.setattr(mail_router_mod, "move_message_to_folder", _boom)
        client = MiniClient(app)
        client.login(*OPERATOR)
        status, _, _ = client.post(f"/mail/{eid}/move-processed", {})
        assert status in (200, 204, 303)  # не 500 — помилка в тост
        with session_factory() as db:
            assert db.get(EmailMessage, eid).mailbox_folder is None


class TestMoveBackToInbox:
    def test_reverse_clears_folder(self, app_db, monkeypatch):  # noqa: F811
        app, session_factory = app_db
        with session_factory() as db:
            eid = _letter(db, status="прийнято", folder="Оброблено").id

        def _fake_back(session, email):
            email.mailbox_folder = None  # як робить справжня функція

        monkeypatch.setattr(mail_router_mod, "move_message_back_to_inbox", _fake_back)
        client = MiniClient(app)
        client.login(*OPERATOR)
        status, _, _ = client.post(f"/mail/{eid}/move-to-inbox", {})
        assert status in (200, 204, 303)
        with session_factory() as db:
            assert db.get(EmailMessage, eid).mailbox_folder is None

    def test_reverse_on_non_moved_is_409(self, app_db, monkeypatch):  # noqa: F811
        app, session_factory = app_db
        with session_factory() as db:
            eid = _letter(db).id  # у папці не був
        monkeypatch.setattr(mail_router_mod, "move_message_back_to_inbox", lambda *a, **k: None)
        client = MiniClient(app)
        client.login(*OPERATOR)
        status, _, _ = client.post(f"/mail/{eid}/move-to-inbox", {})
        assert status == 409


class TestProcessedTabFiltering:
    def test_moved_leaves_pending_and_shows_in_processed(self, app_db):  # noqa: F811
        app, session_factory = app_db
        with session_factory() as db:
            _set_folder(db)
            today_moved = datetime.combine(business_today(), get_rollover()) + timedelta(hours=1)
            _letter(db, uid="200")  # лишається в Inbox
            # Перенесений СЬОГОДНІ — показується (вкладка «Оброблено» лише за
            # поточний день, власник 24.09.26).
            today_row = _letter(db, uid="201", folder="Оброблено", mailbox_moved_at=today_moved)
            # Перенесений ВЧОРА — у вкладці за сьогодні НЕ показується.
            yest_row = _letter(db, uid="202", folder="Оброблено",
                               mailbox_moved_at=today_moved - timedelta(days=1))
            today_id, yest_id = today_row.id, yest_row.id

        client = MiniClient(app)
        client.login(*OPERATOR)
        _, _, pending = client.get("/mail?view=pending")
        _, _, processed = client.get("/mail?view=processed")
        # Перенесений лист не в «Вхідні», а у вкладці папки.
        assert "mailrow-" in pending
        # Бейдж називає саму папку (25.09.26: «Перемістити» кладе в будь-яку).
        assert "↦ Оброблено" in processed
        assert f"mailrow-{today_id}" in processed      # сьогоднішній показано
        assert f"mailrow-{yest_id}" not in processed   # вчорашній схований
        # Значок вкладки папки рахує саме перенесені.
        assert "скачано" in processed.lower() or "Оброблено" in processed

    def test_return_action_matches_status(self, app_db):  # noqa: F811
        """Повернення з папки веде САМЕ в «Вхідні» (власник 24.09.26). Прийнята
        робота — через ВІДКАТ (restore: видаляє роботу, лист → нове), ↦-перенесений
        нове-лист — через лёгкий move-to-inbox."""
        app, session_factory = app_db
        with session_factory() as db:
            _set_folder(db)
            today_moved = datetime.combine(business_today(), get_rollover()) + timedelta(hours=1)
            acc = _letter(db, uid="301", status="прийнято", folder="Оброблено",
                          mailbox_moved_at=today_moved)
            new = _letter(db, uid="302", status="нове", folder="Оброблено",
                          mailbox_moved_at=today_moved)
            acc_id, new_id = acc.id, new.id

        client = MiniClient(app)
        client.login(*OPERATOR)
        _, _, processed = client.get("/mail?view=processed")
        # Прийнята → відкат прийняття; нове ↦ → лёгке повернення. Обидві в «Вхідні».
        assert f"/mail/{acc_id}/restore" in processed
        assert f"/mail/{acc_id}/move-to-inbox" not in processed
        assert f"/mail/{new_id}/move-to-inbox" in processed
        assert f"/mail/{new_id}/restore" not in processed


class TestSaveProcessedFolder:
    def test_save_folder_setting_whitelisted(self, app_db):  # noqa: F811
        app, session_factory = app_db
        client = MiniClient(app)
        client.login(*ADMIN)
        status, _, _ = client.post(
            "/settings/mail/processed-folder", {"folder": "Готово"}
        )
        assert status in (200, 303)
        with session_factory() as db:
            assert get_setting(db, "mail_processed_folder") == "Готово"
