"""`load` у #queue-rows — лише там, де іконок тек ще немає (23.10.26).

Повна сторінка й свап фільтрів малюють рядки БЕЗ іконок (сканування мережевої
шари коштує секунди), і `load` одразу дотягує їх фрагментом полла. Сам фрагмент
полла (`partial=rows`) іконки вже несе, а його свап — outerHTML, тобто НОВИЙ
елемент: з `load` у ньому кожен змінений полл, `refresh-queue` і ранній
перепит Sum3D тягли ще один такий самий GET одразу слідом (у браузері — два
однакові запити з різницею 40 мс).
"""

from __future__ import annotations

import re

import pytest

from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _rows_trigger(html: str) -> str:
    match = re.search(r'<div id="queue-rows"[^>]*?hx-trigger="([^"]*)"', html, re.S)
    assert match, "немає #queue-rows з hx-trigger"
    return match.group(1)


def test_full_page_still_pulls_the_folder_icons_on_load(app_db):  # noqa: F811
    app, _ = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/?ready=all")
    assert status == 200, html[:500]
    trigger = _rows_trigger(html)
    assert trigger.startswith("load, "), trigger
    assert "sheet-recheck" in trigger and "refresh-queue from:body" in trigger


def test_filter_swap_still_pulls_the_folder_icons_on_load(app_db):  # noqa: F811
    app, _ = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/?ready=all", headers={"HX-Request": "true", "HX-Boosted": "true"})
    assert status == 200, html[:500]
    assert _rows_trigger(html).startswith("load, ")


def test_poll_fragment_does_not_fire_itself_again(app_db):  # noqa: F811
    app, _ = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/?ready=all&partial=rows")
    assert status == 200, html[:500]
    trigger = _rows_trigger(html)
    assert "load" not in trigger.split(",")[0], trigger
    # решта тригерів на місці: полл, refresh-queue, ранній перепит
    assert trigger.startswith("every "), trigger
    assert "refresh-queue from:body" in trigger and "sheet-recheck" in trigger
