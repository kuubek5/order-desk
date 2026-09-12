"""Скринька невідомих екранів: рядок на відпечаток, файли на диску, стеля.

Що тут насправді захищається:

- **Скринька не має права валити опитування.** `note()` не кидає НІКОЛИ — ні на
  порожньому кадрі, ні на невідомій причині, ні на обʼєкті, який картинкою й не
  є. Падіння тут зупинило б цикл опитування всіх пристроїв заради зручності.
- **Дублі — умова існування фічі.** Кадр раз на 6 с; якби кожен повтор давав
  рядок і два PNG, скринька зʼїла б диск за добу. Тому: той самий відпечаток —
  той самий рядок, і навіть лічильник чіпається не частіше `TOUCH_EVERY_SECONDS`.
- **Виріз зони — у РІДНОМУ масштабі.** На ньому вчать еталон; зменшена копія
  растрового шрифту перетворює навчання на вгадування. Повний кадр, навпаки,
  тиснеться до `STORED_MAX_SIDE` — він потрібен лише як контекст.
- **Стеля витісняє в правильному порядку**: спершу «неважливо», далі найрідше
  бачене; разом із рядком мусять зникати ФАЙЛИ, інакше стеля тримає базу, а не
  диск.

Дві пастки самої перевірки:

1. `screen_inbox` робить `from app.config import DATA_DIR`, тобто тримає ВЛАСНЕ
   імʼя. `monkeypatch.setattr(app.config, "DATA_DIR", …)` тут мовчазний no-op:
   `root()` читає модульний глобал `screen_inbox.DATA_DIR`, і тест писав би PNG
   у справжню теку даних. Патчимо саме його (перевірено: `root()` після підміни
   дає новий шлях).
2. `_touched` і `_evicted` живуть на ПРОЦЕС. Без `reset_state_for_tests()` між
   тестами перший же файл «затикає» лічильник наступному, і тест про повтор
   стає зеленим і порожнім.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from app.models import ScreenPuzzle
from app.services import screen_inbox as si


# ---------------------------------------------------------------- допоміжне

class _Clock:
    """Годинник, яким керує тест.

    `screen_inbox` бере час лише через `time.monotonic()`, тому підміняємо
    модульне імʼя `time` цілком — це локально для модуля й не чіпає stdlib
    (глобальний патч `time.monotonic` дістав би й `log_throttle`).
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _frame(
    width: int = 1000, height: int = 800, *, shift: int = 0, base: int = 18,
    band: int | None = None, bands: int = 24, accent=(200, 60, 60),
):
    """Синтетичний «екран»: шапка, велика панель, смуги — розкладка, не шум.

    `band` — біла смуга на своєму місці; саме нею робляться РІЗНІ екрани.
    Тотожність вирішує СЕРЕДНЯ різниця яскравості з порогом `NOVELTY_THRESHOLD`,
    тож дрібний зсув чи інші цифри — це той самий екран (і добре, бо в цеху так
    і є: годинник на табло йде, розкладка стоїть), а от смуга в іншому місці
    міняє кадр помітно — як справжній інший екран.
    """
    img = Image.new("RGB", (width, height), (base, base, base + 6))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, height // 8], fill=(60, 90, 140))
    draw.rectangle([40 + shift, height // 5, width - 40, height - height // 5], fill=accent)
    for i in range(4):
        y = height - height // 6 + i * 8
        draw.line([40, y, width - 40, y], fill=(140, 140, 140), width=3)
    if band is not None:
        step = height / bands
        draw.rectangle([0, int(band * step), width, int((band + 1) * step)], fill=(255, 255, 255))
    return img


def _zone(width: int = 140, height: int = 44):
    """Виріз зони — маленький і в рідному масштабі."""
    img = Image.new("RGB", (width, height), (0, 0, 0))
    ImageDraw.Draw(img).text((6, 6), "1250", fill=(255, 255, 255))
    return img


def _rows(db) -> list[ScreenPuzzle]:
    return list(db.scalars(select(ScreenPuzzle).order_by(ScreenPuzzle.id)).all())


def _count(db) -> int:
    return db.scalar(select(func.count()).select_from(ScreenPuzzle)) or 0


def _fill_device(db, key: str, how_many: int) -> list[ScreenPuzzle]:
    """Набити скриньку пристрою різними екранами (маленькими — заради швидкості)."""
    for i in range(how_many):
        got = si.note(
            db,
            kind=si.KIND_MACHINE,
            key=key,
            name="Верстат",
            frame=_frame(240, 180, band=i),
            reason="layout_unknown",
            detail=f"екран {i}",
        )
        assert got is not None, f"екран {i} не відклався — підписи виявились схожими?"
    rows = _rows(db)
    assert len(rows) == how_many
    return rows


# ---------------------------------------------------------------- фікстури

@pytest.fixture(autouse=True)
def inbox_root(tmp_path, monkeypatch):
    """Тека скриньки — у `tmp_path`, стан модуля — чистий до й після тесту."""
    monkeypatch.setattr(si, "DATA_DIR", tmp_path)
    si.reset_state_for_tests()
    assert si.root() == tmp_path / "screen_puzzles", "патч DATA_DIR не влучив у root()"
    yield tmp_path
    si.reset_state_for_tests()


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(si, "time", fake)
    return fake


# ---------------------------------------------------------------- 1. перший кадр

def test_first_note_writes_the_row_and_both_pictures(db_session, inbox_root):
    """Новий екран: рядок із `seen_count=1` і ДВА файли — кадр і виріз."""
    frame = _frame(1000, 800)
    puzzle_id = si.note(
        db_session,
        kind=si.KIND_MACHINE,
        key="192.168.1.27-8765",
        name="CORiTEC 250i",
        frame=frame,
        reason="glyph_unknown",
        detail="символ 3 з «16-38-52»",
        zone_crop=_zone(140, 44),
    )
    assert puzzle_id is not None

    (row,) = _rows(db_session)
    assert row.id == puzzle_id
    assert row.seen_count == 1
    assert row.device_name == "CORiTEC 250i"
    assert row.reason == "glyph_unknown"
    assert row.detail == "символ 3 з «16-38-52»"
    assert row.frame_file and row.zone_file
    assert row.frame_file != row.zone_file, "виріз мусить бути ОКРЕМИМ файлом"

    where = si.folder(si.KIND_MACHINE, "192.168.1.27-8765")
    assert inbox_root in where.parents, "PNG поїхав повз tmp_path"
    assert sorted(p.name for p in where.iterdir()) == sorted([row.frame_file, row.zone_file])

    # Виріз — у РІДНОМУ масштабі: на ньому вчать еталон.
    with Image.open(where / row.zone_file) as zone:
        assert zone.size == (140, 44)

    # Повний кадр — лише контекст, тому тиснеться до STORED_MAX_SIDE.
    with Image.open(where / row.frame_file) as stored:
        assert max(stored.size) == si.STORED_MAX_SIDE
        assert stored.size == (640, 512)

    assert si.image_path(row) == where / row.frame_file
    assert si.image_path(row, "zone") == where / row.zone_file


def test_small_frame_is_stored_as_is(db_session, inbox_root):
    """Кадр, менший за стелю, не «розтягується» до неї — тиснути нема чого."""
    si.note(
        db_session,
        kind=si.KIND_FURNACE,
        key="pich-1",
        name="Піч 1",
        frame=_frame(320, 240),
        reason="zone_clipped",
    )
    (row,) = _rows(db_session)
    with Image.open(si.image_path(row)) as stored:
        assert stored.size == (320, 240)
    assert row.zone_file is None


# ---------------------------------------------------------------- 2. повтор

def test_repeat_stays_quiet_inside_the_window_then_bumps_the_counter(db_session, clock):
    """Той самий кадр: другого рядка немає, лічильник — не частіше раза на хвилину."""
    frame = _frame(400, 300)
    first = datetime(2026, 9, 12, 10, 0, 0)
    puzzle_id = si.note(
        db_session, kind=si.KIND_MACHINE, key="m1", name="Верстат", frame=frame,
        reason="layout_unknown", now=first,
    )
    assert puzzle_id is not None

    # Ще 9 кадрів у межах вікна — жодного дотику до бази.
    for step in range(1, 10):
        clock.sleep(si.TOUCH_EVERY_SECONDS / 10)
        assert si.note(
            db_session, kind=si.KIND_MACHINE, key="m1", name="Верстат", frame=frame,
            reason="layout_unknown", now=datetime(2026, 9, 12, 10, 0, step),
        ) is None
    (row,) = _rows(db_session)
    assert row.seen_count == 1
    assert row.last_seen_at == first

    # Вікно минуло — той самий рядок, лічильник і час зсунулись.
    clock.sleep(si.TOUCH_EVERY_SECONDS + 1)
    later = datetime(2026, 9, 12, 10, 5, 0)
    again = si.note(
        db_session, kind=si.KIND_MACHINE, key="m1", name="Верстат 250i", frame=frame,
        reason="layout_unknown", now=later,
    )
    assert again == puzzle_id
    assert _count(db_session) == 1
    (row,) = _rows(db_session)
    assert row.seen_count == 2
    assert row.last_seen_at == later
    assert row.first_seen_at == first
    # Назва пристрою — свіжа: пічку перейменують, а загадка лишиться зрозумілою.
    assert row.device_name == "Верстат 250i"


def test_the_same_screen_on_another_device_is_its_own_row(db_session, clock):
    """Вікно тиші й стеля — на ПРИСТРІЙ: сусід не має ховати свій екран."""
    frame = _frame(400, 300)
    one = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A", frame=frame,
                  reason="layout_unknown")
    two = si.note(db_session, kind=si.KIND_MACHINE, key="m2", name="B", frame=frame,
                  reason="layout_unknown")
    assert one is not None and two is not None and one != two
    assert _count(db_session) == 2


# ---------------------------------------------------------------- 3. відпечаток

def test_a_different_screen_makes_a_second_row(db_session, clock):
    """Інша розкладка — інший відпечаток, інший рядок."""
    first = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A",
                    frame=_frame(400, 300), reason="layout_unknown")
    second = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A",
                     frame=_frame(400, 300, shift=60, accent=(20, 180, 90)),
                     reason="layout_unknown")
    assert first is not None and second is not None and first != second
    rows = _rows(db_session)
    assert len(rows) == 2
    assert rows[0].fingerprint != rows[1].fingerprint


