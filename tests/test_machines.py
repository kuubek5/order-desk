"""Верстати (Фаза 1 — живі кадри): сервіс, роути, межі.

Знімок іде тим самим app/furnace_vnc.py, що вже покритий справжнім
RFB-стендом у test_furnace_vnc.py (включно з перевіркою «жодного байта
вводу») — тут його не дублюємо, а перевіряємо ВЛАСНУ логіку модуля:
стан у пам'яті, чесність про помилки, межі роутів.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Machine
from app.services import machines as service

FIXTURES = Path(__file__).parent / "fixtures" / "furnace"


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _frame() -> Image.Image:
    return Image.open(FIXTURES / "run.png").convert("RGB")


def _add_machine(db, name="350i №1", host="192.168.1.85", port=5900, enabled=True):
    machine = Machine(
        name=name, host=host, port=port, enabled=enabled,
        sort_order=0, created_at=datetime(2026, 8, 31, 9, 0, 0),
    )
    db.add(machine)
    db.commit()
    return machine


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    service.reset_state_for_tests()
    yield
    service.reset_state_for_tests()


def test_poll_saves_frame_and_remembers_when(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "capture", lambda *a, **k: _frame())
    with Session(_database()) as db:
        _add_machine(db)
        target = service.MachineTarget(name="350i №1", host="192.168.1.85")
        state = service.poll_target(db, target, password="x")
        assert state.frame_at is not None
        assert state.error is None
    assert (tmp_path / "192.168.1.85.png").exists()


def test_dead_machine_stays_on_screen_with_its_reason(monkeypatch, tmp_path):
    """Те саме прохання власника, що з пічкою: налаштований верстат, який
    «злетів», не має тихенько пропасти — має показати причину."""
    with Session(_database()) as db:
        _add_machine(db)
        target = service.MachineTarget(name="350i №1", host="192.168.1.85")
        # Тричі: «немає зв'язку» показується лише після PROBLEM_AFTER_FAILURES
        # невдач поспіль — одна невдача це ще не обрив, а загублений пакет
        # (пом'якшення миготіння, 04.09.26).
        for _ in range(service.PROBLEM_AFTER_FAILURES):
            service.poll_target(db, target, None, error="ПК верстата не відповів за 20 с")

        cards = service.snapshot(db)
        assert [c.target.name for c in cards] == ["350i №1"]
        assert cards[0].has_problem
        assert "не відповів" in cards[0].problem_text


def test_snapshot_shows_all_configured_even_before_first_frame():
    """Щойно доданий верстат видно одразу — з «чекаємо перший кадр», а не
    порожнім екраном (у печей це відкрили пізніше і переробляли)."""
    with Session(_database()) as db:
        _add_machine(db)
        cards = service.snapshot(db)
        assert len(cards) == 1
        assert not cards[0].has_frame
        assert not cards[0].has_problem


def test_disabled_machine_is_not_polled_or_shown():
    with Session(_database()) as db:
        _add_machine(db, enabled=False)
        assert service.configured_targets(db) == []
        assert service.snapshot(db) == []


def test_stale_frame_is_admitted_honestly(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "capture", lambda *a, **k: _frame())
    with Session(_database()) as db:
        _add_machine(db)
        target = service.MachineTarget(name="350i №1", host="192.168.1.85")
        old = datetime.now() - timedelta(seconds=service.STALE_AFTER_SECONDS + 30)
        service.poll_target(db, target, "x", now=old)
        card = service.snapshot(db)[0]
        assert card.has_frame and card.stale


def test_frame_key_is_checked_against_process_state_not_the_url(monkeypatch, tmp_path):
    """`resolve_frame` віддає шлях лише для ключа, який процес справді знає.

    Це та сама межа, що на пічках: невідомий ключ з адресного рядка — None,
    а не спроба зібрати шлях із того, що написав користувач."""
    monkeypatch.setattr(service, "capture", lambda *a, **k: _frame())
    with Session(_database()) as db:
        _add_machine(db)
        target = service.MachineTarget(name="350i №1", host="192.168.1.85")
        service.poll_target(db, target, "x")

    assert service.resolve_frame("192.168.1.85") is not None
    assert service.resolve_frame("..-..-evil") is None
    assert service.resolve_frame("192.168.1.99") is None


def test_machines_settings_section_is_wired():
    """Розділ «Верстати» справді включений у налаштування і містить пастку
    порядку роутів: /password оголошений ВИЩЕ /{machine_id} (та сама пастка,
    що спіймали живою перевіркою на пічках — 422 замість збереження)."""
    from app.routers import settings as settings_router_mod

    paths = [getattr(r, "path", "") for r in settings_router_mod.router.routes]
    literal = paths.index("/settings/machines/password")
    parametric = paths.index("/settings/machines/{machine_id}")
    assert literal < parametric, "password-роут з'їсть /{machine_id} — переставити"

    page = Path("app/templates/settings.html").read_text(encoding="utf-8")
    assert "_settings_machines.html" in page


def test_warmup_takes_the_second_frame_not_the_first():
    """UltraVNC віддає перший кадр «недофарбованим» (полінг працює лише поки
    клієнт підключений — спіймано наживо на 350i: права колонка задач і
    статус-бар приходили білими). Прогрів мусить чекати НЕ відключаючись і
    брати другий кадр — тут стенд підміняє кадр після першого знімка, і
    результатом мусить бути саме другий."""
    from PIL import Image as PILImage

    from app.furnace_vnc import capture
    from tests.fake_vnc_server import FakeFurnace, frame_bytes

    first = PILImage.new("RGB", (64, 48), (255, 255, 255))   # «біла» недофарбованість
    second = PILImage.new("RGB", (64, 48), (0, 128, 255))

    with FakeFurnace(width=64, height=48, frame_rgba=frame_bytes(first)) as bench:
        # Кадр міняється одразу після старту: перший screenshot ще застане
        # білий (він знімається до сну), другий — синій.
        bench.frame_rgba = frame_bytes(second)
        got = capture("127.0.0.1", bench.port, "DEKEMA", warmup=0.3)

    assert got.getpixel((5, 5)) == (0, 128, 255), "узято перший кадр, а не другий"


# ── Авто-збір калібрувальних кадрів ─────────────────────────────────────────
# Робочий ПК має лише встановлену програму (без Python), тому кадри для
# навчання шрифта підпису вона мусить готувати сама. Ці тести стережуть, щоб
# збір робив рівно те, що обіцяно: збирав, доки цифри неповні, і мовчав, коли
# всі вивчено.


def _calib_frame(percent: int) -> Image.Image:
    """Синтетичний кадр зі смугою на заданий відсоток (як у test_machine_ocr)."""
    from PIL import ImageDraw

    img = Image.new("RGB", (1152, 864), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = 400, 700, 700, 723
    draw.rectangle((left, top, right, bottom), outline=(40, 40, 40), fill=(255, 255, 255))
    fill_w = int((right - left) * percent / 100)
    if fill_w > 0:
        draw.rectangle((left, top, left + fill_w, bottom), fill=(0, 0, 128))
    return img


def test_calibration_collects_frames_while_digits_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    # Бракує цифр — збираємо.
    monkeypatch.setattr(service, "missing_caption_digits", lambda: {"7"})

    service.collect_calibration_frame("192.168.1.85", _calib_frame(40), 40)
    service.collect_calibration_frame("192.168.1.85", _calib_frame(60), 60)

    folder = tmp_path / "calib" / "192.168.1.85"
    assert (folder / "pct-040.png").exists()
    assert (folder / "pct-060.png").exists()


def test_calibration_skips_existing_percent(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    monkeypatch.setattr(service, "missing_caption_digits", lambda: {"7"})

    service.collect_calibration_frame("m", _calib_frame(40), 40)
    first = (tmp_path / "calib" / "m" / "pct-040.png").stat().st_mtime_ns
    service.collect_calibration_frame("m", _calib_frame(40), 40)  # той самий %
    second = (tmp_path / "calib" / "m" / "pct-040.png").stat().st_mtime_ns
    assert first == second, "той самий відсоток перезаписався — має пропускатись"


def test_calibration_stops_when_font_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    # Усі цифри вивчено — не збираємо нічого.
    monkeypatch.setattr(service, "missing_caption_digits", lambda: set())

    service.collect_calibration_frame("m", _calib_frame(40), 40)
    assert not (tmp_path / "calib").exists()


def test_calibration_never_raises(monkeypatch, tmp_path):
    """Збір — зручність, не робота: жодна його помилка не сміє впасти в
    опитування верстата."""
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    monkeypatch.setattr(service, "missing_caption_digits", lambda: {"7"})

    class Boom:
        def save(self, *a, **k):
            raise OSError("диск повний")

    # Не кидає, попри збійне збереження.
    service.collect_calibration_frame("m", Boom(), 40)


def test_calibration_status_and_zip(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    monkeypatch.setattr(service, "missing_caption_digits", lambda: {"7", "8"})

    service.collect_calibration_frame("m", _calib_frame(40), 40)
    status = service.calibration_status()
    assert status["active"] is True
    assert status["frames"] == 1
    assert status["missing"] == ["7", "8"]

    import io
    import zipfile

    data = service.calibration_zip_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "m/pct-040.png" in archive.namelist()


def test_calibration_zip_route_is_not_eaten_by_the_frame_route():
    """Та сама пастка, що з паролем печі: /machines/calibration.zip мусить
    бути оголошений ВИЩЕ /machines/{key}/frame.png, інакше параметричний
    з'їв би «calibration» як ключ верстата."""
    from app.routers.machines import router

    paths = [route.path for route in router.routes]
    assert paths.index("/machines/calibration.zip") < paths.index(
        "/machines/{key}/frame.png"
    )


