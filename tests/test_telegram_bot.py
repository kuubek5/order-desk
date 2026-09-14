"""Двосторонній Telegram-бот: розбір оновлень, фільтр чату, переходи, черга.

Головне, що тут стережеться:
* чужий чат не отримує НІЧОГО — навіть відповіді на кнопку;
* сповіщення — на ПЕРЕХІД, підтверджений двома кадрами, і рестарт не шле
  його вдруге;
* переходи печі й Sisma перевіряються на СПРАВЖНІХ кадрах (tests/fixtures),
  прогнаних тим самим конвеєром, що в цеху, а не на станах, зібраних руками;
* черга не губить повідомлення мовчки й не шле застаріле.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import business_today
from app.db import Base
from app.machine_sisma import read_sisma
from app.models import Furnace, Order, TelegramOutbox, TelegramWatch
from app.services import furnace as furnace_service
from app.services import machines as machine_service
from app.services import telegram as tg
from app.services import telegram_bot as bot
from app.services.telegram import ApiResult

FIXTURES = Path(__file__).parent / "fixtures"
CHAT = "555"


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _session():
    """Сесія на свіжій базі, де власник бота — CHAT (як після «Прив'язати чат»)."""
    from app.settings_store import set_setting

    db = Session(_database())
    set_setting(db, "telegram_chat_id", CHAT)
    db.commit()
    return db


@pytest.fixture(autouse=True)
def _clean_state():
    bot.reset_for_tests()
    furnace_service.reset_state_for_tests()
    machine_service.reset_state_for_tests()
    yield
    bot.reset_for_tests()
    furnace_service.reset_state_for_tests()
    machine_service.reset_state_for_tests()


def _message(chat_id, text="/start", *, chat_type="private", date=None):
    return {
        "update_id": 10,
        "message": {
            "message_id": 7,
            "date": date if date is not None else int(datetime.now().timestamp()),
            "chat": {"id": int(chat_id), "type": chat_type},
            "text": text,
        },
    }


def _press(chat_id, data="v:furnaces", *, message_id=42):
    return {
        "update_id": 11,
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": int(chat_id), "type": "private"}},
        },
    }


# ── Фільтр чату й розбір ────────────────────────────────────────────────────


def test_foreign_chat_gets_no_answer_at_all():
    """Сторонній, що знайшов бота, не дізнається навіть, що бот живий:
    ні меню, ні «доступу немає», ні answerCallbackQuery на кнопку."""
    with _session() as db:
        assert bot.handle_update(db, _message("999"), chat_id=CHAT) == []
        assert bot.handle_update(db, _press("999"), chat_id=CHAT) == []


def test_any_text_from_owner_sends_the_menu_as_a_new_message():
    with _session() as db:
        actions = bot.handle_update(db, _message(CHAT, "привіт"), chat_id=CHAT)
    assert [a.method for a in actions] == ["sendMessage"]
    payload = actions[0].payload
    assert payload["chat_id"] == CHAT
    assert payload["parse_mode"] == "HTML"
    assert payload["text"].startswith("<b>🏭 KMill</b>")
    assert "станом на" in payload["text"]
    buttons = [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert {"v:furnaces", "v:orders", "v:machines", "v:sisma"} <= set(buttons)


def test_button_edits_the_same_message_not_a_new_one():
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "v:orders", message_id=42), chat_id=CHAT)
    assert [a.method for a in actions] == ["answerCallbackQuery", "editMessageText"]
    edit = actions[1].payload
    assert edit["message_id"] == 42
    assert "Роботи · сьогодні" in edit["text"]
    # Під не-головним видом є дорога назад.
    buttons = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert "v:home" in buttons and "v:orders" in buttons


def test_unknown_button_only_stops_the_spinner():
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "rm -rf"), chat_id=CHAT)
    assert [a.method for a in actions] == ["answerCallbackQuery"]


def test_old_message_after_downtime_gets_no_menu():
    """ПК стояв уночі, Рома писав — вранці на нього не падає пачка меню."""
    old = int((datetime.now() - timedelta(hours=2)).timestamp())
    with _session() as db:
        assert bot.handle_update(db, _message(CHAT, date=old), chat_id=CHAT) == []


def test_private_chat_is_remembered_for_binding_even_if_foreign():
    """«Прив'язати чат», поки слухач працює, бере останній приватний чат."""
    with _session() as db:
        bot.handle_update(db, _message("777"), chat_id=CHAT)
    assert bot.last_private_chat() == "777"


def test_group_chat_is_not_remembered_for_binding():
    with _session() as db:
        bot.handle_update(db, _message("-100", chat_type="group"), chat_id=CHAT)
    assert bot.last_private_chat() is None


def test_not_modified_edit_is_not_an_error():
    action = bot.Action("editMessageText", {})
    result = ApiResult(False, 400, None, "HTTP 400 Bad Request: message is not modified")
    assert bot._benign_failure(action, result) is True
    assert bot._benign_failure(bot.Action("sendMessage", {}), result) is False


# ── Тексти меню ─────────────────────────────────────────────────────────────


def _order(db, **fields):
    today_tab = business_today().strftime("%d.%m.%y")
    order = Order(sheet_tab=today_tab, status="нове", **fields)
    db.add(order)
    return order


