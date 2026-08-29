"""Печі: читання табло, правила запису, межі роутів.

Два еталонні кадри в tests/fixtures/furnace — це справжні знімки печі
192.168.1.76 (B&R Lasal) у двох станах: програма йде (RUN, 759 °C, лишилось
26:59) і піч у спокої (WAIT, 40 °C, 00:00:00). Вони ж були калібрувальними,
тому головна цінність тестів нижче — не «розпізнає», а «не вигадує»: варто
зіпсувати цифру, і поле мусить стати порожнім, а не правдоподібно неправильним.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.furnace_ocr import (
    STATUS_RUN,
    STATUS_UNKNOWN,
    STATUS_WAIT,
    format_remaining,
    parse_clock,
    parse_temperature,
    read_panel,
)
from app.furnace_vnc import FurnaceVncError
from app.models import FurnaceReading, User
from app.services import furnace as service

FIXTURES = Path(__file__).parent / "fixtures" / "furnace"


def _frame(name: str) -> Image.Image:
    return Image.open(FIXTURES / f"{name}.png").convert("RGB")


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture(autouse=True)
def _clean_state():
    service.reset_state_for_tests()
    yield
    service.reset_state_for_tests()


# ── Читання табло ───────────────────────────────────────────────────────────


def test_running_frame_reads_all_three_numbers():
    reading = read_panel(_frame("run"))
    assert reading.status == STATUS_RUN
    assert reading.temp_c == 759
    assert reading.remaining_seconds == 26 * 60 + 59
    assert reading.command == "T008.A990"
    assert reading.warnings == []


def test_idle_frame_reads_as_wait_with_zero_left():
    reading = read_panel(_frame("wait"))
    assert reading.status == STATUS_WAIT
    assert reading.temp_c == 40
    assert reading.remaining_seconds == 0
    assert reading.command == "C0"


def test_all_three_signals_agree_on_each_frame():
    """Слово, кнопка й «срок» мають казати одне. Розбіжність — це ознака, що
    табло змінилось, і саме її ми хочемо бачити як «?», а не як тихий вибір
    одного з сигналів."""
    for name, expected in (("run", STATUS_RUN), ("wait", STATUS_WAIT)):
        signals = read_panel(_frame(name)).signals
        assert set(signals) == {"word", "button", "remaining"}
        assert set(signals.values()) == {expected}


def test_damaged_digit_yields_empty_value_not_a_guess():
    """Головна властивість усього модуля: зіпсована цифра дає порожнє поле.

    Замальовуємо середину цифри температури кольором тла — форма перестає
    збігатись із будь-яким еталоном. Правильна поведінка — None плюс
    попередження, а НЕ найближча схожа цифра.
    """
    frame = _frame("run")
    for x in range(80, 92):
        for y in range(28, 46):
            frame.putpixel((x, y), (247, 243, 247))
    reading = read_panel(frame)
    assert reading.temp_c is None
    assert reading.fields["temp"].text is None
    assert any("Температуру" in w for w in reading.warnings)
    # Решта табло не постраждала — статус і залишок читаються далі.
    assert reading.status == STATUS_RUN
    assert reading.remaining_seconds == 26 * 60 + 59


def test_contradicting_signals_give_unknown_status():
    """RUN зверху, але кнопка «Запустить» унизу — так табло не виглядає ніколи.
    Отже ми дивимось не туди, і чесна відповідь — «?»."""
    frame = _frame("run")
    for x in range(560, 640):
        for y in range(545, 585):
            frame.putpixel((x, y), (0, 243, 0))
    reading = read_panel(frame)
    assert reading.status == STATUS_UNKNOWN


def test_unexpected_screen_size_is_refused_not_rescaled():
    """Інша роздільність означає інше табло. Масштабувати не можна: розмиті
    пікселі перетворили б порівняння з еталоном на вгадування."""
    reading = read_panel(_frame("run").resize((640, 480)))
    assert reading.status == STATUS_UNKNOWN
    assert reading.temp_c is None
    assert any("розмір екрана" in w for w in reading.warnings)


@pytest.mark.parametrize(
    "text, expected",
    [("00:26:59", 1619), ("01:00:00", 3600), ("00:60:00", None), ("0:26:59", None), (None, None)],
)
def test_parse_clock_rejects_impossible_times(text, expected):
    assert parse_clock(text) == expected


@pytest.mark.parametrize("text, expected", [("759", 759), ("0", 0), ("9999", None), ("7?9", None)])
def test_parse_temperature_rejects_impossible_values(text, expected):
    assert parse_temperature(text) == expected


def test_format_remaining_shows_dash_for_unknown():
    """«—», а не «0:00»: нуль виглядає як справжня відповідь «щойно закінчила»."""
    assert format_remaining(None) == "—"
    assert format_remaining(59) == "0:59"
    assert format_remaining(3661) == "1:01:01"


# ── Перелік печей ───────────────────────────────────────────────────────────


def test_parse_targets_accepts_name_port_and_plain_address():
    targets = service.parse_targets("Піч 1=192.168.1.76\n192.168.1.61:5901\n\n# коментар")
    assert [(t.name, t.host, t.port) for t in targets] == [
        ("Піч 1", "192.168.1.76", 5900),
        ("192.168.1.61", "192.168.1.61", 5901),
    ]


def test_parse_targets_drops_duplicates_but_rejects_nonsense():
    assert len(service.parse_targets("a=192.168.1.76\nb=192.168.1.76")) == 1
    with pytest.raises(service.FurnaceConfigError):
        service.parse_targets("Піч=192.168.1.76/../etc")
    with pytest.raises(service.FurnaceConfigError):
        service.parse_targets("Піч=192.168.1.76:99999")


# ── Опитування й запис ──────────────────────────────────────────────────────


def _target():
    return service.FurnaceTarget(name="Піч 1", host="192.168.1.76")


def test_poll_writes_a_row_and_saves_the_frame(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "FURNACE_FRAMES_PATH", str(tmp_path), raising=False)
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    monkeypatch.setattr(service, "capture", lambda *a, **k: _frame("run"))

    with Session(_database()) as db:
        state = service.poll_target(db, _target(), password="x")
        assert state.status == STATUS_RUN
        assert state.temp_c == 759
        rows = db.query(FurnaceReading).all()
        assert len(rows) == 1
        assert rows[0].host == "192.168.1.76"
        assert rows[0].temp_c == 759
        assert rows[0].raw_remaining == "00:26:59"
    assert (tmp_path / "192.168.1.76.png").exists()


def test_identical_reading_within_a_minute_writes_no_second_row(monkeypatch, tmp_path):
    """Кадр знімається кожні кілька секунд; рядок пишеться на зміну. Без цього
    правила одна піч давала б тисячі рядків на добу про те, що нічого не
    відбувається."""
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    monkeypatch.setattr(service, "capture", lambda *a, **k: _frame("run"))
    start = datetime(2026, 8, 29, 9, 0, 0)

    with Session(_database()) as db:
        service.poll_target(db, _target(), password="x", now=start)
        service.poll_target(db, _target(), password="x", now=start + timedelta(seconds=6))
        assert db.query(FurnaceReading).count() == 1

        # Хвилина мовчання — пишемо «я живий», щоб історія не мала дірки.
        service.poll_target(db, _target(), password="x", now=start + timedelta(seconds=61))
        assert db.query(FurnaceReading).count() == 2


def test_status_change_writes_immediately(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    frames = iter([_frame("run"), _frame("wait")])
    monkeypatch.setattr(service, "capture", lambda *a, **k: next(frames))
    start = datetime(2026, 8, 29, 9, 0, 0)

    with Session(_database()) as db:
        service.poll_target(db, _target(), password="x", now=start)
        service.poll_target(db, _target(), password="x", now=start + timedelta(seconds=6))
        rows = db.query(FurnaceReading).order_by(FurnaceReading.id).all()
        assert [row.status for row in rows] == [STATUS_RUN, STATUS_WAIT]


def test_unreachable_furnace_is_a_state_not_a_crash(monkeypatch, tmp_path):
    """Вимкнена на ніч піч — робочий стан. Воркер має пережити його мовчки, а
    рядки про недоступність — не заповнювати базу."""
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)

    def _boom(*_args, **_kwargs):
        raise FurnaceVncError("Піч 192.168.1.76 недоступна")

    monkeypatch.setattr(service, "capture", _boom)
    start = datetime(2026, 8, 29, 22, 0, 0)

    with Session(_database()) as db:
        state = service.poll_target(db, _target(), password="x", now=start)
        assert state.error and state.status == STATUS_UNKNOWN
        service.poll_target(db, _target(), password="x", now=start + timedelta(minutes=1))
        assert db.query(FurnaceReading).count() == 1

        later = start + timedelta(minutes=20)
        service.poll_target(db, _target(), password="x", now=later)
        assert db.query(FurnaceReading).count() == 2


def test_frame_is_only_served_for_a_known_furnace(monkeypatch, tmp_path):
    """Ключ із URL ніколи не підставляється у шлях. Невідома піч — 404, навіть
    якщо файл із такою назвою на диску є."""
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    monkeypatch.setattr(service, "capture", lambda *a, **k: _frame("run"))
    with Session(_database()) as db:
        service.poll_target(db, _target(), password="x")

    assert service.resolve_frame("192.168.1.76") is not None
    assert service.resolve_frame("../order_desk") is None
    assert service.resolve_frame("192.168.1.99") is None


def test_prune_drops_only_old_readings():
    now = datetime(2026, 8, 29, 12, 0, 0)
    with Session(_database()) as db:
        db.add(FurnaceReading(host="a", captured_at=now - timedelta(days=45), status=STATUS_WAIT))
        db.add(FurnaceReading(host="a", captured_at=now - timedelta(days=2), status=STATUS_RUN))
        db.commit()
        assert service.prune_readings(db, now=now) == 1
        assert db.query(FurnaceReading).count() == 1


# ── Роути ───────────────────────────────────────────────────────────────────


def _request(user_id, session=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        session=session if session is not None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _capture_context(monkeypatch):
    import app.web as web

    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )


def test_furnaces_page_sends_a_guest_to_login(monkeypatch):
    from app.routers import furnace as router

    with Session(_database()) as db:
        response = router.furnaces_page(_request(None, session={}), db)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_furnaces_page_lists_a_card_per_configured_furnace(monkeypatch):
    from app.routers import furnace as router
    from app.settings_store import set_furnace_hosts_raw

    _capture_context(monkeypatch)
    with Session(_database()) as db:
        user = User(username="op", password_hash="x", full_name="Оп")
        db.add(user)
        set_furnace_hosts_raw(db, "Піч 1=192.168.1.76")
        db.commit()

        context = router.furnaces_page(_request(user.id), db)
        assert [card.target.name for card in context["cards"]] == ["Піч 1"]
        # Піч, яку ще не опитували, теж отримує плитку: порожня плитка чесніша
        # за відсутність печі на екрані.
        assert context["cards"][0].never_polled is True
        assert context["config_error"] is None


def test_broken_host_list_explains_itself_instead_of_hiding_the_screen(monkeypatch):
    from app.routers import furnace as router
    from app.settings_store import set_furnace_hosts_raw

    _capture_context(monkeypatch)
    with Session(_database()) as db:
        user = User(username="op", password_hash="x", full_name="Оп")
        db.add(user)
        set_furnace_hosts_raw(db, "Піч 1=не адреса")
        db.commit()

        context = router.furnaces_page(_request(user.id), db)
        assert context["cards"] == []
        assert "Некоректна адреса" in context["config_error"]


def test_saving_furnaces_keeps_the_password_when_the_field_is_left_empty(monkeypatch):
    """Форму відкривають, щоб дописати піч. Порожнє поле пароля означає «не
    міняти», інакше збережений пароль тихо зникав би при кожному редагуванні."""
    from app.routers import settings as settings_router
    from app.settings_store import get_furnace_hosts_raw, get_furnace_vnc_password

    with Session(_database()) as db:
        admin = User(username="root", password_hash="x", full_name="Адмін", role="адмін")
        db.add(admin)
        db.commit()
        request = _request(admin.id)

        settings_router.save_furnaces(request, hosts="Піч 1=192.168.1.76", password="DEKEMA", db=db)
        assert get_furnace_vnc_password(db) == "DEKEMA"

        settings_router.save_furnaces(
            request, hosts="Піч 1=192.168.1.76\nПіч 2=192.168.1.61", password="", db=db
        )
        assert get_furnace_vnc_password(db) == "DEKEMA"
        assert "192.168.1.61" in get_furnace_hosts_raw(db)


def test_saving_a_broken_address_changes_nothing():
    from app.routers import settings as settings_router
    from app.settings_store import get_furnace_hosts_raw

    with Session(_database()) as db:
        admin = User(username="root", password_hash="x", full_name="Адмін", role="адмін")
        db.add(admin)
        db.commit()
        request = _request(admin.id)

        settings_router.save_furnaces(request, hosts="Піч 1=192.168.1.76", password="x", db=db)
        response = settings_router.save_furnaces(request, hosts="Піч=1 2 3", password="", db=db)
        assert response.status_code == 303
        assert get_furnace_hosts_raw(db) == "Піч 1=192.168.1.76"


def test_poll_all_grabs_frames_in_parallel(monkeypatch, tmp_path):
    """Вимкнена піч мовчить до дедлайну. Послідовний обхід означав би, що
    живі печі на екрані старіють через мертві, тому знімки йдуть одночасно."""
    import threading
    import time

    from app.settings_store import set_furnace_hosts_raw

    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    started = threading.Barrier(3, timeout=5)

    def _slow_capture(host, *_args, **_kwargs):
        started.wait()  # кине BrokenBarrierError, якщо виклики не одночасні
        time.sleep(0.05)
        return _frame("run")

    monkeypatch.setattr(service, "capture", _slow_capture)

    with Session(_database()) as db:
        set_furnace_hosts_raw(db, "a=192.168.1.76\nb=192.168.1.61\nc=192.168.1.62")
        db.commit()
        states = service.poll_all(db)
        assert len(states) == 3
        assert all(state.status == STATUS_RUN for state in states)


def test_state_properties_survive_the_furnace_vanishing_mid_read(monkeypatch, tmp_path):
    """Властивості стану читають self.reading один раз, у локальну змінну.

    Інакше `self.reading.temp_c if self.reading else None` — це два окремі
    читання поля, і фоновий воркер може поставити None між ними, щойно піч
    зникла з мережі. Сторінка падала б з AttributeError просто тому, що піч
    вимкнули під час запиту. Тут це відтворюється детерміновано: поле
    підмінене об'єктом, який після першого читання віддає None.
    """

    class _VanishingState(service.FurnaceState):
        _reads = 0

        @property
        def reading(self):
            type(self)._reads += 1
            return self._reading if type(self)._reads == 1 else None

        @reading.setter
        def reading(self, value):
            self._reading = value

    state = _VanishingState(target=_target())
    state.reading = read_panel(_frame("run"))
    assert state.temp_c == 759  # друге читання вже None — не має бути AttributeError


def test_card_reports_when_the_poller_itself_went_quiet():
    """Смерть фонового потоку не сміє виглядати як спокійна піч: числа
    лишились би на екрані, а час кадру просто перестав би йти."""
    now = datetime(2026, 8, 29, 9, 0, 0)
    state = service.FurnaceState(target=_target())
    state.attempted_at = now - timedelta(seconds=service.STALE_AFTER_SECONDS + 1)
    card = service.FurnaceCard(target=_target(), state=state)
    assert card.stale(now=now) is True

    state.attempted_at = now - timedelta(seconds=10)
    assert card.stale(now=now) is False
    # Піч, яку ще жодного разу не опитували, не «стоїть» — вона просто нова.
    assert service.FurnaceCard(target=_target(), state=None).stale(now=now) is False


def test_queue_strip_is_returned_even_with_no_furnaces(monkeypatch):
    """Порожня обгортка — не косметика. Якби роут повертав нічого, елемент
    зник би з DOM разом зі своїм поллом, і після налаштування печей смуга вже
    ніколи б не проступила без перезавантаження сторінки."""
    from app.routers import furnace as router

    _capture_context(monkeypatch)
    with Session(_database()) as db:
        user = User(username="op", password_hash="x", full_name="Оп")
        db.add(user)
        db.commit()
        context = router.furnaces_strip(_request(user.id), db)
        assert context["furnace_cards"] == []


def test_queue_page_renders_the_strip_outside_the_polled_rows():
    """Сторож розкладки: смуга мусить лежати ВИЩЕ `.worklayout`, тобто поза
    `#queue-rows`, який свапає 15-секундний полл черги. Усередині вона
    зникала б на кожному тіку — та сама пастка, що з карткою зміни."""
    from pathlib import Path

    html = (Path("app/templates/queue.html")).read_text(encoding="utf-8")
    assert '_furnace_strip.html' in html
    assert html.index('_furnace_strip.html') < html.index('<div class="worklayout">')


def test_strip_shows_the_moment_not_the_countdown():
    """Головне число смуги — момент («о 17:41»), а не залишок: сторінка живе
    між поллами, і «26:59» на ній протухає щосекунди, а момент лишається
    правдою."""
    from pathlib import Path

    html = (Path("app/templates/_furnace_strip.html")).read_text(encoding="utf-8")
    assert "done_at.strftime('%H:%M')" in html