def test_has_program_avoids_false_not_running(monkeypatch, tmp_path):
    """Коли програма завантажена (є .iso у заголовку), а смугу відсотка на
    поточній вкладці RemiCORE не видно — картка мусить казати «йде · %?», а не
    брехати «програма не йде». Сигнал — заголовок вікна, який агент читає
    незалежно від того, який екран показує RemiCORE (бойовий випадок 04.09.26,
    верстат .76 на вкладці сітки інструментів)."""
    now = datetime(2026, 9, 4, 12, 0, 0)
    with Session(_database()) as db:
        _add_machine(db)
        target = service.MachineTarget(name="350i №1", host="192.168.1.85")
        state = service.MachineState(target=target)
        # Свіжий кадр, програма в заголовку є, але відсоток НЕ прочитався.
        state.frame_at = now
        state.percent = None
        state.iso_name = "3_16-Emotions_2026-09-04_11-25-27.iso"
        state.sum3d_id = "11-25-27"
        card = service.MachineCard(target=target, state=state, now=now)

        assert card.percent is None
        assert card.is_running is False   # без % не рахуємо як «фрезерує»
        assert card.has_program is True   # але програма завантажена
        assert card.has_frame is True


def test_timed_calibration_collects_and_dedups_by_time(monkeypatch, tmp_path):
    """Ручний збір (collect_calibration) відкладає кадр за ЧАСОМ, не за
    відсотком — для верстата, де відсоток ще не читається (нове покоління).
    Дедуп за інтервалом: два поспіль дають ОДИН файл."""
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    with service._calib_lock:
        service._calib_last_timed.clear()

    service.collect_calibration_frame_timed("192.168.1.81-8765", _calib_frame(50))
    service.collect_calibration_frame_timed("192.168.1.81-8765", _calib_frame(51))  # одразу — дедуп

    folder = tmp_path / "calib" / "192.168.1.81-8765"
    assert len(list(folder.glob("t-*.png"))) == 1, "два поспіль мали дати один кадр"

    # Мине інтервал — новий кадр дозволено.
    with service._calib_lock:
        service._calib_last_timed["192.168.1.81-8765"] = 0.0
    service.collect_calibration_frame_timed("192.168.1.81-8765", _calib_frame(60))
    assert len(list(folder.glob("t-*.png"))) == 2