def test_orders_view_counts_readiness_like_the_queue_chips():
    with _session() as db:
        _order(db, source="lab", job_code="P:/a", quantity="2")  # можна брати
        _order(db, source="lab", job_code="P:/b", sum3d_id="12-01-45", quantity="3")  # в роботі
        _order(db, source="lab", quantity="1")  # не готово
        _order(db, source="email", client_name="Клініка", quantity="4")  # можна брати
        _order(db, source="sheet_client", sum3d_id="13-00-00", quantity="1")  # в роботі
        # Архівна й учорашня — не сьогоднішня вкладка.
        gone = _order(db, source="lab", job_code="P:/c", quantity="9")
        gone.archived_at = datetime.now()
        old = Order(source="lab", job_code="P:/d", sheet_tab="01.01.20", status="нове")
        db.add(old)
        db.commit()
        text = bot.orders_text(db)

    assert "5 робіт, 11 од." in text
    lab, clients = text.split("<b>Файли (клієнти)</b>")
    assert "<b>Лабораторія</b> — 3 роботи, 6 од." in lab
    assert "можна брати: <b>1</b>" in lab and "в роботі: 1" in lab and "не готово: 1" in lab
    assert "2 роботи, 5 од." in clients
    assert "можна брати: <b>1</b>" in clients and "в роботі: 1" in clients
    # Клієнтські «не готовими» не бувають — вічного нуля немає.
    assert "не готово" not in clients


def test_client_name_is_escaped_for_html():
    card = machine_service.MachineCard(
        target=machine_service.MachineTarget(name="250i <new>", host="10.0.0.1"),
        state=None,
    )
    card.orders = [Order(source="email", work_order_no="24122", client_name="Сміт & <Ко>")]
    label = bot._order_label(card)
    assert label == "наряд 24122 · Сміт &amp; &lt;Ко&gt;"
    assert "&lt;new&gt;" in bot._machine_line(card)


def test_one_broken_source_does_not_kill_the_menu(monkeypatch):
    def boom(_db):
        raise RuntimeError("впало")

    monkeypatch.setattr(bot, "_furnaces_summary", boom)
    with _session() as db:
        text = bot.render(db, "home")
    assert "Пічки: не вдалось прочитати" in text
    assert "Сьогодні" in text  # решта меню на місці


def test_plural_forms():
    assert bot._works(1) == "1 робота"
    assert bot._works(3) == "3 роботи"
    assert bot._works(5) == "5 робіт"
    assert bot._works(12) == "12 робіт"
    assert bot._works(22) == "22 роботи"


# ── Переходи ────────────────────────────────────────────────────────────────

T0 = datetime(2026, 9, 10, 17, 0, 0)


def test_first_observation_is_a_baseline_not_news():
    with _session() as db:
        assert bot.observe(db, "furnace:x", "RUN", T0) is None
        db.commit()
        assert db.get(TelegramWatch, "furnace:x").state == "RUN"


def test_new_state_needs_two_frames_apart():
    with _session() as db:
        bot.observe(db, "k", "WAIT", T0)
        assert bot.observe(db, "k", "RUN", T0 + timedelta(seconds=6)) is None
        # Другий кадр, але надто близько — ще не підтверджено.
        assert bot.observe(db, "k", "RUN", T0 + timedelta(seconds=9)) is None
        transition = bot.observe(db, "k", "RUN", T0 + timedelta(seconds=18))
    assert transition is not None
    assert (transition.old, transition.new) == ("WAIT", "RUN")
    assert transition.at == T0 + timedelta(seconds=6)


def test_single_frame_flicker_is_not_a_transition():
    with _session() as db:
        bot.observe(db, "k", "WAIT", T0)
        bot.observe(db, "k", "RUN", T0 + timedelta(seconds=6))
        assert bot.observe(db, "k", "WAIT", T0 + timedelta(seconds=12)) is None
        assert bot.observe(db, "k", "RUN", T0 + timedelta(seconds=18)) is None  # знову з нуля
        assert db.get(TelegramWatch, "k").state == "WAIT"


def test_long_gap_rebaselines_silently():
    """Застосунок стояв годину: коли піч закрилась — невідомо, і «закрилась»
    про цикл, що йде давно, було б неправдою. Мовчимо."""
    with _session() as db:
        bot.observe(db, "k", "WAIT", T0)
        later = T0 + timedelta(hours=1)
        assert bot.observe(db, "k", "RUN", later) is None
        assert bot.observe(db, "k", "RUN", later + timedelta(seconds=15)) is None
        assert db.get(TelegramWatch, "k").state == "RUN"


def test_restart_does_not_resend_a_known_state():
    engine = _database()
    with Session(engine) as db:
        bot.observe(db, "k", "WAIT", T0)
        bot.observe(db, "k", "RUN", T0 + timedelta(seconds=6))
        assert bot.observe(db, "k", "RUN", T0 + timedelta(seconds=18)) is not None
        db.commit()
    bot.reset_for_tests()  # «рестарт»: пам'ять процесу порожня, база ні
    with Session(engine) as db:
        assert bot.observe(db, "k", "RUN", T0 + timedelta(seconds=30)) is None
        assert bot.observe(db, "k", "RUN", T0 + timedelta(seconds=45)) is None


# ── Переходи на справжніх кадрах ────────────────────────────────────────────


def _furnace_frame(name):
    return Image.open(FIXTURES / "furnace" / f"{name}.png").convert("RGB")


