"""Аудит 06.09.26: розділ «Ранкова видача» в налаштуваннях і журнал змін за кліком.

1. `POST /settings/handout-qc` існував без жодної кнопки — QC-чеклист (крок 3.6,
   рішення власника «опційно, з перемикачем») вмикався лише curl-ом. Тепер у
   розділу є плита, форма й пункт у меню; стережемо, що форма справді доходить
   до екрана і що перемикач дає той стан, який показує плита.
2. Журнал змін: у /settings їдуть лише перші 8 релізів, решта — фрагментом
   `GET /settings/changelog`. Стережемо, що сторінка справді полегшала, а
   фрагмент віддає все.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import license as license_module
from app.auth import hash_password
from app.db import Base
from app.models import User
from app.services.settings_status import _slab_handout
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

ADMIN = ("qcadmin", "Qc@dmin-Pass-1")


def test_handout_slab_never_goes_green():
    off = _slab_handout({"handout_qc": False})
    on = _slab_handout({"handout_qc": True})
    assert off.tone == on.tone == "none"
    assert "один клік" in off.label
    assert "увімкнено" in on.label


@pytest.fixture
def client(monkeypatch):
    """Застосунок на порожній базі + ліцензія + адмін, залогінений."""
    from app import web
    from app.routers import deps
    from app.settings_store import set_setting

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)

    def session_factory():
        return Session(engine, expire_on_commit=False)

    monkeypatch.setattr(deps, "SessionLocal", session_factory)
    monkeypatch.setattr(web, "SessionLocal", session_factory, raising=False)

    private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        license_module,
        "_PUBLIC_KEY_BYTES",
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    with session_factory() as db:
        set_setting(
            db,
            "license_key",
            license_module.encode_license_key(
                {
                    "machine_id": license_module.get_machine_id(),
                    "customer": "tests",
                    "issued_at": "2026-01-01T00:00:00",
                    "expires_at": "2099-01-01T00:00:00",
                },
                private,
            ),
        )
        db.add(
            User(
                username=ADMIN[0],
                password_hash=hash_password(ADMIN[1]),
                full_name=ADMIN[0],
                role="адмін",
            )
        )
        db.commit()

    c = MiniClient(web.app)
    status, _, _ = c.login(*ADMIN)
    assert status in (200, 303)
    return c


def _section(html: str, sec: str) -> str:
    html = html.split('class="wizard-flow', 1)[-1]
    start = html.find(f'data-sec="{sec}"')
    assert start != -1, f"секції {sec} немає на екрані"
    nxt = html.find("data-sec=", start + 10)
    return html[start : nxt if nxt != -1 else len(html)]


def test_handout_section_has_the_qc_toggle_and_reflects_it(client):
    status, _, html = client.get("/settings")
    assert status == 200
    sec = _section(html, "handout")
    assert 'action="/settings/handout-qc"' in sec
    assert 'name="handout_qc"' in sec
    assert "один клік" in sec

    status, headers, _ = client.post("/settings/handout-qc", {"handout_qc": "1"})
    assert status == 303
    assert "#handout" in dict(headers).get("location", "")

    _, _, html = client.get("/settings")
    sec = _section(html, "handout")
    assert "QC-чеклист увімкнено" in sec
    assert 'name="handout_qc" value="1" checked' in sec


def test_settings_page_carries_only_a_preview_of_the_changelog(client):
    from app.changelog import load_changelog

    total = len(load_changelog())
    _, _, html = client.get("/settings")
    assert html.count('class="scon-rel"') == min(total, 8)
    if total > 8:
        assert 'class="settings-secondary-button scon-rel-more"' in html

    status, _, fragment = client.get("/settings/changelog")
    assert status == 200
    assert fragment.count('class="scon-rel"') == total
    # Кнопки «Показати всі» у повному фрагменті бути не має (текст «Показати
    # всі» трапляється і в самому журналі змін, тому шукаємо клас кнопки).
    assert "scon-rel-more" not in fragment


def test_mail_merge_toggle_is_its_own_form_and_does_not_touch_qc(client):
    """Зведення листів Конвеєра (05.10.26) — окрема форма: незазначена галочка
    не приходить, тож спільна з QC форма вимикала б сусіда (§14)."""
    _, _, html = client.get("/settings")
    sec = _section(html, "handout")
    assert 'action="/settings/mail-merge-rows"' in sec
    assert 'name="mail_merge_rows" value="1" >' in sec  # за замовчуванням вимкнено

    client.post("/settings/handout-qc", {"handout_qc": "1"})
    status, headers, _ = client.post("/settings/mail-merge-rows", {"mail_merge_rows": "1"})
    assert status == 303 and "#handout" in dict(headers).get("location", "")

    _, _, html = client.get("/settings")
    sec = _section(html, "handout")
    assert 'name="mail_merge_rows" value="1" checked' in sec
    assert 'name="handout_qc" value="1" checked' in sec  # QC не зачепило

    client.post("/settings/mail-merge-rows", {})
    _, _, html = client.get("/settings")
    sec = _section(html, "handout")
    assert 'name="mail_merge_rows" value="1" >' in sec
    assert 'name="handout_qc" value="1" checked' in sec


def test_operator_does_not_see_the_mail_merge_toggle(app_db):  # noqa: F811
    """Перемикач зведення бачить і змінює лише адміністратор (власник 05.10.26):
    у оператора на екрані немає ні форми, ні стану."""
    from tests.test_settings_slabs_render import OPERATOR, _open_settings

    app, _ = app_db
    html = _open_settings(app, who=OPERATOR)
    assert "mail-merge-rows" not in html
    assert "Один рядок на клієнта" not in html
    admin_html = _open_settings(app)
    assert 'action="/settings/mail-merge-rows"' in admin_html
