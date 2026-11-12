"""Бот: «📬 Пошта», «🧪 Лабораторія» і сповіщення про вимкнення ПК (08.11.26).

Що стережеться:
* «не взято в роботу» = ті самі «Вхідні», що в CRM, і лише статус «нове»:
  повернутий прийнятий лист роботу вже має; лист під винятком і СЛМ — не
  фрезерування, їх у «Пошті» немає зовсім (власник 10.11.26);
* «Пошта» — картинкою (варіант E), «Листи списком» — текст, і «Оновити»
  там лишає той самий список; картинка впала — текст, а не мовчання;
* «непрочитаний» = ще ніхто не відкрив у KuubMill (`seen_at`), рішення власника;
* «зроблено» в лабораторії = стоїть Sum3D (рішення власника), термінові першими;
* кнопка розгортає список у тому самому повідомленні, вдруге згортає;
* «зникало світло» кажемо лише з доказом Windows; без нього — «ПК вимикався».
"""

import pytest
from datetime import datetime, timedelta, timezone

from app.business_day import business_today
from app.models import Attachment, EmailMessage, Order
from app.services import power_watch as pw
from app.services import telegram_bot as bot
from app.services import telegram_views as tv
from tests.test_telegram_bot import CHAT, _member, _press, _session

NOW = datetime(2026, 11, 8, 10, 52)


@pytest.fixture(autouse=True)
def _legacy_text_bot(monkeypatch):
    """Ці тести стережуть СТАРИЙ бот (текст, окрема картинка пошти й верстатів):
    він лишився запасним шляхом — коли картки вимкнено в налаштуваннях або
    малювання впало. Нові картки — `tests/test_telegram_cards.py`."""
    monkeypatch.setattr(bot, "pictures_enabled", lambda db: False)


def _letter(db, uid, *, name, material=None, minutes_ago=30, seen=False, status="нове",
            files=(), quantity=None, **extra):
    email = EmailMessage(uid=uid, status=status, from_name=name, material_color_guess=material,
                         quantity_guess=quantity, received_at=NOW - timedelta(minutes=minutes_ago),
                         seen_at=NOW if seen else None, **extra)
    db.add(email)
    db.flush()
    for filename in files:
        db.add(Attachment(email_message_id=email.id, filename=filename, saved_path=f"C:/x/{filename}"))
    return email


# ── Пошта ───────────────────────────────────────────────────────────────────


def _mail(db):
    _letter(db, "1", name="Дент-Арт", material="моно А3", minutes_ago=135,
            files=["Job_11-Crown_cad.stl", "Job_21-Crown_cad.stl"], quantity="9")
    _letter(db, "2", name="Smile Lab", material="katana a2", seen=True, quantity="6")
    _letter(db, "3", name="Вітадент", material="пмма A2", quantity="4")
    _letter(db, "4", name="Без матеріалу")
    # Повернутий прийнятий лист — у «Вхідних», але робота вже є.
    _letter(db, "5", name="Повернутий", material="моно А3", status="прийнято", reopened_at=NOW)
    # Під правилом-винятком: не фрезерування.
    _letter(db, "6", name="Друк", filter_category="3D-друк")
    # На погодженні — не в «Вхідних».
    _letter(db, "7", name="Чекає", material="моно А3", hold_at=NOW)
    db.commit()


def test_mail_groups_only_new_letters_in_the_inbox():
    with _session() as db:
        _mail(db)
        groups = tv.mail_groups(db)
    assert [x.name for x in groups["zr"]] == ["Дент-Арт", "Smile Lab"]
    assert [x.name for x in groups["pm"]] == ["Вітадент"]
    assert [x.name for x in groups["none"]] == ["Без матеріалу"]
    # Одиниці з назв STL важать більше за число з тексту листа.
    assert groups["zr"][0].units == 2 and groups["zr"][1].units == 6
    assert (groups["zr"][0].unread, groups["zr"][1].unread) == (True, False)