def _enable_bot(db):
    from app.settings_store import set_setting

    set_setting(db, "telegram_bot_enabled", "1")
    set_setting(db, "telegram_bot_token", "123:ABC")
    set_setting(db, "telegram_chat_id", CHAT)
    db.commit()


def test_furnace_close_and_open_on_real_frames(monkeypatch, tmp_path):
    """wait.png → run.png → wait.png через той самий poll_target, що в цеху:
    голосування статусу, captured_at, has_data — усе справжнє."""
    monkeypatch.setattr(furnace_service, "frames_root", lambda: tmp_path)
    base = datetime.now().replace(microsecond=0)
    with _session() as db:
        db.add(Furnace(name="Піч 1", host="192.168.1.76", port=5900, enabled=True,
                       sort_order=0, created_at=base))
        db.commit()
        target = furnace_service.configured_targets(db)[0]

        def frame_at(seconds, name):
            furnace_service.poll_target(
                db, target, None, now=base + timedelta(seconds=seconds), frame=_furnace_frame(name)
            )
            return bot.watch_tick(db, base + timedelta(seconds=seconds))

        assert frame_at(0, "wait") == 0  # точка відліку
        assert frame_at(6, "run") == 0  # один кадр — ще не факт
        assert frame_at(18, "run") == 1
        assert frame_at(24, "run") == 0  # той самий стан — тиша
        db.commit()
        closed = db.scalars(select(TelegramOutbox)).one()
        assert closed.kind == "furnace_closed"
        assert "Піч 1" in closed.text and "закрилась" in closed.text
        assert "Відкриється ≈" in closed.text  # run.png має залишок програми
        assert closed.text.startswith("🏭 KMill")

        assert frame_at(30, "wait") == 0
        assert frame_at(42, "wait") == 1
        db.commit()
        rows = list(db.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))
        assert [row.kind for row in rows] == ["furnace_closed", "furnace_open"]
        # Висновок «можна відкривати» несе факт із табло (wait.png — 40 °C).
        assert "На табло зараз 40°" in rows[1].text


def test_unreadable_furnace_frame_is_not_an_observation(monkeypatch, tmp_path):
    """Кадр, з якого статус не проголосувався, не рухає стан нікуди."""
    monkeypatch.setattr(furnace_service, "frames_root", lambda: tmp_path)
    base = datetime.now().replace(microsecond=0)
    with _session() as db:
        db.add(Furnace(name="Піч 1", host="192.168.1.76", port=5900, enabled=True,
                       sort_order=0, created_at=base))
        db.commit()
        target = furnace_service.configured_targets(db)[0]
        furnace_service.poll_target(db, target, None, now=base, frame=_furnace_frame("wait"))
        bot.watch_tick(db, base)
        blank = Image.new("RGB", (800, 600), (0, 0, 0))
        for seconds in (6, 18, 30):
            furnace_service.poll_target(
                db, target, None, now=base + timedelta(seconds=seconds), frame=blank
            )
            assert bot.watch_tick(db, base + timedelta(seconds=seconds)) == 0
        assert db.get(TelegramWatch, f"furnace:{target.key}").state == "WAIT"


def _sisma_card(name, at):
    """Стан принтера з кадру — тими самими полями, що пише machines.poll_target."""
    frame = Image.open(FIXTURES / f"{name}.png").convert("RGB")
    reading = read_sisma(frame)
    target = machine_service.MachineTarget(name="SISMA", host="10.0.0.50")
    state = machine_service.MachineState(target=target, frame_at=at)
    state.is_sisma = True
    state.percent = reading.percent
    state.percent_at = at
    state.completed = reading.finished
    state.layer = reading.layer
    state.layers_total = reading.layers_total
    state.started_at = reading.started_at
    state.ends_at = reading.ends_at
    state.lasing = reading.lasing
    return machine_service.MachineCard(target=target, state=state, now=at)


def test_sisma_print_finished_on_real_frames(monkeypatch):
    frames = []
    monkeypatch.setattr(machine_service, "snapshot", lambda _db: frames)
    base = datetime.now().replace(microsecond=0)
    with _session() as db:
        def tick(seconds, name):
            frames[:] = [_sisma_card(name, base + timedelta(seconds=seconds))]
            return bot.watch_tick(db, base + timedelta(seconds=seconds))

        assert tick(0, "sisma_printing_250") == 0
        assert tick(6, "sisma_recoating_253") == 0  # пауза між шарами — теж друк
        assert tick(12, "sisma_report_dialog") == 0
        assert tick(24, "sisma_report_dialog") == 1
        assert tick(36, "sisma_report_dialog") == 0
        db.commit()
        row = db.scalars(select(TelegramOutbox)).one()
    assert row.kind == "sisma_done"
    assert "друк закінчився" in row.text


def test_sisma_report_after_idle_is_not_a_finish(monkeypatch):
    """Звіт, що висить після простою (оператор не закрив вікно, застосунок
    перезапустили), — не «щойно закінчила». Лише друк → звіт."""
    frames = []
    monkeypatch.setattr(machine_service, "snapshot", lambda _db: frames)
    base = datetime.now().replace(microsecond=0)
    with _session() as db:
        for seconds, name in ((0, "sisma_idle"), (12, "sisma_report_dialog"), (24, "sisma_report_dialog")):
            frames[:] = [_sisma_card(name, base + timedelta(seconds=seconds))]
            assert bot.watch_tick(db, base + timedelta(seconds=seconds)) == 0