def test_timed_calibration_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    with service._calib_lock:
        service._calib_last_timed.clear()

    class Boom:
        def save(self, *a, **k):
            raise OSError("диск повний")

    service.collect_calibration_frame_timed("m", Boom())  # не кидає


def test_timed_calibration_zip_includes_timed_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "MACHINE_CALIBRATION_PATH", str(tmp_path / "calib"))
    with service._calib_lock:
        service._calib_last_timed.clear()
    service.collect_calibration_frame_timed("m", _calib_frame(40))

    import io
    import zipfile
    data = service.calibration_zip_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert any(n.startswith("m/t-") for n in z.namelist())
    assert service.calibration_status()["frames"] >= 1


def test_single_failed_poll_does_not_paint_the_tile_red(monkeypatch, tmp_path):
    """Одна невдача — ще не обрив.

    У цеховій мережі губиться пакет, а ПК верстата під фрезеруванням не завжди
    відповідає за 3 с. Раніше вистачало ОДНОГО невдалого опитування, щоб
    плитка почервоніла до наступного тіку — оператор читав це як «зв'язок
    постійно обривається» (скарга 04.09.26). Справжній обрив видно за ~15 с.
    """
    from app.services import machines as service

    service.reset_state_for_tests()
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    target = service.MachineTarget(name="350i", host="10.0.0.9", port=8765, agent_token="t")

    for attempt in range(1, service.PROBLEM_AFTER_FAILURES + 1):
        state = service.poll_target(None, target, None, error="ПК не відповів")
        card = service.MachineCard(target=target, state=state, now=datetime.now())
        expected = attempt >= service.PROBLEM_AFTER_FAILURES
        assert card.has_problem is expected, f"спроба {attempt}"
        # Причина записана з ПЕРШОЇ невдачі — просто ще не показується.
        assert state.error == "ПК не відповів"


