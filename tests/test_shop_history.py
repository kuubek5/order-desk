"""Звіти історії цеху для MCP (12.11.26): лише читання, числа на засіяних даних."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services import shop_history
from app.services.mcp_tools import TOOLS_BY_NAME
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

NEW_TOOLS = ("kmill_machine_history", "kmill_cam_units", "kmill_disc_usage",
             "kmill_furnace_cycles", "kmill_export_overview",
             "kmill_disc_check", "kmill_discs_inventory", "kmill_program_folders")


def test_new_tools_are_registered():
    assert set(NEW_TOOLS) <= set(TOOLS_BY_NAME)


def test_machine_history_run_wait_and_repeat(app_db):  # noqa: F811
    from app.models import Machine, MachineReading

    _app, factory = app_db
    t0 = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=1)
    a = "PMMA14_14-A2-X132_2026-11-11_08-50-00.iso"
    b = "zr14_14-Monolith-A2-x5_2026-11-11_10-30-00.iso"
    with factory() as db:
        db.add(Machine(name="250i-Sec", host="10.0.0.58", port=8765, created_at=t0))
        # Покази частіше за розрив GAP_MINUTES, як на справжньому верстаті.
        rows = [(0, a, 10), (10, a, 60), (20, a, 100), (30, a, 100),  # 20 хв роботи, 15 хв простою
                (35, b, 5), (45, b, 100),  # 10 хв простою до наступної
                (55, a, 20)]  # та сама програма вдруге
        for minute, iso, pct in rows:
            db.add(MachineReading(host="10.0.0.58", captured_at=t0 + timedelta(minutes=minute),
                                  iso_name=iso, percent=pct))
        db.commit()
        report = shop_history.machine_history(db, 3)
    sec = report["верстати"][0]
    assert sec["програм"] == 3
    assert sec["дійшли_до_100"] == 2
    assert sec["повторні_запуски"] == 1 and sec["повторні_приклади"] == [a]
    assert [p["хв"] for p in sec["найдовші_простої"]] == [15, 10]
    assert sec["медіана_роботи_хв"] == 15  # 20 і 10
    assert {"ПММА 14 мм", "Цирконій 14 мм"} <= set(sec["робота_за_диском_медіана_хв"])


def test_furnace_cycles(app_db):  # noqa: F811
    from app.models import Furnace, FurnaceReading

    _app, factory = app_db
    t0 = datetime.now() - timedelta(hours=10)
    with factory() as db:
        db.add(Furnace(name="Піч 1", host="10.0.0.20", port=5900, created_at=t0))
        for minute, status, temp in [(0, "WAIT", 25), (10, "RUN", 300), (70, "RUN", 1450),
                                     (130, "RUN", 1500), (200, "WAIT", 900)]:
            db.add(FurnaceReading(host="10.0.0.20", captured_at=t0 + timedelta(minutes=minute),
                                  status=status, temp_c=temp))
        db.commit()
        report = shop_history.furnace_cycles(db, 2)
    oven = report["печі"][0]
    assert oven["циклів"] == 1
    assert oven["цикли"][0]["макс_°C"] == 1500 and oven["цикли"][0]["до_макс_хв"] == 120


def test_disc_usage_skips_baseline(app_db):  # noqa: F811
    from app.models import CamBlank

    _app, factory = app_db
    now = datetime.now()
    with factory() as db:
        db.add(CamBlank(rel_path="pmma/14/a", material_dir="pmma", height_dir="14", file_name="a.blk",
                        height=14, shade="a2", first_seen_at=now - timedelta(days=2)))
        # «точка відліку»: замовлено в мить появи, без замовлення — не новий диск
        db.add(CamBlank(rel_path="pmma/14/b", material_dir="pmma", height_dir="14", file_name="b.blk",
                        height=14, shade="a2", first_seen_at=now - timedelta(days=2),
                        ordered_at=now - timedelta(days=2)))
        db.commit()
        report = shop_history.disc_usage(db, 4)
    assert report["нових_дисків"] == 1
    assert report["групи"][0]["файлів_у_теці_зараз"] == 2


def test_cam_units_counts_teeth_in_project(app_db, tmp_path):  # noqa: F811
    from app.models import Order
    from app.settings_store import set_setting

    _app, factory = app_db
    day = datetime.now().date()
    tab = day.strftime("%d.%m.%y")
    project = tmp_path / f"{day:%Y-%m-%d}_10-11-12.cam"
    project.write_bytes(
        b"\x00\x01C:\\cad\\Pacient_11-anatomy_cad.stl\x00junk\x02C:\\cad\\Pacient_21-anatomy_cad.stl\x00"
        + "D:\\x\\Pacient_22-anatomy_cad.stl".encode("utf-16-le")
    )
    with factory() as db:
        set_setting(db, "sum3d_projects_path", str(tmp_path))
        db.add(Order(source="sheet_client", sheet_tab=tab, row_number=3, quantity="2",
                     material_color="моно A2", sum3d_id="10-11-12", client_name="Клієнт"))
        db.commit()
        report = shop_history.cam_units(db, 2)
    assert report.get("розбіжність") == 1, report
    row = report["приклади"][0]
    assert (row["у_таблиці"], row["зубів_у_stl"]) == (2, 3)
    assert "Pacient" not in str(report), "назви STL назовні не віддаються"


def test_export_overview_counts_and_never_lists_files(app_db, tmp_path):  # noqa: F811
    from app.models import Order
    from app.settings_store import set_setting

    _app, factory = app_db
    batch = tmp_path / "Клієнт" / "12.11.26" / "mono a2"
    batch.mkdir(parents=True)
    (batch / "Pacient_11.stl").write_bytes(b"x" * 100)
    (tmp_path / "Клієнт" / "01.01.26").mkdir()
    with factory() as db:
        set_setting(db, "export_folder_path", str(tmp_path))
        db.add(Order(source="email", quantity="1", material_color="моно A2",
                     export_folder_path="Клієнт/12.11.26/mono a2"))
        db.commit()
        report = shop_history.export_overview(db)
    exp = report["export"]
    assert (exp["клієнтів"], exp["партій"], exp["файлів"]) == (1, 2, 1)
    assert exp["партій_без_закріпленої_роботи"] == 1
    assert "Pacient" not in str(report)