# ── Черга відправки ─────────────────────────────────────────────────────────


def _queue(db, key="k1", *, now=T0, ttl=timedelta(hours=1)):
    assert bot.enqueue(db, dedup_key=key, kind="furnace_open", text="t", now=now, ttl=ttl)
    db.commit()


def test_same_event_is_queued_once():
    with _session() as db:
        _queue(db)
        assert bot.enqueue(db, dedup_key="k1", kind="x", text="y", now=T0, ttl=timedelta(hours=1)) is False


def test_outbox_delivers_and_marks_sent():
    sent = []
    with _session() as db:
        _queue(db)
        assert bot.flush_outbox(db, lambda chat, text: sent.append(text) or ApiResult(True, 200), T0) == 1
        row = db.scalars(select(TelegramOutbox)).one()
        assert row.sent_at == T0 and row.attempts == 1
        # Відправлене вдруге не йде.
        assert bot.flush_outbox(db, lambda chat, text: sent.append(text) or ApiResult(True, 200), T0) == 0
    assert sent == ["t"]


def test_failed_send_waits_and_then_retries():
    with _session() as db:
        _queue(db)
        fail = ApiResult(False, 502, None, "HTTP 502 Bad Gateway")
        assert bot.flush_outbox(db, lambda _c, _t: fail, T0) == 0
        row = db.scalars(select(TelegramOutbox)).one()
        assert row.last_error == "HTTP 502 Bad Gateway"
        assert row.next_attempt_at == T0 + timedelta(seconds=30)

        calls = []
        ok = lambda _c, t: calls.append(t) or ApiResult(True, 200)  # noqa: E731
        assert bot.flush_outbox(db, ok, T0 + timedelta(seconds=10)) == 0  # ще рано
        assert calls == []
        assert bot.flush_outbox(db, ok, T0 + timedelta(seconds=31)) == 1
        assert row.sent_at is not None and row.last_error is None


def test_network_down_stops_the_batch():
    calls = []

    def down(chat, text):
        calls.append(text)
        return ApiResult(False, None, None, "мережа: timeout")

    with _session() as db:
        _queue(db, "a")
        _queue(db, "b")
        bot.flush_outbox(db, down, T0)
    assert len(calls) == 1


def test_stale_message_is_written_off_not_sent():
    with _session() as db:
        _queue(db, ttl=timedelta(hours=1))
        called = []
        bot.flush_outbox(db, lambda _c, t: called.append(t) or ApiResult(True, 200), T0 + timedelta(hours=2))
        row = db.scalars(select(TelegramOutbox)).one()
    assert called == []
    assert row.gave_up_at is not None and "прострочено" in row.last_error


def test_outbox_summary_shows_failures():
    with _session() as db:
        _queue(db)
        bot.flush_outbox(db, lambda _c, _t: ApiResult(False, 502, None, "HTTP 502 Bad Gateway"), T0)
        summary = bot.outbox_summary(db)
    assert summary["pending"] == 1
    assert summary["last_error"] == "HTTP 502 Bad Gateway"


def test_outbound_tick_is_silent_when_bot_disabled(monkeypatch):
    monkeypatch.setattr(tg, "_new_session", lambda: pytest.fail("мережа при вимкненому боті"))
    with _session() as db:
        _queue(db)
        bot.outbound_tick(db, flush_feedback=False, now=T0)
        assert db.scalars(select(TelegramOutbox)).one().sent_at is None


def test_outbound_tick_sends_when_enabled(monkeypatch):
    posted = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {}}

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            posted.append((url, json))
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(tg, "_new_session", lambda: FakeSession())
    with _session() as db:
        _enable_bot(db)
        _queue(db, now=datetime.now())
        bot.outbound_tick(db, flush_feedback=False)
        assert db.scalars(select(TelegramOutbox)).one().sent_at is not None
    assert posted[0][0].endswith("/sendMessage")
    assert posted[0][1]["chat_id"] == CHAT and posted[0][1]["parse_mode"] == "HTML"


# ── API і токен ─────────────────────────────────────────────────────────────


def test_api_call_never_leaks_the_token():
    class Boom:
        def post(self, url, json=None, timeout=None):
            raise ConnectionError(f"failed {url}")

    result = tg.api_call(Boom(), "123:SECRET", "getUpdates")
    assert result.ok is False and result.status is None
    assert "SECRET" not in result.error and "***" in result.error


def test_api_call_reports_telegram_description():
    class Resp:
        status_code = 409

        def json(self):
            return {"ok": False, "description": "Conflict: terminated by other getUpdates request"}

    class S:
        def post(self, url, json=None, timeout=None):
            return Resp()

    result = tg.api_call(S(), "123:ABC", "getUpdates")
    assert result.status == 409 and "Conflict" in result.error


def test_feedback_caption_carries_the_kmill_prefix():
    from app.models import Feedback

    caption = tg._build_caption(Feedback(id=5, kind="bug", text="скрол"))
    assert caption.startswith("🏭 KMill · 🐞 Баг")


# ── Налаштування: справжній рендер сторінки ─────────────────────────────────
# Контекст роута → шаблон → HTML. Ловить те, чого не бачать тести сервісу:
# зниклий ключ у контексті, помилку Jinja, пігулку не того тону.

