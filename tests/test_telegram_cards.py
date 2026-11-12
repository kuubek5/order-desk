"""Бот картками в стилі CRM (власник 12.11.26).

Старий текстовий бот стережуть `test_telegram_bot/_views/_board` (він лишився
запасним шляхом). Тут — новий режим: кожен екран приходить фото, перехід між
екранами — заміна фото на місці, кнопки ведуть туди, куди обіцяють, а збій
малювання повертає текст, а не мовчання.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from PIL import Image

from app.services import telegram_bot as bot
from app.services import telegram_cards as cards
from app.services import telegram_pictures as tp
from app.services import telegram_views as tv
from tests.test_telegram_bot import CHAT, _member, _message, _press, _session
from tests.test_telegram_views import NOW, _lab, _letter, _mail

ADMIN_VIEWS = ("home", "mail", "furnaces", "lab", "lab_y", "handout", "handout_y", "orders", "orders_y", "sisma")


@pytest.fixture(autouse=True)
def _clean_state():
    bot.reset_for_tests()
    yield
    bot.reset_for_tests()


def _buttons(action) -> list[str]:
    markup = action.payload.get("reply_markup") or {}
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def _size(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def _photo_of(action) -> bytes:
    key = "photo" if action.method == "sendPhoto" else "board"
    return action.files[key][1]


# ── Навігація ───────────────────────────────────────────────────────────────


def test_any_text_from_owner_gets_the_home_card():
    with _session() as db:
        actions = bot.handle_update(db, _message(CHAT), now=NOW)
    assert [a.method for a in actions] == ["sendPhoto"]
    assert _size(_photo_of(actions[0]))[0] == 1080
    buttons = _buttons(actions[0])
    for target in ("v:machines", "v:mail", "v:furnaces", "v:lab", "v:handout", "v:orders", "v:sisma",
                   bot.REPORT_CALLBACK, "v:home", "n:home"):
        assert target in buttons, target


@pytest.mark.parametrize("view", ADMIN_VIEWS)
def test_every_admin_view_is_a_card_with_refresh_and_menu(view):
    with _session() as db:
        _mail(db)
        _lab(db)
        # З текстового меню: нове фото, старе повідомлення прибирається ПІСЛЯ.
        actions = bot.handle_update(db, _press(CHAT, f"v:{view}"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "sendPhoto", "deleteMessage"]
    assert actions[2].after_ok
    w, h = _size(_photo_of(actions[1]))
    assert w == 1080 and 300 < h < 4000
    buttons = _buttons(actions[1])
    assert f"v:{view}" in buttons or any(b.startswith(f"p:{view}:") for b in buttons), "є «Оновити»"
    if view != "home":
        assert "v:home" in buttons
        assert not any(b.startswith("n:") for b in buttons), "дзвіночок лише на головному"
    assert len(actions[1].payload["caption"]) <= bot.CAPTION_LIMIT


def test_between_cards_the_photo_is_replaced_in_place():
    with _session() as db:
        update = _press(CHAT, "v:furnaces")
        update["callback_query"]["message"]["photo"] = [{"file_id": "x"}]
        actions = bot.handle_update(db, update, now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "editMessageMedia"]
    assert actions[1].payload["message_id"] == 42


def test_operator_gets_only_furnaces_and_sisma():
    with _session() as db:
        _member(db)
        home = bot.handle_update(db, _message("700"), now=NOW)
        assert [a.method for a in home] == ["sendPhoto"]
        buttons = _buttons(home[0])
        assert {"v:furnaces", "v:sisma"} <= set(buttons)
        assert not {"v:mail", "v:lab", "v:orders", "v:machines", bot.REPORT_CALLBACK} & set(buttons)
        # Підроблена кнопка чужого виду нічого не відкриває.
        forged = bot.handle_update(db, _press("700", "p:mail:zr.0"), now=NOW)
        assert [a.method for a in forged] == ["answerCallbackQuery"]


def test_bell_on_home_toggles_and_redraws_home():
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "n:home"), now=NOW)
        assert actions[0].payload["text"] == "Сповіщення вимкнено"
        assert actions[1].method == "sendPhoto"
        labels = [b["text"] for row in actions[1].payload["reply_markup"]["inline_keyboard"] for b in row]
        assert any(label.startswith("🔕") for label in labels)


def test_day_switch_on_lab_handout_and_orders():
    with _session() as db:
        for today, yesterday in (("lab", "lab_y"), ("handout", "handout_y"), ("orders", "orders_y")):
            buttons = _buttons(bot.handle_update(db, _press(CHAT, f"v:{yesterday}"), now=NOW)[1])
            assert {f"v:{today}", f"v:{yesterday}"} <= set(buttons)


# ── Пошта ───────────────────────────────────────────────────────────────────


def test_mail_card_has_group_buttons_and_text_list():
    with _session() as db:
        _mail(db)
        action = bot.handle_update(db, _press(CHAT, "v:mail"), now=NOW)[1]
    buttons = _buttons(action)
    assert {"p:mail:zr.0", "p:mail:pm.0", "p:mail:none.0"} <= set(buttons)
    assert f"e:mail:{tv.MAIL_ALL}" in buttons, "текстовий список лишився"
    assert "4" in action.payload["caption"]


def test_mail_group_button_filters_and_second_press_returns_to_all():
    with _session() as db:
        _mail(db)
        whole = bot.handle_update(db, _press(CHAT, "v:mail"), now=NOW)[1]
        only = bot.handle_update(db, _press(CHAT, "p:mail:pm.0"), now=NOW)[1]
    assert _size(_photo_of(only))[1] < _size(_photo_of(whole))[1], "у групі рядків менше"
    buttons = _buttons(only)
    assert "p:mail:.0" in buttons, "активна група веде назад до всіх"
    assert "p:mail:pm.0" in buttons, "«Оновити» лишає ту саму групу"


def test_mail_text_list_still_opens_as_text():
    with _session() as db:
        _mail(db)
        actions = bot.handle_update(db, _press(CHAT, f"e:mail:{tv.MAIL_ALL}"), now=NOW)
    assert actions[1].method == "editMessageText"
    assert "Дент-Арт" in actions[1].payload["text"]


def test_mail_pages_when_letters_do_not_fit():
    with _session() as db:
        for n in range(tp.MAIL_PER_PAGE + 5):
            _letter(db, f"m{n}", name=f"Клієнт {n}", material="моно А3", minutes_ago=5 + n, quantity="1")
        db.commit()
        first = bot.handle_update(db, _press(CHAT, "v:mail"), now=NOW)[1]
        second = bot.handle_update(db, _press(CHAT, "p:mail:.1"), now=NOW)[1]
        beyond = bot.handle_update(db, _press(CHAT, "p:mail:.9"), now=NOW)[1]
    assert "p:mail:.1" in _buttons(first)
    assert _size(_photo_of(second))[1] < _size(_photo_of(first))[1]
    assert _size(_photo_of(beyond)) == _size(_photo_of(second)), "сторінка за межею = остання"


def test_letters_newest_first_with_row_state():
    with _session() as db:
        _mail(db)
        letters = tv.mail_letters(db)
    assert all(a.received_at >= b.received_at for a, b in zip(letters, letters[1:])), "найновіші зверху"
    by_name = {x.name: x for x in letters}
    assert by_name["Дент-Арт"].group == "zr" and by_name["Дент-Арт"].shade == "A3"
    assert by_name["Вітадент"].group == "pm" and by_name["Без матеріалу"].group == "none"
    assert by_name["Дент-Арт"].unread and not by_name["Smile Lab"].unread


def test_mail_picture_never_carries_subject_or_body(monkeypatch):
    """Тема й текст листа на картинку не йдуть: у них бувають імена пацієнтів."""
    seen: list[str] = []
    real = tp.Canvas.text

    def spy(self, x, y, text, *args, **kwargs):
        seen.append(text)
        return real(self, x, y, text, *args, **kwargs)

    monkeypatch.setattr(tp.Canvas, "text", spy)
    with _session() as db:
        _letter(db, "s1", name="Клініка", material="моно А3", quantity="2",
                subject="Пацієнт Петренко коронка", body_text="Іваненко Марія, 2 одиниці")
        db.commit()
        cards.build(db, "mail", NOW, admin=True)
    drawn = " ".join(seen)
    assert "Клініка" in drawn
    for secret in ("Петренко", "Іваненко", "Марія", "Пацієнт"):
        assert secret not in drawn


# ── Лабораторія ─────────────────────────────────────────────────────────────


def test_lab_wait_button_opens_and_closes_the_list():
    with _session() as db:
        _lab(db)
        closed = bot.handle_update(db, _press(CHAT, "v:lab"), now=NOW)[1]
        opened = bot.handle_update(db, _press(CHAT, "p:lab:wait"), now=NOW)[1]
    assert "p:lab:wait" in _buttons(closed)
    assert _size(_photo_of(opened))[1] > _size(_photo_of(closed))[1]
    assert "v:lab" in _buttons(opened) and "p:lab:wait" in _buttons(opened), "закрити й оновити"


# ── Запасний шлях і вимикач ─────────────────────────────────────────────────


def test_broken_drawing_falls_back_to_the_text_view(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("шрифт зник")

    monkeypatch.setattr(tp, "furnaces_png", boom)
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "v:furnaces"), now=NOW)
    assert actions[1].method == "editMessageText"
    assert "Пічки" in actions[1].payload["text"]


def test_switch_off_returns_the_old_text_bot():
    from app.settings_store import set_setting

    with _session() as db:
        assert cards.enabled(db), "типово картки ввімкнено"
        set_setting(db, cards.SETTING_KEY, "0")
        db.commit()
        actions = bot.handle_update(db, _message(CHAT), now=NOW)
        assert [a.method for a in actions] == ["sendMessage"]
        pressed = bot.handle_update(db, _press(CHAT, "v:lab"), now=NOW)
        assert pressed[1].method == "editMessageText"


def test_one_broken_source_does_not_blank_the_home_card(monkeypatch):
    def boom(db, now):
        raise RuntimeError("пошта впала")

    monkeypatch.setattr(tv, "mail_board", boom)
    with _session() as db:
        card = cards.build(db, "home", NOW, admin=True)
    assert card is not None and _size(card.png)[0] == 1080


def test_fonts_ship_with_the_app():
    """Шрифти карток лежать у `app/static`, який їде в інсталятор цілком."""
    from pathlib import Path

    folder = Path("app/static/fonts/bot")
    assert {p.name for p in folder.glob("*.ttf")} == {
        "KMillSans-Regular.ttf", "KMillSans-SemiBold.ttf", "KMillSans-ExtraBold.ttf", "KMillMono-Medium.ttf"}
    from app.services import telegram_draw as td

    td.font.cache_clear()
    for kind in "xstm":
        assert "KMill" in Path(td.font(kind, 20).path).name, kind


def test_old_letters_keep_their_day_separator():
    letters = [tv.Letter(name="А", received_at=NOW - timedelta(days=1), material="", units=1, unread=False),
               tv.Letter(name="Б", received_at=NOW, material="", units=1, unread=False)]
    one_day = tp.mail_png([letters[1], letters[1]], NOW)
    two_days = tp.mail_png(list(reversed(letters)), NOW)
    assert _size(two_days)[1] > _size(one_day)[1], "другий день має власний роздільник"


def test_now_is_not_used_from_the_wall_clock():
    """Картка малюється на переданий час: два виклики з тим самим `now` однакові."""
    assert tp.handout_png("11.11", 3, 1, 5, 2, 9, 4, datetime(2026, 11, 12, 9, 0)) == \
        tp.handout_png("11.11", 3, 1, 5, 2, 9, 4, datetime(2026, 11, 12, 9, 0))
