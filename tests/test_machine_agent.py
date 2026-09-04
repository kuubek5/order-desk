"""HTTP-агент верстата: CRM тягне кадр по /capture з токеном (не VNC)."""
from __future__ import annotations

import io

from PIL import Image

from app.services import machines as ms


class _Resp:
    """Дублер відповіді requests.

    Підтримує `with` і `iter_content` — саме так `_capture_http` читає кадр
    після рев'ю 04.09.26: потоково, з лічильником байтів і сумарним дедлайном
    (таймаут читання в requests рахується МІЖ байтами, тож краплинна віддача
    його обходила). Дублер мусить повторювати справжній контракт, інакше тест
    зеленітиме на API, якого в проді немає."""

    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        import json as _json
        return _json.loads(self.content.decode())


def _png_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


def test_capture_http_returns_image(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None, stream=False):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp(_png_bytes())

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    img = ms._capture_http("192.168.1.85", 8765, "tok123")
    assert isinstance(img, Image.Image)
    assert captured["url"] == "http://192.168.1.85:8765/capture"
    assert captured["headers"]["X-Agent-Token"] == "tok123"


def test_capture_http_403_explains(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(b"", 403))
    try:
        ms._capture_http("h", 8765, "bad")
        assert False, "мало кинути виняток на 403"
    except RuntimeError as exc:
        assert "токен" in str(exc).lower()


def test_capture_http_network_errors_are_human(monkeypatch):
    """Сирий текст requests (HTTPConnectionPool… Max retries…) не має лягати в
    плитку: оператору треба «хто не відповідає» і «що зробити»."""
    import requests

    for exc_type, expect in (
        (requests.exceptions.ConnectTimeout, "брандмауер"),
        (requests.exceptions.ReadTimeout, "не віддав кадр"),
        (requests.exceptions.ConnectionError, "не запущено"),
    ):
        def _boom(*a, _e=exc_type, **k):
            raise _e("HTTPConnectionPool(host='192.0.2.10', port=8765): Max retries exceeded")
        monkeypatch.setattr(requests, "get", _boom)
        try:
            ms._capture_http("192.0.2.10", 8765, "tok")
            assert False, "мало кинути виняток"
        except RuntimeError as exc:
            text = str(exc)
            assert "192.0.2.10:8765" in text and expect in text
            assert "HTTPConnectionPool" not in text and "Max retries" not in text


def test_target_is_agent_flag():
    vnc = ms.MachineTarget(name="a", host="h", port=5900)
    agent = ms.MachineTarget(name="b", host="h", port=8765, agent_token="t")
    assert not vnc.is_agent
    assert agent.is_agent


def test_poll_all_uses_http_for_agent_targets(monkeypatch):
    """Сторож бойового багу 02.09.26: poll_all має ВЛАСНУ grab(), і в ній
    бракувало розвилки транспорту — воркер (він ходить саме через poll_all)
    опитував агентний верстат по VNC і давав «not a VNC server», попри
    правильний токен. Обидві гілки мусять дивитись на target.is_agent."""
    import requests

    calls = {"http": 0}

    def fake_get(url, headers=None, timeout=None, stream=False):
        # Агент опитується двома викликами: /capture (кадр) і /titles (яка
        # програма фрезерується). Рахуємо саме кадри.
        if url.endswith("/titles"):
            return _Resp(b'{"titles": []}')
        calls["http"] += 1
        return _Resp(_png_bytes())

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ms, "capture", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("poll_all пішов у VNC замість HTTP-агента")))
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)
    monkeypatch.setattr(ms, "get_machine_vnc_password", lambda db: None)
    target = ms.MachineTarget(name="350i", host="192.168.1.85", port=8765, agent_token="tok")
    monkeypatch.setattr(ms, "configured_targets", lambda db: [target])

    states = ms.poll_all(None)
    assert calls["http"] == 1  # рівно один /capture
    assert states and states[0].error is None


def test_poll_target_uses_http_when_token(monkeypatch):
    import requests

    calls = {"http": 0, "vnc": 0}

    def fake_get(url, headers=None, timeout=None, stream=False):
        # Агент опитується двома викликами: /capture (кадр) і /titles (яка
        # програма фрезерується). Рахуємо саме кадри.
        if url.endswith("/titles"):
            return _Resp(b'{"titles": []}')
        calls["http"] += 1
        return _Resp(_png_bytes())

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ms, "capture", lambda *a, **k: (_ for _ in ()).throw(AssertionError("VNC не має викликатись")))
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)

    target = ms.MachineTarget(name="350i", host="192.168.1.85", port=8765, agent_token="tok")
    state = ms.poll_target(None, target, password=None)
    assert calls["http"] == 1  # рівно один /capture
    assert state.error is None


