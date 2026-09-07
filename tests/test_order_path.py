"""Шлях роботи в паспорті: одна стрічка, і в ній видно ХТО прорахував.

Раніше паспорт мав дві стрічки — статуси й журнал дій, — і питання «коли
прорахували й хто» вимагало зіставляти їх очима. Прохання власника 07.09.26.
"""

from datetime import datetime

from app.models import ActionLog, Order, StatusEvent, User
from app.services.order_path import build_path


def _order(**kw):
    base = dict(
        source="lab", sheet_tab="07.09.26", row_number=7, work_order_no="24122",
        status="прораховано", created_at=datetime(2026, 9, 7, 8, 0),
    )
    base.update(kw)
    return Order(**base)


def test_path_starts_with_the_moment_the_work_appeared():
    """Найперша подія не була записана НІДЕ — стрічка починалась із середини."""
    steps = build_path(_order(), [])

    assert [s.label for s in steps] == ["зʼявилась у черзі"]
    assert steps[0].who == "з таблиці, лабораторія"


def test_calculation_says_who_and_under_which_id():
    """Заради цього все й робилось: «прораховано» без імені й номера не
    відповідає ні «хто», ні «під яким ID шукати в CAM»."""
    oksana = User(username="oksana", password_hash="h", full_name="Оксана", role="оператор")
    order = _order()
    order.status_events = [StatusEvent(
        status="прораховано", operator=oksana, actor="oksana",
        occurred_at=datetime(2026, 9, 7, 12, 1),
    )]
    # Журнал зберігає ЗНІМОК полів (його читає скасування), не самий номер.
    action = ActionLog(
        action_type="sum3d", field="sum3d_id",
        new_value='{"sum3d_id": "12-01-45", "calculated_raw": "М", "status": "прораховано"}',
        note="Sum3D → 12-01-45", created_at=datetime(2026, 9, 7, 12, 1, 30),
    )
    action.operator = oksana

    steps = build_path(order, [action])

    calc = next(s for s in steps if s.label == "прораховано в Sum3D")
    assert calc.who == "Оксана"
    assert calc.detail == "12-01-45"


def test_newest_first_and_undated_events_survive():
    """Старі рядки без штампа часу мусять лишитись видимими, а не зникнути."""
    order = _order(created_at=datetime(2026, 9, 7, 8, 0))
    order.status_events = [
        StatusEvent(status="у фрезеруванні", actor="sync", occurred_at=datetime(2026, 9, 7, 15, 0)),
        StatusEvent(status="прийнято", actor="sync", occurred_at=None),
    ]

    labels = [s.label for s in build_path(order, [])]

    assert labels[0] == "у фрезеруванні"
    assert labels[-1] == "прийнято"


def test_sync_is_not_a_person():
    """`sync` у полі actor — це синхронізація, не людина на імʼя sync."""
    order = _order()
    order.status_events = [StatusEvent(
        status="прийнято", actor="sync", occurred_at=datetime(2026, 9, 7, 9, 0)
    )]

    assert build_path(order, [])[0].who == "таблиця"


def test_noise_actions_stay_out_of_the_path():
    """Зміна коментаря для CAM — не етап шляху; у стрічці це шум."""
    order = _order()
    noise = ActionLog(
        action_type="comment", field="cam_comment", new_value="на швидку",
        created_at=datetime(2026, 9, 7, 13, 0),
    )

    assert [s.kind for s in build_path(order, [noise])] == ["birth"]


def test_the_first_new_status_does_not_repeat_the_birth():
    """Синк ставить «нове» тієї ж миті, коли робота зʼявилась. Двома рядками
    поспіль це читається як подія, що сталася двічі."""
    order = _order(created_at=datetime(2026, 9, 7, 8, 0))
    order.status_events = [
        StatusEvent(status="нове", actor="sync", occurred_at=datetime(2026, 9, 7, 8, 0, 3)),
        StatusEvent(status="нове", actor="sync", occurred_at=datetime(2026, 9, 8, 10, 0)),
    ]

    labels = [s.label for s in build_path(order, [])]

    assert labels == ["нове", "зʼявилась у черзі"]   # пізніший «нове» — справжня подія


def test_archiving_is_part_of_the_path():
    """«Куди вона поділась» питають саме тоді, коли роботи вже нема в черзі."""
    order = _order(archived_at=datetime(2026, 9, 8, 7, 30))

    steps = build_path(order, [])

    assert steps[0].label == "прибрано з черги"
    assert steps[0].kind == "archive"
