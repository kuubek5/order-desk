"""«Нові диски»: замовлення на склад — запис, скасування, історія, Telegram.

Бриф — NEW_DISCS_BRIEF.md (10.09.26). Тут стережемо правила власника, які
ламаються тихо:

1. «Надіслати на склад» = рядок черги Telegram І одразу «замовлено» в ОДНІЙ
   транзакції; порожній вибір — не «усе».
2. Скасувати можна БУДЬ-ЯКЕ замовлення; диски повертаються в «чекають»,
   запис лишається в історії закресленим; точку відліку не чіпає ніщо.
3. Скасоване в Telegram РЕДАГУЄ те саме повідомлення складу (message_id),
   а не шле нове; недоставлене — відкликається.
4. Склад отримує лише замовлення — сповіщення печей йому не йдуть, меню немає.
5. Справжній POST і рендер екрана: зелений сервіс без роута нічого не доводить.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import CamBlank, CamBlankOrder, TelegramMember, TelegramOutbox
from app.services import disc_orders
from app.services import telegram_bot as bot
from app.services.cam_blanks import pending_blanks
from app.services.telegram import ApiResult
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

SEEN = datetime(2026, 9, 10, 9, 0)
# Коли замовляють. НЕ дорівнює SEEN: `ordered_at == first_seen_at` — ознака
# точки відліку, і такий диск замовленням не вважається.
LATER = datetime(2026, 9, 10, 17, 0)
OWNER = "555"
STORE = "900"


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture(autouse=True)
def _clean_bot():
    bot.reset_for_tests()
    yield
    bot.reset_for_tests()


def _seed(db) -> dict[str, int]:
    db.add_all([
        CamBlank(rel_path="ZR/18/a", material_dir="ZR", file_name="zr18_18-Monolith-A2-x1.blk", first_seen_at=SEEN),
        CamBlank(rel_path="ZR/18/b", material_dir="ZR", file_name="zr18_18-Monolith-A2-x2.blk", first_seen_at=SEEN),
        CamBlank(rel_path="PMMA/20/c", material_dir="PMMA-PEEK", file_name="pmma20_20-a3-x7.blk", first_seen_at=SEEN),
        # Точка відліку: `ordered_at == first_seen_at`, без замовлення.
        CamBlank(rel_path="ZR/12/old", material_dir="ZR", file_name="zr12_12-a1-x1.blk",
                 first_seen_at=datetime(2026, 9, 1, 8, 0), ordered_at=datetime(2026, 9, 1, 8, 0)),
    ])
    db.commit()
    return {row.rel_path: row.id for row in db.scalars(select(CamBlank))}


def _warehouse(db):
    from app.settings_store import set_setting

    set_setting(db, "telegram_bot_enabled", "1")
    set_setting(db, "telegram_bot_token", "123:ABC")
    set_setting(db, "telegram_chat_id", OWNER)
    db.add(TelegramMember(chat_id=STORE, joined_at=SEEN, role=bot.ROLE_WAREHOUSE, label="Склад"))
    db.commit()


# ── Запис ───────────────────────────────────────────────────────────────────


class TestCreate:
    def test_only_the_checked_discs_are_ordered(self, db):
        ids = _seed(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"], ids["ZR/18/b"]], now=SEEN + timedelta(hours=8))
        db.commit()
        assert order.disc_count == 2
        assert order.text == "mono-a2-18(х2)"
        assert [row.rel_path for row in pending_blanks(db)] == ["PMMA/20/c"]
        assert {row.order_id for row in db.scalars(select(CamBlank).where(CamBlank.order_id.is_not(None)))} == {order.id}

    def test_empty_selection_orders_nothing(self, db):
        """Порожній вибір — не «усе». Інакше зняті галочки замовили б список."""
        _seed(db)
        assert disc_orders.create_order(db, ids=[]) is None
        db.rollback()
        assert len(pending_blanks(db)) == 3
        assert db.scalar(select(CamBlankOrder)) is None

    def test_note_alone_is_an_order(self, db):
        """Замовлення може складатися лише з дописаного (фрези)."""
        _seed(db)
        order = disc_orders.create_order(db, ids=[], note="6*2.5 zr (х2)")
        db.commit()
        assert (order.disc_count, order.text, order.label) == (0, "6*2.5 zr (х2)", "лише дописане")
        assert len(pending_blanks(db)) == 3

    def test_note_goes_after_the_discs(self, db):
        ids = _seed(db)
        order = disc_orders.create_order(db, ids=list(ids.values()), note="полірувальні диски")
        db.commit()
        assert order.text == "mono-a2-18(х2)\n\npmma-a3-20\n\nполірувальні диски"
        assert order.disc_count == 3, "точка відліку в замовлення не йде"

    def test_a_disc_already_taken_by_another_operator_is_not_reordered(self, db):
        """Двоє операторів зі застарілими сторінками: той самий диск не йде
        у два замовлення, і час першого не переписується."""
        ids = _seed(db)
        first = disc_orders.create_order(db, ids=[ids["ZR/18/a"]], now=SEEN + timedelta(hours=1))
        db.commit()
        assert disc_orders.create_order(db, ids=[ids["ZR/18/a"]], now=SEEN + timedelta(hours=2)) is None
        db.rollback()
        row = db.get(CamBlank, ids["ZR/18/a"])
        assert (row.order_id, row.ordered_at) == (first.id, SEEN + timedelta(hours=1))

    def test_label_names_the_shifts(self, db):
        db.add_all([
            CamBlank(rel_path="n", material_dir="ZR", file_name="zr25_25-a2-x1.blk", first_seen_at=datetime(2026, 9, 9, 22, 0)),
            CamBlank(rel_path="d", material_dir="ZR", file_name="zr25_25-a2-x2.blk", first_seen_at=datetime(2026, 9, 10, 9, 0)),
        ])
        db.commit()
        order = disc_orders.create_order(db, ids=[r.id for r in pending_blanks(db)])
        assert order.label == "нічна 09.09→10.09 + денна 10.09"


# ── Скасування ──────────────────────────────────────────────────────────────


class TestCancel:
    def test_any_order_can_be_cancelled_not_only_the_last(self, db):
        """Рішення власника 10.09.26: скасувати можна будь-яке. Ризик
        подвійного замовлення прийнятий свідомо."""
        ids = _seed(db)
        older = disc_orders.create_order(db, ids=[ids["PMMA/20/c"]], now=SEEN + timedelta(hours=1))
        disc_orders.create_order(db, ids=[ids["ZR/18/a"]], now=SEEN + timedelta(hours=2))
        db.commit()

        result = disc_orders.cancel_order(db, older.id, now=SEEN + timedelta(hours=3))
        db.commit()
        assert result.returned == 1
        assert sorted(r.rel_path for r in pending_blanks(db)) == ["PMMA/20/c", "ZR/18/b"]

    def test_cancelled_order_stays_in_history_crossed_out(self, db):
        ids = _seed(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"], ids["ZR/18/b"]], note="фрези")
        db.commit()
        disc_orders.cancel_order(db, order.id)
        db.commit()
        [view] = disc_orders.history(db)
        assert view.cancelled
        assert (view.count, view.text) == (2, "mono-a2-18(х2)\n\nфрези"), "знімок пережив відчеплення дисків"

    def test_second_cancel_is_a_no_op(self, db):
        ids = _seed(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"]])
        db.commit()
        assert disc_orders.cancel_order(db, order.id) is not None
        db.commit()
        assert disc_orders.cancel_order(db, order.id) is None

    def test_cancel_never_touches_the_baseline(self, db):
        """ГОЛОВНЕ. Точка відліку має `ordered_at`, але вона не замовлення.
        Навіть якщо рядок відліку якось опинився з `order_id` — скасування
        не має вивалити в список історію теки."""
        ids = _seed(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"]])
        db.flush()
        db.get(CamBlank, ids["ZR/12/old"]).order_id = order.id
        db.commit()
        disc_orders.cancel_order(db, order.id)
        db.commit()
        assert "ZR/12/old" not in [r.rel_path for r in pending_blanks(db)]


# ── Історія ─────────────────────────────────────────────────────────────────


def test_history_is_newest_first_and_skips_the_baseline(db):
    ids = _seed(db)
    disc_orders.create_order(db, ids=[ids["PMMA/20/c"]], now=SEEN + timedelta(hours=1))
    disc_orders.create_order(db, ids=[ids["ZR/18/a"], ids["ZR/18/b"]], now=SEEN + timedelta(hours=5))
    db.commit()
    history = disc_orders.history(db)
    assert [v.count for v in history] == [2, 1]
    assert all(v.delivery.state == "manual" for v in history)
    assert disc_orders.history_total(db) == 2


def test_orders_migrated_from_old_batches_are_rebuilt_from_their_discs(db):
    """Міграція 0060 переносить пачки старої кнопки без знімка тексту —
    екран складає його з прив'язаних дисків."""
    ids = _seed(db)
    stamp = SEEN + timedelta(hours=8)
    legacy = CamBlankOrder(created_at=stamp, via="manual", disc_count=2)
    db.add(legacy)
    db.flush()
    for key in ("ZR/18/a", "ZR/18/b"):
        row = db.get(CamBlank, ids[key])
        row.ordered_at, row.order_id = stamp, legacy.id
    db.commit()
    [view] = disc_orders.history(db)
    assert (view.text, view.label, view.count) == ("mono-a2-18(х2)", "денна 10.09", 2)