def test_mail_view_counts_and_expands_one_group():
    with _session() as db:
        _mail(db)
        closed = tv.mail_view(db, None, NOW)
        opened = tv.mail_view(db, "zr", NOW)
    assert "4 листи чекають · 12 од." in closed.text
    assert "⏳ найдовше чекає 2 год 15 хв · Дент-Арт" in closed.text
    assert "🆕 не відкрито 3" in closed.text
    # Число без листа не вигадуємо, а кажемо, скільки таких.
    assert "у 1 листі числа одиниць немає" in closed.text
    assert "🦷 Цирконій — <b>2</b> листи · 8 од." in closed.text
    assert "❔ Матеріал не розпізнано — <b>1</b> лист\n" in closed.text
    assert "<blockquote" not in closed.text
    # Не фрезерування в «Пошті» немає зовсім (власник 10.11.26).
    assert "Друк" not in closed.text and "фрезерування" not in closed.text
    assert "🆕 <b>Дент-Арт</b> · 08:37 · моно А3 · 2 од." in opened.text
    # Згорнути — лишитись у тексті: «v:mail» уже веде на картинку.
    zr = [b for row in opened.rows for b in row if "Цирконій" in b["text"]][0]
    assert zr == {"text": "▾ 🦷 Цирконій 2", "callback_data": "e:mail:list"}
    pm = [b for row in closed.rows for b in row if "ПММА" in b["text"]][0]
    assert pm["callback_data"] == "e:mail:pm"
    assert closed.rows[-1] == [{"text": "🖼 Картинкою", "callback_data": "v:mail"}]


def test_mail_view_all_lists_every_letter_oldest_first():
    with _session() as db:
        _mail(db)
        text = tv.mail_view(db, "all", NOW).text
    quote = text.split("<b>Усі листи</b>")[1]
    assert quote.index("Дент-Арт") < quote.index("Smile Lab")
    assert "<b>Без матеріалу</b> · 10:22 · число ?" in quote


def test_slm_is_not_milling_and_is_not_counted():
    with _session() as db:
        _letter(db, "1", name="Метал", material="СЛМ", quantity="5")
        _letter(db, "2", name="Цирконій", material="моно А3", quantity="2")
        db.commit()
        board = tv.mail_board(db, NOW)
        text = tv.mail_view(db, None, NOW).text
    assert (board.letters, board.units, board.unknown) == (1, 2, 0)
    assert [r.title for r in board.rows] == ["Цирконій"]
    assert "Метал" not in text and "СЛМ" not in text


def test_mail_board_rows_for_the_picture():
    with _session() as db:
        _mail(db)
        board = tv.mail_board(db, NOW)
    assert (board.letters, board.units, board.unknown, board.unread) == (4, 12, 1, 3)
    assert [(r.title, r.letters, r.units, r.units_known) for r in board.rows] == [
        ("Цирконій", 2, 8, True), ("ПММА", 1, 4, True), ("Без матеріалу", 1, 0, False),
    ]
    assert board.waited == timedelta(hours=2, minutes=15) and board.oldest_name == "Дент-Арт"


def test_mail_time_with_timezone_does_not_break_the_view():
    """`received_at` у базі буває з поясом; змішаний із місцевим часом він
    валив розділ (знайдено на dev-базі)."""
    with _session() as db:
        _letter(db, "1", name="З поясом", material="моно А3")
        db.flush()
        db.add(EmailMessage(uid="2", status="нове", from_name="UTC", material_color_guess="моно А2",
                            received_at=datetime(2026, 11, 8, 7, 0, tzinfo=timezone.utc)))
        db.commit()
        text = tv.mail_view(db, "zr", NOW).text
    assert "UTC" in text and "найдовше чекає" in text


def test_empty_mail_says_everything_is_taken():
    with _session() as db:
        assert "Усі листи взято в роботу" in tv.mail_view(db, None, NOW).text
        assert tv.mail_summary(db) == "📬 Пошта: усі листи взято в роботу"


# ── Лабораторія ─────────────────────────────────────────────────────────────


def _lab(db):
    tab = business_today().strftime("%d.%m.%y")
    rows = [
        dict(work_order_no="1", job_code="P:/1", sum3d_id="10-00-00", quantity="2"),
        dict(work_order_no="2", job_code="P:/2", sum3d_id="10-05-00", quantity="1"),
        dict(work_order_no="3", job_code="P:/3", quantity="3", material_color="моно А3", technician_name="Юля"),
        dict(work_order_no="4", job_code="P:/4", quantity="1", cam_comment="на швидку"),
        dict(work_order_no="5", quantity="2"),
    ]
    for n, row in enumerate(rows, start=7):
        db.add(Order(source="lab", sheet_tab=tab, row_number=n, status="нове", **row))
    db.add(Order(source="sheet_client", sheet_tab=tab, row_number=20, status="нове",
                 client_name="Клієнт", quantity="5", material_color="моно А2"))
    db.commit()