from tests.asgi_client import MiniClient  # noqa: E402
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: E402,F401 — фікстура


def _feedback_page(app) -> str:
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/settings/feedback")
    assert status == 200, status
    return html.split("Бот: меню й сповіщення</h3>", 1)[1].split("</div>", 1)[0]


def test_settings_shows_bot_disabled_grey(app_db):  # noqa: F811
    app, _ = app_db
    assert "stand-state-none\">вимкнено" in _feedback_page(app)


def test_settings_turns_green_only_from_a_real_poll(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        _enable_bot(db)
    # Вимикач стоїть, але жодного вдалого опитування — не зелений.
    assert "stand-state-ok" not in _feedback_page(app)
    bot._set_status(listening=True, since=datetime(2026, 9, 10, 9, 5), last_ok_at=datetime(2026, 9, 10, 9, 6, 1))
    html = _feedback_page(app)
    assert "stand-state-ok\">слухає з 09:05" in html
    assert "09:06:01" in html


def test_settings_explains_webhook_and_conflict(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        _enable_bot(db)
    bot._set_status(webhook_host="hooks.example.com")
    html = _feedback_page(app)
    assert "stand-state-warn" in html and "hooks.example.com" in html
    bot._set_status(webhook_host=None, conflict=True)
    assert "409" in _feedback_page(app)


def test_settings_save_toggles_the_bot(app_db):  # noqa: F811
    app, session_factory = app_db
    from app.settings_store import get_setting

    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, _ = client.post("/settings/feedback", {"telegram_bot_enabled": "1", "telegram_chat_id": CHAT})
    assert status in (302, 303)
    with session_factory() as db:
        assert get_setting(db, "telegram_bot_enabled") == "1"
    client.post("/settings/feedback", {"telegram_chat_id": CHAT})
    with session_factory() as db:
        assert get_setting(db, "telegram_bot_enabled") == ""


def test_prune_keeps_pending_and_fresh_rows():
    with _session() as db:
        _queue(db, "old-sent")
        _queue(db, "pending")
        _queue(db, "fresh-sent")
        rows = {row.dedup_key: row for row in db.scalars(select(TelegramOutbox))}
        rows["old-sent"].sent_at = T0 - timedelta(days=40)
        rows["fresh-sent"].sent_at = T0 - timedelta(days=1)
        db.commit()
        assert bot.prune_outbox(db, T0) == 1
        left = {row.dedup_key for row in db.scalars(select(TelegramOutbox))}
    assert left == {"pending", "fresh-sent"}


# ── Цикл слухача з підробленим Telegram ─────────────────────────────────────


class _InstantEvent:
    """Event, чиє `wait` не спить: цикл слухача проганяється за мілісекунди."""

    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, _timeout=None):
        return self.stopped


def test_offset_belongs_to_the_bot_not_to_the_setting():
    with _session() as db:
        bot._save_offset(db, "111:AAA", 500)
        assert bot._load_offset(db, "111:AAA") == 500
        # Новий бот: чужий offset = жодного (інакше 500 сховав би його
        # повідомлення з меншими номерами).
        assert bot._load_offset(db, "222:BBB") is None


def test_listener_answers_owner_then_survives_token_change(monkeypatch):
    engine = _database()

    def factory():
        return Session(engine, expire_on_commit=False)

    with factory() as db:
        _enable_bot(db)  # токен 123:ABC
        bot._save_offset(db, "123:ABC", 100)

    stop = _InstantEvent()
    calls: list[tuple[str, str, dict]] = []
    script = {
        # Бот 123: одне повідомлення від Роми й одне від стороннього.
        ("123", "getUpdates"): [
            ApiResult(True, 200, [
                dict(_message(CHAT, "меню"), update_id=100),
                dict(_message("999", "hi"), update_id=101),
            ]),
        ],
        # Бот 777 (новий токен): хвіст, що накопичився до нас, — пропустити.
        ("777", "getUpdates"): [
            ApiResult(True, 200, [dict(_message(CHAT, "старе"), update_id=5)]),
        ],
    }

    def fake_call(session, token, method, payload=None, *, timeout=None):
        bot_id = token.split(":")[0]
        calls.append((bot_id, method, payload or {}))
        if method == "getWebhookInfo":
            return ApiResult(True, 200, {"url": ""})
        queue = script.get((bot_id, method))
        if queue:
            result = queue.pop(0)
            if bot_id == "123" and not queue:
                # Після першої пачки власник міняє токен у налаштуваннях.
                from app.settings_store import set_setting

                with factory() as db:
                    set_setting(db, "telegram_bot_token", "777:NEW")
                    db.commit()
            return result
        if bot_id == "777" and method == "getUpdates":
            stop.stopped = True  # другий бот уже слухає — досить
        return ApiResult(True, 200, [] if method == "getUpdates" else {})

    monkeypatch.setattr(bot, "_session_factory", lambda: factory)
    monkeypatch.setattr(tg, "_new_session", lambda: object())
    monkeypatch.setattr(tg, "api_call", fake_call)

    bot.inbound_worker(stop)

    sent = [(b, p) for b, m, p in calls if m == "sendMessage"]
    # Рівно одне меню — Ромі, через старий бот. Сторонньому — нічого, а
    # хвіст нового бота («старе») не виконано.
    assert len(sent) == 1 and sent[0][0] == "123" and sent[0][1]["chat_id"] == CHAT
    polls_123 = [p for b, m, p in calls if b == "123" and m == "getUpdates"]
    assert polls_123[0]["offset"] == 100
    first_777 = [p for b, m, p in calls if b == "777" and m == "getUpdates"][0]
    assert first_777["offset"] == -1
    # Вебхук перевірено для КОЖНОГО бота, а не раз на процес.
    assert {b for b, m, _ in calls if m == "getWebhookInfo"} == {"123", "777"}
    with factory() as db:
        assert bot._load_offset(db, "777:NEW") == 6


# ── Учасники й запрошення ───────────────────────────────────────────────────


def _stranger_start(chat_id, code, *, first="Оля", username="olya_log"):
    update = _message(chat_id, f"/start {code}")
    update["message"]["from"] = {"id": int(chat_id), "first_name": first, "username": username}
    return update


def test_invite_link_lets_exactly_one_person_in():
    from app.models import TelegramMember

    with _session() as db:
        invite = bot.new_invite(db, label="Оля, логіст")
        db.commit()
        actions = bot.handle_update(db, _stranger_start("700", invite.code))
        assert [a.method for a in actions] == ["sendMessage"]
        assert "Доступ до бота KuubMill відкрито" in actions[0].payload["text"]
        member = db.scalars(select(TelegramMember)).one()
        assert (member.chat_id, member.name, member.username, member.label) == ("700", "Оля", "olya_log", "Оля, логіст")
        assert invite.used_by_chat == "700"
        # Власник дізнається, хто зайшов, — посилання могли переслати.
        joined = db.scalars(select(TelegramOutbox).where(TelegramOutbox.kind == "member_joined")).one()
        assert joined.chat_id == CHAT and "Оля, логіст" in joined.text

        # Те саме посилання, переслане далі, нікого більше не впускає.
        assert bot.handle_update(db, _stranger_start("701", invite.code)) == []
        assert db.scalars(select(TelegramMember)).all() == [member]


def test_bad_expired_or_revoked_code_gets_silence():
    with _session() as db:
        expired = bot.new_invite(db, now=datetime.now() - timedelta(days=2))
        revoked = bot.new_invite(db)
        revoked.revoked_at = datetime.now()
        db.commit()
        for code in ("nonsense", expired.code, revoked.code):
            assert bot.handle_update(db, _stranger_start("702", code)) == []
        assert bot.find_recipient(db, "702") is None


def test_member_uses_the_menu_and_mutes_himself():
    with _session() as db:
        invite = bot.new_invite(db)
        db.commit()
        bot.handle_update(db, _stranger_start("700", invite.code))
        menu = bot.handle_update(db, _message("700", "меню"))
        assert menu[0].payload["chat_id"] == "700"

        actions = bot.handle_update(db, _press("700", "n:furnaces"))
        assert actions[0].payload["text"] == "Сповіщення вимкнено"
        buttons = [b["text"] for row in actions[1].payload["reply_markup"]["inline_keyboard"] for b in row]
        assert "🔕 Сповіщення: ні" in buttons
        assert "Пічки" in actions[1].payload["text"]  # лишились на тому ж виді
        assert bot.find_recipient(db, "700").notify is False


def test_owner_bell_lives_in_settings():
    with _session() as db:
        bot.handle_update(db, _press(CHAT, "n:home"))
        assert bot.owner_notify(db) is False
        bot.handle_update(db, _press(CHAT, "n:home"))
        assert bot.owner_notify(db) is True


def test_event_goes_to_everyone_who_did_not_mute():
    from app.models import TelegramMember

    with _session() as db:
        for chat, notify in (("700", True), ("701", False)):
            db.add(TelegramMember(chat_id=chat, joined_at=T0, notify=notify))
        db.commit()
        rows = bot.broadcast(db, event_key="furnace:x:WAIT>RUN:1", kind="furnace_closed",
                             text="t", now=T0, ttl=timedelta(hours=1))
        db.commit()
        chats = sorted(r.chat_id for r in db.scalars(select(TelegramOutbox)))
    assert rows == 2 and chats == [CHAT, "700"]


def test_removed_member_loses_access_and_pending_messages():
    from app.models import TelegramMember

    with _session() as db:
        member = TelegramMember(chat_id="700", joined_at=T0)
        db.add(member)
        db.commit()
        bot.broadcast(db, event_key="e1", kind="k", text="t", now=T0, ttl=timedelta(hours=1))
        db.commit()
        db.delete(member)
        db.commit()
        sent = []
        bot.flush_outbox(db, lambda chat, text: sent.append(chat) or ApiResult(True, 200), T0)
        gone = db.scalars(select(TelegramOutbox).where(TelegramOutbox.chat_id == "700")).one()
        assert sent == [CHAT]
        assert gone.gave_up_at is not None and "адресата прибрано" in gone.last_error
        assert bot.handle_update(db, _message("700", "меню")) == []


def test_blocked_bot_is_written_off_at_once():
    """403 «bot was blocked by the user» не лікується повтором."""
    with _session() as db:
        _queue(db)
        blocked = ApiResult(False, 403, None, "HTTP 403 Forbidden: bot was blocked by the user")
        bot.flush_outbox(db, lambda _c, _t: blocked, T0)
        row = db.scalars(select(TelegramOutbox)).one()
    assert row.gave_up_at is not None and row.next_attempt_at is None


def test_rows_waiting_for_retry_do_not_block_the_batch():
    with _session() as db:
        for n in range(bot.OUTBOX_BATCH + 5):
            _queue(db, f"wait-{n}", ttl=timedelta(hours=5))
        for row in db.scalars(select(TelegramOutbox)):
            row.next_attempt_at = T0 + timedelta(minutes=10)
        db.commit()
        _queue(db, "due", ttl=timedelta(hours=5))
        sent = []
        bot.flush_outbox(db, lambda _c, text: sent.append(text) or ApiResult(True, 200), T0)
        due = db.scalars(select(TelegramOutbox).where(TelegramOutbox.dedup_key == "due")).one()
    assert len(sent) == 1 and due.sent_at is not None


def test_settings_invite_flow_end_to_end(app_db):  # noqa: F811
    """Справжні роути й шаблон: створити посилання → воно на екрані з
    кнопкою копіювання → людина заходить → вона в списку → прибрати."""
    app, session_factory = app_db
    with session_factory() as db:
        _enable_bot(db)
    bot._usernames["123"] = "kmill_test_bot"
    client = MiniClient(app)
    client.login(*ADMIN)

    status, _, _ = client.post("/settings/feedback/invite", {"label": "Оля"})
    assert status == 303
    _, _, html = client.get("/settings/feedback")
    section = html.split('id="bot-members"', 1)[1]
    assert "Запрошення для «Оля»" in section
    assert 'data-copy="https://t.me/kmill_test_bot?start=' in section

    with session_factory() as db:
        code = bot.active_invites(db)[0].code
        bot.handle_update(db, _stranger_start("700", code, first="Ольга"))
        member_id = bot.list_members(db)[0].id
    _, _, html = client.get("/settings/feedback")
    section = html.split('id="bot-members"', 1)[1]
    assert "<b>Оля</b>" in section and "@olya_log" in section
    assert "Запрошення для" not in section  # використане зникло зі списку

    client.post(f"/settings/feedback/member/{member_id}/notify", {})
    with session_factory() as db:
        assert bot.find_recipient(db, "700").notify is False
    client.post(f"/settings/feedback/member/{member_id}/remove", {})
    with session_factory() as db:
        assert bot.find_recipient(db, "700") is None


def test_settings_revoke_kills_the_link(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        _enable_bot(db)
        invite = bot.new_invite(db)
        db.commit()
        invite_id, code = invite.id, invite.code
    client = MiniClient(app)
    client.login(*ADMIN)
    client.post(f"/settings/feedback/invite/{invite_id}/revoke", {})
    with session_factory() as db:
        assert bot.handle_update(db, _stranger_start("700", code)) == []


# ── Два меню: власник-адмін і учасник-оператор ──────────────────────────────


def _member(db, chat="700"):
    from app.models import TelegramMember

    db.add(TelegramMember(chat_id=chat, joined_at=T0))
    db.commit()


def _buttons(payload):
    return [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]


def test_operator_sees_only_furnaces_and_sisma():
    with _session() as db:
        _member(db)
        payload = bot.handle_update(db, _message("700", "меню"))[0].payload
    views = {b for b in _buttons(payload) if b.startswith("v:")}
    assert views == {"v:furnaces", "v:sisma", "v:home"}
    assert "Сьогодні" not in payload["text"] and "Верстати" not in payload["text"]
    assert "Пічки" in payload["text"]


def test_operator_cannot_open_admin_view_with_forged_button():
    """callback_data приходить від клієнта: підроблене «v:orders» від
    оператора не відкриває цифр робіт."""
    with _session() as db:
        _member(db)
        for forged in ("v:orders", "v:handout_y", "v:machines", "n:orders"):
            actions = bot.handle_update(db, _press("700", forged))
            assert [a.method for a in actions] == ["answerCallbackQuery"], forged


def test_admin_has_day_switch_on_orders_and_handout():
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "v:orders_y"))
    edit = actions[1].payload
    assert "Роботи · вчора" in edit["text"]
    texts = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert "✓ Вчора" in texts and "Сьогодні" in texts
    assert {"v:orders", "v:handout", "v:machines"} <= set(_buttons(edit))


