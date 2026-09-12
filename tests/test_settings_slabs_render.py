"""Плита стану доходить до екрана — і міняється від реальних подій.

`tests/test_settings_status.py` перевіряє ЛОГІКУ тонів на голих словниках. Тут
інше питання: чи той тон справді потрапляє в HTML розділу. Між ними живе цілий
шлях — контекст роута, `build_slabs`, макрос `_settings_slab.html`, порядок
CSS, — і саме там ламається тихо: 06.09.26 зниклий `_settings_check_result.html`
девʼять днів віддавав 500 на кожну кнопку «Перевірити», і жоден зелений тест
цього не бачив.

Тому тут — справжній запит до застосунку (`tests/asgi_client.py`), справжній
рендер `/settings` і пошук класу пігулки поруч із `data-sec` розділу.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import Base
from app.models import Furnace, User
from tests.asgi_client import MiniClient

ADMIN = ("slabadmin", "Sl@b-Admin-1")
OPERATOR = ("slaboperator", "Sl@b-Oper-1")


def _tone(html: str, sec: str) -> str | None:
    """Клас пігулки стану всередині СЕКЦІЇ з таким `data-sec`.

    Шукаємо лише в контейнері розділів: той самий `data-sec` стоїть і на
    пункті рейки, і перший збіг завжди був би посиланням меню без пігулки.
    """
    html = html.split('class="wizard-flow', 1)[-1]
    start = html.find(f'data-sec="{sec}"')
    if start == -1:
        return None
    # Наступна секція — межа пошуку: інакше знайшли б чужу пігулку нижче.
    nxt = html.find("data-sec=", start + 10)
    chunk = html[start : nxt if nxt != -1 else len(html)]
    match = re.search(r"stand-state stand-state-(\w+)", chunk)
    return match.group(1) if match else None


@pytest.fixture
def app_db(monkeypatch):
    """Застосунок на порожній базі в памʼяті + ліцензія, інакше гейт /license."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import app.web as web
    from app import license as license_module
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
        for username, password in (ADMIN, OPERATOR):
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    full_name=username,
                    role="адмін" if username == ADMIN[0] else "оператор",
                )
            )
        db.commit()

    return web.app, session_factory


def _open_settings(app, who=ADMIN) -> str:
    client = MiniClient(app)
    status, _, _ = client.login(*who)
    assert status in (200, 302, 303), status
    status, _, html = client.get("/settings")
    assert status == 200, status
    return html


# ── Таблиця й пошта: живий heartbeat ↔ мовчання ↔ помилка ───────────────


def _beat(monkeypatch, key: str, *, status="ok", last_attempt_at=None, error_message=None):
    """Підмінити пульс фонового воркера — те саме, що пише сам воркер."""
    from app import sync_heartbeat

    monkeypatch.setitem(
        sync_heartbeat.heartbeats,
        key,
        sync_heartbeat.SyncHeartbeat(
            last_attempt_at=last_attempt_at,
            status=status,
            error_message=error_message,
        ),
    )


def test_unconfigured_sheet_is_grey_not_green(app_db):
    """Порожня база: таблиця не підключена — сірий, НІКОЛИ не зелений."""
    app, _ = app_db
    html = _open_settings(app)
    assert _tone(html, "sheets") == "none"


def test_paths_never_turn_green_just_because_they_are_filled(app_db):
    """«Поле заповнене» — не доказ, що тека є. Зелений тут не зʼявляється."""
    app, session_factory = app_db
    from app.settings_store import set_setting

    with session_factory() as db:
        set_setting(db, "export_folder_path", r"\\nowhere\share\export")
        set_setting(db, "technician_files_path", r"\\nowhere\share\tech")
        db.commit()
    html = _open_settings(app)
    assert _tone(html, "paths") != "ok"


def test_sync_error_paints_the_sheet_slab_alarm(app_db, monkeypatch):
    from datetime import datetime

    app, session_factory = app_db
    from app.settings_store import set_setting

    with session_factory() as db:
        set_setting(db, "google_sheet_id", "abc123")
        set_setting(db, "google_service_account_json", '{"type":"service_account"}')
        db.commit()
    _beat(
        monkeypatch,
        "sheet",
        status="error",
        error_message="проксі обірвав зʼєднання",
        last_attempt_at=datetime.now(),
    )
    html = _open_settings(app)
    assert _tone(html, "sheets") == "alarm"


