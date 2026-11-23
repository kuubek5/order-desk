"""Бот: «📋 Таблиця дня» (власник 23.11.26).

Що стережеться:
* порядок і номери рядків — з Google-таблиці, стани й суми — як у «Лабораторії»;
* переробка показує, який раз, хто винен і причину; без причини так і каже;
* непозначена переробка впізнається за ранішим прорахунком наряду;
* коментар CAM, шлях до роботи й імена з довільного тексту не виходять у Telegram;
* вид закритий для учасників, довгий день гортається сторінками.
"""

import io
from datetime import datetime, timedelta

import pytest
from PIL import Image

from app.business_day import business_today
from app.models import Material, Order, ReworkRecord
from app.services import telegram_bot as bot
from app.services import telegram_cards as cards
from app.services import telegram_table as tt
from app.services import telegram_table_picture as ttp
from app.services import telegram_views as tv
from tests.test_telegram_bot import CHAT, _member, _press, _session
from tests.test_telegram_views import NOW


@pytest.fixture(autouse=True)
def _clean_state(weekday_clock):
    bot.reset_for_tests()
    yield
    bot.reset_for_tests()


def _tab(days_ago=0):
    return (business_today() - timedelta(days=days_ago)).strftime("%d.%m.%y")


def _add(db, row, **kw):
    base = dict(source="lab", sheet_tab=_tab(), row_number=row, status="нове", work_order_no=str(41000 + row),
                quantity="2", material_color="моно А3", kind="анатомія", technician_name="Кіра")
    base.update(kw)
    order = Order(**base)
    db.add(order)
    db.flush()
    return order


def _table(db):
    return tt.day_table(db, 0)


def test_rows_follow_the_sheet_and_totals_match_the_lab_card():
    with _session() as db:
        zr = Material(name="Цирконій", sort_order=1)
        db.add(zr)
        db.flush()
        _add(db, 3, quantity="4")
        _add(db, 1, job_code="P:/1", sum3d_id="10-00-00", material=zr)
        _add(db, 2, job_code="P:/2", cam_comment="на швидку")
        _add(db, 9, source="sheet_client", client_name="Клієнт")
        db.commit()
        table = _table(db)
        counts = tv.lab_counts(tv._lab_orders(db, 0))
    assert [(r.sheet_row, r.state, r.rush) for r in table.rows] == [
        (7, "done", False), (8, "take", True), (9, "wait", False),
    ], "номер рядка — як у Google, клієнтських робіт немає"
    assert (table.rows[0].group, table.rows[0].shade, table.rows[0].sum3d) == ("zr", "A3", "10-00-00")
    assert (table.units, table.units_left, table.done) == (8, 6, len(counts["done"]))
    assert tt.table_summary(table) == "3 роботи · 8 од. · лишилось 6"


@pytest.mark.parametrize("record, expected", [
    (dict(occurrence=2, blame="технік", blame_quantity="коротка межа", redo_quantity="1"),
     "2-й раз · технік: коротка межа · 1 од."),
    (dict(occurrence=3, blame="обладнання", blame_quantity="2"), "3-й раз · обладнання, причину не вписано · 2 од."),
    (dict(redo_quantity="1"), "причину в таблиці не вписано · 1 од."),
    (dict(occurrence=2, blame="клієнт", blame_quantity="скол Іваненко Марія"), "2-й раз · клієнт: скол"),
])
def test_rework_line_says_which_time_who_and_why(record, expected):
    with _session() as db:
        order = _add(db, 1)
        db.add(ReworkRecord(order_id=order.id, **record))
        db.commit()
        assert _table(db).rows[0].rework.text == expected


def test_rework_is_seen_without_a_record():
    with _session() as db:
        _add(db, 1, mill_count="2")
        _add(db, 2, mill_count="1")
        # Той самий виріб уже прораховано вчора іншим Sum3D — лаба нічого не позначила.
        _add(db, 5, work_order_no="36652", sum3d_id="18-53-11", sheet_tab=_tab(1), archived_at=NOW)
        _add(db, 3, work_order_no="36652", sum3d_id="12-54-26", job_code="P:/3")
        db.commit()
        rows = _table(db).rows
    yesterday = (business_today() - timedelta(days=1)).strftime("%d.%m")
    assert rows[0].rework.text == "2-й раз · причину в таблиці не вписано"
    assert rows[1].rework is None
    assert rows[2].rework.text == f"уже прораховано {yesterday} · причину в таблиці не вписано"


