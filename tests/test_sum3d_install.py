"""Програма Sum3D у налаштуваннях (12.11.26): кілька версій, спільні стратегії, лише читання."""

from __future__ import annotations

import json

from app.services import sum3d_install
from app.services.mcp_tools import TOOLS_BY_NAME
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура


def _fake_install(root):
    (root / "Strategies").mkdir(parents=True)
    (root / "Sum3D.exe").write_bytes(b"MZ")
    (root / "materials.xml").write_text("<x/>", encoding="utf-8")
    (root / "Sum3D.log").write_text("ok", encoding="utf-8")
    (root / "Strategies" / "zr_anatomy.str").write_text("s", encoding="utf-8")
    (root / "Strategies" / "Pacient_11.stl").write_bytes(b"solid")
    return root


def test_clean_installs_drops_empty_quotes_and_duplicates():
    items = sum3d_install.clean_installs([("нова", '"C:\Sum3D 2023"'), ("", ""), ("копія", "c:\sum3d 2023"),
                                          ("", "D:\Sum3D old")])
    assert [(i.name, i.path) for i in items] == [("нова", "C:\Sum3D 2023"), ("Sum3D 2", "D:\Sum3D old")]


def test_probe_reads_structure_and_hides_stl_names(tmp_path):
    root = _fake_install(tmp_path / "sum3d")
    before = sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*"))
    probe = sum3d_install.probe_folder(str(root))
    assert probe.state == "present" and probe.files == 5
    assert [n for n, _v in probe.exes] == ["Sum3D.exe"]
    assert [c[0] for c in probe.configs] == ["materials.xml", "Strategies/zr_anatomy.str"]
    assert [lg[0] for lg in probe.logs] == ["Sum3D.log"]
    assert probe.exts[".stl"] == 1
    assert "Pacient" not in str(probe.newest) and "Pacient" not in str(probe.configs)
    assert sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*")) == before, "нічого не змінено"


def test_admin_saves_two_versions_and_strategies_operator_cannot(app_db, tmp_path):  # noqa: F811
    from app.settings_store import get_setting

    app, factory = app_db
    new = _fake_install(tmp_path / "new")
    old = tmp_path / "old"
    old.mkdir()
    admin = MiniClient(app)
    admin.login(*ADMIN)
    status, _, html = admin.get("/settings")
    assert status == 200 and 'data-sec="sum3d"' in html and "Версії програми" in html

    status, _, body = admin.post("/settings/sum3d/check", {"field": "path_0", "path_0": str(new)})
    assert status == 200 and "Sum3D.exe" in body and "pf-ok" in body
    assert admin.post("/settings/sum3d/check", {"field": "evil", "evil": "C:\\"})[0] == 400

    assert admin.post("/settings/sum3d", {
        "name_0": "нова", "path_0": str(new), "name_1": "стара", "path_1": str(old),
        "name_2": "", "path_2": "", "strategies": str(new / "Strategies"),
    })[0] == 303
    with factory() as db:
        saved = json.loads(get_setting(db, sum3d_install.INSTALLS_KEY))
        assert [s["name"] for s in saved] == ["нова", "стара"]
        assert get_setting(db, sum3d_install.STRATEGIES_KEY) == str(new / "Strategies")
        report = TOOLS_BY_NAME["kmill_sum3d_install"].run(db, {})
    assert [i["назва"] for i in report["встановлення"]] == ["нова", "стара"]
    assert report["стратегії"]["файлів"] == 2
    assert "Pacient" not in json.dumps(report, ensure_ascii=False)

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert 'data-sec="sum3d"' not in operator.get("/settings")[2]
    assert operator.post("/settings/sum3d", {"path_0": ""})[0] == 403
