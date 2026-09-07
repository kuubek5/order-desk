"""Popup-notification preferences: style, placement, which triggers may fire.

The distinction that matters: an UNSET preference falls back to the defaults,
but an explicitly saved EMPTY event list means "the operator turned everything
off" and must NOT silently re-enable the defaults.
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData, Headers
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.routers import settings as settings_router_mod
from app.routers import deps
from app.db import Base
from app.models import Order, User
ROOT = Path(__file__).resolve().parents[1]

from app.settings_store import (
    DEFAULT_NOTIFY_POSITION,
    DEFAULT_NOTIFY_STYLE,
    NOTIFY_EVENTS,
    NOTIFY_STYLES,
    get_notify_events,
    get_notify_position,
    get_notify_style,
    set_notify_prefs,
    set_setting,
)


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session, role="оператор") -> User:
    u = User(username="op", password_hash="unused", full_name="Оператор", role=role)
    db.add(u)
    db.commit()
    return u


def _request(user_id, form=None, headers=None):
    async def _form():
        return FormData(form or [])

    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        form=_form,
        headers=Headers(headers or {}),
    )


# --- defaults ---------------------------------------------------------------


def test_defaults_when_nothing_saved():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        assert get_notify_style(db) == DEFAULT_NOTIFY_STYLE == "glass"
        assert get_notify_position(db) == DEFAULT_NOTIFY_POSITION == "tc"
        assert get_notify_events(db) == {k for k, _, _, on in NOTIFY_EVENTS if on}


def test_unknown_style_or_position_falls_back():
    """A hand-edited/garbage value must not reach the CSS as an attribute."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(db, "notify_style", "neon")
        set_setting(db, "notify_position", "middle")
        db.commit()
        assert get_notify_style(db) == "glass"
        assert get_notify_position(db) == "tc"


# --- saving -----------------------------------------------------------------


def test_saving_keeps_only_known_events():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_notify_prefs(db, style="card", position="br", events={"offline", "не-існує"})
        db.commit()
        assert get_notify_style(db) == "card"
        assert get_notify_position(db) == "br"
        assert get_notify_events(db) == {"offline"}


def test_turning_everything_off_is_respected_not_reset_to_defaults():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_notify_prefs(db, style="glass", position="tc", events=[])
        db.commit()
        assert get_notify_events(db) == set()


# --- route ------------------------------------------------------------------


def test_route_saves_and_answers_toast_for_htmx():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        request = _request(
            user.id,
            form=[("notify_style", "card"), ("notify_position", "bl"),
                  ("notify_events", "offline"), ("notify_events", "new_mail")],
            headers={"HX-Request": "true"},
        )
        response = asyncio.run(settings_router_mod.save_notification_prefs(request=request, db=db))

    assert response.status_code == 204
    assert json.loads(response.headers["HX-Trigger"])["toast"]["kind"] == "success"
    with Session(engine, expire_on_commit=False) as db:
        assert get_notify_style(db) == "card"
        assert get_notify_position(db) == "bl"
        assert get_notify_events(db) == {"offline", "new_mail"}


def test_route_requires_login():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(settings_router_mod.save_notification_prefs(request=_request(None), db=db))
    assert exc.value.status_code == 401


# --- notify_prefs Jinja global ---------------------------------------------


def test_notify_prefs_global_shape():
    """base.html renders these straight into data-attributes."""
    prefs = deps.notify_prefs()
    assert set(prefs) == {"style", "position", "events", "poll_seconds"}
    assert prefs["style"] in NOTIFY_STYLES
    assert prefs["position"] in {"tc", "tr", "br", "bl"}
    assert isinstance(prefs["events"], list)
    # Drives the popup poll interval — follows the sync-speed preset.
    assert isinstance(prefs["poll_seconds"], int) and prefs["poll_seconds"] > 0


# --- state endpoint ---------------------------------------------------------


def test_notify_state_requires_login():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.api_notify_state(request=_request(None), db=db)
    assert exc.value.status_code == 401


def test_notify_state_returns_the_fields_the_client_diffs():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        state = settings_router_mod.api_notify_state(request=_request(user.id), db=db)
    assert set(state) >= {"sheet", "mail", "orders", "mail_pending", "update"}
    assert isinstance(state["orders"], int)
    assert isinstance(state["mail_pending"], int)


def test_technician_change_trigger_is_on_by_default_and_is_a_warning():
    """The scrap-prevention popup must not need switching on: an operator who
    never opens the notification settings still gets told that a work they may
    be milling was corrected. Warning, not info — it outranks "нові роботи"."""
    key, label, kind, default_on = next(
        row for row in NOTIFY_EVENTS if row[0] == "sheet_changed"
    )
    assert default_on is True
    assert kind == "warn"
    assert "технік" in label.lower()


# --- вигляд «Аврора» (08.09.26) ---------------------------------------------


def test_aurora_is_selectable_and_the_old_looks_stay():
    """Аврора додана ПОРУЧ, а не замість.

    Вимога власника: старі вигляди мають лишитись доступними для вмикання —
    якщо новий десь не підійде (слабкий ПК без backdrop-filter, звичка),
    оператор повертається на скляну одним кліком, без релізу.
    """
    assert {"glass", "card", "aurora"} <= NOTIFY_STYLES
    # Дефолт СВІДОМО не чіпаємо: наявні установки не мають змінити вигляд самі.
    assert DEFAULT_NOTIFY_STYLE == "glass"


def test_saving_aurora_round_trips():
    engine = _database()
    with Session(engine) as db:
        set_notify_prefs(db, style="aurora", position="br", events={"new_mail"})
        db.commit()
        assert get_notify_style(db) == "aurora"
        assert get_notify_position(db) == "br"


