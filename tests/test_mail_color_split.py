"""Підказка кольорів багатокольорового листа (app/mail_color_split.py).

Бойовий лист 25.09.26: пересланий від cadcamlab, текст «Monolith кольору денис,
юшков а3.5, роман а3, лідія а 1», дев'ять файлів «16.09.2026-Проект <пацієнт>».
"""

from types import SimpleNamespace

from app.mail_color_split import material_base, parse_name_shades, suggest_color_plan
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

BODY = """--- Повідомлення, що пересилається ---
Від кого: Iris <irisdent.ua@gmail.com>
Кому: <cadcamlab@ukr.net>
Тема: .
Дата: 17 вересня 2026, 00:01:05

Monolith кольору денис, юшков а3.5, роман а3, лідія а 1
Данні для відправки: Грунський Сергій Миколайович 0683992978
Нова пошта:5 м.Фастів
Для зв'язку при технічних проблемах чи недостачі файлів прошу скористатись цим номером +0677783434

--
Ваш найбільший CAD CAM Центр в Україні «Стаханівець»
тел. (095) 435 20 08; (067) 407 69 07
"""


def _att(i, name):
    return SimpleNamespace(id=i, filename=name)


FILES = [
    _att(1, "16.09.2026-Проект денис_2026-09-16_15-10.constructionInfo"),
    _att(2, "16.09.2026-Проект денис_2026-09-16_15-10.stl"),
    _att(3, "16.09.2026-Проект людмила_2026-09-16_15-20.constructionInfo"),
    _att(4, "16.09.2026-Проект людмила_2026-09-16_15-20.stl"),
    _att(5, "16.09.2026-Проект роман_2026-09-16_15-30.constructionInfo"),
    _att(6, "16.09.2026-Проект роман_2026-09-16_15-30.stl"),
    _att(7, "16.09.2026-Проект юшков_2026-09-16_15-40.constructionInfo"),
    _att(8, "16.09.2026-Проект юшков_2026-09-16_15-40.stl"),
    _att(9, "16.09.2026-Проект лідія_2026-09-16_15-50.stl"),
]


def test_names_before_a_shade_take_that_shade():
    pairs = parse_name_shades(BODY, {"monolith"})
    assert pairs == {"денис": "a3.5", "юшков": "a3.5", "роман": "a3", "лідія": "a1"}


def test_delivery_details_and_dates_get_no_shade():
    # «Грунський Сергій» без кольору після себе, «вересня 2026» — не відтінок.
    pairs = parse_name_shades(BODY, {"monolith"})
    assert "грунський" not in pairs and "вересня" not in pairs


def test_real_letter_splits_into_three_colours():
    plan = suggest_color_plan(BODY, "Monolith a3.5", FILES)
    assert plan is not None
    got = {g.shade: sorted(g.attachment_ids) for g in plan.groups}
    assert got == {"a3.5": [1, 2, 7, 8], "a3": [5, 6], "a1": [9]}
    # Порядок груп — як кольори в тексті.
    assert [g.shade for g in plan.groups] == ["a3.5", "a3", "a1"]
    assert plan.groups[0].material == "monolith a3.5"
    assert set(plan.groups[0].names) == {"денис", "юшков"}


def test_file_of_unnamed_patient_is_not_guessed():
    # «людмила» в тексті немає («лідія» — інша людина): файл лишається «?».
    plan = suggest_color_plan(BODY, "Monolith a3.5", FILES)
    assert plan is not None
    assert sorted(plan.unmatched_ids) == [3, 4]
    assert 3 not in plan.by_attachment


def test_latin_filename_matches_cyrillic_name():
    files = [_att(1, "Project_denys.stl"), _att(2, "roman_upper.stl")]
    plan = suggest_color_plan("денис а3.5, роман а3", "mono", files)
    assert plan is not None
    assert plan.by_attachment == {1: "a3.5", 2: "a3"}


def test_real_filenames_of_the_letter():
    # Справжні імена з листа 25.09.26: «деніс» (у тексті «денис»), «люда
    # італія» (у тексті «лідія» — інша людина), «юшков вч» із мостами.
    files = [
        _att(1, "16.09.2026-Проєкт деніс цр.constructionInfo"),
        _att(2, "16.09.2026-Проєкт деніс цр-35-crown_cad.stl"),
        _att(3, "16.09.2026-Проєкт люда італія цр.constructionInfo"),
        _att(4, "16.09.2026-Проєкт люда італія цр-36-crown_cad.stl"),
        _att(5, "16.09.2026-Проєкт роман цр.constructionInfo"),
        _att(6, "16.09.2026-Проєкт роман цр-46-crown_cad.stl"),
        _att(7, "16.09.2026-Проєкт юшков вч цр.constructionInfo"),
        _att(8, "16.09.2026-Проєкт юшков вч цр-24-25-bridge_cad.stl"),
        _att(9, "16.09.2026-Проєкт юшков вч цр-17-16-15-bridge_cad.stl"),
    ]
    plan = suggest_color_plan(BODY, "Monolith", files)
    assert plan is not None
    got = {g.shade: sorted(g.attachment_ids) for g in plan.groups}
    assert got == {"a3.5": [1, 2, 7, 8, 9], "a3": [5, 6]}
    assert sorted(plan.unmatched_ids) == [3, 4]