def test_lab_counts_done_by_sum3d_and_puts_rush_first():
    with _session() as db:
        _lab(db)
        closed = tv.lab_view(db, 0, None, NOW)
        opened = tv.lab_view(db, 0, "take", NOW)
    assert "Зайшло <b>5</b> · зроблено <b>2</b> · лишається <b>3</b>" in closed.text
    assert "одиниць: 9 · зроблено 3 · лишається 6" in closed.text
    assert "✅ Можна брати — <b>2</b>" in closed.text and "⏳ Технік ще не здав — <b>1</b>" in closed.text
    quote = opened.text.split("<blockquote>")[1]
    assert quote.index("🔥 <code>4</code>") < quote.index("<code>3</code> · моно А3 · 3 од. · Юля")
    assert "<b>на швидку</b>" in quote
    assert [b["callback_data"] for row in opened.rows for b in row] == ["v:lab", "e:lab:wait"]


def test_lab_summary_for_home():
    with _session() as db:
        assert tv.lab_summary(db) == "🧪 Лабораторія: робіт сьогодні ще немає"
        _lab(db)
        assert tv.lab_summary(db) == "🧪 Лабораторія: зроблено <b>2</b> з 5 · лишається 3"


# ── Меню бота ───────────────────────────────────────────────────────────────


