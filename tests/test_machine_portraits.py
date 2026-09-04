"""Фото на конкретний верстат (04.09.26).

Що ламається тихо:
- фото з телефона без EXIF-повороту лягає боком — перевіряємо transpose;
- не-зображення або завеликий файл не мають лишати нічого на диску;
- картка бере фото верстата, коли воно є, і дефолт моделі, коли немає;
- модель за назвою: loader / dry / 250 / решта;
- ворота роутів ті самі, що в решти /settings/machines*.
"""
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from app import machine_portraits as mp
from app.services.machines import MachineCard, MachineTarget, machine_model_key


@pytest.fixture
def portraits_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "MACHINE_PORTRAITS_PATH", str(tmp_path / "portraits"))
    return tmp_path / "portraits"


def _jpeg(width=100, height=50, orientation=None) -> io.BytesIO:
    img = Image.new("RGB", (width, height), (200, 30, 30))
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    buf.seek(0)
    return buf


def test_save_applies_exif_rotation_and_resizes(portraits_dir):
    # Orientation 6 = повернути на 90° — телефонне портретне фото.
    path = mp.save_portrait(7, _jpeg(3000, 1500, orientation=6))
    assert path == portraits_dir / "7.jpg" and path.is_file()
    with Image.open(path) as img:
        assert img.size == (600, 1200)       # повернуто (висота > ширина) і зменшено до 1200
    assert mp.portrait_version(7) is not None
    assert not list(portraits_dir.glob("*.tmp"))


def test_save_rejects_garbage_and_leaves_nothing(portraits_dir):
    with pytest.raises(mp.PortraitError):
        mp.save_portrait(3, io.BytesIO(b"not an image at all"))
    with pytest.raises(mp.PortraitError):
        mp.save_portrait(3, io.BytesIO(b""))
    assert not (portraits_dir / "3.jpg").exists()
    assert mp.portrait_version(3) is None


def test_save_rejects_oversize(portraits_dir, monkeypatch):
    monkeypatch.setattr(mp, "MAX_BYTES", 10)
    with pytest.raises(mp.PortraitError):
        mp.save_portrait(4, _jpeg())
    assert not (portraits_dir / "4.jpg").exists()


def test_delete_portrait(portraits_dir):
    mp.save_portrait(5, _jpeg())
    assert mp.delete_portrait(5) is True
    assert mp.delete_portrait(5) is False
    assert mp.portrait_version(5) is None


def test_card_prefers_own_photo_over_model_default(portraits_dir):
    t = MachineTarget(name="350i Loader", host="10.0.0.1", port=8765, machine_id=9)
    card = MachineCard(target=t, state=None)
    assert card.portrait_url is None and card.model_key == "350i-loader"
    mp.save_portrait(9, _jpeg())
    url = card.portrait_url
    assert url and url.startswith("/machines/portrait/9.jpg?v=")
    # ціль без рядка (тести, майбутні джерела) — завжди дефолт
    assert MachineCard(target=MachineTarget(name="x", host="h"), state=None).portrait_url is None


def test_chosen_model_beats_name_guess():
    assert machine_model_key("350i Loader", "250i-dry") == "250i-dry"
    assert machine_model_key("Верстат 1", "350i-loader") == "350i-loader"
    assert machine_model_key("250i dry", "") == "250i-dry"           # авто
    assert machine_model_key("250i dry", "polaroid") == "250i-dry"   # сміття = авто
    t = MachineTarget(name="Верстат 1", host="h", portrait_model="250i")
    assert MachineCard(target=t, state=None).model_key == "250i"


def test_settings_row_has_model_select_with_chosen_option():
    from app.routers.deps import templates
    from starlette.datastructures import Headers

    req = SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"), headers=Headers({}),
                          state=SimpleNamespace(ui_prefs_cache={"machine_card": ""}))
    m = SimpleNamespace(id=1, name="Верстат 1", host="h", port=8765, enabled=True, collect_calibration=False,
                        agent_token_encrypted="x", password_encrypted=None, portrait_model="250i-dry")
    html = templates.env.get_template("_settings_machines.html").render(
        request=req, user=SimpleNamespace(role="адмін"), machines=[m],
        machine_portrait_version={1: None}, machine_password_set=False)
    assert 'form="machine-1" name="portrait_model"' in html
    assert '<option value="250i-dry" selected>250i dry</option>' in html
    assert 'data-model="250i-dry"' in html                 # мініатюра показує обране
    assert html.count('<option value="') == 5 + 5          # рядок + форма «Додати»


