"""Перший малюнок лотка Sum3D — з готового кешу, без скану (01.10.26).

Сторінка черги приходила з порожнім лотком, він наповнювався запитом після
показу, кнопки шапки переносились на другий ряд, і вся черга їхала вниз на
48 px (заміряно layout-shift на dev). Сканувати на рендері не можна (2–3 с на
мережевій теці — `test_full_render_does_not_scan_sum3d`), тому беремо лише те,
що вже тримає грійник.
"""

from app.services import sum3d_capture as cap


def setup_function():
    with cap._cache_lock:
        cap._cache.clear()


def test_no_path_and_no_cache_mean_none():
    assert cap.cached_projects("") is None
    assert cap.cached_projects(r"\\host\cam-work") is None


def test_warm_cache_is_returned_without_touching_disk(monkeypatch, tmp_path):
    (tmp_path / "2026-10-01_10-42-05.cam").write_text("x")
    assert cap.warm_projects(str(tmp_path)) == 1

    def boom(*_a, **_k):
        raise AssertionError("cached_projects не має ходити на диск")

    monkeypatch.setattr(cap.Path, "iterdir", boom)
    [project] = cap.cached_projects(str(tmp_path))
    assert project.sum3d_id == "10-42-05"


def test_stale_cache_is_not_used(monkeypatch, tmp_path):
    (tmp_path / "2026-10-01_10-42-05.cam").write_text("x")
    cap.warm_projects(str(tmp_path))
    real = cap.monotonic
    monkeypatch.setattr(cap, "monotonic", lambda: real() + cap._CACHE_TTL_SECONDS + 1)
    assert cap.cached_projects(str(tmp_path)) is None
