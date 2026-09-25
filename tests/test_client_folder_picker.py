"""Вибір теки клієнта мишею в картці клієнта (власник 25.09.26).

Вікно тек у самій CRM: список тек 1-го рівня export із пошуком, розгортання
теки показує її свіжі підтеки, «Обрати» зберігає назву тією самою формою.
Перегляд теки не випускає за межі export — лише точна назва теки 1-го рівня.
"""

from __future__ import annotations

import os
import time

from app.export_scanner import clear_export_cache, peek_export_client
from tests.test_client_routes import _render_pane


def _tree(tmp_path):
    root = tmp_path / "export"
    (root / "Тест" / "24.09.26" / "mono a2").mkdir(parents=True)
    (root / "Тест" / "25.09.26").mkdir(parents=True)
    (root / "Тест" / "лист.txt").write_text("x", encoding="utf-8")
    (root / "BatchTest").mkdir(parents=True)
    old = time.time() - 86400
    os.utime(root / "Тест" / "24.09.26", (old, old))
    (tmp_path / "secret").mkdir()
    clear_export_cache()
    return root


def test_peek_lists_newest_subfolders_first(tmp_path):
    root = _tree(tmp_path)
    peek = peek_export_client(root, "Тест")
    assert [d["name"] for d in peek["dirs"]] == ["25.09.26", "24.09.26"]
    assert peek["files"] == 1 and peek["more"] == 0


def test_peek_refuses_anything_but_a_top_level_export_folder(tmp_path):
    root = _tree(tmp_path)
    assert peek_export_client(root, "../secret") is None
    assert peek_export_client(root, "Тест/24.09.26") is None
    assert peek_export_client(root, "Немає такої") is None
    assert peek_export_client(root, "") is None


def test_pane_renders_the_picker_with_every_folder_and_marks():
    html = _render_pane(
        folder_names=["BatchTest", "Басараб Лаб", "Тест"],
        bound_folder="Тест",
        folder_suggestions=["Басараб Лаб"],
    )
    assert "data-fp" in html and "data-fp-search" in html
    assert html.count("data-fp-choose=") == 3
    assert 'hx-get="/clients/export-peek?name=' in html
    assert "прив'язано</span>" in html and "схоже</span>" in html


def test_pane_without_export_folders_has_no_picker():
    html = _render_pane(folder_names=[], folder_suggestions=[])
    assert "data-fp-search" not in html