def test_the_same_layout_with_other_digits_is_the_same_screen(db_session, clock):
    """Змінені цифри на тій самій розкладці — ТОЙ САМИЙ екран.

    Саме на цьому тримається вся фіча: кадр знімається раз на 6 с, а годинник,
    відсоток і назва програми на екрані міняються постійно. Якби кожна така
    зміна давала новий рядок, скринька набивала б стелю за хвилини й витісняла
    сама себе — і рідкісний екран, заради якого вона існує, вилітав би раніше,
    ніж людина його побачить.

    Тому тотожність вирішує НЕ хеш (він ламається від кількох пікселів), а
    середня різниця мініатюр із порогом `NOVELTY_THRESHOLD` — той самий спосіб,
    що у відборі калібрувальних кадрів верстатів.
    """
    plain = _frame(800, 600)
    with_digits = plain.copy()
    draw = ImageDraw.Draw(with_digits)
    for x in range(0, 300, 60):
        draw.text((200 + x, 300), "88:31:07", fill=(255, 255, 255))
    # Хеш на такій парі РОЗХОДИТЬСЯ — і це нормально, він лише імʼя файлу.
    assert si.fingerprint(plain) != si.fingerprint(with_digits)
    assert (
        si.distance(si.signature(plain), si.signature(with_digits))
        <= si.NOVELTY_THRESHOLD[si.KIND_MACHINE]
    )

    first = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A", frame=plain,
                    reason="layout_unknown")
    clock.sleep(si.TOUCH_EVERY_SECONDS + 1)
    second = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A", frame=with_digits,
                     reason="layout_unknown")
    assert second == first
    assert _count(db_session) == 1


