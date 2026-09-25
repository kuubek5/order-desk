"""«Відкрити папку з роботами цього клієнта» в картці клієнта (власник 25.09.26)."""

from __future__ import annotations

from app.models import Client, ClientNameAlias
from app.routers import clients as clients_router
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура


def _client(factory, tmp_path, bound=True):
    (tmp_path / "Bogdan Rosocha").mkdir()
    with factory() as db:
        client = Client(canonical_name="Bogdan Rosocha")
        db.add(client)
        if bound:
            db.add(ClientNameAlias(sheet_name="Bogdan Rosocha",
                                   export_folder_name="Bogdan Rosocha", confirmed=True))
        db.commit()
        return client.id


def test_button_opens_bound_folder(app_db, tmp_path, monkeypatch):  # noqa: F811
    app, factory = app_db
    cid = _client(factory, tmp_path)
    monkeypatch.setattr(clients_router, "get_export_folder_path", lambda db: str(tmp_path))
    opened = []
    monkeypatch.setattr(clients_router, "open_folder_in_explorer", lambda path: opened.append(path))

    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, pane = client.get(f"/clients/{cid}/pane")
    assert f'data-open-folder-url="/clients/{cid}/open-folder"' in pane
    status, _, _ = client.post(f"/clients/{cid}/open-folder", {})
    assert status == 200
    assert [str(p) for p in opened] == [str(tmp_path / "Bogdan Rosocha")]


def test_no_button_and_404_without_bound_folder(app_db, tmp_path, monkeypatch):  # noqa: F811
    app, factory = app_db
    cid = _client(factory, tmp_path, bound=False)
    monkeypatch.setattr(clients_router, "get_export_folder_path", lambda db: str(tmp_path))
    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, pane = client.get(f"/clients/{cid}/pane")
    assert "/open-folder" not in pane
    status, _, _ = client.post(f"/clients/{cid}/open-folder", {})
    assert status == 404
