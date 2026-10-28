"""Чіп «на погодженні» — сховати одним кліком (HOLD_FILTER_BRIEF.md, 28.10.26).

Що стережемо:
1. ознака — лише `is_approval_pending_comment`, і пошта не ховається ніколи;
2. сховане не міняє порядок решти (CLAUDE.md §2, правило 1);
3. лічильники готовності — ПІСЛЯ приховування, N — до фільтра готовності;
4. кнопка чіпа не скидає щільність/відступ/крок шестерні (CLAUDE.md §14);
5. N = 0 → чіпа немає;
6. черга і док — незалежні перемикачі.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import business_today
from app.db import Base
from app.models import Order, User
from app.routers.deps import templates
from app.services import queue as queue_mod
from app.services.mail_mirror import mail_dock, mail_dock_for
from app.services.queue import drop_hold_orders, is_hold_order
from app.services.queue_view import build_queue_view
from tests.test_mail_dock import _request, _save, _seed_day, _user

pytestmark = pytest.mark.usefixtures("weekday_clock")

HOLD = "на погодженні з лікарем"
DONE = "погодження отримано"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


# ── 1. Ознака ───────────────────────────────────────────────────────────────


def test_hold_sign_comes_only_from_the_comment_predicate(monkeypatch):
    lab = Order(source="lab", cam_comment="будь-що")
    monkeypatch.setattr(queue_mod, "is_approval_pending_comment", lambda text: True)
    assert is_hold_order(lab)
    monkeypatch.setattr(queue_mod, "is_approval_pending_comment", lambda text: False)
    assert not is_hold_order(lab)


def test_mail_is_never_hidden():
    assert is_hold_order(Order(source="lab", cam_comment=HOLD))
    assert is_hold_order(Order(source="sheet_client", cam_comment=HOLD))
    assert not is_hold_order(Order(source="email", cam_comment=HOLD))
    # «погоджено» — уже не на погодженні (тонкість живе в предикаті).
    assert not is_hold_order(Order(source="lab", cam_comment=DONE))


# ── 2. Порядок ──────────────────────────────────────────────────────────────


def test_hiding_keeps_the_order_of_the_rest():
    rows = [
        Order(id=i, source="lab", cam_comment=HOLD if i in (2, 5) else None)
        for i in range(1, 8)
    ]
    kept, held = drop_hold_orders(rows, True)
    assert [o.id for o in kept] == [1, 3, 4, 6, 7]
    assert held == 2
    shown, held = drop_hold_orders(rows, False)
    assert shown == rows and held == 2


# ── 3. Черга: лічильники і полл ─────────────────────────────────────────────


def _seed_queue(db):
    user = User(username="op", password_hash="x", full_name="op", role="оператор")
    db.add(user)
    tab = business_today().strftime("%d.%m.%y")

    def add(n, **kw):
        db.add(Order(source="lab", sheet_tab=tab, row_number=n, work_order_no=str(30000 + n),
                     quantity="1", status="нове", **kw))

    add(1, job_code="j1")                                # можна брати
    add(2, job_code="j2", cam_comment=HOLD)              # можна брати, на погодженні
    add(3, job_code="j3", sum3d_id="10-00-00")           # в роботі
    add(4, cam_comment=HOLD)                             # не готово, на погодженні
    add(5, job_code="j5", cam_comment=DONE)              # можна брати, вже погоджено
    db.add(Order(source="email", sheet_tab=tab, row_number=6, client_name="Пошта",
                 quantity="1", status="прийнято", job_code="m", cam_comment=HOLD))
    db.commit()
    return user


def test_queue_shows_everything_by_default_and_counts_hold(db):
    user = _seed_queue(db)
    ctx = build_queue_view(db, user, period="today", ready="all").context
    assert ctx["hide_hold"] is False
    assert ctx["hold_count"] == 2, "пошта з тим самим словом не рахується"
    assert len(ctx["orders"]) == 6
    assert ctx["ready_counts"]["can_take"] == 4


def test_queue_hidden_hold_leaves_ready_counts_and_order(db):
    user = _seed_queue(db)
    before = [o.work_order_no for o in build_queue_view(db, user, period="today", ready="all").context["orders"]]
    user.queue_hide_hold = True
    db.commit()
    ctx = build_queue_view(db, user, period="today", ready="all").context
    after = [o.work_order_no for o in ctx["orders"]]
    assert after == [w for w in before if w not in ("30002", "30004")]
    # N — до фільтра готовності і до приховування: чіп каже, скільки сховано.
    assert ctx["hold_count"] == 2
    assert ctx["ready_counts"]["can_take"] == 3, "«Можна брати» без робіт на погодженні"
    assert ctx["ready_counts"]["not_ready"] == 0
    assert ctx["rows_total"] == 4

    # Полл рядків бере прапорець з акаунта — той самий результат без query.
    rows = build_queue_view(db, user, period="today", ready="all", partial="rows").context
    assert [o.work_order_no for o in rows["orders"]] == after
    assert "hold" not in rows["rows_qs"]


def test_queue_hold_count_is_before_the_ready_filter(db):
    user = _seed_queue(db)
    user.queue_hide_hold = True
    db.commit()
    ctx = build_queue_view(db, user, period="today", ready="in_work").context
    assert ctx["hold_count"] == 2
    assert [o.work_order_no for o in ctx["orders"]] == ["30003"]


# ── 4. Збереження: чіп не чіпає шестерню ────────────────────────────────────


def test_queue_chip_saves_and_does_not_reset_the_gear(db):
    user = _user(db)
    request = _request(user.id)
    assert _save(request, db, scope="queue", density="compact", row_pad=12, mat_style="code",
                 step=4).status_code == 204
    db.refresh(user)
    assert (user.queue_density, user.queue_row_pad, user.queue_mat_style, user.queue_ui_step) == (
        "compact", 12, "code", 4)

    assert _save(request, db, scope="queue", hold="hide").status_code == 204
    db.refresh(user)
    assert user.queue_hide_hold is True
    assert (user.queue_density, user.queue_row_pad, user.queue_mat_style, user.queue_ui_step) == (
        "compact", 12, "code", 4), "чіп не має скидати шестерню"

    _save(request, db, scope="queue", hold="show")
    db.refresh(user)
    assert user.queue_hide_hold is False

    # Шестерня (без `hold`) не чіпає чіп.
    _save(request, db, scope="queue", hold="hide")
    _save(request, db, scope="queue", density="", row_pad=0, mat_style="", step=2)
    db.refresh(user)
    assert user.queue_hide_hold is True and user.queue_density == ""

    # Невідоме слово (зокрема порожнє — його сервер і так не побачить) — 422.
    assert _save(request, db, scope="queue", hold="так").status_code == 422


def test_dock_chip_saves_and_does_not_reset_dock_or_gear(db):
    user = _user(db)
    request = _request(user.id)
    _save(request, db, scope="mail", row_pad=14, list_width=700, step=4, dock="lab",
          dock_ready="all", dock_height=320)
    _save(request, db, scope="mail", dock_hold="hide")
    db.refresh(user)
    assert user.mail_dock_hide_hold is True
    assert (user.mail_row_pad, user.mail_list_width, user.mail_ui_step) == (14, 700, 4)
    assert (user.mail_dock_scope, user.mail_dock_ready, user.mail_dock_height) == ("lab", "all", 320)
    assert _save(request, db, scope="mail", dock_hold="xx").status_code == 422


# ── 5. Док пошти ────────────────────────────────────────────────────────────


def _seed_dock_hold(db):
    day = _seed_day(db)
    day.l_take_b.cam_comment = HOLD          # лаба, можна брати
    day.sheet.cam_comment = HOLD             # табличний клієнт
    day.m1.cam_comment = HOLD                # пошта — не ховається
    db.commit()
    return day


@pytest.mark.parametrize("scope", ["lab", "labonly", "all"])
def test_dock_hides_hold_but_never_mail(db, scope):
    day = _seed_dock_hold(db)
    shown = mail_dock(db, scope=scope, ready="all")
    hidden = mail_dock(db, scope=scope, ready="all", hide_hold=True)
    assert hidden.hold_count == shown.hold_count == (2 if scope == "all" else 1)
    removed = {o.id for o in shown.orders} - {o.id for o in hidden.orders}
    expected = {day.l_take_b.id} | ({day.sheet.id} if scope == "all" else set())
    assert removed == expected
    # Порядок решти той самий.
    assert [o.id for o in hidden.orders] == [o.id for o in shown.orders if o.id not in removed]
    if scope != "labonly":
        assert day.m1.id in {o.id for o in hidden.orders}, "пошта не ховається"


def test_dock_ready_counts_after_hiding(db):
    _seed_dock_hold(db)
    shown = mail_dock(db, scope="lab", ready="can_take")
    hidden = mail_dock(db, scope="lab", ready="can_take", hide_hold=True)
    assert shown.ready_counts["can_take"] == 2
    assert hidden.ready_counts["can_take"] == 1
    assert hidden.lab_count == 1


def test_queue_and_dock_toggles_are_independent(db):
    _seed_dock_hold(db)
    user = _user(db, queue_hide_hold=True, mail_dock_scope="lab", mail_dock_ready="all")
    assert mail_dock_for(db, user).hide_hold is False
    user.queue_hide_hold, user.mail_dock_hide_hold = False, True
    db.commit()
    assert mail_dock_for(db, user).hide_hold is True


# ── 6. Розмітка чіпа ────────────────────────────────────────────────────────


def _chip(**ctx):
    return templates.env.get_template("_hold_chip.html").render(**ctx)


def test_no_chip_when_nothing_is_on_hold():
    assert "hold-chip" not in _chip(hold_target="queue", hold_n=0, hold_hidden=True)


def test_chip_text_is_the_same_in_both_states_and_words_are_explicit():
    shown = _chip(hold_target="queue", hold_n=3, hold_hidden=False)
    hidden = _chip(hold_target="dock", hold_n=3, hold_hidden=True)
    for html in (shown, hidden):
        assert "на погодженні <span class=\"n\">3</span>" in html
    assert 'data-hold-next="hide"' in shown and 'aria-pressed="false"' in shown
    assert 'data-hold-next="show"' in hidden and 'aria-pressed="true"' in hidden
    assert 'data-hold-toggle="dock"' in hidden


def test_both_screens_include_the_chip_and_js_sends_own_field_only():
    assert '"_hold_chip.html"' in Path("app/templates/_queue_sidebar.html").read_text(encoding="utf-8")
    assert '"_hold_chip.html"' in Path("app/templates/_mail_dock_controls.html").read_text(encoding="utf-8")
    queue_js = Path("app/static/js/queue.js").read_text(encoding="utf-8")
    assert 'scope: "queue", hold: next' in queue_js
    assert "saveDock({ dock_hold: next }" in Path("app/static/js/mail.js").read_text(encoding="utf-8")