# ---------------------------------------------------------------- 4. стеля

def test_eviction_takes_the_dismissed_row_first(db_session):
    """«Неважливо» йде першим — навіть якщо його бачили найчастіше."""
    rows = _fill_device(db_session, "m1", si.MAX_PER_DEVICE)
    for i, row in enumerate(rows):
        row.seen_count = 2 + i          # найрідше баченим є rows[0]
    victim = rows[5]
    victim.dismissed = True
    victim.seen_count = 999             # ознака «неважливо» мусить перебити частоту
    db_session.commit()

    where = si.folder(si.KIND_MACHINE, "m1")
    victim_frame = where / victim.frame_file
    victim_id = victim.id
    assert victim_frame.exists()

    fresh = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="Верстат",
                    frame=_frame(240, 180, accent=(10, 200, 90)), reason="status_split")
    assert fresh is not None

    assert _count(db_session) == si.MAX_PER_DEVICE
    assert db_session.get(ScreenPuzzle, victim_id) is None
    assert not victim_frame.exists(), "рядок пішов, а PNG лишився — стеля тримає базу, не диск"
    # Найрідше бачений, але потрібний рядок лишився на місці.
    assert db_session.get(ScreenPuzzle, rows[0].id) is not None


def test_eviction_takes_the_least_seen_row_when_none_dismissed(db_session):
    """Без «неважливо» жертва — найрідше бачений рядок, а не найдавніший."""
    rows = _fill_device(db_session, "m1", si.MAX_PER_DEVICE)
    for i, row in enumerate(rows):
        row.seen_count = 50 + i
        row.last_seen_at = datetime(2026, 9, 12, 8, 0, 0)
    rarest = rows[9]
    rarest.seen_count = 1
    # Найдавніший — ІНШИЙ рядок: вік сам по собі про цінність нічого не каже.
    rows[0].last_seen_at = datetime(2026, 1, 1, 0, 0, 0)
    db_session.commit()

    where = si.folder(si.KIND_MACHINE, "m1")
    rarest_frame = where / rarest.frame_file
    rarest_id = rarest.id

    assert si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="Верстат",
                   frame=_frame(240, 180, accent=(10, 200, 90)), reason="status_split") is not None

    assert _count(db_session) == si.MAX_PER_DEVICE
    assert db_session.get(ScreenPuzzle, rarest_id) is None
    assert not rarest_frame.exists()
    assert db_session.get(ScreenPuzzle, rows[0].id) is not None, "витіснили за віком, а не за частотою"


