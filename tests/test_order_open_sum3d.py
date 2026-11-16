"""«Відкрити в Sum3D» у паспорті роботи (власник 16.11.26): файл проєкту
`<дата>_<Sum3D>.cam` відкривається асоціацією Windows, як подвійний клік."""

from __future__ import annotations

import json

from app import platform_windows
from app.models import Order
from app.routers import orders as orders_router
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура


def _order(factory, sum3d="12-01-45", tab="16.11.26"):
    with factory() as db:
        order = Order(source="lab", sheet_tab=tab, row_number=1, work_order_no="29267",
                      sum3d_id=sum3d)
        db.add(order)
        db.commit()
        return order.id


def test_button_opens_project_of_the_work(app_db, tmp_path, monkeypatch):  # noqa: F811
    app, factory = app_db
    (tmp_path / "2026-11-16_12-01-45.cam").write_bytes(b"cam")
    oid = _order(factory)
    monkeypatch.setattr(orders_router, "get_sum3d_projects_path", lambda db: str(tmp_path))
    opened = []
    monkeypatch.setattr(platform_windows, "open_with_default_app", lambda path: opened.append(path))

    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, page = client.get(f"/orders/{oid}")
    assert f'data-open-folder-url="/orders/{oid}/open-sum3d"' in page
    status, _, body = client.post(f"/orders/{oid}/open-sum3d", {})
    assert status == 200 and json.loads(body) == {"opened": True}
    assert [p.name for p in opened] == ["2026-11-16_12-01-45.cam"]


def test_no_project_is_404_with_reason(app_db, tmp_path, monkeypatch):  # noqa: F811
    app, factory = app_db
    oid = _order(factory)
    monkeypatch.setattr(orders_router, "get_sum3d_projects_path", lambda db: str(tmp_path))
    opened = []
    monkeypatch.setattr(platform_windows, "open_with_default_app", lambda path: opened.append(path))
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, body = client.post(f"/orders/{oid}/open-sum3d", {})
    assert status == 404 and "проєкту Sum3D" in json.loads(body)["detail"]
    assert opened == []


def test_no_button_without_projects_folder_or_sum3d(app_db, tmp_path, monkeypatch):  # noqa: F811
    app, factory = app_db
    monkeypatch.setattr(orders_router, "get_sum3d_projects_path", lambda db: "")
    oid = _order(factory)
    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, page = client.get(f"/orders/{oid}")
    assert "/open-sum3d" not in page
    monkeypatch.setattr(orders_router, "get_sum3d_projects_path", lambda db: str(tmp_path))
    bare = _order(factory, sum3d=None)
    _, _, page = client.get(f"/orders/{bare}")
    assert "/open-sum3d" not in page


def test_network_pc_gets_the_path_instead(app_db, tmp_path, monkeypatch):  # noqa: F811
    app, factory = app_db
    (tmp_path / "2026-11-16_12-01-45.cam").write_bytes(b"cam")
    oid = _order(factory)
    monkeypatch.setattr(orders_router, "get_sum3d_projects_path", lambda db: str(tmp_path))
    opened = []
    monkeypatch.setattr(platform_windows, "open_with_default_app", lambda path: opened.append(path))
    from app.services import network_access

    monkeypatch.setattr(network_access, "is_trusted_request", lambda request, db: True)
    client = MiniClient(app, client_host="192.168.1.50")
    client.login(*ADMIN)
    status, _, body = client.post(f"/orders/{oid}/open-sum3d", {})
    payload = json.loads(body)
    assert status == 200 and payload["opened"] is False
    assert payload["path"].endswith("2026-11-16_12-01-45.cam") and opened == []


def test_windows_without_sum3d_association_says_so(app_db, tmp_path, monkeypatch):  # noqa: F811
    app, factory = app_db
    (tmp_path / "2026-11-16_12-01-45.cam").write_bytes(b"cam")
    oid = _order(factory)
    monkeypatch.setattr(orders_router, "get_sum3d_projects_path", lambda db: str(tmp_path))

    def _no_app(path):
        raise OSError(1155, "No application is associated with the specified file")

    monkeypatch.setattr(platform_windows, "open_with_default_app", _no_app)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, body = client.post(f"/orders/{oid}/open-sum3d", {})
    assert status == 500 and "Sum3D" in json.loads(body)["detail"]
