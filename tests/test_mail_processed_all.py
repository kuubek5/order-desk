"""Вкладка папки: «сьогодні» ⇄ «усі в папці» (власник 25.09.26).

Типово вкладка показує лише сьогоднішні переноси (рішення 24.09.26). Але
вчорашній перенесений лист і листи, перенесені ДО появи `mailbox_moved_at`,
не було видно ніде: «Вхідні» й «Архів» перенесених не показують. `period=all`
показує всі листи в папках за весь час.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.test_mail_bulk import _letter
from tests.test_mail_hold import _client, _page
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def test_processed_tab_today_by_default_and_all_on_request(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        _letter(db, "1", folder="Оброблено", subject="Сьогоднішній",
                mailbox_moved_at=datetime.now())
        _letter(db, "2", folder="Оброблено", subject="Вчорашній",
                mailbox_moved_at=datetime.now() - timedelta(days=2))
        _letter(db, "3", folder="Оброблено", subject="Давній-без-часу")

    today = _page(app, "processed")
    assert "Сьогоднішній" in today
    assert "Вчорашній" not in today and "Давній-без-часу" not in today
    assert "period=all" in today and "Показати всі в папці (3)" in today

    _, _, body = _client(app).get("/mail?view=processed&period=all")
    html = body.decode("utf-8") if isinstance(body, bytes) else body
    for subject in ("Сьогоднішній", "Вчорашній", "Давній-без-часу"):
        assert subject in html
    assert "Лише сьогоднішні" in html
