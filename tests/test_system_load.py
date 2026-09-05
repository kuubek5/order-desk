"""Жива стрічка навантаження: CPU/ОЗП ПК і CRM (04.09.26).

Що ламається тихо:
- рівень (ok/mid/hot) і рух рахує СЕРВЕР — інакше на клієнті не було б від чого
  оживати; пороги 70/90;
- CPU процесу нормалізується до 0–100 (сумується по ядрах — міг бути 140%);
- немає psutil / немає заміру → ok=False і чесний «—», не нуль;
- заливка бару — transform:scaleX (не width): сторож проти layout-thrash;
- роут за логіном, обгортка з поллом віддається завжди.
"""
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app.services import system_load as sl


@pytest.mark.parametrize("cpu,level,flow,pulse", [
    (10, "ok", False, False),
    (69, "ok", False, False),
    (70, "mid", True, False),
    (89, "mid", True, False),
    (91, "hot", True, True),
    (100, "hot", True, True),
])
def test_level_thresholds(cpu, level, flow, pulse):
    assert sl._level(cpu) == (level, flow, pulse)


def test_sample_normalizes_process_cpu_and_fills_state(monkeypatch):
    # Фейковий psutil: система 96% (пік), процес 320% по ядрах / 8 = 40%.
    class VM:
        percent = 62.0
        used = 20 * 1073741824
        total = 32 * 1073741824

    class FakeProc:
        def cpu_percent(self, interval=None):
            return 320.0

        def memory_info(self):
            return SimpleNamespace(rss=256 * 1048576)

        def children(self, recursive=False):
            return []

    fake = SimpleNamespace(
        cpu_percent=lambda interval=None: 96.0,
        virtual_memory=lambda: VM(),
        cpu_count=lambda: 8,
    )
    monkeypatch.setattr(sl, "psutil", fake)
    monkeypatch.setattr(sl, "_proc", FakeProc())
    sl.sample()
    st = sl.snapshot()
    assert st["ok"] is True
    assert st["pc_cpu"] == 96 and st["level"] == "hot" and st["flow"] and st["pulse"]
    assert st["crm_cpu"] == 40                      # 320/8, нормалізовано
    assert st["crm_ram_mb"] == 256
    assert st["pc_ram_used_gb"] == 20.0 and st["pc_ram_total_gb"] == 32.0
    sl.reset_for_tests()


def test_process_cpu_capped_at_100(monkeypatch):
    class FakeProc:
        def cpu_percent(self, interval=None):
            return 5000.0

        def memory_info(self):
            return SimpleNamespace(rss=0)

        def children(self, recursive=False):
            return []

    monkeypatch.setattr(sl, "psutil", SimpleNamespace(
        cpu_percent=lambda interval=None: 10.0,
        virtual_memory=lambda: SimpleNamespace(percent=1, used=0, total=1073741824),
        cpu_count=lambda: 4))
    monkeypatch.setattr(sl, "_proc", FakeProc())
    sl.sample()
    assert sl.snapshot()["crm_cpu"] == 100          # не 1250
    sl.reset_for_tests()


def test_sample_without_psutil_is_not_ok(monkeypatch):
    monkeypatch.setattr(sl, "psutil", None)
    sl.sample()
    assert sl.snapshot() == {"ok": False}


def test_sample_swallows_errors(monkeypatch):
    def boom(interval=None):
        raise RuntimeError("no counter")
    monkeypatch.setattr(sl, "psutil", SimpleNamespace(cpu_percent=boom, virtual_memory=lambda: None, cpu_count=lambda: 1))
    sl.sample()                                       # не кидає
    assert sl.snapshot() == {"ok": False}
    sl.reset_for_tests()


def _request(uid=1):
    return SimpleNamespace(session={"user_id": uid} if uid else {},
                           client=SimpleNamespace(host="127.0.0.1"),
                           headers=Headers({}), state=SimpleNamespace())


def _render(state):
    from app.routers.deps import templates
    return templates.env.get_template("_system_load.html").render(request=None, load=state)


def test_template_shows_numbers_and_level_class_and_scalex():
    html = _render({"ok": True, "level": "hot", "flow": True, "pulse": True,
                    "pc_cpu": 96, "pc_ram_pct": 80, "pc_ram_used_gb": 25.6, "pc_ram_total_gb": 32.0,
                    "crm_cpu": 8, "crm_ram_mb": 240})
    assert 'id="system-load"' in html and 'hx-get="/system/load"' in html
    assert "sysload lvl-hot" in html and " flow" in html and " pulse" in html
    assert "transform:scaleX(0.96)" in html and "transform:scaleX(0.08)" in html
    assert "width:" not in html.split("<style")[0]   # заливка НЕ через width
    # порядок CRM → ПК → ОЗП
    assert html.index(">CRM<") < html.index("ПК CPU") < html.index(">ОЗП<")


def test_template_empty_state_keeps_wrapper_and_poll():
    html = _render({"ok": False})
    assert 'id="system-load"' in html and 'hx-get="/system/load"' in html
    assert "—" in html and "lvl-" not in html


def test_route_requires_login_and_returns_wrapper(monkeypatch):
    from app.routers import queue as queue_router
    from fastapi import HTTPException

    monkeypatch.setattr(queue_router, "get_current_user", lambda request, db: None)
    with pytest.raises(HTTPException) as exc:
        queue_router.system_load(request=_request(), db=None)
    assert exc.value.status_code == 401

    monkeypatch.setattr(queue_router, "get_current_user", lambda request, db: SimpleNamespace(id=1))
    monkeypatch.setattr(queue_router, "system_load_snapshot", lambda: {"ok": False})
    resp = queue_router.system_load(request=_request(), db=None)
    assert resp.status_code == 200
    assert b'id="system-load"' in resp.body
