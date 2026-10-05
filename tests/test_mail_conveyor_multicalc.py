"""Конвеєр для мультипрорахунку (власник 02.10.26).

Оператор тягне файли листів у Sum3D прямо зі спулу, розкладає диск і лише
потім приймає — з Sum3D у картках. Тому:

* батч БЕЗ Sum3D поводиться, як і раніше: невдалий лист не зупиняє решту;
* батч ІЗ Sum3D спершу перевіряє всі листи й при будь-якій відмові не
  приймає нічого, а відмова лягає над картками (#mb-preflight);
* над картками — зведення матеріалу; у картці — «Тека в пулі» і «Sum3D усім».
"""

from __future__ import annotations

import json

import pytest

from app.routers import mail as mail_router_mod
from app.services.mail_accept import AcceptResult, accept_blocker
from tests.asgi_client import MiniClient
from tests.test_mail_bulk import _letter
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _accept(app, cards):
    client = MiniClient(app)
    client.login(*OPERATOR)
    return client.post("/mail/accept-batch", {"payload": json.dumps(cards), "confirm_missing": "1"}, {"HX-Request": "true"})


def _headers(result):
    return {k.lower(): v for k, v in dict(result[1]).items()}


def _fake_ok(calls):
    def _fake(db, user, email, **kw):
        calls.append(email.id)
        return AcceptResult(order=None, material_label=kw.get("material_color", ""))
    return _fake


def test_without_sum3d_a_bad_letter_does_not_stop_the_rest(app_db, monkeypatch):  # noqa: F811
    """Поточна поведінка Конвеєра — лишається: без Sum3D листи йдуть кожен сам."""
    app, session_factory = app_db
    with session_factory() as db:
        good = _letter(db, "1")
        busy = _letter(db, "2", attachments_status="pending")
    calls = []
    monkeypatch.setattr(mail_router_mod, "accept_letter", _fake_ok(calls))
    _accept(app, [
        {"email_id": good, "client_name": "А", "material_color": "pmma a2"},
        {"email_id": busy, "client_name": "Б", "material_color": "pmma a2"},
    ])
    assert calls == [good, busy]  # обидва дійшли до accept_letter, як і раніше


def test_with_sum3d_one_bad_letter_blocks_the_whole_disc(app_db, monkeypatch):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        good = _letter(db, "1")
        busy = _letter(db, "2", attachments_status="pending")
    calls = []
    monkeypatch.setattr(mail_router_mod, "accept_letter", _fake_ok(calls))
    result = _accept(app, [
        {"email_id": good, "client_name": "А", "material_color": "pmma a2", "sum3d_id": "12-01-45"},
        {"email_id": busy, "client_name": "Б", "material_color": "pmma a2", "sum3d_id": "12-01-45"},
    ])
    status, _, body = result
    headers = _headers(result)
    assert status == 200
    assert calls == []  # нічого не перенесено
    assert headers.get("hx-retarget") == "#mb-preflight"
    assert "Нічого не прийнято" in body and "Б" in body
    assert "завантажуються" in body
    assert "mailBatchDone" not in headers.get("hx-trigger", "")


def test_with_sum3d_all_good_letters_are_accepted(app_db, monkeypatch):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        a = _letter(db, "1")
        b = _letter(db, "2")
    calls = []
    monkeypatch.setattr(mail_router_mod, "accept_letter", _fake_ok(calls))
    result = _accept(app, [
        {"email_id": a, "client_name": "А", "material_color": "pmma a2", "sum3d_id": "12-01-45"},
        {"email_id": b, "client_name": "Б", "material_color": "pmma a2", "sum3d_id": "12-01-45"},
    ])
    assert calls == [a, b]
    assert "hx-retarget" not in _headers(result)


def test_already_processed_letter_blocks_a_sum3d_disc(app_db, monkeypatch):  # noqa: F811
    """Лист прийняв інший оператор, поки цей розкладав диск, — оператор має
    це побачити, а не отримати пів диска."""
    app, session_factory = app_db
    with session_factory() as db:
        a = _letter(db, "1")
        b = _letter(db, "2", status="прийнято")
    calls = []
    monkeypatch.setattr(mail_router_mod, "accept_letter", _fake_ok(calls))
    _, _, body = _accept(app, [
        {"email_id": a, "client_name": "А", "sum3d_id": "12-01-45"},
        {"email_id": b, "client_name": "Б", "sum3d_id": "12-01-45"},
    ])
    assert calls == [] and "уже оброблено" in body


def test_accept_blocker_is_the_same_gate_as_accept(app_db):  # noqa: F811
    from app.models import EmailMessage

    _, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1", attachments_status="pending")
        email = db.get(EmailMessage, eid)
        assert "завантажуються" in accept_blocker(email)
        email.attachments_status = "ready"
        assert accept_blocker(email) is None