def test_url_noise_does_not_create_shades():
    body = "роман а3\nhttps://files.ukr.net/x?token=Ab-a2-Cd денис\nюшков а1"
    pairs = parse_name_shades(body, set())
    assert pairs == {"роман": "a3", "юшков": "a1"}


def test_transliteration_variants_are_one_person():
    files = [_att(1, "serhii.stl"), _att(2, "iushkov.stl")]
    plan = suggest_color_plan("сергій а2, юшков а3", "", files)
    assert plan is not None
    assert plan.by_attachment == {1: "a2", 2: "a3"}


def test_similar_but_different_people_are_not_merged():
    # Однакова «схожість» із denis/denys, але це інші люди — не вгадуємо.
    files = [_att(1, "ірена.stl"), _att(2, "іванна.stl"), _att(3, "роман.stl")]
    plan = suggest_color_plan("ірина а1, іван а2, роман а3", "", files)
    assert plan is not None
    assert plan.by_attachment == {3: "a3"}
    assert sorted(plan.unmatched_ids) == [1, 2]


def test_remainder_after_first_batch_still_gets_a_hint():
    # Партію a3 (роман) уже прийнято: у листі лишились a3.5 і «?». Блок не
    # мусить зникати лише тому, що колір тепер один.
    files = [
        _att(1, "16.09.2026-Проєкт деніс цр-35-crown_cad.stl"),
        _att(3, "16.09.2026-Проєкт люда італія цр-36-crown_cad.stl"),
        _att(8, "16.09.2026-Проєкт юшков вч цр-24-25-bridge_cad.stl"),
    ]
    plan = suggest_color_plan(BODY, "Monolith", files)
    assert plan is not None
    assert [(g.shade, g.attachment_ids) for g in plan.groups] == [("a3.5", [1, 8])]
    assert plan.unmatched_ids == [3]


def test_single_colour_letter_gives_no_plan():
    files = [_att(1, "денис.stl"), _att(2, "роман.stl")]
    assert suggest_color_plan("денис а3, роман а3", "mono a3", files) is None


def test_no_names_in_files_gives_no_plan():
    files = [_att(1, "scan_upper.stl"), _att(2, "scan_lower.stl")]
    assert suggest_color_plan(BODY, "Monolith", files) is None


def test_material_word_is_not_a_patient():
    # Без здогаду матеріалу «monolith» стояв би перед a3.5 як «пацієнт».
    pairs = parse_name_shades("Monolith денис а3.5, роман а3", {"monolith"})
    assert "monolith" not in pairs


def test_material_base_strips_shade():
    assert material_base("Monolith a3.5") == "monolith"
    assert material_base("моно А3") == "моно"
    assert material_base(None) == ""


# ── Колір за номером зуба (власник 11.11.26, Max Petrenko) ──────────────────
# Текст «Колір - 23 ( B1 ) 47 ( A1 )», файли `…-23-waxup_cad.stl` і
# `…-47-waxup_cad.stl`; пацієнта в назвах немає, тож розбір за іменами мовчав.

TOOTH_BODY = (
    "Коронки Гвинтова Фикс - 47 Коронки Пован Анатомия - 23 Матеріал - ЦИРКОН , "
    "виробник МОНОЛІТ в найвищій частині диску !!! Колір - 23 ( B1 ) 47 ( A1 ) "
    "Відправити в AND Clinick Адреса: Київ, вул. Глибочицька, 13, буд. 3, «ЖК Львівський квартал»"
)
TOOTH_FILES = [
    _att(11, "2026-11-11_Default-canin si molar_strau.constructionInfo"),
    _att(12, "2026-11-11_Default-canin si molar_strau-23-waxup_cad.stl"),
    _att(13, "2026-11-11_Default-canin si molar_strau-47-waxup_cad.stl"),
]


def test_tooth_takes_the_shade_right_after_it():
    """Зуб отримує колір, лише коли колір стоїть одразу за ним (дужки й дефіси
    не рахуються). «47 Коронки …» і «буд. 13» кольору не мають: між зубом і
    кольором інше слово."""
    from app.mail_color_split import parse_tooth_shades

    assert parse_tooth_shades(TOOTH_BODY) == {23: "b1", 47: "a1"}
    assert parse_tooth_shades("23, 24 а2; 36 A3.5") == {23: "a2", 24: "a2", 36: "a3.5"}
    assert parse_tooth_shades("Колір 23 B1, а 23 A2") == {}, "зуб із двома кольорами — не вгадуємо"


