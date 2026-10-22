"""Шпильки «мої зараз» у доці екрана пошти (власник 22.10.26).

Що ламається тихо:
- у блоці ЛАБИ пришпилені стоять першими — тим самим порядком набору, що в
  черзі (час пришпилення), інакше клієнтський підйом (queue.js кладе щойно
  пришпилений у КІНЕЦЬ набору) і полл розходились би, і набір стрибав би
  кожні 15 с;
- блок пошти й «уся вкладка» шпильками НЕ сортуються (рішення власника);
- чужі шпильки мій док не чіпають;
- `focused_ids` для рядків і порядок рахуються з ОДНОГО словника
  (`dock.focus_rank`), а tbody блоку лаби несе `data-pin-lab` — по ньому
  queue.js знає, де можна підіймати.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Order, User
from app.services.focus import ranks, toggle
from app.services.mail_mirror import mail_dock, mail_dock_for
from tests.test_mail_dock import (  # noqa: F401 — app_db: фікстура
    OPERATOR,
    _database,
    _label,
    _login,
    _seed_day,
    _user,
    app_db,
)

T0 = datetime(2026, 10, 22, 9, 0, 0)


def _pin(db, order: Order, user: User, minutes: int) -> None:
    toggle(db, order, user, now=T0 + timedelta(minutes=minutes))
    db.commit()


def test_lab_block_lifts_my_pins_in_pin_order_and_mail_block_stays():
    with Session(_database(), expire_on_commit=False) as db:
        s = _seed_day(db)
        me = _user(db)
        before = [_label(o) for o in mail_dock(db, scope="lab", ready="all").orders]
        assert before == [
            "Пошта-3", "Пошта-2", "Пошта-1",
            "L-в-роботі", "L-можна-1", "L-можна-2", "L-не-готово",
        ]
        # Пришпилюю НЕ в порядку таблиці: спершу нижній рядок, потім верхній.
        _pin(db, s.l_wait, me, 1)
        _pin(db, s.l_take_a, me, 2)
        _pin(db, s.m1, me, 3)  # лист: блок пошти шпилькою не сортується

        dock = mail_dock(db, scope="lab", ready="all", focus_rank=ranks(db, me))
        assert [_label(o) for o in dock.orders] == [
            "Пошта-3", "Пошта-2", "Пошта-1",
            # пришпилені — за ЧАСОМ пришпилення, далі решта в порядку таблиці
            "L-не-готово", "L-можна-1",
            "L-в-роботі", "L-можна-2",
        ]
        assert dock.sep_index == 3 and dock.lab_start == 3
        assert dock.lab_pinned == 2, "лист у наборі не рахується шпилькою блоку лаби"
        assert set(dock.focus_rank) == {s.l_wait.id, s.l_take_a.id, s.m1.id}


def test_labonly_lifts_pins_from_the_top_and_filter_still_applies():
    with Session(_database(), expire_on_commit=False) as db:
        s = _seed_day(db)
        me = _user(db)
        _pin(db, s.l_take_b, me, 1)
        _pin(db, s.l_wait, me, 2)  # «не готово» — під фільтром can_take його нема

        dock = mail_dock(db, scope="labonly", focus_rank=ranks(db, me))
        assert [_label(o) for o in dock.orders] == ["L-можна-2", "L-можна-1"]
        assert dock.lab_start == 0 and dock.sep_index is None

        dock_all = mail_dock(db, scope="labonly", ready="all", focus_rank=ranks(db, me))
        assert [_label(o) for o in dock_all.orders] == [
            "L-можна-2", "L-не-готово", "L-в-роботі", "L-можна-1",
        ]


def test_canon_and_whole_tab_are_not_sorted_by_pins():
    with Session(_database(), expire_on_commit=False) as db:
        s = _seed_day(db)
        me = _user(db)
        plain = {
            scope: [_label(o) for o in mail_dock(db, scope=scope, ready="all").orders]
            for scope in ("", "all")
        }
        _pin(db, s.m1, me, 1)
        _pin(db, s.l_wait, me, 2)
        _pin(db, s.sheet, me, 3)
        for scope in ("", "all"):
            dock = mail_dock(db, scope=scope, ready="all", focus_rank=ranks(db, me))
            assert [_label(o) for o in dock.orders] == plain[scope], scope
            assert dock.lab_start is None, "блоку лаби, де можна підіймати, тут немає"


def test_lab_block_without_lab_rows_has_no_pin_block():
    with Session(_database(), expire_on_commit=False) as db:
        s = _seed_day(db)
        me = _user(db)
        _pin(db, s.m2, me, 1)
        # «не готово»-фільтр: лаби під нього в вкладці немає → лише пошта.
        db.delete(s.l_wait)
        db.commit()
        dock = mail_dock(db, scope="lab", ready="not_ready", focus_rank=ranks(db, me))
        assert [_label(o) for o in dock.orders] == ["Пошта-3", "Пошта-2", "Пошта-1"]
        assert dock.lab_start is None and dock.sep_index is None


def test_other_operators_pins_do_not_reorder_my_dock():
    with Session(_database(), expire_on_commit=False) as db:
        s = _seed_day(db)
        me = _user(db, mail_dock_scope="labonly", mail_dock_ready="all")
        colleague = User(username="op2", password_hash="x", full_name="Колега", role="оператор")
        db.add(colleague)
        db.commit()
        _pin(db, s.l_wait, colleague, 1)

        mine = mail_dock_for(db, me)
        assert [_label(o) for o in mine.orders] == [
            "L-в-роботі", "L-можна-1", "L-можна-2", "L-не-готово",
        ], "чужа шпилька мій док не переставляє"
        assert mine.focus_rank == {}
        assert mail_dock_for(db, colleague).focus_rank == {s.l_wait.id: 0}

        _pin(db, s.l_take_b, me, 2)
        assert [_label(o) for o in mail_dock_for(db, me).orders][0] == "L-можна-2"


def _row_ids(html: str) -> list[int]:
    return [int(m) for m in re.findall(r'id="order-row-(\d+)"', html)]


def test_poll_keeps_my_pins_on_top_of_the_lab_block_and_marks_it(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        s = _seed_day(db)
        me = db.query(User).filter_by(username=OPERATOR[0]).one()
        me.mail_dock_scope = "lab"
        me.mail_dock_ready = "all"
        db.commit()
        _pin(db, s.l_wait, me, 1)
        _pin(db, s.l_take_a, me, 2)
        expected_lab = [s.l_wait.id, s.l_take_a.id, s.l_work.id, s.l_take_b.id]
        mail_ids = [s.m3.id, s.m2.id, s.m1.id]

    client = _login(app)
    first = client.get("/mail/queue-mirror")
    second = client.get("/mail/queue-mirror")
    for status, _, html in (first, second):
        assert status == 200
        assert _row_ids(html) == mail_ids + expected_lab
        assert "<tbody data-pin-lab>" in html
        assert "мої зараз першими, далі як у таблиці" in html, "роздільник не бреше про порядок"
        # Підсвітка — з того самого словника, що й порядок.
        for oid in expected_lab[:2]:
            row = re.search(rf'<tr[^>]*id="order-row-{oid}"', html).group(0)
            assert "queue-row-focus" in row
        row = re.search(rf'<tr[^>]*id="order-row-{s.l_work.id}"', html).group(0)
        assert "queue-row-focus" not in row
    assert first[2] == second[2], "полл без змін віддає те саме — своп пропускається, нічого не стрибає"

    # Повна сторінка — той самий порядок і смуга групового Sum3D (2 шпильки).
    status, _, page = client.get("/mail")
    assert status == 200
    assert _row_ids(page) == mail_ids + expected_lab
    assert 'class="sum3d-batch"' in page


def test_whole_tab_poll_has_no_pin_block_marker(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        _seed_day(db)
        me = db.query(User).filter_by(username=OPERATOR[0]).one()
        me.mail_dock_scope = "all"
        db.commit()
    client = _login(app)
    status, _, html = client.get("/mail/queue-mirror")
    assert status == 200
    assert "data-pin-lab" not in html


def test_js_lifts_only_inside_the_lab_block():
    """queue.js підіймає в доці лише під `data-pin-lab` і не через роздільник;
    знята шпилька в доці перепитує док одразу; mail.js скидає «та сама
    відповідь» після локальної перестановки."""
    queue_js = Path("app/static/js/queue.js").read_text(encoding="utf-8")
    assert 'if (row.closest("#mail-queue-mirror")) return;' not in queue_js
    assert 'body.hasAttribute("data-pin-lab")' in queue_js
    assert "tr.qmir-sep" in queue_js
    assert 'new CustomEvent("mail-dock-refresh")' in queue_js
    mail_js = Path("app/static/js/mail.js").read_text(encoding="utf-8")
    assert re.search(r"focus\$/\.test\(cfg\.path[^)]*\)\) lastMirrorResponse = null", mail_js)