def test_yesterday_counts_only_yesterday():
    with _session() as db:
        yesterday = (business_today() - timedelta(days=1)).strftime("%d.%m.%y")
        db.add(Order(source="lab", job_code="P:/y", quantity="4", sheet_tab=yesterday, status="нове"))
        _order(db, source="lab", job_code="P:/t", quantity="1")
        db.commit()
        assert "1 робота, 4 од." in bot.orders_yesterday_text(db)
        assert "1 робота, 1 од." in bot.orders_text(db)


def test_handout_day_counts_like_the_handout_header():
    yesterday = (business_today() - timedelta(days=1)).strftime("%d.%m.%y")
    older = (business_today() - timedelta(days=4)).strftime("%d.%m.%y")
    with _session() as db:
        for client, qty, status in (
            ("Клініка А", "2", "видано"),
            ("Клініка А", "1", "знайдено при видачі"),
            ("Клініка Б", "3", "відфрезеровано"),
        ):
            db.add(Order(source="email", client_name=client, quantity=qty, sheet_tab=yesterday, status=status))
        # Безіменний рядок таблиці — окрема група «Без імені», як на екрані.
        db.add(Order(source="sheet_client", quantity="1", sheet_tab=yesterday, status="нове"))
        # Лабораторні не видаються клієнтам — поза видачею.
        db.add(Order(source="lab", quantity="5", sheet_tab=yesterday, status="нове"))
        db.add(Order(source="email", client_name="Стара", quantity="1", sheet_tab=older, status="нове"))
        db.commit()
        stats = bot.handout_day(db, -1)
        text = bot.handout_yesterday_text(db)
    assert (stats.clients, stats.clients_done) == (3, 1)
    assert (stats.works, stats.works_done) == (4, 2)
    assert (stats.units, stats.units_done) == (7, 3)
    assert "Клієнтів видано: <b>1</b> з 3" in text and "Ще чекає: <b>2 роботи</b>" in text
    assert "раніших" not in text  # старий хвіст свідомо не показуємо