def test_real_two_colour_letter_splits_by_teeth():
    plan = suggest_color_plan(TOOTH_BODY, "Monolith", TOOTH_FILES)
    assert plan is not None and plan.kind == "teeth"
    assert [(g.shade, g.names, g.attachment_ids) for g in plan.groups] == [
        ("b1", ["23"], [12]), ("a1", ["47"], [13]),
    ]
    assert plan.by_attachment == {12: "b1", 13: "a1"}
    assert plan.shared_ids == [11] and plan.unmatched_ids == []
    # Поки кольорів два, спільний файл не чіпляється до жодного.
    assert all(g.extra_ids == [] for g in plan.groups)


def test_shared_file_goes_with_the_last_colour():
    """Після першої партії (B1) лишились 47-й і спільний `.constructionInfo`:
    чіп останнього кольору бере і його, інакше лист висів би у «Вхідних» з
    одним нікому не потрібним файлом."""
    plan = suggest_color_plan(TOOTH_BODY, "Monolith", [TOOTH_FILES[0], TOOTH_FILES[2]])
    assert plan is not None
    assert [(g.shade, g.attachment_ids, g.extra_ids) for g in plan.groups] == [("a1", [13], [11])]


def test_file_with_a_tooth_missing_from_the_text_is_not_guessed():
    files = TOOTH_FILES + [_att(14, "2026-11-11_x-36-crown_cad.stl"), _att(15, "2026-11-11_x-23-47-bridge_cad.stl")]
    plan = suggest_color_plan(TOOTH_BODY, "Monolith", files)
    assert plan is not None and sorted(plan.unmatched_ids) == [14, 15]


def test_one_shade_by_teeth_gives_no_plan():
    assert suggest_color_plan("Колір 23 B1, 47 B1", "Monolith", TOOTH_FILES) is None


def test_names_still_win_over_teeth():
    """Лист пацієнтів (25.09.26) розбирається, як і раніше, за іменами."""
    plan = suggest_color_plan(BODY, "Monolith a3.5", FILES)
    assert plan is not None and plan.kind == "names" and plan.shared_ids == []


# ── Картка листа: чіпи кольорів за зубами доїжджають до розмітки ────────────

def _tooth_letter(factory, tmp_path):
    from app.models import Attachment, EmailMessage

    with factory() as db:
        email = EmailMessage(uid="7", uid_validity="1", from_address="max@x.ua", subject="Коронки ЦИРКОН",
                             status="нове", attachments_status="ready", message_id="<7@x>",
                             body_text=TOOTH_BODY, material_color_guess="Monolith")
        db.add(email)
        db.flush()
        ids = {}
        for att in TOOTH_FILES:
            path = tmp_path / att.filename
            path.write_bytes(f"solid {att.filename}\nendsolid\n".encode())
            row = Attachment(email_message_id=email.id, filename=att.filename, saved_path=str(path))
            db.add(row)
            db.flush()
            ids[att.id] = row.id
        db.commit()
        return email.id, ids


def _chips(card: str) -> list[tuple[str, str]]:
    import re

    return re.findall(r'data-cp-ids="([^"]*)"\s+data-cp-material="([^"]*)"', card)


def test_card_shows_tooth_colour_chips(app_db, tmp_path):  # noqa: F811
    from tests.asgi_client import MiniClient
    from tests.test_settings_slabs_render import ADMIN

    app, factory = app_db
    eid, ids = _tooth_letter(factory, tmp_path)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, card = client.get(f"/mail/{eid}?panel=1")
    assert status == 200, card[:300]
    chips = _chips(card)
    assert [c[0] for c in chips] == [str(ids[12]), str(ids[13])], "по файлу на колір, спільний — ні до кого"
    assert all(m.endswith(("b1", "a1")) for _, m in chips), chips
    assert "зуб 23 · 1 файл." in card and "зуб 47 · 1 файл." in card
    assert ">спільний<" in card and "без кольору" not in card


def test_card_last_colour_takes_the_shared_file(app_db, tmp_path):  # noqa: F811
    from app.models import Attachment, Order
    from tests.asgi_client import MiniClient
    from tests.test_settings_slabs_render import ADMIN

    app, factory = app_db
    eid, ids = _tooth_letter(factory, tmp_path)
    with factory() as db:
        order = Order(source="email", client_name="Max Petrenko", material_color="mono b1", quantity=1)
        db.add(order)
        db.flush()
        db.get(Attachment, ids[12]).order_id = order.id
        db.commit()
    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, card = client.get(f"/mail/{eid}?panel=1")
    chips = _chips(card)
    assert len(chips) == 1 and set(chips[0][0].split(",")) == {str(ids[13]), str(ids[11])}
    assert "зуб 47 · 2 файл." in card