def test_conveyor_shows_summary_pool_button_and_sum3d_for_all(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        a = _letter(db, "1", subject="pmma a2")
        b = _letter(db, "2", subject="pmma a2")
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.get(f"/mail?partial=batch&batch={a},{b}")
    assert status == 200, html[:300]
    assert 'id="mb-summary"' in html and 'hx-post="/mail/batch-summary"' in html
    assert 'id="mb-preflight"' in html
    assert 'id="mb-sum3d-all"' in html
    for eid in (a, b):
        assert f'data-open-folder-url="/mail/{eid}/open-folder"' in html
        assert f'name="batch_email_id" value="{eid}"' in html
        assert f'id="mb-odd-{eid}"' in html
    # «змінити теку» нікуди не зникла
    assert html.count('name="folder_new"') == 2
    # Картки НЕ в <form>: htmx дочіпив би до запиту однієї картки поля всіх, і
    # рядок «ляже у» показував би теку останньої картки (02.10.26).
    assert 'id="mail-batch-form"' in html and '<form id="mail-batch-form"' not in html


def test_not_ready_letter_shows_as_blocked_card_not_silently_dropped(app_db, tmp_path):  # noqa: F811
    """Власник 02.10.26: обрано два листи одного клієнта, а в Конвеєрі один —
    другий «1 з 2 файл.». Неготовий лист — окрема картка з причиною й «Докачати»,
    у прийняття не йде."""
    from app.models import Attachment

    app, session_factory = app_db
    with session_factory() as db:
        ready = _letter(db, "1", subject="pmma a2")
        half = _letter(db, "2", subject="pmma a2")
        have = tmp_path / "crown.stl"
        have.write_bytes(b"STL")
        db.add(Attachment(email_message_id=half, filename="crown.stl", saved_path=str(have), size_bytes=3))
        db.add(Attachment(email_message_id=half, filename="bridge.stl",
                          saved_path=str(tmp_path / "bridge.stl"), size_bytes=3))
        db.commit()
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.get(f"/mail?partial=batch&batch={ready},{half}&ready={ready}")
    assert status == 200, html[:300]
    assert f'data-batch-id="{ready}"' in html
    assert f'data-batch-id="{half}"' not in html  # у payload прийняття не потрапить
    assert f'data-blocked-id="{half}"' in html
    assert "Скачано 1 з 2 файлів" in html
    assert f'hx-post="/mail/{half}/redownload"' in html and 'hx-swap="none"' in html
    assert "обрано <b>2</b>" in html and "готових 1" in html
    assert "Прийняти 1 в чергу" in html


def test_skipped_letter_offers_download_and_no_accept_without_ready(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        skipped = _letter(db, "1", attachments_status="skipped")
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, html = client.get(f"/mail?partial=batch&batch={skipped}&ready=")
    assert "Вкладення ще не скачано" in html
    assert f'hx-post="/mail/{skipped}/download-attachments"' in html
    assert "mb-accept" not in html and "Готових листів ще немає" in html


def test_without_ready_param_every_letter_is_a_normal_card(app_db):  # noqa: F811
    """Стара сторінка (без `ready`) — поведінка як і була."""
    app, session_factory = app_db
    with session_factory() as db:
        a = _letter(db, "1")
        b = _letter(db, "2", attachments_status="skipped")
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, html = client.get(f"/mail?partial=batch&batch={a},{b}")
    assert f'data-batch-id="{a}"' in html and f'data-batch-id="{b}"' in html
    assert "data-blocked-id" not in html


def test_summary_route_recounts_with_current_card_values(app_db):  # noqa: F811
    app, _ = app_db
    client = MiniClient(app)
    client.login(*OPERATOR)
    form = [
        ("batch_email_id", "11"), ("client_name", "Середюк"), ("material_color", "pmma a2"), ("quantity", "1"),
        ("batch_email_id", "12"), ("client_name", "Іваненко"), ("material_color", "пмма а2"), ("quantity", "2"),
        ("batch_email_id", "13"), ("client_name", "Петренко"), ("material_color", "pmma a3"), ("quantity", "2"),
    ]
    status, _, html = client.post("/mail/batch-summary", form)
    assert status == 200, html[:300]
    assert "pmma a2" in html and "2 листи" in html and "3 од." in html
    assert "Петренко — pmma a3" in html
    # позначки на картках: 13 — «інший колір», 11 і 12 — очищено
    assert 'id="mb-odd-13" hx-swap-oob="true"><span class="mb-oddchip">' in html
    assert 'id="mb-odd-11" hx-swap-oob="true"></span>' in html