def test_machine_matches_a_reworked_order_by_its_redo_id(monkeypatch, tmp_path):
    """Верстат, що фрезерує ПЕРЕРОБКУ, мусить знайти свою роботу.

    Для переробленої роботи живий Sum3D ID лежить не в Order, а в її
    ReworkRecord (колонка W таблиці) — саме туди пише оператор і саме його
    показує рядок черги. Пошук лише по Order давав «немає в черзі» при тому,
    що робота лежала поруч, а ID на екрані верстата був правильний (бойовий
    випадок 250-New, 04.09.26).
    """
    from app.models import Order, ReworkRecord
    from app.services import machines as service

    service.reset_state_for_tests()
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)

    with Session(_database()) as db:
        order = Order(
            source="lab", sheet_tab="04.09.26", row_number=7,
            work_order_no="29702", status="прораховано",
            sum3d_id="11-29-59",           # перше фрезерування, колонка L
        )
        db.add(order)
        db.flush()
        db.add(ReworkRecord(order_id=order.id, blame="обладнання",
                            sum3d_id="18-44-57"))   # переробка, колонка W
        _add_machine(db, name="250-New", host="10.0.0.64", port=8765)
        db.commit()

        target = service.configured_targets(db)[0]
        state = service._states.setdefault(
            target.key, service.MachineState(target=target)
        )
        state.frame_at = datetime.now()
        state.sum3d_id = "18-44-57"        # саме це читається з екрана верстата

        card = service.snapshot(db)[0]
        assert card.sum3d_id == "18-44-57"
        assert card.order is not None, "переробка мусить знайтись"
        assert card.order.work_order_no == "29702"


