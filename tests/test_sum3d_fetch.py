"""Забрати програмний файл Sum3D на аналіз (`sum3d_fetch`, `kmill_sum3d_fetch`).

Власник 22.11.26 дозволив копіювати файли з робочого ПК «не змінюючи їх».
Стережемо межі: лише теки Sum3D із Налаштувань, лише програмні розширення,
одноразове посилання з терміном, і жодного сліду на самому файлі."""

from __future__ import annotations

import hashlib

import pytest

from app.services import mcp_gateway as mg
from app.services import sum3d_explore, sum3d_fetch
from app.services.mcp_common import ToolError
from app.services.mcp_tools import TOOLS_BY_NAME
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура
from tests.test_sum3d_explore import install  # noqa: F401 — фікстура

BODY = b"MZ-ascii-only-program-bytes-" * 40


@pytest.fixture(autouse=True)
def _no_tickets():
    sum3d_fetch.forget_all()
    yield
    sum3d_fetch.forget_all()


def _number(root, name: str, **listing) -> int:
    """Номер «№» файла в переліку розвідки (тим самим шляхом, що в MCP)."""
    report = TOOLS_BY_NAME["kmill_sum3d_explore"].run(None, {"what": "files", "root": 1, **listing})
    assert report["файли"], "перелік порожній"
    with sum3d_explore._lock:
        paths = list(sum3d_explore._last)
    return next(i for i, p in enumerate(paths, start=1) if p.endswith(name))


def _offer(number: int) -> dict:
    return TOOLS_BY_NAME["kmill_sum3d_fetch"].run(None, {"file": number})


def _gateway(monkeypatch, factory, enabled: bool = True) -> MiniClient:
    from app.routers.mcp import create_mcp_app
    from app.settings_store import set_setting

    with factory() as db:
        set_setting(db, mg.ENABLED_KEY, "1" if enabled else "")
        db.commit()
    monkeypatch.setattr("app.db.SessionLocal", factory)
    return MiniClient(create_mcp_app())


def test_program_file_goes_out_once_and_stays_untouched(install, app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    target = install / "Sum3dDll_x64.dll"
    target.write_bytes(BODY)
    before = (target.stat().st_mtime_ns, target.stat().st_size)

    ticket = _offer(_number(install, "Sum3dDll_x64.dll", ext="dll"))
    assert ticket["файл"] == "Sum3dDll_x64.dll" and ticket["байт"] == len(BODY)
    assert ticket["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert ticket["посилання"].startswith("/fetch/") and len(ticket["посилання"]) > 40

    client = _gateway(monkeypatch, factory)
    status, headers, body = client.get(ticket["посилання"])
    assert status == 200 and body.encode() == BODY
    assert headers["content-length"] == str(len(BODY)) and headers["x-sha256"] == ticket["sha256"]
    # Удруге те саме посилання — як будь-який невідомий шлях.
    assert client.get(ticket["посилання"])[0] == 404
    assert (target.stat().st_mtime_ns, target.stat().st_size) == before, "файл не змінено"
    assert sorted(p.name for p in install.iterdir()) == sorted(
        ["Strategies", "CAM-work", "Sum3D.exe", "Qt5Core.dll", "Sum3dDll_x64.dll"]), "нічого не дописано в теку"


def test_projects_work_files_and_unknown_types_are_refused(install):  # noqa: F811
    (install / "Welcome.cam").write_bytes(b"TECN 2.0 project")
    (install / "disc.blk").write_bytes(b"<SUM3D_BLANK V1.2>")
    for name, why in (("Welcome.cam", ".cam"), ("disc.blk", ".blk"), (".str", ".str")):
        with pytest.raises(ToolError) as err:
            _offer(_number(install, name))
        assert why in str(err.value), name
    # Конфігураційний файл стратегії — можна.
    assert _offer(_number(install, "crown_fine.xml"))["байт"] > 0


def test_path_outside_sum3d_folders_is_refused(install, tmp_path, monkeypatch):  # noqa: F811
    stranger = tmp_path / "elsewhere" / "kuubmill.dll"
    stranger.parent.mkdir()
    stranger.write_bytes(BODY)
    work = install / "CAM-work" / "notes.txt"
    work.write_text("x", encoding="utf-8")
    for path, why in ((stranger, "поза теками"), (work, "теці робіт")):
        monkeypatch.setattr(sum3d_explore, "_last", [str(path)])
        with pytest.raises(ToolError) as err:
            _offer(1)
        assert why in str(err.value)
    with pytest.raises(ToolError):
        TOOLS_BY_NAME["kmill_sum3d_fetch"].run(None, {})


def test_ticket_expires_and_live_tickets_are_capped(install):  # noqa: F811
    number = _number(install, "Qt5Core.dll", ext="dll")
    old = sum3d_fetch.offer(None, number, now=1000.0)
    key = old["посилання"].rsplit("/", 1)[1]
    assert sum3d_fetch.redeem(key, now=1000.0 + sum3d_fetch.TTL_SECONDS + 1) is None, "прострочене не діє"
    assert sum3d_fetch.redeem(key, now=1001.0) is None, "і вдруге теж — квиток уже вийнято"

    for _ in range(sum3d_fetch.MAX_LIVE):
        sum3d_fetch.offer(None, number, now=2000.0)
    with pytest.raises(sum3d_fetch.FetchError):
        sum3d_fetch.offer(None, number, now=2001.0)
    # Прострочені місце звільняють.
    assert sum3d_fetch.offer(None, number, now=2000.0 + sum3d_fetch.TTL_SECONDS + 1)["байт"] > 0


def test_closed_gateway_and_wrong_key_give_plain_404(install, app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    ticket = _offer(_number(install, "Qt5Core.dll", ext="dll"))
    closed = _gateway(monkeypatch, factory, enabled=False)
    assert closed.get(ticket["посилання"])[0] == 404, "вимкнений доступ — жодних файлів"
    opened = _gateway(monkeypatch, factory)
    status, _, body = opened.get("/fetch/" + "x" * 43)
    assert status == 404 and body == "Не знайдено"
    # Квиток під час закритого доступу НЕ згорів: ключ не чіпали.
    assert opened.get(ticket["посилання"])[0] == 200