def test_live_sheet_sync_paints_ok(app_db, monkeypatch):
    from datetime import datetime

    app, session_factory = app_db
    from app.settings_store import set_setting

    with session_factory() as db:
        set_setting(db, "google_sheet_id", "abc123")
        set_setting(db, "google_service_account_json", '{"type":"service_account"}')
        db.commit()
    now = datetime.now()
    _beat(monkeypatch, "sheet", status="ok", last_attempt_at=now)
    html = _open_settings(app)
    assert _tone(html, "sheets") == "ok"


def test_silent_worker_is_warn_not_ok(app_db, monkeypatch):
    """Воркер мовчить довше за свій інтервал — жовтий, а не «все добре»."""
    from datetime import datetime, timedelta

    from app.sync_heartbeat import STALE_HEARTBEAT_MULTIPLIER

    app, session_factory = app_db
    from app.settings_store import set_setting

    with session_factory() as db:
        set_setting(db, "google_sheet_id", "abc123")
        set_setting(db, "google_service_account_json", '{"type":"service_account"}')
        db.commit()
    stale = datetime.now() - timedelta(seconds=60 * STALE_HEARTBEAT_MULTIPLIER * 3)
    _beat(monkeypatch, "sheet", status="ok", last_attempt_at=stale)
    html = _open_settings(app)
    assert _tone(html, "sheets") == "warn"


# ── Обладнання ──────────────────────────────────────────────────────────


def test_no_machines_is_grey(app_db):
    app, _ = app_db
    html = _open_settings(app)
    assert _tone(html, "machines") == "none"
    assert _tone(html, "furnaces") == "none"


def _card(**kwargs):
    """Картка верстата так, як її бачить плита: памʼять процесу, не БД."""
    from types import SimpleNamespace

    base = dict(
        has_problem=False,
        has_frame=True,
        is_running=False,
        target=SimpleNamespace(is_agent=False),
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_machine_without_a_frame_is_warn_not_ok(app_db, monkeypatch):
    """Верстат доданий, кадру немає — «жодного кадру», а не «працює».

    Снапшот беремо з памʼяті процесу навмисно: у плиті стоїть те, що справді
    прийшло з ПК верстата, а не те, що записано в БД (CLAUDE.md §14 — хибне
    число гірше за жодне).
    """
    app, _ = app_db
    from app.services import machines as machines_service

    monkeypatch.setattr(machines_service, "snapshot", lambda db: [_card(has_frame=False)])
    assert _tone(_open_settings(app), "machines") == "warn"


def test_machine_out_of_touch_is_alarm(app_db, monkeypatch):
    app, _ = app_db
    from app.services import machines as machines_service

    monkeypatch.setattr(
        machines_service, "snapshot", lambda db: [_card(has_problem=True), _card()]
    )
    assert _tone(_open_settings(app), "machines") == "alarm"


def test_live_machine_is_ok(app_db, monkeypatch):
    app, _ = app_db
    from app.services import machines as machines_service

    monkeypatch.setattr(machines_service, "snapshot", lambda db: [_card(is_running=True)])
    assert _tone(_open_settings(app), "machines") == "ok"


def test_furnace_added_but_silent_is_not_ok(app_db):
    """Піч у базі є, даних з неї немає — зеленого бути не може."""
    from datetime import datetime

    app, session_factory = app_db
    with session_factory() as db:
        # created_at заповнюємо руками: у моделі стоїть server_default лише в
        # міграції, а create_all піднімає таблицю без нього.
        db.add(
            Furnace(
                name="Піч 1",
                host="192.0.2.2",
                port=5900,
                enabled=True,
                created_at=datetime.now(),
            )
        )
        db.commit()
    assert _tone(_open_settings(app), "furnaces") != "ok"


# ── Люди ────────────────────────────────────────────────────────────────


def test_operators_slab_is_ok_while_an_active_admin_exists(app_db):
    app, _ = app_db
    html = _open_settings(app)
    assert _tone(html, "operators") == "ok"


# ── Роль не міняє КОЛІР, лише набір видимих плит ────────────────────────


def test_operator_sees_the_same_tone_on_shared_sections(app_db, monkeypatch):
    from datetime import datetime

    app, session_factory = app_db
    from app.settings_store import set_setting

    with session_factory() as db:
        set_setting(db, "google_sheet_id", "abc123")
        set_setting(db, "google_service_account_json", '{"type":"service_account"}')
        db.commit()
    now = datetime.now()
    _beat(monkeypatch, "sheet", status="ok", last_attempt_at=now)

    as_admin = _open_settings(app, ADMIN)
    as_operator = _open_settings(app, OPERATOR)
    assert _tone(as_operator, "sheets") == _tone(as_admin, "sheets") == "ok"
    # Адмінські розділи оператор не отримує взагалі — плити немає, а не сіра.
    assert _tone(as_operator, "operators") is None


# ── Чесність метрик ─────────────────────────────────────────────────────


def test_stale_journal_entry_is_not_shown_as_fresh_activity(app_db):
    """Стара помилка в журналі не має виглядати як щойно те, що сталося.

    SyncLog не пише рядка на тихий фоновий тік, тому «останній запис» може
    бути тижневої давності, поки синк усе це врем'я справно працює. Метрика в
    плиті показує лише запис молодший за добу — інакше вона відповідає на
    питання, якого ніхто не ставив (PLAN §3.5).
    """
    from datetime import datetime, timedelta

    from app.models import SyncLog
    from app.services.settings_status import _last_sync_log

    app, session_factory = app_db
    with session_factory() as db:
        db.add(
            SyncLog(
                occurred_at=datetime.now() - timedelta(days=7),
                direction="read",
                status="error",
                message="стара помилка",
            )
        )
        db.commit()
        assert _last_sync_log(db) is None

        db.add(
            SyncLog(
                occurred_at=datetime.now() - timedelta(hours=1),
                direction="read",
                status="ok",
                message="свіжий тік",
            )
        )
        db.commit()
        fresh = _last_sync_log(db)
        assert fresh is not None and fresh.status == "ok"


# ── Доступ по мережі (/mcp): адмін-only секція ─────────────────────────


def test_admin_gets_the_mcp_section_operator_does_not(app_db):
    """Форма і кнопки /settings/mcp/* — лише адміну: це безпекове
    налаштування (відкриває порт у мережу), і `roles=ADMIN_ONLY` у
    settings_nav.py ховає його взагалі з екрана оператора, а не лише з меню."""
    app, _ = app_db
    as_admin = _open_settings(app, ADMIN)
    as_operator = _open_settings(app, OPERATOR)

    assert 'data-sec="mcp"' in as_admin
    assert '/settings/mcp/toggle' in as_admin
    assert '/settings/mcp/token' in as_admin

    assert 'data-sec="mcp"' not in as_operator
    assert '/settings/mcp/toggle' not in as_operator
    assert '/settings/mcp/token' not in as_operator
    assert _tone(as_operator, "mcp") is None


def test_mcp_disabled_by_default_is_grey_not_green(app_db):
    """Порожня база: перемикач вимкнений, токена немає — сірий, не зелений."""
    app, _ = app_db
    html = _open_settings(app)
    assert _tone(html, "mcp") == "none"


def test_mcp_toggle_requires_admin(app_db):
    """Оператор не може відкрити доступ за саморобним POST, навіть якщо
    форма йому взагалі не віддається."""
    from tests.asgi_client import MiniClient

    app, _ = app_db
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303), status
    status, _, _ = client.post("/settings/mcp/toggle", {})
    assert status == 403, status