def test_a_full_device_does_not_evict_its_neighbour(db_session):
    """Стеля рахується на пристрій: повний верстат не чіпає рядків печі."""
    _fill_device(db_session, "m1", si.MAX_PER_DEVICE)
    si.note(db_session, kind=si.KIND_FURNACE, key="pich-1", name="Піч 1",
            frame=_frame(240, 180, accent=(240, 220, 40)), reason="layout_unknown")
    assert _count(db_session) == si.MAX_PER_DEVICE + 1

    si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="Верстат",
            frame=_frame(240, 180, accent=(10, 200, 90)), reason="status_split")
    survivors = db_session.scalar(
        select(func.count()).select_from(ScreenPuzzle).where(ScreenPuzzle.device_key == "pich-1")
    )
    assert survivors == 1
    assert _count(db_session) == si.MAX_PER_DEVICE + 1


# ---------------------------------------------------------------- 5. кривий вхід

@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"frame": None, "reason": "layout_unknown"}, id="no-frame"),
        pytest.param({"frame": _frame(200, 150), "reason": "хтозна"}, id="unknown-reason"),
        pytest.param({"frame": object(), "reason": "layout_unknown"}, id="not-an-image"),
        pytest.param({"frame": _frame(200, 150), "reason": ""}, id="empty-reason"),
    ],
)
def test_note_never_raises_on_broken_input(db_session, kwargs):
    """Скринька — зручність, а не робота: падіння тут зупинило б опитування."""
    assert si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A", **kwargs) is None
    assert _count(db_session) == 0


def test_the_session_still_works_after_a_broken_frame(db_session):
    """Після відкату сесія жива — наступний кадр відкладається нормально."""
    assert si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A",
                   frame=object(), reason="layout_unknown") is None
    assert si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A",
                   frame=_frame(240, 180), reason="layout_unknown") is not None
    assert _count(db_session) == 1


# ---------------------------------------------------------------- 6. причина

