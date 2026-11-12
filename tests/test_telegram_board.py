"""Картинка-табло верстатів у Telegram-боті (власник 08.11.26, варіант C).

Що стережеться:
* дані плитки беруться з того самого збирача, що й телевізор (`board_machine`);
* натискання «Верстати» з текстового меню шле фото й лише ПОТІМ прибирає
  старе меню; збій відправки старого меню не стирає;
* з фото на текстовий вид — нове повідомлення, бо текст у фото не вписати;
* кнопка верстата — підпис з усіма роботами на диску, підроблена кнопка
  оператора нічого не відкриває;
* малювання впало — бот показує старий текстовий список, а не мовчить.
"""

import zlib
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services import telegram as tg
from app.services import telegram_board as tb
from app.services import telegram_bot as bot
from app.services.telegram import ApiResult
from tests.test_telegram_bot import CHAT, _member, _press, _session

NOW = datetime(2026, 11, 8, 10, 52)


@pytest.fixture(autouse=True)
def _legacy_text_bot(monkeypatch):
    """Ці тести стережуть СТАРИЙ бот (текст, окрема картинка пошти й верстатів):
    він лишився запасним шляхом — коли картки вимкнено в налаштуваннях або
    малювання впало. Нові картки — `tests/test_telegram_cards.py`."""
    monkeypatch.setattr(bot, "pictures_enabled", lambda db: False)


def _order(id_, client=None, number=None, material=None, quantity=None):
    return SimpleNamespace(id=id_, client_name=client, work_order_no=number,
                           material_color=material, quantity=quantity, kind=None,
                           technician_name=None)


def _card(name, state_key, *, percent=None, orders=(), last_orders=(), word="", note="",
          iso_name=None, sum3d=None, sisma=False):
    return SimpleNamespace(
        # Сталий ключ: `hash(name)` у Python щоразу інший, і двоє верстатів
        # іноді діставали ту саму адресу — тест падав навмання (CI 0.21.116).
        key=f"10.0.0.{zlib.crc32(name.encode()) % 250}",
        target=SimpleNamespace(name=name, machine_id=None, portrait_model="", show_on_board=True),
        state_key=state_key, state_word=word, state_note=note, percent=percent,
        sum3d_id=sum3d, orders=list(orders), last_orders=list(last_orders),
        is_sisma_machine=sisma, layers=None, iso_name=iso_name,
        # Для запасного текстового списку (`machines_text`).
        is_running=state_key == "run", problem_text=note,
    )


def _cards():
    return [
        _card("350i-Boris", "run", percent=67, iso_name="1_14-MONOLITH-A3-X89_2026-11-08_09-12-40.iso",
              orders=[_order(2, "Дент-Арт", "24125", "моно А3", "2"),
                      _order(1, "Дент-Арт", "24122", "моно А3", "4"),
                      _order(3, "Білодент", None, "моно А3", "3")]),
        _card("250i-Sec", "done", word="завершено", note="програма завершена · зняти",
              orders=[_order(4, "Коваль", "24130", "katana a2", "1")]),
        _card("250i-dry", "idle", word="стоїть", note="програма не йде"),
        _card("150i-Olejka", "off", word="немає зв'язку", note="немає зв'язку 12 хв"),
        _card("350i-2", "fault", word="помилка", note="на екрані повідомлення про помилку"),
        _card("Sisma", "run", percent=40, sisma=True),
    ]


@pytest.fixture
def machines(monkeypatch):
    cards = _cards()
    from app.services import machines as machines_mod

    monkeypatch.setattr(machines_mod, "snapshot", lambda db: cards)
    return cards


# ── Плитки й малювання ──────────────────────────────────────────────────────


