"""Тека для 3D на табло, коли роботі її ніхто не закріпив (27.11.26).

Бойовий випадок: Струбицький, рядок «додано вручну», файли покладено руками в
`Юрій Струбицький/Новая папка (19)/A1 Monolit`. Видача теку знаходила, а табло
брало лише закріплену теку чи «Номер роботи» — на 250i-Tolik зуби не крутились.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.business_day import business_date_of
from app.export_scanner import clear_export_cache
from app.services import board_models as bm
from app.services.handout import single_export_folder
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура
from tests.test_stl_shapes import _CUBE, _write_binary


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "cache_dir", lambda: tmp_path / "cache")
    clear_export_cache()
    bm._index.clear()
    bm._guessed.clear()
    yield
    clear_export_cache()
    bm._index.clear()
    bm._guessed.clear()


def _tab() -> str:
    # Партія створюється зараз — день роботи той самий робочий день.
    return business_date_of(datetime.now()).strftime("%d.%m.%y")


def _typed_row(db, **kw):
    from app.models import Order

    order = Order(source="sheet_client", client_name="Струбицький", material_color="mono a1",
                  quantity="6", sum3d_id="13-34-23", sheet_tab=_tab(), **kw)
    db.add(order)
    db.commit()
    return order


def test_typed_row_finds_the_one_folder_of_its_day_and_colour(app_db, tmp_path):  # noqa: F811
    _, factory = app_db
    root = tmp_path / "export"
    folder = root / "Юрій Струбицький" / "Новая папка (19)" / "A1 Monolit"
    folder.mkdir(parents=True)
    _write_binary(folder / "Струбицький-bridge_cad.stl", _CUBE)
    (root / "Юрій Струбицький" / "Новая папка (19)" / "mono a3").mkdir()
    with factory() as db:
        order = _typed_row(db)
        found = single_export_folder(db, order, root)
    assert found is not None and found.name == "A1 Monolit"


def test_two_folders_of_the_same_colour_that_day_give_nothing(app_db, tmp_path):  # noqa: F811
    """На видачі тут обирає людина; табло з чужими зубами гірше за порожнє."""
    _, factory = app_db
    root = tmp_path / "export"
    for batch in ("Новая папка (19)", "Новая папка (20)"):
        folder = root / "Юрій Струбицький" / batch / "mono a1"
        folder.mkdir(parents=True)
        _write_binary(folder / "crown_cad.stl", _CUBE)
    with factory() as db:
        order = _typed_row(db)
        assert single_export_folder(db, order, root) is None


def test_folder_pinned_to_a_letter_is_not_given_to_the_typed_row(app_db, tmp_path):  # noqa: F811
    from app.models import Order

    _, factory = app_db
    root = tmp_path / "export"
    folder = root / "Юрій Струбицький" / "Новая папка (19)" / "mono a1"
    folder.mkdir(parents=True)
    _write_binary(folder / "crown_cad.stl", _CUBE)
    with factory() as db:
        db.add(Order(source="email", client_name="Струбицький", material_color="mono a1",
                     quantity="6", sheet_tab=_tab(),
                     export_folder_path="Юрій Струбицький/Новая папка (19)/mono a1"))
        db.commit()
        order = _typed_row(db)
        assert single_export_folder(db, order, root) is None


def _machine_with(order):
    return [SimpleNamespace(orders=[order])]


def test_work_on_the_spindle_without_a_folder_gets_its_models(app_db, tmp_path, monkeypatch):  # noqa: F811
    _, factory = app_db
    folder = tmp_path / "found"
    folder.mkdir()
    _write_binary(folder / "Струбицький-36-crown_cad.stl", _CUBE)
    calls = []

    def fake_single(db, order, root):
        calls.append(order.id)
        return folder

    monkeypatch.setattr("app.services.handout.single_export_folder", fake_single)
    monkeypatch.setattr("app.services.stl_height.still_settling", lambda files, now: False)
    with factory() as db:
        order = _typed_row(db)
        monkeypatch.setattr("app.services.machines.snapshot", lambda db: _machine_with(order))
        bm.warm_once(db, None, tmp_path / "export")
        bm.warm_once(db, None, tmp_path / "export")
        assert len(bm.models_for([order], "tok")) == 1
        assert calls == [order.id], "зіставлення не повторюється щопрохід"


def test_lab_work_and_works_off_the_spindle_are_not_guessed(app_db, tmp_path, monkeypatch):  # noqa: F811
    from app.models import Order

    _, factory = app_db
    calls = []
    monkeypatch.setattr("app.services.handout.single_export_folder",
                        lambda db, order, root: calls.append(order.id))
    with factory() as db:
        lab = Order(source="lab", material_color="моно а3", quantity="1", sum3d_id="13-54-05",
                    job_code="2026-11-27_00077-004", sheet_tab=_tab())
        db.add(lab)
        db.commit()
        _typed_row(db)                      # Sum3D є, але не на шпинделі
        monkeypatch.setattr("app.services.machines.snapshot", lambda db: _machine_with(lab))
        monkeypatch.setattr("app.order_folder.resolve_job_code_folder", lambda tech, code: None)
        bm.warm_once(db, None, tmp_path / "export")
    assert calls == []
