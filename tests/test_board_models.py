"""3D-моделі робіт для табло цеху (`app/services/board_models.py`).

Справжніх STL тут немає (форма робіт пацієнтів) — механіку перевіряємо на
синтетичних тілах, вигляд звірено на бойових файлах 12.11.26 (`kmill_stl_shapes`).
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.services import board_models as bm
from app.services import stl_shapes
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура
from tests.test_stl_shapes import _CUBE, _write_binary


@pytest.fixture(autouse=True)
def _own_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(bm, "cache_dir", lambda: cache)
    bm._index.clear()
    bm._meta_cache.clear()
    yield cache
    bm._index.clear()
    bm._meta_cache.clear()


def _sphere(n_lat=40, n_lon=60, r=5.0):
    """Замкнена сфера з трикутників (для спрощення)."""
    tris = []
    for i in range(n_lat):
        a0, a1 = np.pi * i / n_lat, np.pi * (i + 1) / n_lat
        for j in range(n_lon):
            b0, b1 = 2 * np.pi * j / n_lon, 2 * np.pi * (j + 1) / n_lon

            def p(a, b):
                return [r * np.sin(a) * np.cos(b), r * np.sin(a) * np.sin(b), r * np.cos(a)]
            q = [p(a0, b0), p(a1, b0), p(a1, b1), p(a0, b1)]
            if i > 0:
                tris.append([q[0], q[1], q[3]])
            if i < n_lat - 1:
                tris.append([q[1], q[2], q[3]])
    return np.array(tris)


def test_work_files_prefer_exocad_export_and_skip_jaw_models(tmp_path):
    folder = tmp_path / "робота"
    (folder / "sub").mkdir(parents=True)
    _write_binary(folder / "Петренко-36-crown_cad.stl", _CUBE)
    _write_binary(folder / "sub" / "Петренко-36-crown_cad.stl", _CUBE)  # копія — не подвоює
    _write_binary(folder / "Петренко-UpperJaw.stl", _CUBE)
    assert [p.name for p in bm.work_stl_files(folder)] == ["Петренко-36-crown_cad.stl"]

    other = tmp_path / "лист"
    other.mkdir()
    _write_binary(other / "crown 16.stl", _CUBE)
    _write_binary(other / "Model_upper.stl", _CUBE)
    _write_binary(other / "antagonist.stl", _CUBE)
    assert [p.name for p in bm.work_stl_files(other)] == ["crown 16.stl"], \
        "без експорту Exocad — усі STL, крім моделей щелеп і антагоністів"


def test_pose_rule_rotates_only_a_single_exocad_crown(monkeypatch):
    """Правило з бойових файлів 12.11.26: «як у файлі», поворот — лише
    одиночній коронці Exocad, яку він робить нижчою хоча б на 15 %."""
    flat_rot = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])
    calls = []

    def fake_upright(tris):
        calls.append(len(tris))
        # «Поворот» кладе куб 8×8×8 так само заввишки — підмінимо висоту через масштаб
        return stl_shapes.Upright(fake_upright.rot, 100)

    monkeypatch.setattr(stl_shapes, "upright", fake_upright)
    tall = _CUBE * np.array([1.0, 1.0, 1.5])  # 8×8×12: «під нахилом»
    fake_upright.rot = np.array([[1.0, 0, 0], [0, 0, 0.6], [0, -1.0, 0]])  # висота ×0.6
    assert bm.pick_pose(tall, "x-36-crown_cad.stl")[1] == "поворот"
    assert bm.pick_pose(tall, "x-36-abutment_cad.stl")[1] == "файл", "абатмент поворот клав на бік"
    assert bm.pick_pose(tall, "x-36-tooth_without_abutment_cad.stl")[1] == "файл"
    assert bm.pick_pose(tall, "crown 36.stl")[1] == "файл", "не Exocad — не повертаємо"
    wide = _CUBE * np.array([2.5, 1.0, 1.5])  # 20 мм — міст
    assert bm.pick_pose(wide, "x-35-36-37-waxup_cad.stl")[1] == "файл"
    assert len(calls) == 1, "поворот рахується лише для кандидата — на капі це секунди"
    fake_upright.rot = flat_rot * np.array([[1.0], [0.95], [1.0]])  # лише −5 %
    assert bm.pick_pose(tall, "x-36-crown_cad.stl")[1] == "файл"


def test_build_model_keeps_shape_and_marks_open_models(tmp_path):
    path = tmp_path / "x-36-crown_cad.stl"
    _write_binary(path, _CUBE)
    payload = bm.build_model(path)
    assert payload is not None and payload["closed"] is True and payload["units"] == 1
    pos = np.frombuffer(base64.b64decode(payload["pos"]), "<i2").reshape(-1, 3) / 32767 * payload["S"]
    idx = np.frombuffer(base64.b64decode(payload["idx"]), payload["it"]).reshape(-1, 3)
    assert len(idx) == 12 and np.allclose(np.ptp(pos, axis=0), 8.0, atol=0.01)
    assert np.allclose(pos.min(0) + pos.max(0), 0, atol=0.01), "модель по центру"

    hole = tmp_path / "x-35-36-37-waxup_cad.stl"
    _write_binary(hole, _CUBE[1:])
    opened = bm.build_model(hole)
    assert opened["closed"] is False and opened["units"] == 3


def test_heavy_model_is_simplified_under_the_limits(tmp_path, monkeypatch):
    path = tmp_path / "capa.stl"
    _write_binary(path, _sphere())
    monkeypatch.setattr(bm, "BIG_TRIS", 1500)
    monkeypatch.setattr(bm, "LO_TRIS", 400)
    payload = bm.build_model(path)
    assert payload["tris"] <= 1500 and payload["lo_tris"] <= 400
    assert payload["closed"] is True


def test_not_a_work_is_skipped(tmp_path):
    jaw = tmp_path / "scene.stl"
    _write_binary(jaw, _CUBE * 12)  # 96 мм
    assert bm.build_model(jaw) is None
    (tmp_path / "junk.stl").write_bytes(b"nope")
    assert bm.build_model(tmp_path / "junk.stl") is None


def test_cache_key_follows_the_file_and_rejects_foreign_keys(tmp_path, _own_cache):
    path = tmp_path / "Петренко-36-crown_cad.stl"
    _write_binary(path, _CUBE)
    key, meta = bm.ensure_model(path)
    assert len(key) == 24 and "Петренко" not in key and meta["units"] == 1
    stored = bm.cached_file(key)
    assert stored is not None and json.loads(gzip.decompress(stored.read_bytes()))["v"] == bm.FORMAT
    assert bm.ensure_model(path)[0] == key, "той самий файл — той самий ключ, з кешу"
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert bm.cache_key(path) != key, "файл змінився — новий ключ"
    for bad in ("../../etc", "ABCDEF0123456789abcdef01", key + "0", ""):
        assert bm.cached_file(bad) is None


def test_prune_drops_old_and_keeps_fresh(tmp_path, _own_cache):
    path = tmp_path / "x-36-crown_cad.stl"
    _write_binary(path, _CUBE)
    key, _ = bm.ensure_model(path)
    assert bm.prune_cache() == 0
    assert bm.prune_cache(now=time.time() + (bm.CACHE_KEEP_DAYS + 1) * 86400) == 1
    assert bm.cached_file(key) is None


def test_warm_pass_feeds_the_tile_without_names(app_db, tmp_path, monkeypatch):  # noqa: F811
    from app.models import Order

    _, factory = app_db
    folder = tmp_path / "Клієнт Петренко" / "12.11.26" / "mono a2"
    folder.mkdir(parents=True)
    _write_binary(folder / "Петренко-36-crown_cad.stl", _CUBE)
    _write_binary(folder / "Петренко-45-46-47-bridge_slm_cad.stl", _CUBE * np.array([2.5, 1, 1]))
    with factory() as db:
        db.add(Order(source="email", client_name="Петренко", material_color="mono a2",
                     quantity=4, sum3d_id="12-01-45"))
        db.add(Order(source="lab", client_name="Без Sum3D", material_color="mono a2", quantity=1))
        db.commit()
    monkeypatch.setattr("app.services.stl_height.work_folder", lambda order, tech, export: folder)
    monkeypatch.setattr("app.services.machines.snapshot", lambda db: [])
    settling = {"now": True}
    monkeypatch.setattr("app.services.stl_height.still_settling", lambda files, now: settling["now"])
    with factory() as db:
        bm.warm_once(db, None, None)
        assert not bm._index, "файл ще копіюється — не збираємо, наступний прохід візьме"
        settling["now"] = False
        assert bm.warm_once(db, None, None) == 1, "лише робота з Sum3D"
        order = db.query(Order).filter(Order.sum3d_id == "12-01-45").one()
    models = bm.models_for([order, order], "tok")
    assert [m["units"] for m in models] == [1, 3], "порядок файлів, повтор роботи не подвоює"
    assert all(m["url"].startswith("/t/tok/model/") and m["closed"] for m in models)
    assert "Петренко" not in repr(models)

    tile = SimpleNamespace(orders=[order])
    assert bm.models_for(tile.orders, "tok") == models


def test_board_serves_models_only_behind_the_secret(app_db, tmp_path, monkeypatch):  # noqa: F811
    from app.routers.furnace_board import create_board_app
    from tests.asgi_client import MiniClient
    from tests.test_furnace_board import _enable

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    path = tmp_path / "x-36-crown_cad.stl"
    _write_binary(path, _CUBE)
    key, _ = bm.ensure_model(path)
    board = MiniClient(create_board_app())
    status, headers, _body = board.get(f"/t/{token}/model/{key}.json")
    assert status == 200
    assert headers.get("content-encoding") == "gzip"
    assert board.get(f"/t/wrong/model/{key}.json")[0] == 404
    assert board.get(f"/t/{token}/model/{'0' * 24}.json")[0] == 404
    assert board.get(f"/t/{token}/model/..%2F..%2Fsecret.json")[0] == 404


def test_running_tile_carries_its_models_and_others_do_not(monkeypatch):
    """Плитка, що фрезерує, несе адреси моделей (екрановані в атрибуті);
    завершена — ні, навіть коли моделі є: 3D там нічого не ріже."""
    from app.services import shop_board as sb
    from app.routers.deps import templates
    from tests.test_shop_board import _card, _order, _view

    models = [{"url": "/t/t/model/" + "a" * 24 + ".json", "units": 3, "closed": True}]
    monkeypatch.setattr(bm, "models_for", lambda orders, token: models)
    view = _view([
        _card("A", "run", percent=40, sum3d="12-00-40", orders=[_order("Клієнт", "mono a2", "3")]),
        _card("B", "done", word="завершено", orders=[_order("Інший", "pmma", "1")]),
    ])
    html = templates.env.get_template("_shop_board_cards.html").render(view=view, token="t")
    assert html.count("data-models=") == 1
    assert '&#34;url&#34;: &#34;/t/t/model/' in html and 'data-models="[{&#34;' in html, "лапки JSON не рвуть атрибут"
    assert 'data-pct="40"' in html and 'data-mat="mono a2 · 3"' in html
    assert sb.board_machine(_card("C", "run", percent=1)).models == [], "картинка бота адрес не має"


def test_board_serves_the_3d_scripts():
    from app.routers import furnace_board as fbr

    assert {"three-0.128.0.min.js", "shop_board_3d.js", "storage.js"} <= fbr._JS
