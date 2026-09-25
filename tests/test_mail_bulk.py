"""Масові дії вкладок пошти (`POST /mail/bulk`, 25.09.26).

Власник: «виділити всі й щось зробити з ними всіма» — у кожній вкладці. Кожна
дія — та сама, що кнопка рядка, з тими самими гейтами. Прийнятий лист масово не
повертається й не відхиляється: його «↩» видалив би живі роботи з черги (рішення
власника — лише поштучно). Після дії оператор лишається у своїй вкладці.
IMAP замокано — стережемо логіку роуту, не мережу.
"""

from __future__ import annotations

import pytest

from app.models import EmailMessage
from app.routers import mail as mail_router_mod
from app.settings_store import set_setting
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _letter(db, uid, status="нове", folder=None, **kw):
    kw.setdefault("subject", "моно а3")
    kw.setdefault("attachments_status", "ready")
    email = EmailMessage(
        uid=uid, uid_validity="1", from_address="lab@ukr.net", from_name="Клієнт",
        status=status,
        mailbox_folder=folder, message_id=f"<{uid}@x>", **kw,
    )
    db.add(email)
    db.commit()
    return email.id


def _bulk(app, action, ids, view=None):
    client = MiniClient(app)
    client.login(*OPERATOR)
    headers = {"referer": f"http://127.0.0.1:8000/mail?view={view}"} if view else None
    return client.post("/mail/bulk", {"action": action, "ids": ",".join(map(str, ids))}, headers)


def _location(result):
    status, headers, _ = result
    assert status == 303
    return dict((k.lower(), v) for k, v in dict(headers).items())["location"]


