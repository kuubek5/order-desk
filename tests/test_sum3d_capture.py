"""Захоплення проєктів Sum3D із теки Cam-work (хід 1).

Реальність цеху: проєкт — це ФАЙЛ `*.cam` з іменем `YYYY-MM-DD_HH-MM-SS`
(перевірено на D:\\CAM-work). Windows-копії дописують «— копия». Тека-проєкт —
запасний формат. Ці тести стережуть, щоб читання не зламалось на жодному з них.
"""
from __future__ import annotations

import os
import time

from app.services.sum3d_capture import parse_project_name, scan_projects


def _touch(path, when=None):
    """Створити файл і (опційно) виставити mtime для детермінованого порядку."""
    path.write_bytes(b"cam")
    if when is not None:
        os.utime(path, (when, when))


# ── розбір імені ──────────────────────────────────────────────────────────

def test_parse_valid_name():
    assert parse_project_name("2026-09-02_17-55-28") == ("2026-09-02", "17-55-28")


def test_parse_space_separator():
    assert parse_project_name("2026-09-02 18-04-12") == ("2026-09-02", "18-04-12")


def test_parse_with_suffix():
    # Дублікат/повтор із суфіксом — дату+час усе одно беремо з початку.
    assert parse_project_name("2026-09-02_17-55-28 (2)") == ("2026-09-02", "17-55-28")


def test_parse_windows_copy_suffix():
    # Реальний суфікс Windows «— копия» / «— копия (2)» зі скріна цеху.
    assert parse_project_name("2025-12-23_14-00-08 — копия") == ("2025-12-23", "14-00-08")
    assert parse_project_name("2025-09-29_10-30-36 — копия (2)") == ("2025-09-29", "10-30-36")


def test_parse_rejects_non_project():
    assert parse_project_name("нова папка") is None
    assert parse_project_name("24122") is None
    assert parse_project_name("") is None


# ── скан: порожні/биті шляхи ───────────────────────────────────────────────

def test_scan_empty_or_missing_path():
    assert scan_projects(None) == []
    assert scan_projects("") == []
    assert scan_projects("   ") == []
    assert scan_projects("P:/definitely/not/here/xyz") == []


# ── скан: РЕАЛЬНИЙ формат — файли .cam ────────────────────────────────────

def test_scan_reads_cam_files_newest_first(tmp_path):
    names = ["2025-09-29_10-30-36", "2025-12-22_11-09-07", "2025-12-23_14-33-04"]
    for i, n in enumerate(names):
        _touch(tmp_path / f"{n}.cam", when=time.time() + i)

    got = scan_projects(str(tmp_path))
    assert [p.sum3d_id for p in got] == ["14-33-04", "11-09-07", "10-30-36"]
    assert got[0].date == "2025-12-23"
    assert got[0].folder == "2025-12-23_14-33-04.cam"  # повна назва для діагностики


def test_scan_cam_extension_case_insensitive(tmp_path):
    _touch(tmp_path / "2026-01-05_09-00-00.CAM")
    got = scan_projects(str(tmp_path))
    assert [p.sum3d_id for p in got] == ["09-00-00"]


def test_scan_windows_copies_dedup_to_one(tmp_path):
    # Оригінал + дві копії того самого проєкту → один запис, найсвіжіший mtime.
    base = "2025-09-29_10-30-36"
    _touch(tmp_path / f"{base}.cam", when=1000.0)
    _touch(tmp_path / f"{base} — копия.cam", when=2000.0)
    _touch(tmp_path / f"{base} — копия (2).cam", when=3000.0)

    got = scan_projects(str(tmp_path))
    assert len(got) == 1
    assert got[0].sum3d_id == "10-30-36"
    assert got[0].mtime == 3000.0  # найсвіжіша копія виграє


def test_scan_same_time_different_days_kept_separate(tmp_path):
    # Той самий час у різні дні — це РІЗНІ проєкти, дедуп їх НЕ зливає.
    _touch(tmp_path / "2025-09-29_10-30-36.cam", when=1000.0)
    _touch(tmp_path / "2025-12-01_10-30-36.cam", when=2000.0)
    got = scan_projects(str(tmp_path))
    assert len(got) == 2
    assert {(p.date, p.sum3d_id) for p in got} == {
        ("2025-09-29", "10-30-36"), ("2025-12-01", "10-30-36"),
    }


# ── скан: сміття відкидається ──────────────────────────────────────────────

def test_scan_ignores_non_cam_files(tmp_path):
    _touch(tmp_path / "2026-09-02_11-11-11.txt")   # не проєкт
    _touch(tmp_path / "2026-09-02_12-12-12.stl")   # STL, не проєкт
    _touch(tmp_path / "report.cam")                # .cam, але ім'я не проєктне
    _touch(tmp_path / "2026-09-02_13-13-13.cam")   # єдиний справжній
    got = scan_projects(str(tmp_path))
    assert [p.sum3d_id for p in got] == ["13-13-13"]


def test_scan_ignores_bare_cam_extension_dir_named_project(tmp_path):
    # Файл без розширення з проєктним іменем (не .cam) — не проєкт.
    _touch(tmp_path / "2026-09-02_10-00-00")
    assert scan_projects(str(tmp_path)) == []


# ── скан: запасний формат — теки-проєкти ──────────────────────────────────