def test_expand_button_edits_the_same_message():
    with _session() as db:
        _mail(db)
        actions = bot.handle_update(db, _press(CHAT, "e:mail:pm"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "editMessageText"]
    payload = actions[1].payload
    assert "<b>Вітадент</b>" in payload["text"] and payload["message_id"] == 42
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows[0] == [{"text": "📋 Усі листи 4", "callback_data": "e:mail:all"}]
    assert any(b["text"] == "▾ ◻️ ПММА 1" for b in rows[1])


def _png_size(data: bytes) -> tuple[int, int]:
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        return image.size


def test_mail_from_text_menu_sends_picture_then_deletes_old():
    from tests.test_telegram_board import _photo_press

    with _session() as db:
        _mail(db)
        actions = bot.handle_update(db, _press(CHAT, "v:mail"), now=NOW)
        refresh = bot.handle_update(db, _photo_press("v:mail"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "sendPhoto", "deleteMessage"]
    assert actions[2].after_ok
    photo = actions[1]
    assert _png_size(photo.files["photo"][1])[0] == 1080
    assert "📬 <b>4 листи чекають · 12 од.</b>\nу 1 листі числа одиниць немає" in photo.payload["caption"]
    buttons = [b["callback_data"] for row in photo.payload["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["e:mail:all", "v:mail", "v:home", "n:mail"]
    # «Оновити» на картинці — та сама картинка на місці.
    assert [a.method for a in refresh] == ["answerCallbackQuery", "editMessageMedia"]


def test_list_button_under_the_picture_sends_text_and_keeps_it_on_refresh():
    from tests.test_telegram_board import _photo_press

    with _session() as db:
        _mail(db)
        actions = bot.handle_update(db, _photo_press("e:mail:all"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "sendMessage", "deleteMessage"]
    payload = actions[1].payload
    assert "<b>Усі листи</b>" in payload["text"]
    buttons = [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert "e:mail:all" in buttons and "v:mail" in buttons  # «Картинкою»
    refresh = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row if b["text"] == "🔄 Оновити"]
    assert refresh == [{"text": "🔄 Оновити", "callback_data": "e:mail:all"}]


def test_empty_mail_picture_has_no_list_button():
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "v:mail"), now=NOW)
    photo = actions[1]
    assert "Усі листи взято в роботу" in photo.payload["caption"]
    buttons = [b["callback_data"] for row in photo.payload["reply_markup"]["inline_keyboard"] for b in row]
    assert "e:mail:all" not in buttons


def test_broken_mail_picture_falls_back_to_text(monkeypatch):
    from app.services import telegram_mail_picture

    def boom(*_a, **_k):
        raise RuntimeError("шрифт")

    monkeypatch.setattr(telegram_mail_picture, "render_png", boom)
    with _session() as db:
        _mail(db)
        actions = bot.handle_update(db, _press(CHAT, "v:mail"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "editMessageText"]
    assert "4 листи чекають" in actions[1].payload["text"]


def test_operator_cannot_expand_admin_sections():
    with _session() as db:
        _member(db)
        for forged in ("e:mail:zr", "e:mail:all", "v:mail", "e:lab:take", "v:lab_y"):
            actions = bot.handle_update(db, _press("700", forged), now=NOW)
            assert [a.method for a in actions] == ["answerCallbackQuery"], forged


def test_home_has_mail_and_lab_lines_and_buttons():
    with _session() as db:
        _mail(db)
        _lab(db)
        payload = bot.handle_update(db, _press(CHAT, "v:home"), now=NOW)[1].payload
    assert "📬 Пошта: <b>4</b> листи чекають · <b>12</b> од.\n      ⏳ найдовше" in payload["text"]
    assert "🧪 Лабораторія: зроблено <b>2</b> з 5" in payload["text"]
    buttons = {b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row}
    assert {"v:mail", "v:lab", "v:machines", "v:orders"} <= buttons


# ── Вимкнення ПК ────────────────────────────────────────────────────────────

ALIVE = datetime(2026, 11, 8, 3, 12)
BOOT = datetime(2026, 11, 8, 7, 44)
START = datetime(2026, 11, 8, 7, 45)


def test_power_loss_needs_the_windows_record():
    outage = pw.classify(last_alive=ALIVE, clean_exit=None, boot=BOOT, now=START, unexpected=True)
    assert outage.kind == pw.KIND_POWER and outage.back_at == BOOT
    assert pw.classify(last_alive=ALIVE, clean_exit=None, boot=BOOT, now=START,
                       unexpected=False).kind == pw.KIND_PC
    assert pw.classify(last_alive=ALIVE, clean_exit=None, boot=BOOT, now=START,
                       unexpected=None).kind == pw.KIND_PC_UNKNOWN


def test_app_restart_without_reboot():
    early = datetime(2026, 11, 7, 20, 0)
    clean = pw.classify(last_alive=ALIVE, clean_exit=ALIVE, boot=early, now=START, unexpected=None)
    crash = pw.classify(last_alive=ALIVE, clean_exit=None, boot=early, now=START, unexpected=None)
    assert (clean.kind, clean.notify) == (pw.KIND_APP, False)
    assert (crash.kind, crash.notify) == (pw.KIND_APP_CRASH, True)


def test_update_restart_is_not_a_crash():
    """Оновлення може добити програму без мітки виходу — це не аварія."""
    early = datetime(2026, 11, 7, 20, 0)
    outage = pw.classify(last_alive=ALIVE, clean_exit=None, boot=early, now=START, unexpected=None,
                         update_at=ALIVE + timedelta(seconds=30))
    assert (outage.kind, outage.notify) == (pw.KIND_APP, False)
    old_update = pw.classify(last_alive=ALIVE, clean_exit=None, boot=early, now=START, unexpected=None,
                             update_at=ALIVE - timedelta(days=1))
    assert old_update.kind == pw.KIND_APP_CRASH


def test_short_gap_and_first_start_are_silent():
    assert pw.classify(last_alive=None, clean_exit=None, boot=BOOT, now=START, unexpected=None) is None
    assert pw.classify(last_alive=START - timedelta(minutes=2), clean_exit=None, boot=BOOT,
                       now=START, unexpected=True) is None


def test_windows_log_time_is_read_as_utc():
    xml = "<Event><System><TimeCreated SystemTime='2026-11-08T05:44:10.1234567Z'/></System></Event>"
    local_alive = datetime(2026, 11, 8, 5, 44, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert pw.any_event_after(xml, local_alive) is True
    assert pw.any_event_after(xml, local_alive + timedelta(minutes=1)) is False
    assert pw.any_event_after("<Event/>", local_alive) is False


def test_message_and_home_line():
    outage = pw.Outage(pw.KIND_POWER, ALIVE, BOOT, START)
    text = pw.message(outage)
    assert "⚡ <b>ПК цеху був вимкнений аварійно</b>" in text
    assert "Зник: <b>сьогодні 03:12</b>" in text
    assert "перерва <b>4 год 32 хв</b>" in text and "Схоже, зникало світло." in text


def test_check_on_start_queues_one_message_and_shows_home_line(monkeypatch):
    from app.models import TelegramOutbox
    from app.settings_store import get_setting, set_setting

    monkeypatch.setattr(pw, "boot_time", lambda: BOOT)
    monkeypatch.setattr(pw, "unexpected_shutdown_since", lambda since: True)
    monkeypatch.setattr(bot, "bot_enabled", lambda db: True)
    with _session() as db:
        set_setting(db, pw.ALIVE_KEY, ALIVE.isoformat())
        db.commit()
        outage = pw.check_on_start(db, now=START)
        assert outage.kind == pw.KIND_POWER
        rows = db.query(TelegramOutbox).all()
        assert len(rows) == 1 and rows[0].chat_id == CHAT and "зникало світло" in rows[0].text
        # Мітка «живий» уже нова: повторний старт за хвилину нічого не шле.
        assert get_setting(db, pw.ALIVE_KEY) == START.isoformat(timespec="seconds")
        assert pw.check_on_start(db, now=START + timedelta(minutes=1)) is None
        assert db.query(TelegramOutbox).count() == 1
        assert pw.home_line(db, START + timedelta(hours=2)) == \
            "⚡ Світло зникало сьогодні 03:12–07:44 (4 год 32 хв)"
        assert pw.home_line(db, START + timedelta(hours=25)) == ""