def test_pick_reason_returns_the_most_important_one():
    """Одна загадка на кадр: чужий екран провалює всі зони одразу."""
    assert si.pick_reason(["glyph_unknown", "status_split", "zone_clipped"]) == "status_split"
    assert si.pick_reason(["newgen_unread", "layout_unknown"]) == "layout_unknown"
    assert si.pick_reason(["pattern_mismatch", "newgen_unread"]) == "pattern_mismatch"
    assert si.pick_reason(["glyph_unknown"]) == "glyph_unknown"
    # Порядок аргументів нічого не вирішує — вирішує REASON_PRIORITY.
    assert si.pick_reason(["status_split", "layout_unknown"]) == "layout_unknown"


def test_pick_reason_ignores_codes_it_does_not_know():
    assert si.pick_reason(["хтозна", "reason_x"]) is None
    assert si.pick_reason([]) is None
    assert si.pick_reason(["хтозна", "zone_clipped"]) == "zone_clipped"


# ---------------------------------------------------------------- 7. підпис / забути

def test_set_label_writes_the_answer_and_clears_it_back(db_session):
    """Підпис — дані: зʼявився автор і час, порожній підпис їх прибирає."""
    puzzle_id = si.note(db_session, kind=si.KIND_FURNACE, key="pich-1", name="Піч 1",
                        frame=_frame(240, 180), reason="layout_unknown")
    when = datetime(2026, 9, 12, 11, 30, 0)

    row = si.set_label(db_session, puzzle_id, "  екран калібрування  ", user_id=7, now=when)
    assert row is not None
    assert row.label == "екран калібрування"
    assert row.labeled_at == when
    assert row.labeled_by_id == 7

    row = si.set_label(db_session, puzzle_id, "", user_id=7)
    assert row.label == ""
    assert row.labeled_at is None
    assert row.labeled_by_id is None

    assert si.set_label(db_session, 999999, "щось") is None


def test_set_dismissed_flips_the_flag_and_keeps_the_counter(db_session):
    """«Не питай більше» не стирає рядок: частота колись переверне рішення."""
    puzzle_id = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A",
                        frame=_frame(240, 180), reason="layout_unknown")
    row = si.set_dismissed(db_session, puzzle_id)
    assert row.dismissed is True
    assert row.seen_count == 1
    assert si.listing(db_session) == []
    assert len(si.listing(db_session, include_dismissed=True)) == 1
    assert si.counts(db_session) == {"всього": 1, "без_підпису": 0, "неважливих": 1}

    row = si.set_dismissed(db_session, puzzle_id, False)
    assert row.dismissed is False
    assert len(si.listing(db_session)) == 1
    assert si.set_dismissed(db_session, 999999) is None


def test_forget_removes_the_row_and_the_files(db_session, clock):
    """Забути — значить і з диска: інакше тека росте без жодного рядка в базі."""
    frame = _frame(240, 180)
    puzzle_id = si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A", frame=frame,
                        reason="glyph_unknown", zone_crop=_zone())
    row = db_session.get(ScreenPuzzle, puzzle_id)
    where = si.folder(si.KIND_MACHINE, "m1")
    files = [where / row.frame_file, where / row.zone_file]
    assert all(p.exists() for p in files)

    assert si.forget(db_session, puzzle_id) is True
    assert _count(db_session) == 0
    assert not any(p.exists() for p in files)
    assert si.forget(db_session, puzzle_id) is False

    # Вікно тиші теж забуте — той самий кадр відкладається знову, без чекання.
    assert si.note(db_session, kind=si.KIND_MACHINE, key="m1", name="A", frame=frame,
                   reason="glyph_unknown") is not None


# ---------------------------------------------------------------- 8. as_dict

def test_as_dict_speaks_human_words(db_session):
    """Словник для MCP і екрана: причина словами, лічильник, ознака вирізу."""
    puzzle_id = si.note(
        db_session, kind=si.KIND_MACHINE, key="192.168.1.27-8765", name="CORiTEC 250i",
        frame=_frame(240, 180), reason="glyph_unknown", detail="символ 3",
        zone_crop=_zone(), now=datetime(2026, 9, 12, 9, 15, 30),
    )
    row = db_session.get(ScreenPuzzle, puzzle_id)
    data = si.as_dict(row)

    assert data["бачено"] == 1
    assert data["причина"] == "немає еталона символу"
    assert data["причина_код"] == "glyph_unknown"
    assert data["є_виріз"] is True
    assert data["вид"] == "верстат"
    assert data["пристрій"] == "CORiTEC 250i"
    assert data["ключ"] == "192.168.1.27-8765"
    assert data["подробиці"] == "символ 3"
    assert data["уперше"] == "2026-09-12 09:15:30"
    assert data["востаннє"] == "2026-09-12 09:15:30"
    assert data["неважливо"] is False