def test_no_match_says_why_and_duplicates_show_all_orders(monkeypatch, tmp_path):
    """Дубль ID — це НЕ помилка: показуємо всі роботи.

    Один проєкт Sum3D може містити кілька робіт з однієї заготовки (підтвердив
    власник 04.09.26), тож правильна відповідь — показати обидві, а не мовчати.
    А там, де пари справді немає, картка називає причину: такого ID немає
    ніде чи робота вже в архіві.
    """
    from app.models import Order
    from app.services import machines as service

    service.reset_state_for_tests()
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)

    with Session(_database()) as db:
        db.add(Order(source="lab", sheet_tab="04.09.26", row_number=1,
                     work_order_no="A", status="нове", sum3d_id="11-11-11"))
        db.add(Order(source="lab", sheet_tab="04.09.26", row_number=2,
                     work_order_no="B", status="нове", sum3d_id="11-11-11"))
        db.add(Order(source="lab", sheet_tab="01.08.26", row_number=3,
                     work_order_no="C", status="видано", sum3d_id="22-22-22",
                     archived_at=datetime(2026, 8, 20, 9, 0)))
        for i, host in enumerate(("10.0.0.1", "10.0.0.2", "10.0.0.3")):
            _add_machine(db, name=f"м{i}", host=host, port=8765)
        db.commit()

        want = {"10.0.0.1-8765": "11-11-11", "10.0.0.2-8765": "22-22-22",
                "10.0.0.3-8765": "33-33-33"}
        for target in service.configured_targets(db):
            st = service._states.setdefault(
                target.key, service.MachineState(target=target)
            )
            st.frame_at = datetime.now()
            st.sum3d_id = want[target.key]

        cards = {c.target.host: c for c in service.snapshot(db)}
        notes = {h: c.match_note for h, c in cards.items()}

    assert notes["10.0.0.1"] == "", "дубль ID — не причина мовчати"
    assert "архів" in notes["10.0.0.2"]        # робота архівна
    assert "жодна" in notes["10.0.0.3"]        # такого ID немає ніде
    assert [o.work_order_no for o in cards["10.0.0.1"].orders] == ["A", "B"]


def test_outage_history_counts_drops_and_durations(monkeypatch, tmp_path):
    """Історія обривів: скільки разів рвалось і на скільки.

    На питання «зв'язок періодично обривається» дотепер не було чим
    відповісти, крім відчуття. Обрив записується РАЗ (на третій невдачі, коли
    його визнано), закривається на відновленні — тож мовчазний верстат не
    множить записи щотіку.
    """
    from app.services import machines as service

    service.reset_state_for_tests()
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    monkeypatch.setattr(service, "capture", lambda *a, **k: _frame())
    target = service.MachineTarget(name="350i", host="10.0.0.7")

    service.poll_target(None, target, "x")                       # живий
    for _ in range(service.PROBLEM_AFTER_FAILURES + 4):          # обрив
        service.poll_target(None, target, "x", error="ПК не відповів")
    state = service.poll_target(None, target, "x")               # відновився

    assert len(state.outages) == 1, "обрив записується раз, а не щотіку"
    start, end, reason = state.outages[0]
    assert end is not None and end >= start
    assert reason == "ПК не відповів"

    card = service.MachineCard(target=target, state=state, now=datetime.now())
    assert "обривів: 1" in card.link_report
    assert card.link_outages and "–" in card.link_outages[0]


def test_machines_poll_pauses_for_any_open_details():
    """Полл не сміє згортати РОЗГОРНУТИЙ блок на картці верстата.

    Умова стоїть на будь-який <details>, а не на окремий клас. Спершу вона
    ловила лише повний кадр (`.fu-frame[open]`), і коли поруч зʼявились обриви
    звʼязку та перелік вікон, вони згортались самі через півсекунди — полл
    приносив розмітку із закритим блоком (скарга 05.09.26). Тест сторожить
    саме узагальнення: новий розгортний блок мусить працювати без правок тут.
    """
    import re
    from pathlib import Path

    src = Path("app/templates/machines.html").read_text(encoding="utf-8")
    trigger = re.search(r'hx-trigger="([^"]+)"', src)
    assert trigger, "у машинного полла зник hx-trigger"
    condition = trigger.group(1)
    assert "#machine-cards details[open]" in condition, condition
    # Саме УМОВА, а не файл: у коментарі поруч старий селектор згадується
    # навмисно, як пояснення.
    assert "fu-frame" not in condition, "умова знову звузилась до одного класу"
