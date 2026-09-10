"""Нові диски з теки CAM: замовлення на склад без ручного вводу.

Головне, що стережуть ці тести, — три речі, на яких така фіча ламається тихо:

1. **Повторена назва після підчистки.** Номер свій на кожну групу і після
   очищення теки починається з малого, тобто назви повторюються. Якби ми
   звіряли самі назви, після першої ж підчистки нові диски рахувались би як
   давно бачені й у замовлення не потрапляли б.
2. **Нерозібрана назва.** Формат — конвенція, не примус. Файл, який не
   розібрався, усе одно взятий диск; тихо викинути його не можна.
3. **Вікно замовлення.** Рахується від позначки «замовлено», а не від
   робочої доби: склад закривається о 18:00, далі бере нічна зміна, а у
   вихідні склад не працює взагалі.

Замовлення як записи (створення, скасування, історія) — tests/test_disc_orders.py.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import CamBlank
from app.services.cam_blanks import (
    COUNT_MARK,
    order_groups,
    order_text,
    parse_blank_name,
    pending_blanks,
    scan_blanks,
    shift_groups,
    shift_of,
    shifts_label,
    sync_blanks,
)
from app.services.disc_orders import create_order


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def make_tree(root, files: dict[str, list[str]]):
    """`{'zr/12': ['12-monolith-a2-x14.blk']}` → справжні теки й файли."""
    for rel_dir, names in files.items():
        directory = root / rel_dir
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_text("", encoding="utf-8")


def seed_baseline(db, root):
    """Поставити базу відліку.

    Перший прохід по теці — це БАЗА, а не вантаж замовлень: у теці лежать
    диски, накопичені роками. Тести, які перевіряють ПОЯВУ нового диска,
    мусять спершу базу поставити, інакше вони міряли б саме її.
    """
    make_tree(root, {"zr/10": ["10-baseline-a0-x1.blk"]})
    result = sync_blanks(db, root)
    assert (result.baseline, result.appeared) == (1, 0)


# ── розбір назви ────────────────────────────────────────────────────────────


def test_parses_the_shape_the_shop_actually_writes():
    parsed = parse_blank_name("12-monolith-a2-x14.blk")
    assert (parsed.height, parsed.brand, parsed.shade, parsed.serial) == (12, "monolith", "a2", 14)


def test_serial_is_read_from_the_end_even_when_the_shade_has_dashes():
    """Номер ЗАВЖДИ останній і завжди з «x» — на це й спираємось, бо колір
    буває складений («прозора», «a3-5»), і різати за першим дефісом не можна."""
    parsed = parse_blank_name("20-pmma-прозора-x3.blk")
    assert (parsed.height, parsed.shade, parsed.serial) == (20, "прозора", 3)

    parsed = parse_blank_name("18-emotions-a3-5-x207.blk")
    assert (parsed.shade, parsed.serial) == ("a3-5", 207)


def test_unparseable_name_yields_empty_fields_not_an_exception():
    parsed = parse_blank_name("якась-чужа-назва.blk")
    assert (parsed.height, parsed.brand, parsed.shade, parsed.serial) == (None, None, None, None)


# ── сканування теки ─────────────────────────────────────────────────────────


def test_scan_walks_material_and_height_and_ignores_foreign_files(tmp_path):
    make_tree(tmp_path, {
        "zr/12": ["12-monolith-a2-x14.blk", "readme.txt"],
        "zr/18": ["18-emotions-a3-x7.BLK"],
        "pmma/20": ["20-pmma-прозора-x3.blk"],
    })
    found = {item.rel_path for item in scan_blanks(tmp_path)}
    assert found == {
        "zr/12/12-monolith-a2-x14.blk",
        "zr/18/18-emotions-a3-x7.BLK",
        "pmma/20/20-pmma-прозора-x3.blk",
    }


def test_missing_root_is_not_an_error(tmp_path):
    assert scan_blanks(tmp_path / "немає") == []


def test_height_in_the_name_must_match_its_folder(tmp_path):
    """Власник підтвердив: якщо тека 12, диск має починатися з 12. Отже
    розбіжність — помилка розкладання, і її треба ПОКАЗУВАТИ, а не мовчати."""
    make_tree(tmp_path, {"zr/12": ["14-monolith-a2-x14.blk", "12-monolith-a3-x15.blk"]})
    by_name = {item.file_name: item for item in scan_blanks(tmp_path)}
    assert by_name["14-monolith-a2-x14.blk"].height_mismatch is True
    assert by_name["12-monolith-a3-x15.blk"].height_mismatch is False


# ── поява й зникнення ───────────────────────────────────────────────────────


def test_new_file_appears_once_and_is_not_counted_again(db, tmp_path):
    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x14.blk"]})
    first = sync_blanks(db, tmp_path)
    assert (first.appeared, first.vanished) == (1, 0)

    second = sync_blanks(db, tmp_path)
    assert (second.appeared, second.vanished) == (0, 0)
    assert db.scalar(select(CamBlank).where(CamBlank.rel_path.like("%x14%"))) is not None


def test_removed_file_is_marked_gone_but_the_row_survives(db, tmp_path):
    """Рядок лишається: диск усе одно взяли, і замовити його треба, навіть
    якщо файл уже прибрали."""
    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x14.blk"]})
    sync_blanks(db, tmp_path)
    (tmp_path / "zr/12/12-monolith-a2-x14.blk").unlink()

    result = sync_blanks(db, tmp_path)
    assert (result.appeared, result.vanished) == (0, 1)
    # База відліку теж лежить у таблиці — беремо саме той рядок, що зник.
    row = db.scalars(select(CamBlank).where(CamBlank.file_name.like("%x14%"))).one()
    assert row.gone_at is not None


def test_same_name_after_a_cleanup_counts_as_a_new_disc(db, tmp_path):
    """ЦЕНТРАЛЬНИЙ тест фічі.

    Після підчистки теки нумерація починається з малого, тож назви
    повторюються. Порівняння самих назв дало б «уже бачив» і новий диск не
    потрапив би в замовлення — саме тоді, коли на список уже покладаються.
    """
    seed_baseline(db, tmp_path)
    path = tmp_path / "zr/12/12-monolith-a2-x1.blk"
    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x1.blk"]})
    sync_blanks(db, tmp_path)
    create_order(db, ids=[r.id for r in pending_blanks(db)])
    db.commit()

    # Підчистка теки.
    path.unlink()
    sync_blanks(db, tmp_path)

    # Нумерація почалась спочатку — та сама назва, але ІНШИЙ фізичний диск.
    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x1.blk"]})
    result = sync_blanks(db, tmp_path)

    assert result.appeared == 1, "повторена назва мусить рахуватись як новий диск"
    assert len(pending_blanks(db)) == 1


def test_unparsed_file_is_still_recorded_as_a_taken_disc(db, tmp_path):
    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": ["чудернацька назва.blk"]})
    sync_blanks(db, tmp_path)
    # Незамовлені = все, крім бази відліку.
    row = pending_blanks(db)[0]
    assert row.height is None and row.brand is None
    assert row.file_name == "чудернацька назва.blk"
    assert len(pending_blanks(db)) == 1


# ── вікно замовлення ────────────────────────────────────────────────────────


def test_window_runs_from_the_order_mark_not_from_the_business_day(db, tmp_path):
    """Комірниця йде о 18:00, далі бере нічна зміна, а у вихідні її немає
    взагалі. Тому межа — позначка «замовлено», і вона переживає і ніч, і
    вихідні, і забутий день, без жодної календарної логіки."""
    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x1.blk"]})
    sync_blanks(db, tmp_path, now=datetime(2026, 9, 4, 23, 40))   # пʼятниця, ніч
    assert len(pending_blanks(db)) == 1

    order = create_order(db, ids=[r.id for r in pending_blanks(db)], now=datetime(2026, 9, 4, 17, 30))
    db.commit()
    assert order is not None and order.disc_count == 1
    assert pending_blanks(db) == []

    # Вихідні: оператори працюють, комірниці немає.
    make_tree(tmp_path, {"zr/18": ["18-emotions-a3-x2.blk"]})
    sync_blanks(db, tmp_path, now=datetime(2026, 9, 6, 2, 15))    # неділя, ніч
    pending = pending_blanks(db)
    assert len(pending) == 1, "взяте у вихідні мусить дочекатись понеділка"
    assert pending[0].file_name == "18-emotions-a3-x2.blk"


# ── текст для складу: mono-a2-25(х2) ────────────────────────────────────────


def _disc(path, brand, shade, height, folder, when=datetime(2026, 9, 8, 17, 30), **extra):
    return CamBlank(rel_path=path, brand=brand, shade=shade, height=height,
                    material_dir=folder, first_seen_at=when, **extra)


def test_order_text_matches_the_shape_the_owner_reads():
    """Формат задав власник 10.09.26 (бриф «Нові диски»): виробник-колір-висота
    через дефіс, малими; кількість — у дужках КИРИЛИЧНОЮ «х» і лише коли
    дисків більше одного; цирконій, порожній рядок, ПММА."""
    rows = [
        _disc("a", "monolith", "a2", 18, "ZR"),
        _disc("b", "monolith", "a2", 25, "ZR"),
        _disc("b2", "monolith", "a2", 25, "ZR"),
        _disc("c", "emotions", "a1", 20, "ZR"),
        _disc("d", "pmma", "a2", 20, "PMMA-PEEK"),
        _disc("f", "monolith", "a3-5", 20, "ZR"),
    ]
    assert order_text(rows) == (
        "emo-a1-20\n"
        "mono-a2-18\n"
        f"mono-a2-25({COUNT_MARK}2)\n"
        "mono-a3.5-20\n"
        "\n"
        "pmma-a2-20"
    )


def test_the_count_mark_is_the_cyrillic_letter():
    """Латинська x у «(x2)» виглядає так само, але це не те, що пише власник
    і що звик читати склад. Константа — щоб її не «виправили» при правці."""
    assert COUNT_MARK == "х"
    rows = [_disc("a", "zr", "a2", 25, "ZR"), _disc("b", "zr", "a2", 25, "ZR")]
    assert order_text(rows) == "zr-a2-25(х2)"


def test_shade_sorts_like_a_human_reads_it():
    rows = [_disc(p, "monolith", s, 14, "ZR") for p, s in (("1", "b1"), ("2", "a3-5"), ("3", "a3"), ("4", "a1"))]
    assert order_text(rows).splitlines() == ["mono-a1-14", "mono-a3-14", "mono-a3.5-14", "mono-b1-14"]


def test_note_goes_last_as_its_own_block():
    """Дописане від руки (фрези) — окремим блоком після дисків; замовлення
    може складатися лише з нього."""
    rows = [_disc("a", "zr", "a2", 25, "ZR")]
    assert order_text(rows, "  6*2.5 zr (х2)  ") == "zr-a2-25\n\n6*2.5 zr (х2)"
    assert order_text([], "полірувальні диски") == "полірувальні диски"
    assert order_text([]) == ""


def test_groups_keep_zirconium_first_then_pmma_then_the_rest():
    rows = [
        _disc("c", "crco", "hpp", 20, "CRCO"),
        _disc("p", "pmma", "a1", 16, "PMMA-PEEK"),
        _disc("z", "zr", "a2", 25, "ZR"),
    ]
    groups = order_groups(rows)
    assert [g.group for g in groups] == ["zr", "pmma", "dir:crco"]
    assert [g.title for g in groups] == ["Цирконій", "ПММА · PEEK", "CRCO"]


def test_order_text_shows_unparsed_files_instead_of_hiding_them():
    """Нерозібраний файл — теж узятий диск; іде сирою назвою без `.blk`, а
    рядок позначений як той, що просить погляду."""
    now = datetime(2026, 9, 8, 17, 30)
    rows = [
        _disc("a", "monolith", "a2", 18, "ZR"),
        CamBlank(rel_path="ZR/12/дивна.blk", file_name="дивна.blk", material_dir="ZR", first_seen_at=now),
    ]
    text = order_text(rows)
    assert text == "mono-a2-18\nдивна"
    raw = [line for g in order_groups(rows) for line in g.lines if line.pos.raw]
    assert raw and raw[0].warn


def test_the_same_position_from_two_shifts_is_one_line():
    """Той самий диск, узятий удень і вночі, — одна позиція з кількістю."""
    rows = [
        _disc("a", "zr", "a2", 25, "ZR", datetime(2026, 9, 8, 22, 12)),
        _disc("b", "zr", "a2", 25, "ZR", datetime(2026, 9, 9, 10, 0)),
    ]
    lines = [line for g in order_groups(rows) for line in g.lines]
    assert len(lines) == 1 and lines[0].count == 2
    assert "22:12" not in order_text(rows), "час у текст для складу не йде"


# ── зміни: денна 07:30–18:00, нічна 18:00–07:30 ─────────────────────────────


def test_night_disc_after_midnight_belongs_to_yesterdays_night_shift():
    shift = shift_of(datetime(2026, 9, 10, 1, 9))
    assert shift.kind == "night"
    assert (shift.start, shift.end) == (datetime(2026, 9, 9, 18, 0), datetime(2026, 9, 10, 7, 30))
    assert shift.short == "нічна 09.09→10.09"
    assert shift.when == "ср 09.09 → чт 10.09"


def test_shift_borders_are_0730_and_1800():
    assert shift_of(datetime(2026, 9, 10, 7, 29)).kind == "night"
    assert shift_of(datetime(2026, 9, 10, 7, 30)).kind == "day"
    assert shift_of(datetime(2026, 9, 10, 17, 59)).kind == "day"
    assert shift_of(datetime(2026, 9, 10, 18, 0)).kind == "night"
    assert shift_of(datetime(2026, 9, 10, 12, 0)).short == "денна 10.09"


def test_shift_groups_are_freshest_first_and_know_the_live_one():
    rows = [
        _disc("a", "zr", "a2", 25, "ZR", datetime(2026, 9, 9, 21, 40)),
        _disc("b", "zr", "a2", 25, "ZR", datetime(2026, 9, 10, 8, 14)),
        _disc("c", "zr", "a3", 18, "ZR", datetime(2026, 9, 10, 15, 26)),
    ]
    groups = shift_groups(rows, now=datetime(2026, 9, 10, 16, 40))
    assert [g.shift.key for g in groups] == ["d20260910", "n20260909"]
    assert [r.rel_path for r in groups[0].rows] == ["c", "b"], "у зміні свіжі згори"
    assert groups[0].live and not groups[1].live


def test_shifts_label_names_two_and_counts_more():
    two = [
        _disc("a", "zr", "a2", 25, "ZR", datetime(2026, 9, 9, 21, 0)),
        _disc("b", "zr", "a2", 25, "ZR", datetime(2026, 9, 10, 9, 0)),
    ]
    assert shifts_label(two) == "нічна 09.09→10.09 + денна 10.09"
    many = two + [_disc("c", "zr", "a2", 25, "ZR", datetime(2026, 9, 7, 9, 0))]
    assert shifts_label(many) == "3 зміни · 07.09–10.09"


# ── тека, якої немає ────────────────────────────────────────────────────────


def test_missing_folder_changes_nothing(db, tmp_path):
    """Відсутня тека — не порожня тека. Раніше помилка в шляху, виправлена за
    хвилину, робила всі 19 тисяч дисків точки відліку «новими»: прохід без
    теки позначав їх зниклими, а коли тека поверталась — кожен файл
    зʼявлявся знову, уже як узятий диск."""
    make_tree(tmp_path, {"zr/12": [f"12-monolith-a2-x{i}.blk" for i in range(1, 6)]})
    assert sync_blanks(db, tmp_path).baseline == 5

    gone = sync_blanks(db, tmp_path / "немає")
    assert gone.missing and gone.vanished == 0
    assert db.scalar(select(CamBlank).where(CamBlank.gone_at.is_not(None))) is None

    back = sync_blanks(db, tmp_path)
    assert (back.appeared, back.vanished) == (0, 0)
    assert pending_blanks(db) == []


# ── проба теки (діагностика перед впровадженням) ────────────────────────────
# Проба існує рівно для одного: перед вмиканням на робочому ПК побачити, чи
# розбір влучає в реальні файли цеху, і мати звіт, який можна переслати.


def test_probe_reads_without_writing_anything(db, tmp_path):
    """Головна властивість: у базу НЕ пише. Помилковий шлях або дивна тека
    нічого не псують, тому пробою безпечно тицяти наосліп."""
    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x14.blk"]})
    from app.services.cam_blanks import probe_blanks

    probe = probe_blanks(tmp_path)
    assert probe.files == 1
    assert db.scalars(select(CamBlank)).all() == [], "проба не має нічого записувати"


def test_probe_names_what_it_did_not_understand(db, tmp_path):
    """Найцінніше в звіті — саме нерозібрані назви: за ними видно, чого
    читачеві бракує на реальних даних."""
    from app.services.cam_blanks import probe_blanks

    make_tree(tmp_path, {
        "zr/12": ["12-monolith-a2-x14.blk", "щось не те.blk"],
        "zr/18": ["14-monolith-a1-x3.blk"],
    })
    probe = probe_blanks(tmp_path)
    assert (probe.files, probe.parsed, probe.unparsed) == (3, 2, 1)
    assert probe.unparsed_samples == ["zr/12/щось не те.blk"]
    assert probe.mismatched == 1
    assert "тека 18, назва 14" in probe.mismatch_samples[0]

    text = probe.as_text()
    assert "НЕ РОЗІБРАНІ назви" in text
    assert "щось не те.blk" in text
    assert "Виробники" in text and "monolith" in text


def test_probe_says_plainly_when_the_folder_is_not_there(tmp_path):
    from app.services.cam_blanks import probe_blanks

    probe = probe_blanks(tmp_path / "немає")
    assert probe.exists is False
    assert "НЕ ЗНАЙДЕНА" in probe.as_text()

    empty = probe_blanks("")
    assert "шлях не задано" in empty.as_text()


def test_probe_caps_its_samples_so_a_huge_folder_stays_readable(tmp_path):
    """На теці з десятками тисяч файлів звіт має лишатись таким, щоб його
    можна було прочитати очима й переслати повідомленням."""
    from app.services.cam_blanks import PROBE_SAMPLES, probe_blanks

    make_tree(tmp_path, {"zr/12": [f"дивна-{i}.blk" for i in range(PROBE_SAMPLES + 20)]})
    probe = probe_blanks(tmp_path)
    assert probe.unparsed == PROBE_SAMPLES + 20
    assert len(probe.unparsed_samples) == PROBE_SAMPLES


# ── стійкість до того, що робить справжній Windows ──────────────────────────
# Кожен випадок нижче перевірено наживо перед тим, як писати тест: три з
# чотирьох виявились безпечними самі, а один був справжньою вадою.


def test_case_rename_is_not_a_new_disc(db, tmp_path):
    """Windows не розрізняє регістр — наш ключ мусить так само.

    Знайдено живою пробою 08.09.26: перейменування `12-Mono-A2-x1` на
    `12-mono-a2-x1` давало appeared=1 і vanished=1, тобто в замовлення
    комірниці потрапляв би диск, якого фізично немає.
    """
    import os

    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": ["12-Mono-A2-x1.blk"]})
    sync_blanks(db, tmp_path)
    os.rename(tmp_path / "zr/12/12-Mono-A2-x1.blk", tmp_path / "zr/12/12-mono-a2-x1.blk")

    result = sync_blanks(db, tmp_path)
    assert (result.appeared, result.vanished) == (0, 0), "зміна регістру — це той самий файл"
    assert len(pending_blanks(db)) == 1


def test_absurdly_long_name_is_cut_to_the_column_width(db, tmp_path):
    """SQLite довжину не перевіряє й мовчки проковтне будь-що, але
    180-символьний «колір» у таблиці на екрані — це вже зламана верстка."""
    seed_baseline(db, tmp_path)
    long_shade = "дужедовгийколір" * 12
    make_tree(tmp_path, {"zr/12": [f"12-monolith-{long_shade}-x1.blk"]})
    sync_blanks(db, tmp_path)

    row = pending_blanks(db)[0]
    assert len(row.shade) <= 60
    assert len(row.file_name) <= 200
    assert len(row.rel_path) <= 400


def test_odd_characters_in_names_do_not_break_the_scan(db, tmp_path):
    """Дужки, апострофи, кирилиця, тире — усе це реальні назви з цеху."""
    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": [
        "12-mono-a2-x1.blk",
        "12-mono-a2 (копия)-x2.blk",
        "12-mono-a'2-x3.blk",
        "тест — тире.blk",
    ]})
    result = sync_blanks(db, tmp_path)
    assert result.appeared == 4, "жоден файл не має губитись через символи в назві"


def test_a_file_where_the_root_should_be_is_not_a_crash(tmp_path):
    """Оператор може вписати шлях до ФАЙЛУ замість теки. Це не аварія."""
    from app.services.cam_blanks import probe_blanks

    fake = tmp_path / "не-тека.txt"
    fake.write_text("x", encoding="utf-8")
    assert scan_blanks(fake) == []
    assert probe_blanks(fake).exists is False


def test_first_run_is_a_baseline_not_an_order_for_everything(db, tmp_path):
    """НАЙВАЖЛИВІШИЙ тест впровадження.

    У теці лежать диски, накопичені роками — номери доходили до 891 у групі,
    старі не прибирали. Якби перший прохід порахував їх як «щойно взяті»,
    перше ж натискання «Перечитати теку» дало б комірниці замовлення на
    десятки тисяч дисків. Перевірено до фіксу: 900 файлів перетворювались на
    рядок «Mono a2 12(300)+18(300)+25(300)».
    """
    make_tree(tmp_path, {
        "zr/12": [f"12-monolith-a2-x{i}.blk" for i in range(1, 31)],
        "zr/18": [f"18-emotions-a3-x{i}.blk" for i in range(1, 21)],
    })
    first = sync_blanks(db, tmp_path)
    assert first.baseline == 50
    assert first.appeared == 0
    assert pending_blanks(db) == [], "старі диски не мають потрапляти в замовлення"
    assert order_text(pending_blanks(db)) == ""

    # А ось диск, створений ПІСЛЯ вмикання, — це вже взятий диск.
    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x31.blk"]})
    second = sync_blanks(db, tmp_path)
    assert (second.baseline, second.appeared) == (0, 1)
    assert len(pending_blanks(db)) == 1


def test_baseline_is_set_once_even_after_the_folder_is_emptied(db, tmp_path):
    """Умова саме «таблиця порожня», а не «немає живих рядків».

    Після повної підчистки теки всі рядки стають зниклими, але база відліку
    вже стоїть. Якби її ставили вдруге, диски, створені після підчистки,
    мовчки випали б із замовлення.
    """
    import shutil

    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x1.blk"]})
    assert sync_blanks(db, tmp_path).baseline == 1

    shutil.rmtree(tmp_path / "zr")
    sync_blanks(db, tmp_path)          # усе зникло

    make_tree(tmp_path, {"zr/12": ["12-monolith-a2-x1.blk"]})
    again = sync_blanks(db, tmp_path)
    assert (again.baseline, again.appeared) == (0, 1)
    assert len(pending_blanks(db)) == 1


# ── «схоже, забули замовити» ────────────────────────────────────────────────


def test_pileup_hint_stays_quiet_for_a_normal_long_weekend(db, tmp_path):
    """Поріг за КІЛЬКІСТЮ, а не за часом — і саме тому мовчить у понеділок.

    Часовий поріг здавався природнішим, але давав би хибну тривогу щопонеділка:
    комірниця не працює у вихідні, оператори працюють, тож найстаршому диску
    законно 72 години. Хибний сигнал гірший за жодного.
    """
    from app.services.cam_blanks import BLANKS_PILEUP, pileup_note

    seed_baseline(db, tmp_path)
    # довгі вихідні: три доби по десять дисків
    names = [f"12-monolith-a2-x{i}.blk" for i in range(100, 130)]
    make_tree(tmp_path, {"zr/12": names})
    sync_blanks(db, tmp_path, now=datetime(2026, 9, 5, 19, 0))   # пʼятниця ввечері

    rows = pending_blanks(db)
    assert len(rows) == 30 < BLANKS_PILEUP
    assert pileup_note(rows) is None, "за звичайні вихідні підказка не спрацьовує"


def test_pileup_hint_names_the_number_and_the_oldest_date(db, tmp_path):
    from app.services.cam_blanks import BLANKS_PILEUP, pileup_note

    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": [f"12-monolith-a2-x{i}.blk" for i in range(200, 260)]})
    sync_blanks(db, tmp_path, now=datetime(2026, 9, 1, 10, 0))

    rows = pending_blanks(db)
    assert len(rows) >= BLANKS_PILEUP
    note = pileup_note(rows)
    assert note is not None
    assert "01.09" in note, "оператор має бачити, з якої дати тягнеться список"
    assert str(len(rows)) in note


def test_pileup_hint_is_a_hint_not_a_block(db, tmp_path):
    """Список і текст лишаються ПОВНИМИ: обрізати їх означало б тихо
    загубити частину замовлення."""
    from app.services.cam_blanks import pileup_note

    seed_baseline(db, tmp_path)
    make_tree(tmp_path, {"zr/12": [f"12-monolith-a2-x{i}.blk" for i in range(300, 360)]})
    sync_blanks(db, tmp_path)

    rows = pending_blanks(db)
    assert pileup_note(rows) is not None
    assert len(rows) == 60
    assert order_text(rows), "текст для комірниці не обрізається"


# ── Справжні назви з робочого ПК ────────────────────────────────────────────
# Зразки, на яких фічу писали, мали вигляд `12-monolith-a2-x14`. CAM на
# робочому ПК пише ІНАКШЕ, і 08.09.26 виявилось, що розбір не влучає в жоден
# файл із 19 тисяч. Диски рахувались (тотожність іде за шляхом, не за назвою),
# але замовлення для комірниці виходило купою сирих імен замість списку.
#
# Ці назви взяті з робочої машини як є. Вони тут не «приклад», а контракт:
# якщо розбір знову перестане їх розуміти, фіча тихо працюватиме наполовину.


class TestRealNamesFromTheShop:
    def test_zirconium(self):
        p = parse_blank_name("zr25_25-a1-x29.blk")
        assert (p.height, p.brand, p.shade, p.serial) == (25, "zr", "a1", 29)

    def test_zirconium_with_a_disc_diameter_prefix(self):
        """`D98_` — це діаметр диска, не матеріал. Перший підхід брав звідси
        «d» і показував би комірниці неіснуючий матеріал."""
        p = parse_blank_name("D98_zr25_25-a2-x54.blk")
        assert (p.height, p.brand, p.shade, p.serial) == (25, "zr", "a2", 54)

    def test_pmma(self):
        p = parse_blank_name("pmma25_25-a3-x261.blk")
        assert (p.height, p.brand, p.shade, p.serial) == (25, "pmma", "a3", 261)

    def test_cobalt_chrome(self):
        p = parse_blank_name("crco20_20-hpp-x18.blk")
        assert (p.height, p.brand, p.shade, p.serial) == (20, "crco", "hpp", 18)

    def test_serial_without_the_x(self):
        """Не всі назви мають `x` перед номером — стара регулярка вимагала."""
        p = parse_blank_name("pmmac25_25-pmmaProzrach-281.blk")
        assert (p.height, p.shade, p.serial) == (25, "pmmaprozrach", 281)

    def test_the_old_format_still_parses(self):
        """Формат зразків нікуди не дівся — на іншій машині CAM може писати
        саме так."""
        p = parse_blank_name("12-monolith-a2-x14.blk")
        assert (p.height, p.brand, p.shade, p.serial) == (12, "monolith", "a2", 14)

    def test_a_template_file_stays_unparsed_and_that_is_fine(self):
        """`D98_ti26` не має ні кольору, ні номера — це шаблон, а не диск.
        Він однаково рахується як узятий файл, просто без полів."""
        p = parse_blank_name("D98_ti26.blk")
        assert p.serial is None

    def test_the_order_line_is_readable_by_a_human(self):
        """Головне, заради чого все: комірниця має побачити список, а не імена
        файлів."""
        rows = [
            CamBlank(rel_path=n, file_name=n, **{
                k: v for k, v in zip(
                    ("height", "brand", "shade", "serial"),
                    (lambda p: (p.height, p.brand, p.shade, p.serial))(parse_blank_name(n)),
                )
            })
            for n in ("zr25_25-a2-x1.blk", "zr25_25-a2-x2.blk", "pmma25_20-a3-x7.blk")
        ]
        text = order_text(rows)
        assert f"zr-a2-25({COUNT_MARK}2)" in text
        assert "pmma-a3-20" in text
        assert ".blk" not in text, "у замовлення потрапили сирі імена файлів"

    # Формат C — назви зі скріну робочого ПК 10.09.26. До цього розбору вони
    # йшли комірниці сирими іменами файлів, а `A3-5` читалось як «zr, колір 5».
    @pytest.mark.parametrize("name, expected", [
        ("zr18_18-Monolith-A2-x37.blk", (18, "monolith", "a2", 37)),
        ("zr20_20-Monolith-a3-5-x24.blk", (20, "monolith", "a3-5", 24)),
        ("zr14_14-Monolith-A3-5-x40.blk", (14, "monolith", "a3-5", 40)),
        ("zr18_18-Emotions-a1-x194.blk", (18, "emotions", "a1", 194)),
        ("zr18_18-Monolith-C2-x81.blk", (18, "monolith", "c2", 81)),
        ("zr14_14-Emotions-A3-x843PRO.blk", (14, "emotions", "a3", 843)),
        ("zr14_14-s1-x80.blk", (14, "zr", "s1", 80)),
        ("pmma25_25-pmmaProzrach-362.blk", (25, "pmma", "pmmaprozrach", 362)),
        ("pmma16_16-a1-x89.blk", (16, "pmma", "a1", 89)),
    ])
    def test_names_with_a_brand_word(self, name, expected):
        p = parse_blank_name(name)
        assert (p.height, p.brand, p.shade, p.serial) == expected

    def test_brand_word_names_read_as_the_owner_writes_them(self):
        names = ("zr20_20-Monolith-a3-5-x24.blk", "zr18_18-Emotions-a1-x194.blk",
                 "zr14_14-Emotions-A3-x843PRO.blk")
        rows = [CamBlank(rel_path=n, file_name=n, first_seen_at=datetime(2026, 9, 8, 9, i))
                for i, n in enumerate(names)]
        assert sorted(order_text(rows).splitlines()) == ["emo-a1-18", "emo-a3-14", "mono-a3.5-20"]

    def test_history_rows_saved_by_the_old_parser_are_read_from_the_name(self):
        """Поля в базі пише розбір, що був у момент появи диска. Замовлені до
        навчання формату C рядки мають «zr / 5» — текст бере назву заново."""
        n = "zr14_14-Monolith-A3-5-x40.blk"
        row = CamBlank(rel_path=n, file_name=n, brand="zr", shade="5", height=None,
                       first_seen_at=datetime(2026, 9, 8, 21, 24))
        assert order_text([row]) == "mono-a3.5-14"


def test_sync_rereads_pending_rows_saved_by_the_old_parser(db, tmp_path):
    """Незамовлені рядки перечитуються новим розбором: інакше таблиця на
    екрані й прапорець «не на місці» лишились би зі старими полями."""
    seed_baseline(db, tmp_path)
    name = "zr14_14-Monolith-A3-5-x40.blk"
    make_tree(tmp_path, {"zr/14": [name]})
    sync_blanks(db, tmp_path)
    row = db.scalars(select(CamBlank).where(CamBlank.file_name == name)).one()
    row.brand, row.shade, row.height = "zr", "5", None   # як записав старий розбір
    db.commit()

    result = sync_blanks(db, tmp_path)

    assert result.reparsed == 1
    db.refresh(row)
    assert (row.brand, row.shade, row.height) == ("monolith", "a3-5", 14)
    assert sync_blanks(db, tmp_path).reparsed == 0, "другий прохід нічого не переписує"