def test_tiles_follow_the_tv_board_and_skip_sisma():
    board = tb.board_of(_cards())
    assert [t.name for t in board.tiles] == ["350i-Boris", "250i-Sec", "250i-dry", "150i-Olejka", "350i-2"]
    boris, sec, dry, olejka, fault = board.tiles
    # Перша робота — за id, як на телевізорі; решта — «+N».
    assert (boris.material, boris.line) == ("МОНО А3 · №14", "Дент-Арт +2")
    assert (sec.lead, sec.line) == ("зняти", "Коваль")
    assert dry.line == "стоїть"
    assert olejka.line == "немає зв'язку 12 хв"
    assert fault.line == "помилка на екрані"
    assert tb.summary_words(board) == "фрезерують 1 з 5 · зняти 1 · тривога 2"


def test_machine_without_works_names_the_disc_from_the_program():
    """«2_18-…» — дві одиниці на диску №18 (власник 08.11.26)."""
    tile = tb.tile_of(_card("250-pmma", "busy", word="запуск",
                            iso_name="2_18-Emotions-a1-x193_2026-10-12_09-09-40.iso"))
    assert tile.line == "диск №18 · 2 од."


def test_program_without_disc_mark_is_shown_without_the_date():
    tile = tb.tile_of(_card("250-pmma", "busy", word="запуск",
                            iso_name="Emotions-a1_2026-10-12_09-09-40.iso"))
    assert tile.line == "Emotions-a1"


def test_disc_mark_reads_units_and_disc():
    assert tb.disc_mark("1_14-MONOLITH-A3-X89_2026-11-08_") == (1, 14)
    assert tb.disc_mark("2026-11-08_09-12-40") is None
    assert tb.disc_mark(None) is None


def test_render_is_a_png_that_grows_with_rows():
    small = tb.render_png(tb.board_of(_cards()[:2]), NOW)
    big = tb.render_png(tb.board_of(_cards()), NOW, focus=tb.board_of(_cards()).tiles[0].key)
    small_img, big_img = Image.open(BytesIO(small)), Image.open(BytesIO(big))
    assert small_img.format == "PNG" and small_img.width == tb.WIDTH
    assert big_img.height > small_img.height


def test_button_label_names_the_state():
    board = tb.board_of(_cards())
    labels = [tb.button_label(t) for t in board.tiles]
    assert labels == ["350i-Boris 67%", "250i-Sec ✓", "250i-dry ·", "150i-Olejka ✕", "350i-2 ⚠"]
    assert tb.button_label(board.tiles[0], focus=True).startswith("◉ ")


# ── Меню бота ───────────────────────────────────────────────────────────────


def _photo_press(data, message_id=42):
    update = _press(CHAT, data, message_id=message_id)
    update["callback_query"]["message"]["photo"] = [{"file_id": "x"}]
    return update


def test_machines_from_text_menu_sends_photo_then_deletes_old(machines):
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "v:machines"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "sendPhoto", "deleteMessage"]
    photo, delete = actions[1], actions[2]
    assert photo.files["photo"][1][:8] == b"\x89PNG\r\n\x1a\n"
    assert "Фрезерують 1 з 5" in photo.payload["caption"]
    buttons = [b["callback_data"] for row in photo.payload["reply_markup"]["inline_keyboard"] for b in row]
    assert f"m:{machines[0].key}" in buttons and "v:home" in buttons and "n:machines" in buttons
    # Sisma має свій вид і кнопки верстата не отримує.
    assert f"m:{machines[-1].key}" not in buttons
    assert delete.after_ok and delete.payload["message_id"] == 42


