"""Лист із кількома матеріалами й чесна галочка (власник 05.11.26).

Клієнти надсилають в одному листі кілька матеріалів, а чіп рядка показує один.
Друга половина — рядок із написом «не скачано» й зеленою галочкою «можна приймати»
одночасно. Обидва сигнали видно лише в розмітці, тому тут справжній рендер.
"""

from __future__ import annotations

import pytest

from app.models import EmailMessage
from app.routers.mail import _material_mix
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


@pytest.fixture(autouse=True)
def _fresh_material_cache():
    from app.services.material_suggest import invalidate_cache

    invalidate_cache()
    yield
    invalidate_cache()


def _seed_library(factory) -> None:
    """Лінії канону береться з історії робіт: без неї `mono` й `emo` немає."""
    from datetime import timedelta

    from app.business_day import utc_now
    from app.material_catalog import backfill_orders
    from app.models import Order
    from app.services.material_suggest import invalidate_cache

    with factory() as db:
        for text, n in {"mono a3": 9, "emo a2": 6, "pmma a2": 6, "tit": 6, "wax": 6}.items():
            for _ in range(n):
                db.add(Order(source="lab", status="нове", material_color=text,
                             created_at=utc_now() - timedelta(days=1)))
        db.flush()
        backfill_orders(db, only_unresolved=False)
        db.commit()
    invalidate_cache()


def _letter(factory, uid: str, **fields) -> int:
    values = {
        "uid": uid, "uid_validity": "1", "from_address": f"c{uid}@ukr.net",
        "from_name": f"Клієнт {uid}", "subject": "робота", "status": "нове",
        "attachments_status": "ready", "message_id": f"<{uid}@x>",
        "material_color_guess": "mono a3", **fields,
    }
    with factory() as db:
        email = EmailMessage(**values)
        db.add(email)
        db.commit()
        return email.id


def _row(page: str, email_id: int) -> str:
    start = page.index(f'id="mailrow-{email_id}"')
    end = page.find('id="mailrow-', start + 1)
    return page[start:end if end != -1 else None]


@pytest.mark.parametrize("text, expected", [
    ("Коронки цирконій, тимчасові ПММА", {"Zr", "PMMA"}),
    ("Коронки ЦИРКОН - pmma a2", {"Zr", "PMMA"}),
    ("Абатмент титан і коронка цирконій", {"Zr", "Ti"}),
    ("Emotions a2 та Monolith a3", {"mono", "emo"}),
    ("2 моно а3, 1 емо а2", {"mono", "emo"}),
    # Один матеріал, написаний по-різному, — не мішанина.
    ("Monolith A3", set()),
    ("Циркон моноліт А3", set()),
    ("цирконій багатошаровий emotions a2", set()),
    ("ПММА капа", set()),
    # Титанова основа під коронку — звичайна гвинтова робота.
    ("Цирконій на титанових основах", set()),
    ("Коронка на ti-base циркон", set()),
    ("Гвинтова фіксація на титанбазі, Monolith A3", set()),
    # Слова, що лише починаються як матеріал.
    ("Пацієнт Титаренко, моно а2", set()),
    ("Пацієнтка Воскобойник, цирконій а2", set()),
    ("Тетяна Цирконенко PMMA a2", set()),
    ("мономер pmma а2", set()),
    ("тітка просила швидше, моно а3", set()),
])
def test_letter_materials_on_real_wording(app_db, text, expected):  # noqa: F811
    from app.services.material_suggest import letter_materials

    _app, factory = app_db
    _seed_library(factory)
    with factory() as db:
        assert set(letter_materials(db, None, text)) == expected


def test_mix_shows_only_what_the_chip_does_not():
    assert _material_mix([], {"symbol": "Zr"}) is None
    pmma_chip = {"symbol": "PMMA", "color": "a2"}
    assert _material_mix(["Zr", "PMMA"], pmma_chip)["extra"] == ["Zr"]
    mono_chip = {"symbol": "Zr", "color": "mono a3"}
    assert _material_mix(["mono", "emo"], mono_chip)["extra"] == ["emo"]
    assert _material_mix(["Zr", "PMMA"], None) == {"all": ["Zr", "PMMA"], "extra": ["Zr", "PMMA"]}


def test_row_and_card_mark_the_mix_but_stay_ready(app_db):  # noqa: F811
    app, factory = app_db
    _seed_library(factory)
    mixed = _letter(factory, "1", material_color_guess="pmma a2", subject="Коронки ЦИРКОН - pmma a2")
    plain = _letter(factory, "2", subject="Monolith a3")
    client = MiniClient(app)
    client.login(*OPERATOR)

    _, _, page = client.get("/mail")
    row = _row(page, mixed)
    assert 'class="matmix"' in row and "+Zr" in row
    assert "PMMA + Zr" in row  # першим — матеріал зі здогаду, той самий, що в чіпі
    # Попередження, а не перешкода: лист лишається готовим для Конвеєра.
    assert "data-ready" in row
    assert "matmix" not in _row(page, plain)

    _, _, card = client.get(f"/mail/{mixed}?panel=1")
    assert "у листі: PMMA + Zr" in card
    _, _, card = client.get(f"/mail/{plain}?panel=1")
    assert "у листі:" not in card


def test_undownloaded_letter_is_not_green(app_db):  # noqa: F811
    app, factory = app_db
    _seed_library(factory)
    skipped = _letter(factory, "1", attachments_status="skipped")
    fetched = _letter(factory, "2")
    client = MiniClient(app)
    client.login(*OPERATOR)

    _, _, page = client.get("/mail")
    row = _row(page, skipped)
    assert "не скачано" in row
    assert "Уся інформація є" not in row
    assert "Файли листа ще не скачано" in row
    assert "data-ready" not in row
    assert "statedot warn" in row

    ready = _row(page, fetched)
    assert "Уся інформація є" in ready
    assert "data-ready" in ready
