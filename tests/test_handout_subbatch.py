"""Кілька партій одного кольору в одній теці (власник 25.09.26, Середюк 24.09).

Оператор за день клав листи того самого кольору в одну теку кольору: перший —
у корінь, наступні — у `Новая папка`, `Новая папка (2)`… Видача зіставляла
кожну роботу з правильною текою, але прев'ю завжди відкривалось на першому
файлі кореня — на трьох роботах із чотирьох оператор бачив чужу коронку.
Тепер рядок каже прев'ю, з якого STL почати: своя партія за часом Sum3D.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.export_scanner import SubBatch, scan_export_folder
from app.services.handout import start_stl_for

# Бойові дані 24.09.26: тека `Середюк/Новая папка (762)/mono a3.5`.
SEREDIUK = SimpleNamespace(
    created_at=datetime(2026, 9, 24, 10, 8, 1),  # тека партії дня (рівень 2)
    parts=(
        SubBatch(datetime(2026, 9, 24, 10, 2, 0), "2026-09-23_00002-005-36-tooth.stl"),
        SubBatch(datetime(2026, 9, 24, 21, 46, 0), "Новая папка/a.stl"),
        SubBatch(datetime(2026, 9, 24, 23, 4, 0), "Новая папка (2)/b.stl"),
        SubBatch(datetime(2026, 9, 25, 0, 24, 0), "Новая папка (3)/c.stl"),
    ),
)


def test_each_work_opens_its_own_part():
    assert start_stl_for(SEREDIUK, "10-08-17") == "2026-09-23_00002-005-36-tooth.stl"
    assert start_stl_for(SEREDIUK, "21-46-36") == "Новая папка/a.stl"
    assert start_stl_for(SEREDIUK, "23-05-13") == "Новая папка (2)/b.stl"
    # Нічний Sum3D (до межі доби) — наступна календарна дата: 25.09 00:24.
    assert start_stl_for(SEREDIUK, "00-24-43") == "Новая папка (3)/c.stl"


def test_no_choice_without_several_parts_or_sum3d():
    single = SimpleNamespace(created_at=SEREDIUK.created_at, parts=SEREDIUK.parts[:1])
    assert start_stl_for(single, "10-08-17") is None
    assert start_stl_for(SimpleNamespace(created_at=SEREDIUK.created_at, parts=()), "10-08-17") is None
    assert start_stl_for(SEREDIUK, None) is None
    assert start_stl_for(SEREDIUK, "") is None
    # Sum3D раніший за будь-яку партію — не вгадуємо.
    assert start_stl_for(SEREDIUK, "09-00-00") is None


def test_clock_skew_of_a_minute_still_picks_the_part():
    """Годинник файлового сервера й ПК із Sum3D розходяться: підтека «пізніша»
    за Sum3D на хвилину — однаково її партія."""
    assert start_stl_for(SEREDIUK, "23-03-10") == "Новая папка (2)/b.stl"


def test_multiline_sum3d_uses_the_earliest():
    assert start_stl_for(SEREDIUK, "21-46-36\n23-05-13") == "Новая папка/a.stl"


def test_scanner_records_parts_of_a_material_folder(tmp_path):
    material = tmp_path / "Середюк" / "Новая папка (762)" / "mono a3.5"
    material.mkdir(parents=True)
    (material / "root.stl").write_bytes(b"x")
    (material / "note.xml").write_bytes(b"x")
    for sub, name in (("Новая папка", "a.stl"), ("Новая папка (2)", "b.stl")):
        (material / sub).mkdir()
        (material / sub / name).write_bytes(b"x")
    (material / "порожня").mkdir()  # без STL — не партія

    [entry] = [e for e in scan_export_folder(tmp_path) if e.material_color_folder_name == "mono a3.5"]
    assert {p.first_stl for p in entry.parts} == {"root.stl", "Новая папка/a.stl", "Новая папка (2)/b.stl"}
    assert list(entry.parts) == sorted(entry.parts, key=lambda p: p.created_at)


def test_scanner_single_part_stays_empty(tmp_path):
    material = tmp_path / "Клієнт" / "Новая папка" / "mono a2"
    material.mkdir(parents=True)
    (material / "Іваненко").mkdir()
    (material / "Іваненко" / "crown.stl").write_bytes(b"x")
    [entry] = scan_export_folder(tmp_path)
    assert entry.parts == ()


def test_handout_row_tells_the_preview_where_to_start(app_db, monkeypatch, tmp_path):  # noqa: F811
    """Справжній рендер /handout: рядок роботи несе data-stl-preview-file своєї
    партії, а список файлів прев'ю (сам /stl-preview) не змінюється."""
    from sqlalchemy import select

    import app.web as web
    from app.models import Order, User
    from app.routers import handout as handout_router_mod
    from tests.asgi_client import MiniClient
    from tests.test_settings_slabs_render import OPERATOR

    app, factory = app_db
    tab = "24.09.26"
    with factory() as db:
        user = db.scalars(select(User).where(User.username == OPERATOR[0])).one()
        user.handout_layout = "nav"
        for row, sum3d in ((7, "10-08-17"), (8, "00-24-43")):
            db.add(Order(source="sheet_client", sheet_tab=tab, row_number=row,
                         client_name="Середюк", material_color="mono a3.5", quantity="1",
                         sum3d_id=sum3d, status="відфрезеровано"))
        db.commit()

    folder = tmp_path / "Середюк" / "Новая папка (762)" / "mono a3.5"
    folder.mkdir(parents=True)
    entry = SimpleNamespace(
        client_folder_name="Середюк", batch_folder_name="Новая папка (762)",
        material_color_folder_name="mono a3.5", created_at=SEREDIUK.created_at,
        files=["2026-09-23_00002-005-36-tooth.stl"], folder_path=folder,
        subfolders=3, parts=SEREDIUK.parts,
    )
    monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
    monkeypatch.setattr(handout_router_mod, "list_export_client_names_cached", lambda root: ["Середюк"])
    monkeypatch.setattr(web, "list_export_client_names_cached", lambda root: ["Середюк"])
    monkeypatch.setattr(handout_router_mod, "scan_export_for_clients",
                        lambda root, folders, nb: {"Середюк": [entry]})
    monkeypatch.setattr(handout_router_mod, "scan_export_latest_for_clients", lambda root, folders: {})
    monkeypatch.setattr(handout_router_mod, "scan_export_client_cached", lambda root, f, nb: [entry])
    monkeypatch.setattr(handout_router_mod, "scan_export_client_latest_cached", lambda root, f: [])

    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.get(f"/handout?source=all&day={tab}")
    assert status == 200, html[:300]
    assert 'data-stl-preview-file="2026-09-23_00002-005-36-tooth.stl"' in html
    assert 'data-stl-preview-file="Новая папка (3)/c.stl"' in html


from tests.test_settings_slabs_render import app_db  # noqa: E402,F401 — фікстура
