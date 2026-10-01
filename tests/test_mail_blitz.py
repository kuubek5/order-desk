"""Бліц пошти 25.09.26 (власник: «так» на всі чотири).

1. Конвеєр приймає Опак і Sum3D, як картка листа.
2. Причину «На уточненні» можна змінити — час і автор паузи лишаються.
3. Лист із дублями файлів у СПИСКУ — «!» замість зеленої галочки.
4. Чіп без кольору — «PMMA», а не «PMMA pmma».
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.mail_duplicates import has_duplicate_files
from app.mail_hold import put_on_hold
from app.models import Attachment
from app.routers import mail as mail_router_mod
from app.services.mail_accept import AcceptResult
from app.triage_status import triage_readiness
from tests.asgi_client import MiniClient
from tests.test_mail_bulk import _letter
from tests.test_mail_hold import _page
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def test_conveyor_passes_opak_and_sum3d_to_accept(app_db, monkeypatch):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1")
    seen = {}

    def _fake_accept(db, user, email, **kw):
        seen.update(kw)
        return AcceptResult(error="зупинено в тесті")  # лише перевіряємо аргументи

    monkeypatch.setattr(mail_router_mod, "accept_letter", _fake_accept)
    client = MiniClient(app)
    client.login(*OPERATOR)
    payload = json.dumps([{
        "email_id": eid, "client_name": "Клієнт", "material_color": "mono a3",
        "opak": "2", "sum3d_id": "12-01-45",
    }])
    client.post("/mail/accept-batch", {"payload": payload}, {"HX-Request": "true"})
    assert seen["opak"] == "2" and seen["sum3d_id"] == "12-01-45"


def test_conveyor_passes_the_changed_folder_to_accept(app_db, monkeypatch):  # noqa: F811
    """01.10.26: у картці Конвеєра є «змінити теку» (той самий dir_editor, що
    в картці листа) — вписана нова тека й підпапка матеріалу мусять дійти до
    `accept_letter`, а не губитись, як доти (сервер підставляв порожні)."""
    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "2")
    seen = {}

    def _fake_accept(db, user, email, **kw):
        seen.update(kw)
        return AcceptResult(error="зупинено в тесті")

    monkeypatch.setattr(mail_router_mod, "accept_letter", _fake_accept)
    client = MiniClient(app)
    client.login(*OPERATOR)
    payload = json.dumps([{
        "email_id": eid, "client_name": "Клієнт", "material_color": "mono a3",
        "folder_pick": "", "folder_new": "Клієнт Новий", "material_folder": "моно а3",
    }])
    client.post("/mail/accept-batch", {"payload": payload}, {"HX-Request": "true"})
    assert seen["folder_new"] == "Клієнт Новий" and seen["material_folder"] == "моно а3"


def test_conveyor_card_has_path_and_folder_editor(app_db):  # noqa: F811
    """Картка Конвеєра: від кого, шлях «ляжуть у» і редактор теки на рядок."""
    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "3")
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.get(f"/mail?partial=batch&batch={eid}")
    assert status == 200, html[:300]
    assert f'class="mb-row" data-batch-id="{eid}"' in html
    assert f'id="bt-path-{eid}"' in html and "ляжуть у" in html
    assert 'name="folder_new"' in html and 'name="material_folder"' in html
    assert 'hx-include="closest .mb-row"' in html


def test_changing_hold_reason_keeps_time_and_author():
    email = SimpleNamespace(status="нове", hold_at=None, hold_reason=None, hold_note=None, hold_by=None)
    put_on_hold(email, "", "", "Рома")
    first_at = email.hold_at
    email.hold_at = datetime(2026, 9, 25, 9, 12)
    put_on_hold(email, "dup_files", "дзвонити після обіду", "Інший")
    assert email.hold_at == datetime(2026, 9, 25, 9, 12) and first_at is not None
    assert email.hold_by == "Рома"
    assert email.hold_reason == "dup_files" and email.hold_note == "дзвонити після обіду"


def test_duplicate_files_turn_the_list_badge_orange(tmp_path):
    def att(i, name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return SimpleNamespace(id=i, filename=name, saved_path=str(path),
                               size_bytes=len(data), order_id=None)

    dup = [att(1, "crown.stl", b"CROWN"), att(2, "crown (2).stl", b"CROWN")]
    other = [att(3, "a.stl", b"AAAAA"), att(4, "b.stl", b"BBBBB")]  # розмір той самий, вміст різний
    assert has_duplicate_files(dup) is True
    assert has_duplicate_files(other) is False

    ready = SimpleNamespace(service_type_guess=None, material_color_guess="mono a3")
    assert triage_readiness(ready)["state"] == "ready"
    ready.has_duplicates = True
    assert triage_readiness(ready) == {"state": "dups", "missing": ["дублі файлів"]}


def test_list_shows_duplicates_state_for_letter_with_same_files(app_db, tmp_path):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1", subject="Дубль-лист", material_color_guess="mono a3")
        for name in ("crown.stl", "crown (2).stl"):
            path = tmp_path / name
            path.write_bytes(b"CROWN")
            db.add(Attachment(email_message_id=eid, filename=name,
                              saved_path=str(path), size_bytes=5))
        db.commit()
    html = _page(app, "pending")
    row = html.split(f'id="mailrow-{eid}"', 1)[1].split('class="mailrow', 1)[0]
    assert "Клієнт надіслав ту саму роботу двічі" in row
    assert 'data-ready="1"' not in row  # конвеєр такий лист і так не приймає


def test_row_chip_without_colour_does_not_repeat_the_category():
    badge = mail_router_mod._row_badge({"badge": "PMMA", "text": "pmma"}, None)
    assert badge["symbol"] == "PMMA" and badge["color"] == ""
    zr = mail_router_mod._row_badge({"badge": "Zr", "text": "mono"}, None)
    assert zr["color"] == "mono"
    # 29.09.26: «PMMA pmma a3» → «PMMA a3» (слово категорії не повторюємо).
    shade = mail_router_mod._row_badge({"badge": "PMMA", "text": "pmma a2"}, None)
    assert shade["color"] == "a2"
    kappa = mail_router_mod._row_badge({"badge": "PMMA", "text": "kappa"}, None)
    assert kappa["color"] == "kappa"
    zr_shade = mail_router_mod._row_badge({"badge": "Zr", "text": "emo a3"}, None)
    assert zr_shade["color"] == "emo a3"
