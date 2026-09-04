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