def test_update_route_persists_chosen_model_and_ignores_junk():
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Machine, User
    from app.routers import settings as settings_router

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        admin = User(username="a", password_hash="x", full_name="А", role="адмін")
        machine = Machine(name="Верстат 1", host="10.0.0.1", port=8765, created_at=datetime.now())
        db.add_all([admin, machine])
        db.commit()
        req = SimpleNamespace(session={"user_id": admin.id}, client=SimpleNamespace(host="127.0.0.1"))
        base = dict(name="Верстат 1", host="10.0.0.1", port="8765", enabled="1", password="", agent_token="", collect_calibration="")
        r = settings_router.update_machine(request=req, machine_id=machine.id, db=db, portrait_model="350i-loader", **base)
        assert r.status_code == 303
        db.refresh(machine)
        assert machine.portrait_model == "350i-loader"
        settings_router.update_machine(request=req, machine_id=machine.id, db=db, portrait_model="polaroid", **base)
        db.refresh(machine)
        assert machine.portrait_model == ""


@pytest.mark.parametrize("name,expected", [
    ("350i Loader", "350i-loader"), ("350i №2", "350i"), ("CORiTEC 350i", "350i"),
    ("250i", "250i"), ("250i dry", "250i-dry"), ("250i DRY №1", "250i-dry"),
    ("Верстат", "350i"), ("", "350i"),
])
def test_model_key_from_name(name, expected):
    assert machine_model_key(name) == expected


def test_settings_table_has_photo_control():
    from app.routers.deps import templates
    from starlette.datastructures import Headers

    req = SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"), headers=Headers({}),
                          state=SimpleNamespace(ui_prefs_cache={"machine_card": ""}))
    m1 = SimpleNamespace(id=1, name="350i Loader", host="h", port=8765, enabled=True, collect_calibration=False,
                         agent_token_encrypted="x", password_encrypted=None)
    m2 = SimpleNamespace(id=2, name="250i dry", host="h2", port=5900, enabled=True, collect_calibration=False,
                         agent_token_encrypted=None, password_encrypted=None)
    html = templates.env.get_template("_settings_machines.html").render(
        request=req, user=SimpleNamespace(role="адмін"), machines=[m1, m2],
        machine_portrait_version={1: 1700000000, 2: None}, machine_password_set=False)
    assert 'action="/settings/machines/1/portrait"' in html and 'enctype="multipart/form-data"' in html
    assert '/machines/portrait/1.jpg?v=1700000000' in html
    assert 'action="/settings/machines/1/portrait/delete"' in html
    assert 'data-model="250i-dry"' in html and 'action="/settings/machines/2/portrait/delete"' not in html


def test_portrait_routes_gate_and_serve(portraits_dir):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Machine, User
    from app.routers import machines as machines_router
    from app.routers import settings as settings_router

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        admin = User(username="a", password_hash="x", full_name="А", role="адмін")
        op = User(username="o", password_hash="x", full_name="О", role="оператор")
        from datetime import datetime
        machine = Machine(name="350i", host="10.0.0.1", port=8765, created_at=datetime.now())
        db.add_all([admin, op, machine])
        db.commit()

        def req(uid, host="127.0.0.1"):
            return SimpleNamespace(session={"user_id": uid} if uid else {}, client=SimpleNamespace(host=host))

        upload = SimpleNamespace(file=_jpeg(), filename="p.jpg")
        import asyncio
        with pytest.raises(HTTPException) as exc:
            asyncio.run(settings_router.upload_machine_portrait(request=req(op.id), machine_id=machine.id, photo=upload, db=db))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            asyncio.run(settings_router.upload_machine_portrait(request=req(admin.id, "10.0.0.9"), machine_id=machine.id, photo=upload, db=db))
        assert exc.value.status_code == 403

        r = asyncio.run(settings_router.upload_machine_portrait(request=req(admin.id), machine_id=machine.id, photo=SimpleNamespace(file=_jpeg()), db=db))
        assert r.status_code == 303 and (portraits_dir / f"{machine.id}.jpg").is_file()

        with pytest.raises(HTTPException) as exc:
            machines_router.machine_portrait(request=req(None), machine_id=machine.id, db=db)
        assert exc.value.status_code == 401
        served = machines_router.machine_portrait(request=req(op.id), machine_id=machine.id, db=db)
        assert Path(served.path) == portraits_dir / f"{machine.id}.jpg"
        with pytest.raises(HTTPException) as exc:
            machines_router.machine_portrait(request=req(op.id), machine_id=999, db=db)
        assert exc.value.status_code == 404

        r = settings_router.delete_machine_portrait(request=req(admin.id), machine_id=machine.id, db=db)
        assert r.status_code == 303 and not (portraits_dir / f"{machine.id}.jpg").exists()

        # видалення верстата прибирає і фото
        mp.save_portrait(machine.id, _jpeg())
        settings_router.delete_machine(request=req(admin.id), machine_id=machine.id, db=db)
        assert not (portraits_dir / f"{machine.id}.jpg").exists()
