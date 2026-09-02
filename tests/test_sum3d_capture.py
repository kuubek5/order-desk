"""Захоплення проєктів Sum3D із теки Cam-work (хід 1)."""
from __future__ import annotations

import os
import time

from app.services.sum3d_capture import parse_project_name, scan_projects


def test_parse_valid_name():
    assert parse_project_name("2026-09-02_17-55-28") == ("2026-09-02", "17-55-28")


def test_parse_space_separator():
    assert parse_project_name("2026-09-02 18-04-12") == ("2026-09-02", "18-04-12")


def test_parse_with_suffix():
    # Дублікат/повтор із суфіксом — дату+час усе одно беремо з початку.
    assert parse_project_name("2026-09-02_17-55-28 (2)") == ("2026-09-02", "17-55-28")


def test_parse_rejects_non_project():
    assert parse_project_name("нова папка") is None
    assert parse_project_name("24122") is None
    assert parse_project_name("") is None


def test_scan_empty_or_missing_path():
    assert scan_projects(None) == []
    assert scan_projects("") == []
    assert scan_projects("P:/definitely/not/here/xyz") == []


def test_scan_lists_projects_newest_first(tmp_path):
    # Три теки-проєкти + одна службова, з різним часом зміни.
    names = ["2026-09-02_17-40-03", "2026-09-02_17-55-28", "2026-09-02_18-04-12"]
    for i, n in enumerate(names):
        d = tmp_path / n
        d.mkdir()
        # штучно рознести mtime, щоб порядок був детермінований
        t = time.time() + i
        os.utime(d, (t, t))
    (tmp_path / "temp_cache").mkdir()  # не проєкт — має відсіятись
    (tmp_path / "2026-09-02_11-11-11.txt").write_text("файл, не тека")

    got = scan_projects(str(tmp_path))
    assert [p.sum3d_id for p in got] == ["18-04-12", "17-55-28", "17-40-03"]
    assert all(p.date == "2026-09-02" for p in got)


def test_scan_respects_limit(tmp_path):
    for i in range(20):
        (tmp_path / f"2026-09-02_10-00-{i:02d}").mkdir()
    assert len(scan_projects(str(tmp_path), limit=5)) == 5