def test_new_events_are_present_with_sane_levels():
    rows = {row[0]: row for row in NOTIFY_EVENTS}
    assert "ready_to_take" in rows, "«можна брати» — подія, з якої почалась переробка"
    assert "row_deleted" in rows
    # info для готовності: це добра новина, вона не має кричати.
    assert rows["ready_to_take"][2] == "info"
    # warn для зниклого рядка: хтось прибрав роботу, яку могли вже фрезерувати.
    assert rows["row_deleted"][2] == "warn"
    assert rows["ready_to_take"][3] is True and rows["row_deleted"][3] is True


def test_ready_counts_readiness_not_queue_size():
    """Цей лічильник — не воскреслий `new_orders`.

    Старий рахував РОЗМІР черги (status != видано) і роздувався від будь-якого
    повернення «видано»→«нове». Цей рахує стан готовності (§5): job_code є,
    Sum3D порожній. Робота, яку вже взяли в Sum3D, у лічильник не входить, і
    робота без шляху до папки — теж.
    """
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add_all([
            Order(source="lab", job_code="2026-07-21_00016", sum3d_id=None),   # можна брати
            Order(source="lab", job_code="23203", sum3d_id=""),                # можна брати
            Order(source="lab", job_code="23204", sum3d_id="12-01-45"),        # уже в роботі
            Order(source="lab", job_code=None, sum3d_id=None),                 # технік не здав
            Order(source="lab", job_code="23205", sum3d_id=None, status="видано"),
            Order(source="lab", job_code="23206", sum3d_id=None, archived_at=datetime(2026, 9, 1)),
        ])
        db.commit()
        state = settings_router_mod.api_notify_state(request=_request(user.id), db=db)
    assert state["ready"] == 2


def test_deleted_counts_rows_that_vanished_from_the_sheet():
    """`archived_at` штампує лише зникнення рядка — retention чергу тільки
    фільтрує за датою і нічого не позначає. Тому лічильник чесний."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add_all([
            Order(source="lab", archived_at=datetime(2026, 9, 1)),
            Order(source="lab", archived_at=datetime(2026, 9, 2)),
            Order(source="lab"),
        ])
        db.commit()
        state = settings_router_mod.api_notify_state(request=_request(user.id), db=db)
    assert state["deleted"] == 2


def test_every_new_event_can_be_switched_off_one_by_one():
    """Головна вимога до переробки: будь-яку подію можна прибрати з екрана,
    і це не зачіпає решту. Гейт один (`enabled.has` у app.js), тож канал
    (картка / стрічка / кромка) на вимикання не впливає."""
    engine = _database()
    everything = {row[0] for row in NOTIFY_EVENTS}
    with Session(engine) as db:
        for key in ("ready_to_take", "row_deleted", "sheet_error"):
            set_notify_prefs(
                db, style="aurora", position="tc", events=everything - {key}
            )
            db.commit()
            live = get_notify_events(db)
            assert key not in live
            assert live == everything - {key}


def test_turning_everything_off_survives_the_aurora_look():
    engine = _database()
    with Session(engine) as db:
        set_notify_prefs(db, style="aurora", position="tc", events=set())
        db.commit()
        assert get_notify_events(db) == set()
        assert get_notify_style(db) == "aurora"


# --- клієнт: те, що не має розійтись із сервером -----------------------------


def _app_js() -> str:
    return (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_every_declared_event_is_actually_fired_by_the_client():
    """Подія в налаштуваннях, якої ніхто не піднімає, — мертвий прапорець:
    оператор його вмикає і нічого не відбувається. `offline` єдиний, що
    визначається на клієнті, але й він проходить через той самий fire()."""
    js = re.sub(r"\s+", "", _app_js())
    for key, label, _level, _default in NOTIFY_EVENTS:
        assert f'fire("{key}"' in js, f"подію «{label}» ({key}) ніхто не піднімає в app.js"


def test_fault_events_go_to_the_edge_channel_not_the_card_stack():
    """Стан збою не має жити карткою: липкий тост займав би стек назавжди
    і виштовхував решту. Кромка тримає його, поки збій триває."""
    js = _app_js()
    start = js.index("const AURORA_EVENT_CHANNEL")
    block = js[start : js.index("}", start)]
    for key in ("offline", "sheet_error", "mail_error", "sheet_recovered"):
        assert f'{key}: "edge"' in block, f"{key} має йти в кромку"
    # А подія ззовні — навпаки, мусить лишитись карткою.
    assert "ready_to_take" not in block
    assert "sheet_changed" not in block


def test_aurora_styles_do_not_touch_the_old_looks():
    """Кожне правило нового вигляду скоуплене на .toast-style-aurora (або на
    власні класи каналів). Якщо колись з'явиться голе `.toast{...}` після
    цього маркера — скляна й карткова тихо поїдуть разом із ним."""
    css = (ROOT / "app" / "static" / "css" / "update_overlay.css").read_text(encoding="utf-8")
    start = css.index("/* ── стиль: «Аврора»")
    # Останній рядок самого блоку — далі в файлі вже чужі секції.
    end = css.index(".toast-chip .tc-dot{animation:none!important}", start)
    aurora = css[start:end]
    # Коментарі геть: у них теж бувають рядки, що починаються з крапки
    # (посилання на клас у поясненні), і вони не селектори.
    aurora = re.sub(r"/\*.*?\*/", "", aurora, flags=re.S)
    own = ("toast-style-aurora", "toast-line", "toast-edge", "toast-chip", "toast-ring", "@keyframes", "@property", "@media", "@supports", "body:has", ".toast-ic .toast-ring", ".toast:hover")
    for line in aurora.splitlines():
        line = line.strip()
        if not line.startswith("."):
            continue
        selector = line.split("{")[0]
        assert any(mark in selector for mark in own), f"незаскоуплений селектор: {selector}"