def test_snapshot_links_running_program_to_order(monkeypatch):
    """Зв'язка верстат ↔ наряд: Sum3D ID із заголовка знаходить роботу в черзі.

    Власна in-memory БД (як у test_order_focus) — спільну чіпати не можна.
    """
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.models import Order

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        order = Order(source="lab", sheet_tab="02.09.26", work_order_no="24122",
                      client_name="Дент-Арт", sum3d_id="23-04-33", status="прораховано")
        db.add(order)
        db.commit()
        oid = order.id

        target = ms.MachineTarget(name="350i", host="10.0.0.9", port=8765, agent_token="t")
        now = datetime.now()
        with ms._states_lock:
            ms._states[target.key] = ms.MachineState(
                target=target, frame_at=now, percent=9,
                sum3d_id="23-04-33", iso_name="x_2026-09-02_23-04-33.iso",
            )
        monkeypatch.setattr(ms, "configured_targets", lambda _db: [target])
        cards = ms.snapshot(db)

    assert cards[0].sum3d_id == "23-04-33"
    assert cards[0].order is not None and cards[0].order.id == oid
    assert cards[0].percent == 9


def test_milling_now_maps_sum3d_to_machine_and_percent():
    """Підсвітка рядка черги: Sum3D ID → верстат+відсоток, лише зі свіжого
    кадру. Протухлий кадр і збій не потрапляють — показати «фрезерується» для
    роботи, зняту пів години тому, гірше, ніж не показати нічого."""
    from datetime import datetime, timedelta

    t1 = ms.MachineTarget(name="350i", host="10.0.0.1", port=8765, agent_token="t")
    t2 = ms.MachineTarget(name="250i", host="10.0.0.2", port=8765, agent_token="t")
    t3 = ms.MachineTarget(name="стара", host="10.0.0.3", port=8765, agent_token="t")
    now = datetime.now()
    stale = now - timedelta(seconds=ms.STALE_AFTER_SECONDS + 60)

    with ms._states_lock:
        ms._states.clear()
        ms._states[t1.key] = ms.MachineState(target=t1, frame_at=now, percent=9,
                                             sum3d_id="23-04-33")
        ms._states[t2.key] = ms.MachineState(target=t2, frame_at=now, percent=50,
                                             sum3d_id="11-22-33", error="немає звʼязку")
        ms._states[t3.key] = ms.MachineState(target=t3, frame_at=stale, percent=70,
                                             sum3d_id="44-55-66")
    try:
        got = ms.milling_now()
    finally:
        with ms._states_lock:
            ms._states.clear()

    assert got == {"23-04-33": {"machine": "350i", "percent": 9, "stalled": False}}


def test_milling_now_drops_the_same_id_on_two_machines():
    """Той самий Sum3D ID на двох верстатах — не вгадуємо, який саме."""
    from datetime import datetime

    now = datetime.now()
    a = ms.MachineTarget(name="A", host="10.0.0.1", port=8765, agent_token="t")
    b = ms.MachineTarget(name="B", host="10.0.0.2", port=8765, agent_token="t")
    with ms._states_lock:
        ms._states.clear()
        ms._states[a.key] = ms.MachineState(target=a, frame_at=now, percent=10, sum3d_id="dup")
        ms._states[b.key] = ms.MachineState(target=b, frame_at=now, percent=90, sum3d_id="dup")
    try:
        assert ms.milling_now() == {}
    finally:
        with ms._states_lock:
            ms._states.clear()


def test_finished_program_clears_stale_percent_and_link(monkeypatch):
    """Бойовий випадок 03.09.26: програма завершилась (85%→готово), смуга
    зникла з екрана — а чіп показував «85%» і стару прив'язку назавжди. Свіжий
    кадр БЕЗ смуги / вікно без .iso мусять СКИНУТИ і відсоток, і прив'язку."""
    import requests

    target = ms.MachineTarget(name="350i", host="10.9.9.9", port=8765, agent_token="t")
    # Попередній тік: фрезерувалось 85%, робота Кривовид (23-04-33).
    with ms._states_lock:
        ms._states[target.key] = ms.MachineState(
            target=target, percent=85, sum3d_id="23-04-33",
            iso_name="x_2026-09-02_23-04-33.iso",
        )

    # Тепер: кадр БЕЗ смуги (сірий екран), вікна .iso вже немає.
    from PIL import Image
    blank = Image.new("RGB", (300, 200), (240, 240, 240))

    def fake_get(url, headers=None, timeout=None, stream=False):
        return _Resp(b'{"titles": ["RemiCORE", "Notepad"]}')  # без .iso

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)

    state = ms.poll_target(None, target, password=None, frame=blank)
    with ms._states_lock:
        ms._states.clear()

    assert state.percent is None, "старий відсоток залип після завершення"
    assert state.sum3d_id is None, "стара прив'язка залипла після завершення"