# ── «🩺 Стан системи» ───────────────────────────────────────────────────────
# Власник тисне кнопку (або пише «звіт») — у фоні збирається той самий «Звіт
# для розробника» і приходить двома новими повідомленнями: підсумок + файл.


class _Result:
    def __init__(self, name, ok, warn=False, detail=""):
        self.name, self.ok, self.warn, self.detail = name, ok, warn, detail


def _run_report(monkeypatch, db, update):
    """handle_update + дочекатись фонового потоку звіту. Повертає (дії, що пішло в API)."""
    import threading as _th

    calls = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    class FakeSession:
        def post(self, url, json=None, data=None, files=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], json or data, files))
            return FakeResp()

        def close(self):
            pass

    factory_engine = db.get_bind()
    monkeypatch.setattr(bot, "_session_factory", lambda: (lambda: Session(factory_engine)))
    monkeypatch.setattr(tg, "_new_session", lambda: FakeSession())
    monkeypatch.setattr(
        "app.services.support_report.recent_errors", lambda lines=None: ["2026-09-11 10:00:01 ERROR app: зламалось"]
    )
    bot.set_report_builder(lambda _db: ("ЗВІТ\nрядок", [_Result("Google", False, detail="немає доступу"), _Result("Пошта", True)]))
    try:
        actions = bot.handle_update(db, update)
        for t in _th.enumerate():
            if t.name == "kuubmill-tg-report":
                t.join(timeout=10)
    finally:
        bot.set_report_builder(None)
    return actions, calls