def test_mcp_gives_one_ready_line_and_reissue_replaces_it(app_db):
    """Екран мусить віддавати ГОТОВИЙ рядок «адреса + токен», і при перевипуску
    старий рядок мусить зникати.

    Чому не «показати токен один раз»: перший варіант саме так і робив, і це
    означало «запиши зараз, бо більше не побачиш» плюс складання адреси з
    токеном руками. Власник сказав «намудрено» — тож рядок видимий щоразу
    (екран і так лише для адміна й лише з цього компʼютера), а межу тримає
    кнопка перевипуску: цей тест доводить саме її, бо мертвий старий рядок —
    єдине, що тут справді захищає."""
    from tests.asgi_client import MiniClient

    from app.services import mcp_gateway

    app, session_factory = app_db
    client = MiniClient(app)
    status, _, _ = client.login(*ADMIN)
    assert status in (200, 302, 303), status

    status, _, _ = client.post("/settings/mcp/token", {})
    assert status == 303, status
    with session_factory() as db:
        first = mcp_gateway.gateway_token(db)
        links = mcp_gateway.connect_links(db)

    status, _, page = client.get("/settings")
    assert status == 200
    assert first and first in page, "готового рядка з токеном на сторінці немає"
    for link in links:
        assert link in page, f"рядок {link} не показано"
        assert link.startswith("http://") and f":{mcp_gateway.GATEWAY_PORT}/mcp?t=" in link

    status, _, _ = client.post("/settings/mcp/token", {})
    assert status == 303, status
    status, _, after = client.get("/settings")
    with session_factory() as db:
        second = mcp_gateway.gateway_token(db)
    assert second != first, "перевипуск не змінив токен"
    assert first not in after, "старий токен усе ще на сторінці"
    assert second in after
