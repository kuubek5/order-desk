"""Список листів (власник 29.09.26): панель «Обрати всі | фільтр | N листів»,
роздільники днів і край рядка за матеріалом."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.material_catalog import ensure_seeded
from app.models import EmailMessage
from app.routers import mail as mail_router_mod
from tests.test_mail_bulk import _letter
from tests.test_mail_hold import _page
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def test_toolbar_days_and_material_edge(app_db):  # noqa: F811
    app, session_factory = app_db
    # Той самий годинник, яким сторінка рахує «Сьогодні» (київський). Із
    # `datetime.now()` тест падав у CI (UTC) щоночі 00:00–03:00 за Києвом:
    # листи «щойно» лягали на вчорашню дату (реліз 0.21.36, 02.10.26).
    now = mail_router_mod.business_now().replace(tzinfo=None)  # у базі час без зони
    # Роздільники днів — за КАЛЕНДАРНОЮ датою листа. О 00:06 «мінус 9 хвилин»
    # уже вчора, і «сьогоднішні» листи розпадались на два дні (падіння
    # 08.10.26 о 00:06). Відлік — не раніше 00:10 поточного дня.
    now = max(now, now.replace(hour=0, minute=10, second=0, microsecond=0))
    with session_factory() as db:
        ensure_seeded(db)  # бібліотека матеріалів, як у живій базі — інакше бейджів немає
        db.commit()
        today_id = _letter(db, "71", received_at=now - timedelta(minutes=5))
        db.get(EmailMessage, today_id).material_color_guess = "ПММА а2"
        db.commit()
        zr_id = _letter(db, "72", received_at=now - timedelta(minutes=9))
        db.get(EmailMessage, zr_id).material_color_guess = "моно а3"
        db.commit()
        _letter(db, "73", received_at=now - timedelta(days=3))
    html = _page(app, "pending")

    # «Обрати всі» — усі, не лише готові (конвеєр однаково бере лише готові).
    assert "Обрати всі готові" not in html and "data-ready-only" not in html
    bar = html.split('id="mail-batchbar"', 1)[1].split('<div class="listwrap">', 1)[0]
    assert 'class="famchips"' in bar, "фільтр матеріалу — усередині панелі списку"
    assert 'id="mail-total"' in bar and "3 листи" in bar

    # Роздільник на кожен день, з лічильником саме цього дня.
    heads = html.count('class="mail-day"')
    assert heads == 2
    assert "Сьогодні" in html and "· 2 листи" in html and "· 1 лист" in html

    # Край матеріалу — лише для PMMA-листа (цирконію й невідомому — ні).
    row = html.split(f'id="mailrow-{today_id}"', 1)[1].split(">", 1)[0]
    assert 'data-edge="mat-pmma"' in row
    assert html.count("data-edge=") == 1, "цирконій (моно) смуги не має"
