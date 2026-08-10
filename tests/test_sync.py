from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Comment, Order
from app.parser import OrderRow
from app.sync import sync_tab


def make_row(**overrides):
    values = {
        "row_number": 1,
        "seq_no": "1",
        "work_order_no": "24122",
        "quantity": "2",
        "material_color": "моно A2",
        "kind": "анатомія",
        "due_time": "14:00",
        "job_code": "2026-08-01_00001-001",
        "technician_name": "Технік",
        "cam_comment": "",
        "sum3d_id": "SUM1",
        "calculated": "+ 10:00",
        "milled": "+ 12:00",
        "last_milled_date": "01.08.26",
        "mill_count": "1",
    }
    values.update(overrides)
    return OrderRow(**values)


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_new_sheet_comment_is_imported_to_history():
    with make_session() as session:
        sync_tab(session, "01.08.26", [make_row(cam_comment="Обережно з краєм")])
        session.commit()

        comment = session.scalar(select(Comment))
        assert comment is not None
        assert comment.source == "sheet"
        assert comment.text == "Обережно з краєм"


def test_only_appended_sheet_comment_text_is_added_to_history():
    with make_session() as session:
        order = Order(
            source="lab",
            sheet_tab="01.08.26",
            row_number=1,
            work_order_no="24122",
            cam_comment="Перший коментар",
            status="відфрезеровано",
        )
        session.add(order)
        session.commit()

        sync_tab(
            session,
            "01.08.26",
            [make_row(cam_comment="Перший коментар\nДругий коментар")],
        )
        session.commit()

        comments = session.scalars(select(Comment)).all()
        assert [comment.text for comment in comments] == ["Другий коментар"]


def test_handout_status_survives_sheet_sync():
    with make_session() as session:
        order = Order(
            source="lab",
            sheet_tab="01.08.26",
            row_number=1,
            work_order_no="24122",
            status="видано",
        )
        session.add(order)
        session.commit()

        result = sync_tab(session, "01.08.26", [make_row(milled="+ 12:00")])
        session.commit()

        assert order.status == "видано"
        assert result.updated == 1


def test_problem_status_survives_sheet_sync():
    with make_session() as session:
        order = Order(
            source="lab",
            sheet_tab="01.08.26",
            row_number=1,
            work_order_no="24122",
            status="проблема",
        )
        session.add(order)
        session.commit()

        sync_tab(session, "01.08.26", [make_row(milled="+ 12:00")])
        session.commit()

        assert order.status == "проблема"


def test_in_milling_advances_when_sheet_reports_milled():
    with make_session() as session:
        order = Order(
            source="lab",
            sheet_tab="01.08.26",
            row_number=1,
            work_order_no="24122",
            status="у фрезеруванні",
        )
        session.add(order)
        session.commit()

        sync_tab(session, "01.08.26", [make_row(milled="+ 12:00")])
        session.commit()

        assert order.status == "відфрезеровано"


def test_accepted_status_does_not_regress_when_sheet_has_no_markers():
    with make_session() as session:
        order = Order(
            source="lab",
            sheet_tab="01.08.26",
            row_number=1,
            work_order_no="24122",
            status="прийнято",
        )
        session.add(order)
        session.commit()

        sync_tab(
            session,
            "01.08.26",
            [make_row(sum3d_id="", calculated="", milled="")],
        )
        session.commit()

        assert order.status == "прийнято"


def test_calculated_status_does_not_regress_to_accepted():
    with make_session() as session:
        order = Order(
            source="lab",
            sheet_tab="01.08.26",
            row_number=1,
            work_order_no="24122",
            status="прораховано",
        )
        session.add(order)
        session.commit()

        sync_tab(
            session,
            "01.08.26",
            [make_row(calculated="", milled="")],
        )
        session.commit()

        assert order.status == "прораховано"