def test_refresh_on_the_photo_replaces_the_picture_in_place(machines):
    with _session() as db:
        actions = bot.handle_update(db, _photo_press("v:machines"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "editMessageMedia"]
    media = actions[1].payload["media"]
    assert media["media"] == "attach://board" and "board" in actions[1].files


def test_machine_button_shows_every_work_on_the_disc(machines):
    with _session() as db:
        actions = bot.handle_update(db, _photo_press(f"m:{machines[0].key}"), now=NOW)
    caption = actions[1].payload["media"]["caption"]
    assert "<b>350i-Boris</b>" in caption and "67%" in caption
    assert "<code>1_14-MONOLITH-A3-X89_2026-11-08_</code><b>09-12-40</b>" in caption
    assert "Диск <b>№14</b> · 1 од." in caption
    order = ["24122", "24125", "Білодент"]
    assert [caption.index(x) for x in order] == sorted(caption.index(x) for x in order)
    assert "Дент-Арт · наряд 24122 · моно А3 · 4 од." in caption
    texts = [b["text"] for row in actions[1].payload["reply_markup"]["inline_keyboard"] for b in row]
    assert "◉ 350i-Boris 67%" in texts and "⬅ Усі верстати" in texts


def test_machine_caption_does_not_repeat_the_state(machines):
    """Слово стану й пояснення не дублюють одне одного: «немає зв'язку ·
    немає зв'язку 12 хв» читалось як збій самого бота."""
    sec, olejka = machines[1], machines[3]
    assert bot._machine_detail(sec).splitlines()[1] == "✅ завершено · <b>зняти</b>"
    assert bot._machine_detail(olejka).splitlines()[1] == "⚠️ немає зв'язку 12 хв"


def test_unknown_machine_button_falls_back_to_the_board(machines):
    with _session() as db:
        actions = bot.handle_update(db, _photo_press("m:10.9.9.9"), now=NOW)
    assert "Фрезерують" in actions[1].payload["media"]["caption"]


def test_leaving_the_photo_sends_text_and_removes_the_picture(machines):
    with _session() as db:
        actions = bot.handle_update(db, _photo_press("v:home"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "sendMessage", "deleteMessage"]
    assert "message_id" not in actions[1].payload
    assert actions[2].after_ok


def test_operator_cannot_open_machines_with_a_forged_machine_button(machines):
    with _session() as db:
        _member(db)
        actions = bot.handle_update(db, _press("700", f"m:{machines[0].key}"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery"]


def test_broken_render_falls_back_to_the_text_list(machines, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("шрифт")

    monkeypatch.setattr(tb, "render_png", boom)
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "v:machines"), now=NOW)
    assert [a.method for a in actions] == ["answerCallbackQuery", "editMessageText"]
    assert "350i-Boris" in actions[1].payload["text"]


def test_no_machines_keeps_the_text_view(monkeypatch):
    from app.services import machines as machines_mod

    monkeypatch.setattr(machines_mod, "snapshot", lambda db: [])
    with _session() as db:
        actions = bot.handle_update(db, _press(CHAT, "v:machines"), now=NOW)
    assert actions[1].method == "editMessageText"


# ── Відправка ───────────────────────────────────────────────────────────────


def test_old_menu_is_not_deleted_when_the_photo_did_not_go(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "api_call_files", lambda *a, **k: calls.append(a[2]) or ApiResult(False, None, None, "net"))
    monkeypatch.setattr(tg, "api_call", lambda s, t, method, payload=None, **k: calls.append(method) or ApiResult(True, 200))
    bot.run_actions(None, "T", [
        bot.Action("sendPhoto", {"chat_id": CHAT}, files={"photo": ("a.png", b"x", "image/png")}),
        bot.Action("deleteMessage", {"chat_id": CHAT, "message_id": 1}, after_ok=True),
    ])
    assert calls == ["sendPhoto"]


def test_multipart_call_sends_nested_fields_as_json():
    seen = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"message_id": 5}}

    class Session:
        def post(self, url, data=None, files=None, timeout=None):
            seen.update(url=url, data=data, files=files)
            return Resp()

    result = tg.api_call_files(Session(), "TOKEN", "sendPhoto",
                               {"chat_id": "1", "reply_markup": {"inline_keyboard": [[{"text": "Ок"}]]}},
                               {"photo": ("a.png", b"x", "image/png")})
    assert result.ok and seen["url"].endswith("/sendPhoto")
    assert seen["data"]["reply_markup"] == '{"inline_keyboard": [[{"text": "Ок"}]]}'
    assert seen["files"]["photo"][0] == "a.png"