def test_scan_reads_project_folders(tmp_path):
    for i, n in enumerate(["2026-09-02_17-40-03", "2026-09-02_18-04-12"]):
        d = tmp_path / n
        d.mkdir()
        t = time.time() + i
        os.utime(d, (t, t))
    (tmp_path / "temp_cache").mkdir()  # службова тека — відсіється
    got = scan_projects(str(tmp_path))
    assert [p.sum3d_id for p in got] == ["18-04-12", "17-40-03"]


def test_scan_mixed_files_and_folders(tmp_path):
    _touch(tmp_path / "2026-09-02_09-00-00.cam", when=1000.0)
    d = tmp_path / "2026-09-02_10-00-00"
    d.mkdir()
    os.utime(d, (2000.0, 2000.0))
    got = scan_projects(str(tmp_path))
    assert [p.sum3d_id for p in got] == ["10-00-00", "09-00-00"]


# ── скан: ліміт ────────────────────────────────────────────────────────────

def test_scan_respects_limit(tmp_path):
    for i in range(20):
        _touch(tmp_path / f"2026-09-02_10-00-{i:02d}.cam")
    assert len(scan_projects(str(tmp_path), limit=5)) == 5


def test_scan_real_screenshot_names(tmp_path):
    # Точні імена з D:\CAM-work (скрін оператора) — сторож проти регресій.
    real = [
        "2025-09-29_10-30-36 — копия (2)",
        "2025-09-29_12-29-46",
        "2025-09-29_12-52-34",
        "2025-09-29_13-13-45",
        "2025-12-11_19-55-23",
        "2025-12-22_11-09-07",
        "2025-12-23_14-00-08 — копия",
        "2025-12-23_14-13-12 — копия",
        "2025-12-23_15-05-15 — копия",
    ]
    for i, n in enumerate(real):
        _touch(tmp_path / f"{n}.cam", when=1000.0 + i)
    got = scan_projects(str(tmp_path))
    # Усі 9 імен унікальні за (date, time) → 9 записів, жоден не загублено.
    assert len(got) == 9
    assert got[0].sum3d_id == "15-05-15"  # останній за mtime


# ── Привид у пришпиленому рядку (слайс 2) ─────────────────────────────────

def _row_html(**ctx):
    from types import SimpleNamespace
    import app.web as web
    order = SimpleNamespace(
        id=1, source="lab", sheet_tab="03.09.26", status="прийнято",
        material_color="цирконій", kind="анатомія", quantity="4", job_code="x",
        job_code_folder_uri=None, job_code_folder_preview_token=None,
        sum3d_id=ctx.pop("sum3d_id", ""), export_folder_uri=None,
        export_folder_preview_token=None, technician_name="Іван", cam_comment=None,
        client_name="Кривовид", work_order_no="24122", active_rework=None,
        sheet_changed_at=None, sheet_changed_fields=None, calculated_raw="",
    )
    return web.templates.get_template("_order_row.html").render(
        order=order, statuses=["нове"], sync_error=None, **ctx
    )


def test_ghost_appears_only_in_a_pinned_row_with_empty_field():
    """Підказка захопленого ID — рівно там, де є НАМІР (рядок у «мої зараз»)
    і куди її ще можна вписати. Без піна ми не знаємо, до якого рядка належить
    проєкт (§2 — ніяких авто-зіставлень)."""
    pinned = _row_html(focused_ids={1}, sum3d_latest="02-52-10")
    assert 'data-ghost="02-52-10"' in pinned and "sum3d-ghost" in pinned

    # не пришпилений — підказки немає
    assert "sum3d-ghost" not in _row_html(focused_ids=set(), sum3d_latest="02-52-10")
    # поле вже заповнене — не перебиваємо
    assert "sum3d-ghost" not in _row_html(
        focused_ids={1}, sum3d_latest="02-52-10", sum3d_id="11-11-11"
    )
    # нічого не захоплено — підказки немає
    assert "sum3d-ghost" not in _row_html(focused_ids={1}, sum3d_latest=None)


def test_row_renders_without_the_sum3d_context_at_all():
    """Рядок малюється і з місць, які про лоток не знають (пошук, картка) —
    там він мусить просто не показувати підказку, а не падати."""
    assert "sum3d-ghost" not in _row_html()


def test_scan_uses_a_short_cache_so_two_callers_do_not_hit_disk_twice(tmp_path):
    """Теку питають лоток і полл черги — обидва раз на 15с. Кеш (5с) не дає
    подвоїти обхід диска; свіжість не страждає, бо TTL менший за інтервал."""
    (tmp_path / "2026-09-03_02-52-10.cam").write_bytes(b"cam")
    first = scan_projects(str(tmp_path))
    assert [p.sum3d_id for p in first] == ["02-52-10"]

    # Новий файл З'ЯВИВСЯ, але в межах TTL кеш віддає попередній результат…
    (tmp_path / "2026-09-03_03-00-00.cam").write_bytes(b"cam")
    assert [p.sum3d_id for p in scan_projects(str(tmp_path))] == ["02-52-10"]
    # …а обхід у обхід кешу бачить обидва.
    fresh = scan_projects(str(tmp_path), use_cache=False)
    assert {p.sum3d_id for p in fresh} == {"02-52-10", "03-00-00"}