def test_frame_is_written_to_disk_less_often_than_it_is_analysed(monkeypatch):
    """При десяти верстатах запис кадру щотіку давав би ~35 ГБ/добу — місце не
    росте (файл один), але ресурс SSD витрачається дарма. Відсоток має бути
    свіжим (він у памʼяті), а картинку дивляться оком, тож диск чіпаємо рідше."""
    from datetime import datetime, timedelta

    from PIL import Image

    saves = []
    monkeypatch.setattr(ms, "save_frame", lambda key, img: saves.append(key))
    target = ms.MachineTarget(name="350i", host="10.7.7.7", port=8765, agent_token="t")
    frame = Image.new("RGB", (300, 200), (240, 240, 240))
    with ms._states_lock:
        ms._states.pop(target.key, None)

    try:
        t0 = datetime(2026, 9, 3, 10, 0, 0)
        ms.poll_target(None, target, None, now=t0, frame=frame)          # перший — пишемо
        ms.poll_target(None, target, None, now=t0 + timedelta(seconds=5), frame=frame)
        ms.poll_target(None, target, None, now=t0 + timedelta(seconds=10), frame=frame)
        assert len(saves) == 1, "кадр пишеться на кожному тіку"

        # За інтервалом — знову пишемо.
        ms.poll_target(None, target, None, now=t0 + timedelta(seconds=16), frame=frame)
        assert len(saves) == 2

        # А от відсоток/свіжість кадру оновлюються ЩОРАЗУ, не раз на 15 с.
        with ms._states_lock:
            state = ms._states[target.key]
        assert state.frame_at == t0 + timedelta(seconds=16)
    finally:
        with ms._states_lock:
            ms._states.pop(target.key, None)


def test_frozen_percent_is_reported_as_stalled_not_as_milling():
    """Бойові кадри 03.09.26: верстат чесно показував 81% шість хвилин поспіль,
    бо СТОЯВ — подача 0.0 mm/min, шпиндель 0 U/min, інструмент 17 у помилці.
    Число правдиве, але оператор прочитав його як «CRM залипла» і пішов шукати
    баг у нас. Різниця між «фрезерує» і «стоїть на 81%» видима лише в ЧАСІ."""
    from datetime import datetime, timedelta

    now = datetime.now()
    live = ms.MachineTarget(name="2wax18", host="10.0.0.1", port=8765, agent_token="t")
    dead = ms.MachineTarget(name="1dd18", host="10.0.0.2", port=8765, agent_token="t")
    with ms._states_lock:
        ms._states.clear()
        ms._states[live.key] = ms.MachineState(
            target=live, frame_at=now, percent=57, sum3d_id="02-45-49",
            percent_changed_at=now - timedelta(seconds=30),
        )
        ms._states[dead.key] = ms.MachineState(
            target=dead, frame_at=now, percent=81, sum3d_id="00-00-19",
            percent_changed_at=now - timedelta(minutes=6),
        )
    try:
        got = ms.milling_now()
    finally:
        with ms._states_lock:
            ms._states.clear()

    assert got["02-45-49"]["stalled"] is False
    assert got["00-00-19"]["stalled"] is True
    # Число ЛИШАЄТЬСЯ — воно правдиве, змінюється лише трактування.
    assert got["00-00-19"]["percent"] == 81


def test_hundred_percent_is_finished_not_stalled():
    """100% стоїть на місці за визначенням — це завершення, не зупинка."""
    from datetime import datetime, timedelta

    now = datetime.now()
    t = ms.MachineTarget(name="350i", host="10.0.0.3", port=8765, agent_token="t")
    with ms._states_lock:
        ms._states.clear()
        ms._states[t.key] = ms.MachineState(
            target=t, frame_at=now, percent=100, sum3d_id="11-11-11",
            percent_changed_at=now - timedelta(minutes=30),
        )
    try:
        assert ms.milling_now()["11-11-11"]["stalled"] is False
    finally:
        with ms._states_lock:
            ms._states.clear()


def test_poll_target_stamps_percent_change_only_when_number_moves():
    """percent_at — коли читали, percent_changed_at — коли ЗМІНИЛОСЬ. Плутати
    їх не можна: перше свіже щотіку, і на ньому зупинку не побачиш."""
    from datetime import datetime, timedelta

    from PIL import Image

    target = ms.MachineTarget(name="350i", host="10.6.6.6", port=8765, agent_token="t")
    frame = Image.new("RGB", (300, 200), (240, 240, 240))  # без смуги → None
    with ms._states_lock:
        ms._states.pop(target.key, None)

    try:
        t0 = datetime(2026, 9, 3, 10, 0, 0)
        ms.poll_target(None, target, None, now=t0, frame=frame)
        with ms._states_lock:
            first = ms._states[target.key].percent_changed_at
        assert first == t0

        # Той самий результат через 5 хв — момент ЗМІНИ не рухається.
        later = t0 + timedelta(minutes=5)
        ms.poll_target(None, target, None, now=later, frame=frame)
        with ms._states_lock:
            state = ms._states[target.key]
        assert state.percent_changed_at == t0, "момент зміни поїхав без зміни числа"
        assert state.percent_at == later, "момент читання мусить бути свіжим"
    finally:
        with ms._states_lock:
            ms._states.pop(target.key, None)