def test_reject_skips_accepted_and_stays_on_tab(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        new_a = _letter(db, "1")
        new_b = _letter(db, "2")
        accepted = _letter(db, "3", status="прийнято")
    loc = _location(_bulk(app, "reject", [new_a, new_b, accepted], view="gone"))
    assert loc == "/mail?view=gone"
    with session_factory() as db:
        assert db.get(EmailMessage, new_a).status == "відхилено"
        assert db.get(EmailMessage, new_b).status == "відхилено"
        assert db.get(EmailMessage, accepted).status == "прийнято"


def test_unfilter_clears_the_rule_stamp(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        ids = [_letter(db, str(n), filter_category="спам") for n in range(3)]
    assert _location(_bulk(app, "unfilter", ids, view="filtered")) == "/mail?view=filtered"
    with session_factory() as db:
        assert all(db.get(EmailMessage, i).filter_category is None for i in ids)


def test_restore_returns_rejected_but_never_accepted(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        rejected = _letter(db, "1", status="відхилено")
        accepted = _letter(db, "2", status="прийнято")
    _bulk(app, "restore", [rejected, accepted], view="archive")
    with session_factory() as db:
        assert db.get(EmailMessage, rejected).status == "нове"
        assert db.get(EmailMessage, accepted).status == "прийнято"


def test_to_inbox_commits_each_letter_even_when_one_fails(app_db, monkeypatch):  # noqa: F811
    """IMAP-переніс уже стався в скриньці — збій на сусідньому листі не має
    відкотити базу вже перенесених (інакше база розійдеться зі скринькою)."""
    app, session_factory = app_db
    with session_factory() as db:
        ok = _letter(db, "1", folder="Оброблено")
        broken = _letter(db, "2", folder="Оброблено")
        accepted = _letter(db, "3", status="прийнято", folder="Оброблено")

    def fake_back(db, email):
        if email.uid == "2":
            raise RuntimeError("Лист не знайдено в папці")
        email.mailbox_folder = None

    monkeypatch.setattr(mail_router_mod, "move_message_back_to_inbox", fake_back)
    assert _location(_bulk(app, "to_inbox", [ok, broken, accepted], view="processed")) == (
        "/mail?view=processed"
    )
    with session_factory() as db:
        assert db.get(EmailMessage, ok).mailbox_folder is None
        assert db.get(EmailMessage, broken).mailbox_folder == "Оброблено"
        assert db.get(EmailMessage, accepted).mailbox_folder == "Оброблено"


def test_move_processed_moves_only_inbox_letters(app_db, monkeypatch):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        set_setting(db, "mail_processed_folder", "Оброблено")
        db.commit()
        a = _letter(db, "1")
        already = _letter(db, "2", folder="Інша")
    calls: list[tuple[list[str], str]] = []

    def fake_move(db, emails, folder):
        calls.append(([e.uid for e in emails], folder))
        return {}

    monkeypatch.setattr(mail_router_mod, "move_messages_to_folder", fake_move)
    assert _location(_bulk(app, "move_processed", [a, already])) == "/mail"
    # Один виклик на всі листи (один IMAP-вхід), уже перенесений не йде.
    assert calls == [(["1"], "Оброблено")]
    with session_factory() as db:
        got = db.get(EmailMessage, a)
        assert got.mailbox_folder == "Оброблено" and got.mailbox_moved_at is not None
        assert db.get(EmailMessage, already).mailbox_folder == "Інша"


def test_move_to_chosen_folder_marks_only_successful(app_db, monkeypatch):  # noqa: F811
    """Меню «Перемістити»: папку обирає оператор. Лист, який IMAP не переніс,
    лишається у Вхідних і в базі — інакше база розійшлась би зі скринькою."""
    app, session_factory = app_db
    with session_factory() as db:
        ok = _letter(db, "1")
        bad = _letter(db, "2")

    def fake_move(db, emails, folder):
        return {e.id: "no such message" for e in emails if e.uid == "2"}

    monkeypatch.setattr(mail_router_mod, "move_messages_to_folder", fake_move)
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, _ = client.post("/mail/bulk", {"action": "move_to", "ids": f"{ok},{bad}", "folder": "Спам"})
    assert status == 303
    with session_factory() as db:
        assert db.get(EmailMessage, ok).mailbox_folder == "Спам"
        assert db.get(EmailMessage, bad).mailbox_folder is None


def test_move_to_refuses_empty_or_inbox_folder(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1")
    client = MiniClient(app)
    client.login(*OPERATOR)
    for folder in ("", "INBOX"):
        status, _, _ = client.post("/mail/bulk", {"action": "move_to", "ids": str(eid), "folder": folder})
        assert status == 400


def test_move_folders_menu_puts_processed_folder_first(app_db, monkeypatch):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        set_setting(db, "mail_processed_folder", "Скачано")
        db.commit()
    monkeypatch.setattr(
        mail_router_mod, "list_move_target_folders", lambda db: ["Спам", "Скачано", "Видалені"]
    )
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, body = client.get("/mail/move-folders")
    assert status == 200
    order = [f for f in ("Скачано", "Спам", "Видалені") if f'data-folder="{f}"' in body]
    assert order == ["Скачано", "Спам", "Видалені"]
    assert body.index('data-folder="Скачано"') < body.index('data-folder="Спам"')


def test_move_folders_menu_shows_imap_error_instead_of_500(app_db, monkeypatch):  # noqa: F811
    app, _ = app_db

    def boom(db):
        raise RuntimeError("login failed")

    monkeypatch.setattr(mail_router_mod, "list_move_target_folders", boom)
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, body = client.get("/mail/move-folders")
    assert status == 200 and "login failed" in body


def test_move_targets_drop_inbox_sent_and_drafts(monkeypatch):
    """Службові папки ukr.net локалізовані, тож відсіюємо за IMAP-прапорцями."""
    from types import SimpleNamespace

    from app import mail_reader

    listing = [
        SimpleNamespace(name="INBOX", flags=()),
        SimpleNamespace(name="Надіслані", flags=("\\Sent",)),
        SimpleNamespace(name="Чернетки", flags=("\\Drafts",)),
        SimpleNamespace(name="Спам", flags=("\\Junk",)),
        SimpleNamespace(name="скачано прощитано", flags=("\\HasNoChildren",)),
    ]

    class FakeBox:
        def __init__(self, *a, **k):
            self.folder = SimpleNamespace(list=lambda: listing)

        def login(self, *a):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mail_reader, "MailBox", FakeBox)
    monkeypatch.setattr(mail_reader, "get_imap_login", lambda s: "u")
    monkeypatch.setattr(mail_reader, "get_imap_password", lambda s: "p")
    assert mail_reader.list_move_target_folders(None) == ["Спам", "скачано прощитано"]


def test_return_inbox_brings_gone_letters_back_to_all_letters(app_db, monkeypatch):  # noqa: F811
    """«Покинули Вхідні» → «↩ У Вхідні»: лист знайдено в скриньці — мітка
    «покинув» знята, відхилений знову в тріажі, стоїть позначка повернення
    (синк за віком не забере його назад). Не знайдений (видалено назавжди)
    лишається, прийнятий — пропуск (його «↩» видалило б роботи)."""
    from datetime import datetime

    app, session_factory = app_db
    gone_at = datetime(2026, 9, 24, 20, 0)
    with session_factory() as db:
        rejected = _letter(db, "1", status="відхилено", inbox_gone_at=gone_at)
        fresh = _letter(db, "2", inbox_gone_at=gone_at)
        deleted = _letter(db, "3", inbox_gone_at=gone_at)
        accepted = _letter(db, "4", status="прийнято", inbox_gone_at=gone_at)
    seen: list[str] = []

    def fake_return(db, emails):
        seen.extend(e.uid for e in emails)
        for e in emails:
            if e.uid == "2":
                e.uid = "902"  # знайдено в папці й перенесено → новий UID
        return {e.id: "видалено назавжди" for e in emails if e.uid == "3"}

    monkeypatch.setattr(mail_router_mod, "return_messages_to_inbox", fake_return)
    loc = _location(_bulk(app, "return_inbox", [rejected, fresh, deleted, accepted], view="gone"))
    assert loc == "/mail?view=gone"
    assert seen == ["1", "2", "3"]  # прийнятий у скриньку навіть не пішов
    with session_factory() as db:
        r, f, d, a = (db.get(EmailMessage, i) for i in (rejected, fresh, deleted, accepted))
        assert r.status == "нове" and r.inbox_gone_at is None and r.inbox_returned_at
        assert f.uid == "902" and f.inbox_gone_at is None
        assert d.inbox_gone_at == gone_at and d.inbox_returned_at is None
        assert a.status == "прийнято" and a.inbox_gone_at == gone_at


def test_gone_tab_row_has_return_button_except_accepted(app_db):  # noqa: F811
    from datetime import datetime

    app, session_factory = app_db
    with session_factory() as db:
        rej = _letter(db, "1", status="відхилено", inbox_gone_at=datetime(2026, 9, 24))
        acc = _letter(db, "2", status="прийнято", inbox_gone_at=datetime(2026, 9, 24))
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, page = client.get("/mail?view=gone")
    row_rej = page.split(f'id="mailrow-{rej}"', 1)[1].split('class="mailrow', 1)[0]
    row_acc = page.split(f'id="mailrow-{acc}"', 1)[1].split('class="mailrow', 1)[0]
    assert 'value="return_inbox"' in row_rej and f'value="{rej}"' in row_rej
    assert 'value="return_inbox"' not in row_acc
    assert 'value="return_inbox"' in page.split('id="mail-bulk-form"', 1)[1]


def test_all_letters_rows_have_no_per_row_move_or_reject(app_db):  # noqa: F811
    """«Вхідні»: ↦/✕ у рядку прибрано (власник 25.09.26) — те саме робить
    галочка + смуга масових дій. Смуга й пейджер на місці."""
    app, session_factory = app_db
    with session_factory() as db:
        set_setting(db, "mail_processed_folder", "Оброблено")
        db.commit()
        eid = _letter(db, "1")
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, page = client.get("/mail?view=pending")
    row = page.split(f'id="mailrow-{eid}"', 1)[1].split("</div>", 1)[0]
    assert f"/mail/{eid}/move-processed" not in page
    assert f"/mail/{eid}/reject" not in page
    assert 'class="mailcb"' in row
    assert 'id="mail-bulk-form"' in page and 'id="mail-pager"' in page


def test_unknown_action_is_refused(app_db):  # noqa: F811
    app, _ = app_db
    status, _, _ = _bulk(app, "delete_everything", [1])
    assert status == 400


def test_archive_tab_renders_bulk_bar_and_locks_accepted_rows(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        rejected = _letter(db, "1", status="відхилено")
        accepted = _letter(db, "2", status="прийнято")
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, body = client.get("/mail?view=archive")
    assert status == 200
    page = body if isinstance(body, str) else body.decode()
    assert 'id="mail-bulk-form"' in page and 'value="restore"' in page
    rej_cb = page.split(f'data-cb-id="{rejected}"', 1)[1].split(">", 1)[0]
    acc_cb = page.split(f'data-cb-id="{accepted}"', 1)[1].split(">", 1)[0]
    assert "disabled" not in rej_cb
    assert "disabled" in acc_cb


def test_card_prefills_latin_canon_and_offers_latin_chips(app_db):  # noqa: F811
    """Картка листа: поле матеріалу й чіпи — латинський канон цеху, як у ручному
    додаванні (власник 25.09.26), а не сире «ПММА а2» з листа."""
    from datetime import timedelta

    from app.business_day import utc_now
    from app.material_catalog import backfill_orders
    from app.models import Order
    from app.services.material_suggest import invalidate_cache

    app, session_factory = app_db
    with session_factory() as db:
        for text, n in {"pmma a2": 5, "пмма а 2": 2, "mono a3": 4}.items():
            for _ in range(n):
                db.add(Order(source="lab", status="нове", material_color=text,
                             created_at=utc_now() - timedelta(days=1)))
        db.flush()
        backfill_orders(db, only_unresolved=False)
        db.commit()
        eid = _letter(db, "1", material_color_guess="ПММА а2")
    invalidate_cache()
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, card = client.get(f"/mail/{eid}?panel=1")
    invalidate_cache()
    assert 'id="mc-material"' in card
    field = card.split('id="mc-material"', 1)[1].split(">", 1)[0]
    assert 'value="pmma a2"' in field
    assert 'data-mat="pmma a2"' in card and "пмма" not in card.split('class="mc-cands"', 1)[1].split("</div>", 1)[0]


def test_list_row_chip_shows_category_and_latin_canon(app_db):  # noqa: F811
    """Чіп рядка списку: «Zr mono b1», а не «Zr B1» (власник 25.09.26) — матеріал
    знайдено в тексті замовника («Monolight»), а тема теж джерело («емоутіонс а2»)."""
    from datetime import timedelta

    from app.business_day import utc_now
    from app.material_catalog import backfill_orders
    from app.models import Order
    from app.services.material_suggest import invalidate_cache

    app, session_factory = app_db
    with session_factory() as db:
        for text, n in {"mono b1": 6, "mono a3": 9, "emo a2": 6, "emo b1": 5}.items():
            for _ in range(n):
                db.add(Order(source="lab", status="нове", material_color=text,
                             created_at=utc_now() - timedelta(days=1)))
        db.flush()
        backfill_orders(db, only_unresolved=False)
        db.commit()
        body_id = _letter(db, "1", material_color_guess="B1",
                          body_text="Колір B1, циркон, Monolight Покритий опаком")
        subj_id = _letter(db, "2", material_color_guess="Цирконій", subject="емоутіонс а2")
    invalidate_cache()
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, page = client.get("/mail?view=pending")
    invalidate_cache()

    def chip(eid):
        row = page.split(f'id="mailrow-{eid}"', 1)[1].split('class="mailrow', 1)[0]
        return row.split('class="matmini', 1)[1].split("</span></span>", 1)[0]

    assert "<b>Zr</b>" in chip(body_id) and 'mm-c">mono b1' in chip(body_id)
    assert 'mm-c">emo a2' in chip(subj_id)


def test_card_warns_about_duplicate_files_and_leaves_copy_unchecked(app_db, tmp_path):  # noqa: F811
    """Клієнт надіслав ту саму роботу двічі (окремо й в архіві): картка каже
    про дубль, а копію не позначає — щоб не прийняли й не відфрезерували двічі."""
    from app.models import Attachment

    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1")
        for name, data in (("crown.stl", b"CROWN"), ("bridge.stl", b"BRIDGE"),
                           ("crown (2).stl", b"CROWN")):
            path = tmp_path / name
            path.write_bytes(data)
            db.add(Attachment(email_message_id=eid, filename=name,
                              saved_path=str(path), size_bytes=len(data)))
        db.commit()
        ids = {a.filename: a.id for a in db.query(Attachment).filter_by(email_message_id=eid)}
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, card = client.get(f"/mail/{eid}?panel=1")

    def checkbox(att_id):
        return card.split(f'name="attachment_ids" value="{att_id}"', 1)[1].split(">", 1)[0]

    assert "клієнт надіслав ту саму роботу двічі" in card
    assert "checked" not in checkbox(ids["crown (2).stl"])
    assert "checked" in checkbox(ids["crown.stl"]) and "checked" in checkbox(ids["bridge.stl"])
    assert card.count('class="mc-attdup"') == 1


def test_conveyor_refuses_letter_with_duplicate_files(app_db, tmp_path, monkeypatch):  # noqa: F811
    """Конвеєр бере ВСІ файли листа — з дублями він прийняв би роботу двічі.
    Такий лист він не приймає, а каже відкрити картку й обрати файли."""
    import json

    from app.models import Attachment

    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1")
        for name in ("crown.stl", "crown (2).stl"):
            path = tmp_path / name
            path.write_bytes(b"CROWN")
            db.add(Attachment(email_message_id=eid, filename=name,
                              saved_path=str(path), size_bytes=5))
        db.commit()
    called = []
    monkeypatch.setattr(mail_router_mod, "accept_letter", lambda *a, **k: called.append(1))
    client = MiniClient(app)
    client.login(*OPERATOR)
    payload = json.dumps([{"email_id": eid, "client_name": "Клієнт", "material_color": "mono a3"}])
    status, _, body = client.post("/mail/accept-batch", {"payload": payload}, {"HX-Request": "true"})
    assert status == 200
    assert called == []
    assert "однакові файли" in body


def test_manual_download_extracts_archive_like_link_download(app_db, tmp_path, monkeypatch):  # noqa: F811
    """«Скачати вкладення»: 2 STL + архів з ними ж. Архів мусить розпакуватись
    одразу (як після скачування за посиланням), а не лишитись «Desktop.rar»."""
    import zipfile

    from app.models import Attachment, EmailMessage

    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1", attachments_status="skipped")

    def fake_download(db, email, root):
        spool = tmp_path / "spool"
        spool.mkdir(exist_ok=True)
        (spool / "crown.stl").write_bytes(b"CROWN")
        with zipfile.ZipFile(spool / "Desktop.zip", "w") as zf:
            zf.writestr("crown.stl", b"CROWN")
        for name in ("crown.stl", "Desktop.zip"):
            path = spool / name
            db.add(Attachment(email_message_id=email.id, filename=name,
                              saved_path=str(path), size_bytes=path.stat().st_size))
        email.attachments_status = "ready"
        return 2

    monkeypatch.setattr(mail_router_mod, "download_attachments_now", fake_download)
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, card = client.post(f"/mail/{eid}/download-attachments", {}, {"HX-Request": "true"})
    assert status == 200
    with session_factory() as db:
        names = sorted(a.filename for a in db.get(EmailMessage, eid).attachments)
    assert names == ["crown (2).stl", "crown.stl"]  # архів розпаковано, копія поруч
    assert "клієнт надіслав ту саму роботу двічі" in card


def test_archive_is_labelled_and_file_actions_are_outside_the_chip_menu(app_db, tmp_path):  # noqa: F811
    from app.models import Attachment

    app, session_factory = app_db
    with session_factory() as db:
        eid = _letter(db, "1")
        for name in ("crown.stl", "Desktop.rar"):
            path = tmp_path / name
            path.write_bytes(b"X" * 10)
            db.add(Attachment(email_message_id=eid, filename=name,
                              saved_path=str(path), size_bytes=10))
        db.commit()
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, card = client.get(f"/mail/{eid}?panel=1")
    assert "архів · не розпаковано" in card
    after_menu = card.split("</details>", 1)[1]
    assert "Розпакувати архіви" in after_menu and "Відкрити папку" in after_menu