def _threaded_session():
    """Сесія на базі, яку можна читати й з фонового потоку звіту."""
    from tests.conftest import make_memory_engine

    return Session(make_memory_engine(check_same_thread=False))


def test_owner_gets_a_summary_and_the_full_report_as_a_file(monkeypatch):
    with _threaded_session() as db:
        _enable_bot(db)
        actions, calls = _run_report(monkeypatch, db, _press(CHAT, bot.REPORT_CALLBACK))
    assert [a.method for a in actions] == ["answerCallbackQuery"]
    assert "Збираю звіт" in actions[0].payload["text"]
    methods = [c[0] for c in calls]
    assert methods == ["sendMessage", "sendDocument"]
    summary = calls[0][1]["text"]
    assert "Самоперевірка: 1 з 2" in summary and "Google — немає доступу" in summary
    assert "Помилок у лозі за добу: 1" in summary
    filename, content, _ = calls[1][2]["document"]
    assert filename.startswith("kuubmill-zvit_") and content == "ЗВІТ\nрядок".encode("utf-8")
    assert calls[1][1]["chat_id"] == CHAT


def test_owner_can_ask_for_the_report_with_a_word(monkeypatch):
    with _threaded_session() as db:
        _enable_bot(db)
        actions, calls = _run_report(monkeypatch, db, _message(CHAT, "звіт"))
    assert "Збираю звіт" in actions[0].payload["text"]
    assert [c[0] for c in calls] == ["sendMessage", "sendDocument"]


def test_member_and_warehouse_never_get_the_report(monkeypatch):
    """У звіті стан усього ПК і лог — лише власнику, навіть з підробленою кнопкою."""
    from app.models import TelegramMember

    with _threaded_session() as db:
        _enable_bot(db)
        db.add(TelegramMember(chat_id="700", joined_at=T0))
        db.add(TelegramMember(chat_id="900", joined_at=T0, role=bot.ROLE_WAREHOUSE))
        db.commit()
        for chat in ("700", "900"):
            actions, calls = _run_report(monkeypatch, db, _press(chat, bot.REPORT_CALLBACK))
            assert [a.method for a in actions] == ["answerCallbackQuery"]
            assert "text" not in actions[0].payload
            assert calls == []
        buttons = [b["callback_data"] for row in bot.keyboard("home", admin=False)["inline_keyboard"] for b in row]
        assert bot.REPORT_CALLBACK not in buttons
        assert bot.REPORT_CALLBACK in [b["callback_data"] for row in bot.keyboard("home")["inline_keyboard"] for b in row]


def test_second_press_while_building_does_not_start_another(monkeypatch):
    bot.set_report_builder(lambda _db: ("x", []))
    assert bot._report_lock.acquire(blocking=False)
    try:
        assert bot.start_report(CHAT) is False
    finally:
        bot._report_lock.release()
        bot.set_report_builder(None)
    assert bot.start_report(CHAT) is None, "без зареєстрованого збирача — чесне «недоступно»"