def test_as_dict_says_there_is_no_crop_and_falls_back_to_the_key(db_session):
    """Без вирізу — `є_виріз` False; без назви пристрою показуємо ключ."""
    puzzle_id = si.note(db_session, kind=si.KIND_FURNACE, key="pich-4", name="",
                        frame=_frame(240, 180), reason="status_split")
    data = si.as_dict(db_session.get(ScreenPuzzle, puzzle_id))
    assert data["є_виріз"] is False
    assert data["вид"] == "піч"
    assert data["пристрій"] == "pich-4"
    assert data["причина"] == "сигнали статусу не сходяться"


# ------------------------------------------------- поріг на СПРАВЖНІХ кадрах

REAL = Path(__file__).resolve().parent / "fixtures"

# Пари кадрів із цеху й те, чим вони мусять бути одне одному. Синтетика тут не
# годиться: поріг `NOVELTY_THRESHOLD` — це число про справжні екрани, і саме на
# них він одного разу вже виявився хибним (хеш давав новий рядок на кожен кадр,
# бо на екрані йдуть годинник, відсоток і назва програми).
SAME_SCREEN = [
    # Той самий екран JOBS, різні назва програми й час.
    ("newgen_250i_16-14-29.png", "newgen_250i_16-38-52.png"),
    # Та сама смуга прогресу, 0 % і 30 %.
    ("newgen_progress_0.png", "newgen_progress_30.png"),
    # SISMA пише шар / розрівнює порошок — друк іде в обох.
    ("sisma_printing_250.png", "sisma_recoating_253.png"),
]
OTHER_SCREEN = [
    ("newgen_summary_done.png", "newgen_validate_56.png"),
    ("sisma_idle.png", "sisma_printing_250.png"),
]


@pytest.mark.parametrize("left,right", SAME_SCREEN)
def test_real_frames_of_one_screen_stay_one_screen(left, right):
    """Цехові кадри однієї розкладки — один екран, попри інші цифри на ньому."""
    with Image.open(REAL / left) as a, Image.open(REAL / right) as b:
        gap = si.distance(si.signature(a.convert("RGB")), si.signature(b.convert("RGB")))
    limit = si.NOVELTY_THRESHOLD[si.KIND_MACHINE]
    assert gap <= limit, f"{left} і {right} розійшлись на {gap:.2f} (поріг {limit})"


@pytest.mark.parametrize("left,right", OTHER_SCREEN)
def test_real_frames_of_different_screens_stay_apart(left, right):
    """І навпаки: різні екрани не мають злипатись — інакше поріг просто глухий."""
    with Image.open(REAL / left) as a, Image.open(REAL / right) as b:
        gap = si.distance(si.signature(a.convert("RGB")), si.signature(b.convert("RGB")))
    limit = si.NOVELTY_THRESHOLD[si.KIND_MACHINE]
    assert gap > limit, f"{left} і {right} злились ({gap:.2f}, поріг {limit})"


def test_two_real_frames_of_one_screen_make_one_row(db_session, clock):
    """Те саме, але через `note()`: два кадри JOBS — один рядок із лічильником 2."""
    with Image.open(REAL / "newgen_250i_16-14-29.png") as a:
        first_frame = a.convert("RGB")
    with Image.open(REAL / "newgen_250i_16-38-52.png") as b:
        second_frame = b.convert("RGB")

    first = si.note(db_session, kind=si.KIND_MACHINE, key="250i", name="CORiTEC",
                    frame=first_frame, reason="newgen_unread", detail="цифра №3")
    clock.sleep(si.TOUCH_EVERY_SECONDS + 1)
    second = si.note(db_session, kind=si.KIND_MACHINE, key="250i", name="CORiTEC",
                     frame=second_frame, reason="newgen_unread", detail="цифра №3")

    assert second == first
    rows = _rows(db_session)
    assert len(rows) == 1 and rows[0].seen_count == 2
