"""Кнопка «Відновити рядок» доїжджає до HTML (B.8).

Роут і його гейти перевіряє tests/test_sync_journal.py — але ці тести
підмінюють шаблон, тож не побачили б, що кнопки в розмітці просто немає.
Тут справжній рендер: слід у журналі мусить перетворюватись на дію, а вже
відновлений запис — на підпис без кнопки.
"""

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import SyncLog, User
from app.routers import sync_journal as sj


def _rendered(entry_kwargs):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        admin = User(username="admin", password_hash="x", full_name="Адмін", role="адмін")
        db.add(admin)
        entry = SyncLog(
            direction="db_to_sheet", status="ok", sheet_tab="07.09.26",
            message="видалено роботу 42: рядок очищено", **entry_kwargs,
        )
        db.add(entry)
        db.commit()
        response = sj.get_sync_journal(
            request=SimpleNamespace(
                session={"user_id": admin.id}, query_params={}, headers={},
                client=SimpleNamespace(host="127.0.0.1"),
            ),
            db=db,
        )
        return response.body.decode("utf-8"), entry.id


def test_erased_row_gets_a_restore_button():
    body, entry_id = _rendered({
        "erased_row": 13,
        "erased_values": '["1", "24122"]',
    })

    assert f'action="/journal/sync/{entry_id}/restore-row"' in body
    assert "Відновити рядок" in body


def test_already_restored_entry_shows_no_button():
    body, entry_id = _rendered({
        "erased_row": 13,
        "erased_values": '["1", "24122"]',
        "erased_restored_at": datetime(2026, 9, 7, 9, 15),
    })

    assert "restore-row" not in body
    assert "відновлено 09:15" in body


def test_plain_entry_has_no_action():
    body, _ = _rendered({})

    assert "restore-row" not in body
    assert "Відновити рядок" not in body
