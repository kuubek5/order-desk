from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Comment, Order, ReworkRecord
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


def test_rework_row_creates_rework_record():
    with make_session() as session:
        sync_tab(
            session,
            "01.08.26",
            [make_row(
                mill_count="2",
                rework_blame={"технік": "3"},
                redo_quantity="3",
                redo_cam_comment="перефрезерувати балансир",
                redo_sum3d_id="SUM-REDO",
                redo_calculated="+ 09:00",
                redo_milled="+ 11:00",
            )],
        )
        session.commit()

        rec = session.scalar(select(ReworkRecord))
        assert rec is not None
        assert rec.occurrence == 2
        assert rec.blame == "технік"
        assert rec.blame_quantity == "3"
        assert rec.redo_quantity == "3"
        assert rec.cam_comment == "перефрезерувати балансир"
        assert rec.sum3d_id == "SUM-REDO"


def test_no_rework_columns_creates_no_record():
    with make_session() as session:
        sync_tab(session, "01.08.26", [make_row()])
        session.commit()
        assert session.scalar(select(ReworkRecord)) is None


def test_rework_sync_is_idempotent_and_updates_in_place():
    with make_session() as session:
        row = make_row(rework_blame={"клієнт": "1"}, redo_cam_comment="варіант 1")
        sync_tab(session, "01.08.26", [row])
        session.commit()

        # Re-sync with the blame edited in the sheet — must update the SAME
        # record, not add a second one.
        row2 = make_row(rework_blame={"обладнання": "2"}, redo_cam_comment="варіант 2")
        sync_tab(session, "01.08.26", [row2])
        session.commit()

        records = session.scalars(select(ReworkRecord)).all()
        assert len(records) == 1
        assert records[0].blame == "обладнання"
        assert records[0].blame_quantity == "2"
        assert records[0].cam_comment == "варіант 2"


def test_active_rework_returns_latest_record():
    from datetime import datetime
    with make_session() as session:
        order = Order(source="lab", sheet_tab="01.08.26", row_number=1, work_order_no="24122", status="нове")
        session.add(order)
        session.flush()
        session.add(ReworkRecord(order_id=order.id, blame="технік", created_at=datetime(2026, 8, 1, 9, 0)))
        session.add(ReworkRecord(order_id=order.id, blame="клієнт", created_at=datetime(2026, 8, 2, 9, 0)))
        session.commit()
        session.refresh(order)
        assert order.active_rework is not None
        assert order.active_rework.blame == "клієнт"


def test_row_removed_from_sheet_is_deleted():
    with make_session() as session:
        # Two rows imported.
        sync_tab(
            session,
            "01.08.26",
            [make_row(row_number=1, work_order_no="24122"),
             make_row(row_number=2, work_order_no="24123")],
        )
        session.commit()
        assert session.scalar(select(Order).where(Order.work_order_no == "24123")) is not None

        # Technician removed the second row from the sheet — its row_number no
        # longer appears, so the order must go.
        result = sync_tab(session, "01.08.26", [make_row(row_number=1, work_order_no="24122")])
        session.commit()

        assert result.deleted == 1
        remaining = session.scalars(select(Order)).all()
        assert [o.work_order_no for o in remaining] == ["24122"]


def test_deleted_order_cascades_history():
    from app.models import StatusEvent
    with make_session() as session:
        sync_tab(session, "01.08.26", [make_row(row_number=1, cam_comment="лишиться в історії")])
        session.commit()
        assert session.scalar(select(Comment)) is not None
        assert session.scalar(select(StatusEvent)) is not None

        # Row gone → order + its comment/status history removed with it.
        sync_tab(session, "01.08.26", [make_row(row_number=2, work_order_no="99999")])
        session.commit()

        assert session.scalar(select(Order).where(Order.work_order_no == "24122")) is None
        assert session.scalar(select(Comment).where(Comment.text == "лишиться в історії")) is None


def test_empty_rows_never_wipe_tab():
    # A transient empty read (headers only, via the lab TLS proxy) must not
    # delete every order in the tab.
    with make_session() as session:
        sync_tab(session, "01.08.26", [make_row(row_number=1)])
        session.commit()

        result = sync_tab(session, "01.08.26", [])
        session.commit()

        assert result.deleted == 0
        assert session.scalar(select(Order)) is not None


def test_email_order_in_tab_is_not_deleted_by_sheet_sync():
    with make_session() as session:
        email_order = Order(
            source="email",
            sheet_tab="01.08.26",
            row_number=5,
            work_order_no="MAIL-1",
            status="нове",
        )
        session.add(email_order)
        session.commit()

        # A lab sync of the same tab whose rows don't include row 5 must leave
        # the email-sourced order untouched.
        result = sync_tab(session, "01.08.26", [make_row(row_number=1)])
        session.commit()

        assert result.deleted == 0
        assert session.scalar(select(Order).where(Order.source == "email")) is not None


def test_active_rework_none_without_records():
    with make_session() as session:
        order = Order(source="lab", sheet_tab="01.08.26", row_number=1, work_order_no="24122", status="нове")
        session.add(order)
        session.commit()
        assert order.active_rework is None