def test_recent_notes_drop_counts_and_template_burs(db):
    _seed(db)
    for note in ("6*2.5 zr (х3)", "полірувальні диски (х2)\nфреза 9.9"):
        disc_orders.create_order(db, ids=[], note=note)
    db.commit()
    assert disc_orders.recent_notes(db) == ["полірувальні диски", "фреза 9.9"]


def test_journal_hides_the_baseline_and_knows_what_waits(db):
    ids = _seed(db)
    disc_orders.create_order(db, ids=[ids["PMMA/20/c"]], now=SEEN + timedelta(hours=1))
    db.commit()
    j = disc_orders.journal(db)
    assert (j.total, j.waiting) == (3, 2)
    assert [(b.zr, b.pmma) for b in j.bars] == [(2, 1)]
    assert disc_orders.journal(db, st="done").days[0].rows[0].row.rel_path == "PMMA/20/c"
    assert disc_orders.journal(db, mat="zr").bars[0].total == 2
    assert disc_orders.journal(db, q="pmma").bars[0].total == 1


# ── Telegram ────────────────────────────────────────────────────────────────


class TestTelegram:
    def test_send_queues_one_row_per_warehouse_in_the_same_transaction(self, db):
        ids = _seed(db)
        _warehouse(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"]], via="telegram", now=LATER)
        db.commit()
        [row] = db.scalars(select(TelegramOutbox)).all()
        assert (row.chat_id, row.kind) == (STORE, bot.DISC_ORDER_KIND)
        assert row.text == "<b>Замовлення дисків · 10.09</b>\n\nmono-a2-18"
        assert disc_orders.order_view(db, order.id).delivery.state == "queued"

    def test_warehouse_blocker_names_the_reason(self, db):
        assert "вимкнено" in bot.warehouse_blocker(db)
        from app.settings_store import set_setting

        set_setting(db, "telegram_bot_enabled", "1")
        set_setting(db, "telegram_bot_token", "123:ABC")
        set_setting(db, "telegram_chat_id", OWNER)
        db.commit()
        assert "Склад не підключено" in bot.warehouse_blocker(db)
        db.add(TelegramMember(chat_id=STORE, joined_at=SEEN, role=bot.ROLE_WAREHOUSE))
        db.commit()
        assert bot.warehouse_blocker(db) is None

    def test_sent_message_id_is_kept_and_cancel_edits_that_message(self, db):
        """Скасування не шле нового — редагує те саме повідомлення складу."""
        ids = _seed(db)
        _warehouse(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"]], via="telegram", now=LATER)
        db.commit()
        sent = bot.flush_outbox(db, lambda chat, text: ApiResult(True, 200, {"message_id": 4242}), SEEN)
        assert sent == 1
        original = db.scalars(select(TelegramOutbox)).one()
        assert original.message_id == 4242
        assert disc_orders.order_view(db, order.id).delivery.state == "sent"

        disc_orders.cancel_order(db, order.id, now=SEEN + timedelta(minutes=5))
        db.commit()
        edits = []

        def edit(chat, message_id, text):
            edits.append((chat, message_id, text))
            return ApiResult(True, 200, {"message_id": message_id})

        bot.flush_outbox(db, lambda *_: pytest.fail("скасування не шле нового"), SEEN + timedelta(minutes=5), edit=edit)
        assert len(edits) == 1
        chat, message_id, text = edits[0]
        assert (chat, message_id) == (STORE, 4242)
        assert text.startswith("❌ <b>Скасовано 09:05</b>") and "<s>mono-a2-18</s>" in text
        assert disc_orders.order_view(db, order.id).delivery.cancel == "edited"

    def test_unchanged_edit_counts_as_done(self, db):
        ids = _seed(db)
        _warehouse(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"]], via="telegram", now=LATER)
        db.commit()
        bot.flush_outbox(db, lambda *_: ApiResult(True, 200, {"message_id": 7}), SEEN)
        disc_orders.cancel_order(db, order.id, now=SEEN)
        db.commit()
        not_modified = ApiResult(False, 400, None, "HTTP 400 Bad Request: message is not modified")
        bot.flush_outbox(db, lambda *_: None, SEEN, edit=lambda *_: not_modified)
        assert disc_orders.order_view(db, order.id).delivery.cancel == "edited"

    def test_cancel_before_delivery_withdraws_the_message(self, db):
        """Замовлення ще в черзі (мережа лежить) — скасування його відкликає:
        склад не має отримати замовлення вже після скасування."""
        ids = _seed(db)
        _warehouse(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"]], via="telegram", now=LATER)
        db.commit()
        disc_orders.cancel_order(db, order.id, now=SEEN + timedelta(minutes=1))
        db.commit()
        calls = []
        bot.flush_outbox(db, lambda *a: calls.append(a) or ApiResult(True, 200, {"message_id": 1}),
                         SEEN + timedelta(minutes=2), edit=lambda *a: calls.append(a) or ApiResult(True, 200))
        assert calls == [], "до складу не пішло нічого"
        assert disc_orders.order_view(db, order.id).delivery.cancel == "withdrawn"

    def test_edit_waits_for_an_original_still_in_flight(self, db):
        """Гонка: оригінал ще не має message_id, але й не списаний."""
        ids = _seed(db)
        _warehouse(db)
        order = disc_orders.create_order(db, ids=[ids["ZR/18/a"]], via="telegram", now=LATER)
        db.commit()
        original = db.scalars(select(TelegramOutbox)).one()
        disc_orders.cancel_order(db, order.id, now=SEEN)
        original.gave_up_at = None  # відправник устиг забрати його раніше
        db.commit()
        edit_row = db.scalars(select(TelegramOutbox).where(TelegramOutbox.edit_of_id.is_not(None))).one()
        edits = []
        # Оригінал не пройшов (502) — редагувати ще нема чого, правка чекає.
        bot.flush_outbox(db, lambda *_: ApiResult(False, 502, None, "HTTP 502"), SEEN,
                         edit=lambda c, m, t: edits.append(m) or ApiResult(True, 200))
        assert edits == [], "у оригіналу ще немає message_id"
        assert edit_row.next_attempt_at is not None and edit_row.gave_up_at is None
        # Мережа ожила: оригінал іде першим (менший id), правка — за ним у
        # тому самому проході, уже з його message_id.
        bot.flush_outbox(db, lambda *_: ApiResult(True, 200, {"message_id": 9}), SEEN + timedelta(minutes=1),
                         edit=lambda c, m, t: edits.append(m) or ApiResult(True, 200))
        assert edits == [9]

    def test_order_rows_survive_the_outbox_cleanup(self, db):
        """Без них не видно стану доставки, і не скасувати давнє замовлення
        редагуванням."""
        ids = _seed(db)
        _warehouse(db)
        disc_orders.create_order(db, ids=[ids["ZR/18/a"]], via="telegram", now=LATER)
        db.commit()
        bot.flush_outbox(db, lambda *_: ApiResult(True, 200, {"message_id": 1}), SEEN)
        assert bot.enqueue(db, dedup_key="furnace-old", kind="furnace_open", text="t", now=SEEN, ttl=timedelta(hours=1))
        db.commit()
        bot.flush_outbox(db, lambda *_: ApiResult(True, 200), SEEN)
        bot.prune_outbox(db, now=SEEN + timedelta(days=90))
        kinds = [row.kind for row in db.scalars(select(TelegramOutbox))]
        assert kinds == [bot.DISC_ORDER_KIND]

    def test_warehouse_gets_no_furnace_notifications(self, db):
        _warehouse(db)
        db.add(TelegramMember(chat_id="700", joined_at=SEEN))
        db.commit()
        bot.broadcast(db, event_key="furnace:x:WAIT>RUN:1", kind="furnace_closed", text="t",
                      now=SEEN, ttl=timedelta(hours=1))
        db.commit()
        chats = sorted(row.chat_id for row in db.scalars(select(TelegramOutbox)))
        assert chats == [OWNER, "700"]

    def test_warehouse_has_no_menu_and_no_shop_data(self, db):
        _warehouse(db)
        now = int(datetime.now().timestamp())
        command = {"update_id": 1, "message": {"message_id": 3, "date": now,
                   "chat": {"id": int(STORE), "type": "private"}, "text": "/start", "from": {"id": int(STORE)}}}
        [action] = bot.handle_update(db, command)
        assert action.method == "sendMessage"
        assert "лише для замовлень дисків" in action.payload["text"]
        assert "reply_markup" not in action.payload

        reply = json.loads(json.dumps(command))
        reply["message"]["text"] = "прийнято"
        assert bot.handle_update(db, reply) == [], "на «прийнято» бот мовчить"

        press = {"update_id": 2, "callback_query": {"id": "cb", "data": "v:furnaces",
                 "message": {"message_id": 5, "chat": {"id": int(STORE), "type": "private"}}}}
        assert [a.method for a in bot.handle_update(db, press)] == ["answerCallbackQuery"]

    def test_warehouse_invite_brings_the_role_and_no_menu(self, db):
        from app.settings_store import set_setting

        set_setting(db, "telegram_chat_id", OWNER)
        invite = bot.new_invite(db, label="Склад", role=bot.ROLE_WAREHOUSE)
        db.commit()
        update = {"update_id": 3, "message": {"message_id": 1, "date": int(datetime.now().timestamp()),
                  "chat": {"id": 901, "type": "private"}, "text": f"/start {invite.code}",
                  "from": {"id": 901, "first_name": "Оля"}}}
        [action] = bot.handle_update(db, update)
        assert "замовлення дисків" in action.payload["text"]
        assert "reply_markup" not in action.payload
        member = db.scalars(select(TelegramMember)).one()
        assert member.role == bot.ROLE_WAREHOUSE


# ── Екран: справжні POST і рендер ───────────────────────────────────────────


def _client(app, who=OPERATOR) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*who)
    assert status in (200, 302, 303), status
    return client


def _seed_app(session_factory) -> dict[str, int]:
    from app.settings_store import set_setting

    with session_factory() as s:
        set_setting(s, "cam_blanks_path", r"C:\cam-bl")
        s.commit()
        return _seed(s)


class TestScreen:
    def test_page_renders_the_three_parts(self, app_db):  # noqa: F811
        app, factory = app_db
        _seed_app(factory)
        status, _, html = _client(app).get("/discs")
        assert status == 200, html[:500]
        assert 'id="dz-work"' in html and 'id="dz-orders"' in html and 'id="dz-all"' in html
        assert "mono" in html and "Денна зміна" in html
        assert "Надіслати на склад" in html
        assert "6*2.5 zr" in html, "шаблон фрез власника"
        assert "комірни" not in html.lower(), "слово «комірниця» прибрано з інтерфейсу"
        assert "Нові диски" in html

    def test_operator_orders_and_the_page_offers_cancel(self, app_db):  # noqa: F811
        app, factory = app_db
        ids = _seed_app(factory)
        client = _client(app)
        status, headers, html = client.post(
            "/discs/order",
            {"ids": str(ids["PMMA/20/c"]), "via": "manual", "note": "6*2.5 zr"},
            headers={"HX-Request": "true", "HX-Target": "dz-work"},
        )
        assert status == 200, html[:500]
        trigger = json.loads(headers["hx-trigger"])
        assert trigger["dz-changed"] is True
        assert trigger["toast"]["undoUrl"].startswith("/discs/orders/")
        assert "Позначено замовленим" in html, "рядок щойно зробленого замовлення"
        with factory() as s:
            order = s.scalars(select(CamBlankOrder)).one()
            assert (order.disc_count, order.note, order.created_by_id is not None) == (1, "6*2.5 zr", True)

        status, headers, html = client.post(
            f"/discs/orders/{order.id}/cancel", {"off": "", "note": ""},
            headers={"HX-Request": "true", "HX-Target": "dz-work"},
        )
        assert status == 200
        assert "скасовано" in json.loads(headers["hx-trigger"])["toast"]["message"]
        with factory() as s:
            assert len(pending_blanks(s)) == 3

    def test_cancel_from_the_toast_asks_the_page_to_refresh(self, app_db):  # noqa: F811
        """«Скасувати» в тості шле POST без цілі — відповідь 204 і подія
        `dz-work-refresh`, інакше робоча зона лишилась би старою."""
        app, factory = app_db
        ids = _seed_app(factory)
        with factory() as s:
            order = disc_orders.create_order(s, ids=[ids["ZR/18/a"]])
            s.commit()
            order_id = order.id
        status, headers, _ = _client(app).post(f"/discs/orders/{order_id}/cancel", {}, headers={"HX-Request": "true"})
        assert status == 204
        assert json.loads(headers["hx-trigger"])["dz-work-refresh"] is True

    def test_send_without_a_warehouse_is_refused_and_says_why(self, app_db):  # noqa: F811
        app, factory = app_db
        ids = _seed_app(factory)
        status, headers, html = _client(app).post(
            "/discs/order", {"ids": str(ids["ZR/18/a"]), "via": "telegram"},
            headers={"HX-Request": "true", "HX-Target": "dz-work"},
        )
        assert status == 200
        assert json.loads(headers["hx-trigger"])["toast"]["kind"] == "error"
        with factory() as s:
            assert s.scalar(select(CamBlankOrder)) is None
            assert len(pending_blanks(s)) == 3

    def test_basket_fragment_follows_the_checkboxes(self, app_db):  # noqa: F811
        app, factory = app_db
        ids = _seed_app(factory)
        status, _, html = _client(app).post("/discs/basket", {"ids": f"{ids['ZR/18/a']},{ids['ZR/18/b']}", "note": "фрези"})
        assert status == 200
        assert 'id="dz-live-sum"' in html and 'id="dz-live-acts"' in html
        assert "mono-a2-18(х2)" in html and "фрези" in html
        assert "1 лишиться в списку" in html

    def test_other_tabs_render_as_fragments(self, app_db):  # noqa: F811
        app, factory = app_db
        _seed_app(factory)
        client = _client(app)
        status, _, html = client.get("/discs/all?st=wait&q=pmma")
        assert status == 200 and 'id="dz-all"' in html and "pmma" in html
        status, _, html = client.get("/discs/orders")
        assert status == 200 and "Замовлень ще не було" in html
        status, _, html = client.get("/discs/footer")
        assert status == 200 and "cam-bl" in html

    def test_settings_no_longer_have_the_blanks_section(self, app_db):  # noqa: F811
        app, factory = app_db
        status, _, html = _client(app, ADMIN).get("/settings")
        assert status == 200
        assert 'data-sec="blanks"' not in html