@pytest.mark.parametrize("raw, safe", [
    ("анатомія гвинтова 24-26", "анатомія гвинтова 24-26"),
    ("Скол при фрезеруванні", "Скол при фрезеруванні"),
    ("скол, Петренко", "скол"),
    ("коронка Іваненко І.П.", "коронка"),
    ("Іваненко І.П.", ""),
    ("моно A3 ПММА", "моно A3 ПММА"),
    (None, ""),
])
def test_names_are_cut_from_free_text(raw, safe):
    assert tt.public_text(raw) == safe


def test_private_fields_never_reach_the_picture_or_text(monkeypatch):
    seen: list[str] = []
    original = ttp.Canvas.text

    def spy(self, x, y, text, *args, **kwargs):
        seen.append(str(text))
        return original(self, x, y, text, *args, **kwargs)

    monkeypatch.setattr(ttp.Canvas, "text", spy)
    with _session() as db:
        order = _add(db, 1, job_code="2026-11-23_Шевченко-007", cam_comment="Петренко Марія, зуб 11",
                     kind="коронка Іваненко")
        db.add(ReworkRecord(order_id=order.id, blame="технік", blame_quantity="брак Бондар", cam_comment="Коваль"))
        db.commit()
        pressed = bot.handle_update(db, _press(CHAT, "v:table"), now=NOW)[1]
        drawn = " ".join(seen) + pressed.payload["caption"] + tt.table_text(db, 0)
    assert "коронка" in drawn and "41001" in drawn
    for secret in ("Шевченко", "Петренко", "Марія", "Іваненко", "Бондар", "Коваль"):
        assert secret not in drawn


def test_weekend_shows_friday_and_monday_yesterday_is_friday(monkeypatch):
    from app import business_day

    with _session() as db:
        _add(db, 1, sheet_tab="20.11.26")
        db.commit()
        for moment, offset in ((datetime(2026, 11, 21, 12), 0), (datetime(2026, 11, 22, 12), 0),
                               (datetime(2026, 11, 23, 12), -1)):
            monkeypatch.setattr(business_day, "business_now", lambda moment=moment: moment)
            assert len(tt.day_table(db, offset).rows) == 1, moment
            assert tt.table_day(offset).day == 20


def test_several_calculations_and_plain_text_in_the_id_cell():
    assert tt._sum3d_label("10-00-00\n12-54-26") == "12-54-26 +1"
    assert tt._sum3d_label("2026-11-23_10-00-00") in ("10-00-00", "2026-11-23_10-00-00")
    assert tt._sum3d_label("немає папки") == "немає папки"
    assert tt._sum3d_label(None) == ""


def test_long_day_is_paged_and_refresh_keeps_the_page():
    with _session() as db:
        for row in range(1, tt.LINES_PER_PAGE + 11):
            _add(db, row)
        db.commit()
        table = _table(db)
        assert [len(p) for p in table.pages()] == [tt.LINES_PER_PAGE, 10]
        first = bot.handle_update(db, _press(CHAT, "v:table"), now=NOW)[1]
        second = bot.handle_update(db, _press(CHAT, "p:table:1"), now=NOW)[1]
    flat = [b["callback_data"] for r in second.payload["reply_markup"]["inline_keyboard"] for b in r]
    assert flat.count("p:table:1") >= 2, "сторінка й «Оновити»"
    assert "v:table_y" in flat
    png = first.files["photo" if first.method == "sendPhoto" else "board"][1]
    w, h = Image.open(io.BytesIO(png)).size
    assert w == 1080 and h < 3400


def test_members_cannot_open_it_and_text_bot_has_it(monkeypatch):
    with _session() as db:
        _add(db, 1, mill_count="2")
        db.commit()
        assert "table" not in bot.views_for(False)
        monkeypatch.setattr(bot, "pictures_enabled", lambda db: False)
        text = bot.handle_update(db, _press(CHAT, "v:table"), now=NOW)[1].payload["text"]
        assert "41001" in text and "↺ переробка · 2-й раз" in text
        _member(db, "777")
        actions = bot.handle_update(db, _press("777", "v:table"), now=NOW)
        assert all("41001" not in str(a.payload) for a in actions)


def test_huge_day_text_fits_one_message():
    with _session() as db:
        for row in range(1, 121):
            _add(db, row, kind="анатомія гвинтова міст 45-47", mill_count="2")
        db.commit()
        assert len(bot.render(db, "table", NOW)) < 4096


def test_broken_table_drawing_falls_back_to_text(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("шрифт зник")

    monkeypatch.setattr(ttp, "table_png", boom)
    with _session() as db:
        _add(db, 1)
        db.commit()
        assert cards.build(db, "table", NOW, admin=True) is None
        assert "41001" in bot.handle_update(db, _press(CHAT, "v:table"), now=NOW)[1].payload["text"]
