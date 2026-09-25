"""Ярлики родини матеріалу над списком листів (власник 25.09.26).

«Бувають дні, коли фрезеруємо лише цирконій (його спікати), а пластмасу
лишаємо на потім — ярликом перебрати, щоб не мелькала». Клік лишає одну
родину; фільтрує сервер, тож полл і повернення після дій тримають вибір.
"""

from __future__ import annotations

import re

import pytest

from app.models import EmailMessage
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


@pytest.fixture(autouse=True)
def _fresh_material_cache():
    """Кеш бібліотеки матеріалів процесний — не лишати засіяний сусідам."""
    from app.services.material_suggest import invalidate_cache

    invalidate_cache()
    yield
    invalidate_cache()


def _seed(factory):
    """Чіп рядка береться з бібліотеки матеріалів, а її будує історія робіт —
    без неї чіпів (і ярликів) немає, як і в порожній базі."""
    from datetime import timedelta

    from app.business_day import utc_now
    from app.material_catalog import backfill_orders
    from app.models import Order
    from app.services.material_suggest import invalidate_cache

    ids = {}
    with factory() as db:
        for text, n in {"mono a3": 9, "mono a2": 6, "pmma a2": 6}.items():
            for _ in range(n):
                db.add(Order(source="lab", status="нове", material_color=text,
                             created_at=utc_now() - timedelta(days=1)))
        db.flush()
        backfill_orders(db, only_unresolved=False)
        db.commit()
    invalidate_cache()
    with factory() as db:
        for uid, guess in (("1", "mono a3"), ("2", "mono a2"), ("3", "pmma a2"), ("4", None)):
            email = EmailMessage(
                uid=uid, uid_validity="1", from_address=f"c{uid}@ukr.net", from_name=f"Клієнт {uid}",
                subject="робота", status="нове", attachments_status="ready",
                material_color_guess=guess, message_id=f"<{uid}@x>",
            )
            db.add(email)
            db.flush()
            ids[uid] = email.id
        db.commit()
    return ids


def _rows(html):
    return set(re.findall(r'mailrow-(\d+)', html))


def test_chips_count_and_filter(app_db):  # noqa: F811
    app, factory = app_db
    ids = _seed(factory)
    client = MiniClient(app)
    client.login(*OPERATOR)

    _, _, page = client.get("/mail")
    assert 'class="famchips"' in page
    assert re.search(r'<b>Zr</b><span class="famchip-n mono">2</span>', page)
    assert re.search(r'<b>PMMA</b><span class="famchip-n mono">1</span>', page)
    assert "без матеріалу" in page
    assert {str(i) for i in ids.values()} <= _rows(page)

    _, _, pmma = client.get("/mail?fam=pmma")
    assert str(ids["3"]) in _rows(pmma)
    assert not ({str(ids[k]) for k in ("1", "2", "4")} & _rows(pmma))
    # Вибраний ярлик підсвічений; лічильники — з усього списку, не з відфільтрованого.
    assert re.search(r'famchip mat-pmma is-on', pmma)
    assert re.search(r'<b>Zr</b><span class="famchip-n mono">2</span>', pmma)
    # Полл 15 с тримає вибір.
    assert "&fam=pmma&partial=list" in pmma


def test_unknown_family_shows_everything(app_db):  # noqa: F811
    app, factory = app_db
    ids = _seed(factory)
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, page = client.get("/mail?fam=bogus")
    assert {str(i) for i in ids.values()} <= _rows(page)


def test_return_url_keeps_family():
    from types import SimpleNamespace

    from app.routers.mail import _mail_back_url

    req = SimpleNamespace(headers={"HX-Current-URL": "http://x/mail?fam=pmma"})
    assert _mail_back_url(req) == "/mail?fam=pmma"
    bad = SimpleNamespace(headers={"HX-Current-URL": "http://x/mail?fam=../evil"})
    assert _mail_back_url(bad) == "/mail"
