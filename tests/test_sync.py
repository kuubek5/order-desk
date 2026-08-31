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


def age_orders(session):
    """Backdate created_at past sync_tab's deletion grace window (120s) — a
    just-created order is deliberately shielded from reconciliation (see the
    manual-add read/write race note in app/sync.py), so deletion tests must
    age their fixtures first, like real orders that have been around."""
    from datetime import datetime, timedelta

    old = datetime.utcnow() - timedelta(minutes=10)
    for order in session.scalars(select(Order)):
        order.created_at = old
    session.commit()


def make_client_row(**overrides):
    """A наряд-less client row: no work_order_no/technician, client name in the
    "вид" (kind) column, only material + quantity (real-tab shape)."""
    values = {
        "row_number": 5,
        "seq_no": "60",
        "work_order_no": "",
        "quantity": "1",
        "material_color": "mono a3",
        "kind": "Басараб",  # client name, not a work type
        "due_time": None,
        "job_code": "",
        "technician_name": "",
        "cam_comment": "",
        "sum3d_id": "10-19-48",
        "calculated": "D",
        "milled": "",
        "last_milled_date": "",
        "mill_count": "",
    }
    values.update(overrides)
    return OrderRow(**values)


def test_client_row_is_imported_as_sheet_client_with_name():
    with make_session() as session:
        res = sync_tab(session, "22.06.26", [make_client_row()])
        session.commit()

        order = session.scalar(select(Order).where(Order.sheet_tab == "22.06.26"))
        assert res.created == 1
        assert order.source == "sheet_client"
        assert order.client_name == "Басараб"
        assert order.work_order_no is None
        assert order.kind is None  # the "вид" column held the client, not a type
        assert order.material_color == "mono a3"
        assert order.quantity == "1"
        assert order.status == "нове"
        # Sum3D (column L) is read, not ignored — otherwise a later sync would
        # wipe whatever the operator typed on the row.
        assert order.sum3d_id == "10-19-48"
        # «Прорахував» (column М) is read for client rows too — a client work is
        # calculated by an operator just like a lab work (regression: it used to
        # be hardcoded None, so the operator column stayed empty on client rows).
        assert order.calculated_raw == "D"


def test_client_row_operator_survives_resync():
    """The operator letter (column М) an operator sets on a client row must not
    be wiped by the next sync — same round-trip guarantee as Sum3D."""
    with make_session() as session:
        sync_tab(session, "22.06.26", [make_client_row(row_number=5, calculated="")])
        session.commit()
        order = session.scalar(select(Order).where(Order.source == "sheet_client"))
        assert not order.calculated_raw
        # Operator types the letter in the CRM (written back to column М).
        order.calculated_raw = "К"
        session.commit()
        # Next sync reads the sheet, now carrying that value in column М.
        sync_tab(session, "22.06.26", [make_client_row(row_number=5, calculated="К")])
        session.commit()
        session.refresh(order)
        assert order.calculated_raw == "К"  # not wiped back to None


def test_client_row_sum3d_survives_resync():
    with make_session() as session:
        sync_tab(session, "22.06.26", [make_client_row(row_number=5, sum3d_id="")])
        session.commit()
        order = session.scalar(select(Order).where(Order.source == "sheet_client"))
        # Operator types a Sum3D in the CRM (written back to column L).
        order.sum3d_id = "PRJ-CLIENT-1"
        session.commit()

        # Next sync reads the sheet, which now carries that value in column L.
        sync_tab(session, "22.06.26", [make_client_row(row_number=5, sum3d_id="PRJ-CLIENT-1")])
        session.commit()
        session.refresh(order)
        assert order.sum3d_id == "PRJ-CLIENT-1"  # not wiped back to None


def test_slm_row_by_material_text_is_not_imported():
    """A row whose "Колір роботи" is one of the SLM/non-queue words never
    becomes an Order — the lab records it for stats only, not for milling."""
    with make_session() as session:
        result = sync_tab(session, "22.06.26", [
            make_row(row_number=1, material_color="слм"),
            make_row(row_number=2, work_order_no="24999", material_color="моно A2"),
        ])
        session.commit()
        assert result.created == 1
        orders = session.scalars(select(Order)).all()
        assert [o.work_order_no for o in orders] == ["24999"]


def test_slm_row_matching_is_case_and_space_insensitive():
    with make_session() as session:
        result = sync_tab(session, "22.06.26", [
            make_row(row_number=1, material_color="  Моделювання  "),
        ])
        session.commit()
        assert result.created == 0
        assert session.scalar(select(Order)) is None


def test_grey_fill_row_is_not_imported_even_without_slm_text():
    """The fill is the second marker — a row that's grey but whose material
    text isn't one of the known words is still skipped."""
    with make_session() as session:
        result = sync_tab(
            session, "22.06.26",
            [make_row(row_number=1, material_color="щось інше")],
            row_fills={1: "grey"},
        )
        session.commit()
        assert result.created == 0


def test_previously_imported_slm_row_is_removed_on_next_sync():
    """An order imported before this filter existed (or before the lab
    started marking a row grey) is treated as vanished — same deletion path
    as a row the lab cleared."""
    with make_session() as session:
        sync_tab(session, "22.06.26", [make_row(row_number=1, work_order_no="24999")])
        session.commit()
        age_orders(session)
        assert session.scalar(select(Order)) is not None

        result = sync_tab(session, "22.06.26", [
            make_row(row_number=1, work_order_no="24999", material_color="слм"),
        ])
        session.commit()
        assert result.deleted == 1
        # Kept, not deleted: the vanished row is archived (leaves the working
        # queue, stays for the Archive) rather than removed from the DB.
        archived = session.scalar(select(Order))
        assert archived is not None and archived.archived_at is not None


def test_client_row_and_normal_row_coexist_in_one_tab():
    with make_session() as session:
        sync_tab(session, "22.06.26", [
            make_row(row_number=1, work_order_no="24122"),
            make_client_row(row_number=5),
        ])
        session.commit()

        by_source = {o.source for o in session.scalars(select(Order))}
        assert by_source == {"lab", "sheet_client"}


def test_vanished_client_row_is_deleted_like_a_lab_row():
    with make_session() as session:
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)])
        session.commit()
        age_orders(session)
        assert session.scalar(select(Order)) is not None

        # Client got issued / row removed → its row_number is gone → archive it.
        res = sync_tab(session, "22.06.26", [make_row(row_number=1)])
        session.commit()
        assert res.deleted == 1
        client = session.scalar(select(Order).where(Order.source == "sheet_client"))
        assert client is not None and client.archived_at is not None


def test_client_row_not_blue_is_issued():
    with make_session() as session:
        # row_fills says row 5 is NOT blue → the lab cleared it → issued.
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)], row_fills={5: ""})
        session.commit()
        order = session.scalar(select(Order).where(Order.source == "sheet_client"))
        assert order.status == "видано"


def test_client_row_blue_stays_pending():
    with make_session() as session:
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)], row_fills={5: "blue"})
        session.commit()
        order = session.scalar(select(Order).where(Order.source == "sheet_client"))
        assert order.status == "нове"


def test_client_row_without_colour_info_stays_pending():
    with make_session() as session:
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)])  # row_fills=None
        session.commit()
        order = session.scalar(select(Order).where(Order.source == "sheet_client"))
        assert order.status == "нове"


def test_existing_client_flips_to_issued_when_blue_cleared():
    with make_session() as session:
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)], row_fills={5: "blue"})
        session.commit()
        order = session.scalar(select(Order).where(Order.source == "sheet_client"))
        assert order.status == "нове"

        # Operator clears the blue → next sync flips it to issued.
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)], row_fills={5: ""})
        session.commit()
        session.refresh(order)
        assert order.status == "видано"

        # Re-bluing does NOT un-issue (видано is protected from downgrade).
        sync_tab(session, "22.06.26", [make_client_row(row_number=5)], row_fills={5: "blue"})
        session.commit()
        session.refresh(order)
        assert order.status == "видано"


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
        age_orders(session)
        assert session.scalar(select(Order).where(Order.work_order_no == "24123")) is not None

        # Technician removed the second row from the sheet — its row_number no
        # longer appears, so it leaves the working queue (archived, not deleted).
        result = sync_tab(session, "01.08.26", [make_row(row_number=1, work_order_no="24122")])
        session.commit()

        assert result.deleted == 1
        active = session.scalars(select(Order).where(Order.archived_at.is_(None))).all()
        assert [o.work_order_no for o in active] == ["24122"]
        gone = session.scalar(select(Order).where(Order.work_order_no == "24123"))
        assert gone is not None and gone.archived_at is not None


def test_freshly_created_order_survives_stale_sync_read():
    """The manual-add race: an operator's add writes the sheet row and commits
    the Order while a hot-lane tick is mid-flight with values fetched BEFORE
    that write. The stale read doesn't contain the new row — reconciliation
    must NOT delete the just-created order (grace window)."""
    with make_session() as session:
        # Freshly created manual order (created_at = now, inside the grace).
        session.add(Order(
            source="sheet_client", sheet_tab="01.08.26", row_number=163,
            client_name="Свіжий", material_color="emo a3", status="нове",
        ))
        session.commit()

        # Stale sync read: the sheet snapshot predates the manual add, so row
        # 163 is absent from `rows`.
        result = sync_tab(session, "01.08.26", [make_row(row_number=1)])
        session.commit()

        assert result.deleted == 0
        assert session.scalar(
            select(Order).where(Order.client_name == "Свіжий")
        ) is not None

        # Once aged past the grace, a genuinely vanished row IS reconciled.
        age_orders(session)
        result = sync_tab(session, "01.08.26", [make_row(row_number=1)])
        session.commit()
        assert result.deleted == 1


def test_archived_order_keeps_its_history():
    from app.models import StatusEvent
    with make_session() as session:
        sync_tab(session, "01.08.26", [make_row(row_number=1, cam_comment="лишиться в історії")])
        session.commit()
        age_orders(session)
        assert session.scalar(select(Comment)) is not None
        assert session.scalar(select(StatusEvent)) is not None

        # Row gone → order is ARCHIVED, not deleted, so its comment/status
        # history is preserved for the Archive (nothing cascades away).
        sync_tab(session, "01.08.26", [make_row(row_number=2, work_order_no="99999")])
        session.commit()

        archived = session.scalar(select(Order).where(Order.work_order_no == "24122"))
        assert archived is not None and archived.archived_at is not None
        assert session.scalar(select(Comment).where(Comment.text == "лишиться в історії")) is not None


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


# --- Видалення рядка в Google зсуває номери -------------------------------
#
# Позиція в таблиці НЕ є стабільним ключем: «Видалити рядок» у Google посуває
# все нижче вгору. Раніше звʼязок тримався лише на номері рядка, і кожна робота
# нижче тихо перезаписувалась даними сусіда, а в архів їхала не та. Тепер
# спершу зіставляємо за стійкою ознакою (наряд / клієнт+матеріал), і лише потім
# за позицією.


def test_deleted_sheet_row_archives_that_work_and_shifts_the_rest():
    session = make_session()
    sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="AAA", sum3d_id="S-A"),
        make_row(row_number=8, work_order_no="BBB", sum3d_id="S-B"),
        make_row(row_number=9, work_order_no="CCC", sum3d_id="S-C"),
    ])
    session.commit()
    age_orders(session)

    # Оператор ВИДАЛЯЄ рядок BBB (не очищає) — CCC переїжджає на 8.
    result = sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="AAA", sum3d_id="S-A"),
        make_row(row_number=8, work_order_no="CCC", sum3d_id="S-C"),
    ])
    session.commit()

    by_naryad = {o.work_order_no: o for o in session.scalars(select(Order))}
    assert by_naryad["BBB"].archived_at is not None, "видалена робота має піти в архів"
    assert by_naryad["CCC"].archived_at is None, "уціліла робота не має архівуватись"
    assert by_naryad["CCC"].row_number == 8, "CCC мав перезвʼязатись на новий рядок"
    assert by_naryad["AAA"].row_number == 7
    assert result.moved == 1


def test_shift_does_not_overwrite_a_work_with_its_neighbours_data():
    """Найгірший наслідок старої поведінки: BBB лишався в черзі, але показував
    дані CCC — разом із чужою історією статусів."""
    session = make_session()
    sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="BBB", sum3d_id="S-B", material_color="пмма A2"),
        make_row(row_number=8, work_order_no="CCC", sum3d_id="S-C", material_color="титан"),
    ])
    session.commit()
    age_orders(session)

    sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="CCC", sum3d_id="S-C", material_color="титан"),
    ])
    session.commit()

    by_naryad = {o.work_order_no: o for o in session.scalars(select(Order))}
    assert set(by_naryad) == {"BBB", "CCC"}, "жодна робота не мала зникнути чи здублюватись"
    assert by_naryad["BBB"].material_color == "пмма A2", "BBB не має отримати матеріал CCC"
    assert by_naryad["BBB"].archived_at is not None
    assert by_naryad["CCC"].material_color == "титан"
    assert by_naryad["CCC"].archived_at is None


def test_client_row_survives_a_shift_by_client_and_material():
    """У клієнтських рядків немає наряду — ознака це клієнт + матеріал + к-сть."""
    session = make_session()
    sync_tab(session, "25.08.26", [
        make_client_row(row_number=5, kind="Басараб", material_color="mono a3"),
        make_client_row(row_number=6, kind="Ковальчук", material_color="пмма A2"),
    ])
    session.commit()
    age_orders(session)

    sync_tab(session, "25.08.26", [
        make_client_row(row_number=5, kind="Ковальчук", material_color="пмма A2"),
    ])
    session.commit()

    by_client = {o.client_name: o for o in session.scalars(select(Order))}
    assert by_client["Басараб"].archived_at is not None
    assert by_client["Ковальчук"].archived_at is None
    assert by_client["Ковальчук"].row_number == 5


def test_repeat_works_sharing_a_naryad_fall_back_to_position():
    """Повторні роботи законно мають однаковий наряд — вгадувати між ними
    гірше, ніж лишитись на позиції. Неоднозначна ознака не бере участі."""
    session = make_session()
    sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="DUP", sum3d_id="S-1", material_color="моно A2"),
        make_row(row_number=8, work_order_no="DUP", sum3d_id="S-2", material_color="титан"),
    ])
    session.commit()
    age_orders(session)

    result = sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="DUP", sum3d_id="S-1", material_color="моно A2"),
        make_row(row_number=8, work_order_no="DUP", sum3d_id="S-2", material_color="титан"),
    ])
    session.commit()
    assert result.moved == 0
    assert session.query(Order).count() == 2


def test_unchanged_tab_moves_nothing():
    session = make_session()
    sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="AAA"),
        make_row(row_number=8, work_order_no="BBB"),
    ])
    session.commit()
    age_orders(session)

    result = sync_tab(session, "25.08.26", [
        make_row(row_number=7, work_order_no="AAA"),
        make_row(row_number=8, work_order_no="BBB"),
    ])
    assert result.moved == 0
    assert result.deleted == 0


def test_refilled_row_returns_an_archived_order_to_the_queue():
    """A row that is populated again brings its order back.

    Real failure this guards (25.08.26, prod): works added by hand without a
    наряд were overwritten in place by a bug, the operator deleted the bogus
    entries (archiving them), and technicians then wrote REAL works into those
    same rows. The sync matched each row to the archived order and updated its
    fields — but left archived_at set, so the queue kept insisting the works did
    not exist while they sat plainly in the sheet.
    """
    from datetime import datetime

    session = make_session()
    order = Order(
        source="lab", sheet_tab="25.08.26", row_number=25,
        work_order_no="TEST", material_color="TEST2", status="нове",
        archived_at=datetime(2026, 8, 25, 9, 0, 0),
    )
    session.add(order)
    session.commit()

    sync_tab(session, "25.08.26", [make_row(
        row_number=25, work_order_no="28393", material_color="1333",
        kind="абатмент", quantity="1", job_code="", sum3d_id="",
        calculated="", milled="",
    )])
    session.commit()

    refreshed = session.get(Order, order.id)
    assert refreshed.archived_at is None, "робота мала повернутись у чергу"
    assert refreshed.work_order_no == "28393"
    assert refreshed.material_color == "1333"


def test_sync_does_not_touch_an_archived_order_whose_row_stays_empty():
    """The counterpart: an order archived because its row was cleared must STAY
    archived while the row remains empty — the fix above must not resurrect
    everything the operator has ever deleted."""
    from datetime import datetime

    session = make_session()
    order = Order(
        source="lab", sheet_tab="25.08.26", row_number=25,
        work_order_no="24122", status="нове",
        archived_at=datetime(2026, 8, 25, 9, 0, 0),
    )
    session.add(order)
    session.commit()
    age_orders(session)

    # Another row is present, so this is not an empty/transient read — row 25
    # simply is not in the sheet any more. A different наряд, so the identity
    # re-link cannot bind the archived order to this row.
    sync_tab(session, "25.08.26", [make_row(row_number=1, work_order_no="99999")])
    session.commit()

    refreshed = session.get(Order, order.id)
    assert refreshed.archived_at == datetime(2026, 8, 25, 9, 0, 0)


def test_deleted_work_stays_deleted_while_its_row_is_still_being_blanked():
    """Regression (0.3.6): deleting from the CRM must stick.

    Delete archives the order and blanks its sheet row on the BACKGROUND
    writer, so a sync tick routinely reads the row while that blanking is still
    in flight. Resurrecting on presence alone bounced every delete straight
    back into the queue — the row still held the same наряд.
    """
    from datetime import datetime, timedelta

    session = make_session()
    order = Order(
        source="lab", sheet_tab="25.08.26", row_number=25,
        work_order_no="28393", material_color="1333", status="нове",
        # СВІЖА архівація — саме так виглядає гонка з бланкером: видалили
        # секунди тому, фоновий запис у таблицю ще летить. Стара дата тут
        # означала б інший випадок — помилкову архівацію, яку синк тепер
        # ПОВЕРТАЄ (див. тест нижче про 17 робіт).
        archived_at=datetime.utcnow() - timedelta(seconds=30),
    )
    session.add(order)
    session.commit()

    # The row still carries the SAME work — blanking has not landed yet.
    sync_tab(session, "25.08.26", [make_row(
        row_number=25, work_order_no="28393", material_color="1333",
        kind="абатмент", quantity="1", job_code="", sum3d_id="",
        calculated="", milled="",
    )])
    session.commit()

    assert session.get(Order, order.id).archived_at is not None


def test_deleted_client_row_reused_by_another_client_comes_back():
    """The client-row counterpart: a наряд-less row reused by a DIFFERENT
    client is a new work and belongs in the queue."""
    from datetime import datetime

    session = make_session()
    order = Order(
        source="sheet_client", sheet_tab="25.08.26", row_number=63,
        client_name="TEST", material_color="TEST", status="нове",
        archived_at=datetime(2026, 8, 25, 9, 0, 0),
    )
    session.add(order)
    session.commit()

    sync_tab(session, "25.08.26", [make_row(
        row_number=63, work_order_no="", kind="Неда", material_color="mono a3",
        quantity="6", job_code="", sum3d_id="", calculated="", milled="",
        technician_name="",
    )])
    session.commit()

    refreshed = session.get(Order, order.id)
    assert refreshed.archived_at is None
    assert refreshed.client_name == "Неда"


def test_clearing_the_only_row_of_a_tab_still_archives_it():
    """Rома 25.08.26: added a work to tomorrow's tab in the SHEET, deleted it
    there, and the work stayed in the CRM forever.

    The tab is nearly empty (tomorrow's usually is), so deleting its only row
    makes the parsed row list empty — and the empty-read guard (meant for the
    proxy returning just headers) then skips reconciliation entirely.
    """
    session = make_session()
    sync_tab(session, "26.08.26", [make_row(row_number=1, work_order_no="28393")])
    session.commit()
    age_orders(session)
    assert session.scalar(select(Order)).archived_at is None

    # Operator clears that row in the sheet → the tab now parses to NOTHING,
    # but the read itself was fine: the header block still came back.
    sync_tab(session, "26.08.26", [], raw_row_count=6)
    session.commit()

    order = session.scalar(select(Order))
    assert order.archived_at is not None, "робота мала піти в архів"


def test_a_truly_empty_read_still_does_not_wipe_a_tab():
    """The guard this must not break: the lab proxy occasionally returns an
    empty response. Nothing at all came back, so nothing may be archived."""
    session = make_session()
    sync_tab(session, "26.08.26", [make_row(row_number=1, work_order_no="28393")])
    session.commit()
    age_orders(session)

    sync_tab(session, "26.08.26", [], raw_row_count=0)
    session.commit()

    assert session.scalar(select(Order)).archived_at is None


def test_technician_correcting_a_row_flags_it_for_the_operator():
    """A technician who mistypes and fixes the row must not slip past silently.

    The corrected row looks identical on screen, so an operator can mill the
    version they read minutes earlier — scrap the lab pays for. The flag names
    WHAT moved so they can judge whether it touches their work.
    """
    session = make_session()
    sync_tab(session, "26.08.26", [make_row(row_number=1, material_color="моно A2")])
    session.commit()
    order = session.scalar(select(Order))
    assert order.sheet_changed_at is None  # first import is not a correction

    sync_tab(session, "26.08.26", [make_row(row_number=1, material_color="моно A3.5")])
    session.commit()
    session.refresh(order)

    assert order.sheet_changed_at is not None
    assert order.sheet_changed_fields == "колір"


def test_change_flag_accumulates_until_dismissed():
    """A second correction before the operator acknowledges must ADD to the
    list, not replace it — otherwise the earlier change silently disappears."""
    session = make_session()
    sync_tab(session, "26.08.26", [make_row(row_number=1)])
    session.commit()
    order = session.scalar(select(Order))

    sync_tab(session, "26.08.26", [make_row(row_number=1, material_color="титан")])
    session.commit()
    sync_tab(session, "26.08.26", [
        make_row(row_number=1, material_color="титан", quantity="9")
    ])
    session.commit()
    session.refresh(order)

    assert "колір" in order.sheet_changed_fields
    assert "кількість" in order.sheet_changed_fields


def test_portal_own_writeback_does_not_raise_the_flag():
    """Sum3D and the milling markers are written BY the portal and read back on
    the next sync. Flagging those would bury real corrections in noise."""
    session = make_session()
    sync_tab(session, "26.08.26", [make_row(row_number=1, sum3d_id="", calculated="")])
    session.commit()
    order = session.scalar(select(Order))

    sync_tab(session, "26.08.26", [
        make_row(row_number=1, sum3d_id="12-01-45", calculated="+ 10:00")
    ])
    session.commit()
    session.refresh(order)

    assert order.sheet_changed_at is None


def test_filling_an_empty_field_for_the_first_time_is_not_a_correction():
    """The technician filling in the шлях later is normal progress, not a fix —
    flagging it would make the badge routine and train the operator to ignore
    it, which is exactly what must not happen to a scrap-prevention signal."""
    session = make_session()
    sync_tab(session, "26.08.26", [make_row(row_number=1, job_code="")])
    session.commit()
    order = session.scalar(select(Order))

    sync_tab(session, "26.08.26", [make_row(row_number=1, job_code="2026-08-26_00042-001")])
    session.commit()
    session.refresh(order)

    assert order.sheet_changed_at is None
    assert order.job_code == "2026-08-26_00042-001"


def test_row_reused_by_a_different_KIND_of_work_fully_replaces_the_old():
    """Rома 25.08.26: created a CLIENT row via the CRM, deleted it, admins then
    put a LAB наряд in that same row — and the operator saw a hybrid ("partially
    my test work"). Reviving the archived order must reset it to the new work's
    shape completely: source, status, and every stale field of the old kind.
    """
    from datetime import datetime

    session = make_session()
    ghost = Order(
        source="sheet_client", sheet_tab="26.08.26", row_number=63,
        client_name="TESTCLIENT", material_color="TEST", quantity="1",
        status="нове", archived_at=datetime(2026, 8, 25, 9, 0),
    )
    session.add(ghost)
    session.commit()

    # Admin writes a normal lab work (наряд in col B) into that same row.
    sync_tab(session, "26.08.26", [make_row(
        row_number=63, work_order_no="28500", material_color="цирконій",
        kind="абатмент", quantity="3", technician_name="Денис",
        job_code="", sum3d_id="", calculated="", milled="",
    )])
    session.commit()
    session.refresh(ghost)

    assert ghost.archived_at is None
    assert ghost.source == "lab"           # not the stale sheet_client
    assert ghost.work_order_no == "28500"
    assert ghost.material_color == "цирконій"
    assert ghost.quantity == "3"
    assert ghost.client_name is None        # the old client name must be gone


def test_row_reused_lab_to_client_also_fully_replaces():
    """Mirror of the hybrid bug the other way: a deleted LAB row reused for a
    client work must not keep the old наряд/технік."""
    from datetime import datetime

    session = make_session()
    ghost = Order(
        source="lab", sheet_tab="26.08.26", row_number=64,
        work_order_no="28400", technician_name="Юля", material_color="титан",
        kind="абатмент", status="прораховано", archived_at=datetime(2026, 8, 25, 9, 0),
    )
    session.add(ghost)
    session.commit()

    sync_tab(session, "26.08.26", [make_client_row(
        row_number=64, kind="Басараб", material_color="mono a3", quantity="2",
        sum3d_id="", calculated="", milled="",
    )])
    session.commit()
    session.refresh(ghost)

    assert ghost.archived_at is None
    assert ghost.source == "sheet_client"
    assert ghost.client_name == "Басараб"
    assert ghost.work_order_no is None      # old наряд gone
    assert ghost.technician_name is None     # old технік gone
    assert ghost.kind is None                # "вид" column held the client name


def test_active_hybrid_order_self_heals_on_next_sync():
    """Self-heal for rows the 0.3.6–0.3.9 resurrect bug already corrupted: an
    ACTIVE order (already un-archived) whose source no longer matches the row's
    kind is reset on the next sync, without needing another delete cycle."""
    session = make_session()
    hybrid = Order(
        source="sheet_client", sheet_tab="26.08.26", row_number=30,
        client_name="TEST", work_order_no="28500", material_color="цирконій",
        kind="абатмент", status="нове", archived_at=None,  # already active
    )
    session.add(hybrid)
    session.commit()

    # The row genuinely holds a lab наряд now.
    sync_tab(session, "26.08.26", [make_row(
        row_number=30, work_order_no="28500", material_color="цирконій",
        kind="абатмент", quantity="1", job_code="", sum3d_id="",
        calculated="", milled="", technician_name="",
    )])
    session.commit()
    session.refresh(hybrid)

    assert hybrid.source == "lab"
    assert hybrid.client_name is None
    assert hybrid.work_order_no == "28500"


def test_two_naryads_deleted_together_both_archive():
    """Rома 25.08.26: two наряди added to a tab, both deleted from the sheet,
    but only ONE left the CRM. Clean flow — distinct rows — must archive both."""
    session = make_session()
    sync_tab(session, "26.08.26", [
        make_row(row_number=1, work_order_no="28601"),
        make_row(row_number=2, work_order_no="28602"),
    ], raw_row_count=10)
    session.commit()
    age_orders(session)
    assert sum(o.archived_at is None for o in session.scalars(select(Order))) == 2

    # Both cleared in the sheet → tab parses empty, but the read worked (headers).
    sync_tab(session, "26.08.26", [], raw_row_count=6)
    session.commit()

    active = [o for o in session.scalars(select(Order)) if o.archived_at is None]
    assert active == [], f"обидва мали піти в архів, лишилось: {[o.work_order_no for o in active]}"


def test_duplicate_row_numbers_do_not_hide_an_order_from_deletion():
    """Root-cause guard: two orders sharing a row_number (left by the earlier
    manual-add overwrite bug) must BOTH be reconciled. Keying the preload by
    row_number silently dropped one, so it was never archived — the '2 deleted,
    1 stayed' report."""
    from datetime import datetime, timedelta
    session = make_session()
    old = datetime.utcnow() - timedelta(minutes=10)
    for naryad in ("28601", "28602"):
        session.add(Order(
            source="lab", sheet_tab="26.08.26", row_number=1,  # SAME row_number
            work_order_no=naryad, material_color="цирконій", status="нове",
            created_at=old,
        ))
    session.commit()

    sync_tab(session, "26.08.26", [], raw_row_count=6)
    session.commit()

    active = [o for o in session.scalars(select(Order)) if o.archived_at is None]
    assert active == [], f"обидва мали піти в архів, лишилось: {[o.work_order_no for o in active]}"


def test_recently_imported_then_deleted_still_stuck_within_grace():
    """Reproduce Rома's 'delete from sheet, stays in CRM, manual sync no help':
    a наряд imported seconds ago and then deleted is inside the 120s deletion
    grace, so reconciliation skips it — and a manual sync within that window
    keeps skipping. Demonstrates the grace is the blocker for sheet-native
    works the operator deleted deliberately."""
    session = make_session()
    # Fresh import — created_at is NOW (within grace).
    sync_tab(session, "26.08.26", [make_row(row_number=1, work_order_no="28700")],
             raw_row_count=10)
    session.commit()

    # Deleted in the sheet moments later; operator hits sync.
    sync_tab(session, "26.08.26", [], raw_row_count=6)
    session.commit()

    order = session.scalar(select(Order))
    # Background sync keeps the grace, so a just-imported+deleted order survives.
    assert order.archived_at is None

    # A MANUAL sync bypasses the grace (deletion_grace_seconds=0) — the operator
    # deleted the row and asked to reconcile now.
    sync_tab(session, "26.08.26", [], raw_row_count=6, deletion_grace_seconds=0)
    session.commit()
    assert session.scalar(select(Order)).archived_at is not None


def test_empty_sheet_sum3d_clears_the_db_value_so_work_is_takeable_again():
    """The sheet is the source of truth for Sum3D. "Можна брати" (takeable) is
    exactly job_code present + Sum3D EMPTY, so when staff clear column L in the
    sheet to hand a work back to the queue, the next sync MUST clear the DB value
    too — otherwise the work stays stuck in "В роботі" and disappears from the
    takeable list. Regression guard for the 0.3.15 fill-only bug."""
    with make_session() as session:
        # Imported with a Sum3D (operator had taken it) → "В роботі".
        sync_tab(session, "T", [make_row(sum3d_id="12-01-45", calculated="", milled="")])
        session.commit()
        order = session.scalar(select(Order))
        assert order.sum3d_id == "12-01-45"

        # Staff clear column L in the sheet to return it to the queue.
        sync_tab(session, "T", [make_row(sum3d_id="", calculated="", milled="")])
        session.commit()
        session.refresh(order)
        assert order.sum3d_id is None  # cleared → job_code + no Sum3D = "можна брати"


def test_empty_sheet_sum3d_clears_a_client_row_too():
    """Same source-of-truth rule for наряд-less client rows."""
    with make_session() as session:
        sync_tab(session, "T", [make_client_row(row_number=5, sum3d_id="10-19-48")])
        session.commit()
        order = session.scalar(select(Order).where(Order.source == "sheet_client"))
        assert order.sum3d_id == "10-19-48"

        sync_tab(session, "T", [make_client_row(row_number=5, sum3d_id="")])
        session.commit()
        session.refresh(order)
        assert order.sum3d_id is None


def test_sheet_fills_and_changes_sum3d():
    """A non-empty sheet value sets Sum3D (first fill) and a later non-empty value
    updates it (a genuine correction)."""
    with make_session() as session:
        sync_tab(session, "T", [make_row(sum3d_id="")])
        session.commit()
        order = session.scalar(select(Order))
        assert order.sum3d_id is None
        sync_tab(session, "T", [make_row(sum3d_id="12-01-45")])
        session.commit()
        session.refresh(order)
        assert order.sum3d_id == "12-01-45"
        sync_tab(session, "T", [make_row(sum3d_id="17-55-28")])
        session.commit()
        session.refresh(order)
        assert order.sum3d_id == "17-55-28"


def test_naryadless_lab_work_imports_as_lab_not_a_fake_client():
    """A technician recorded a work before its наряд was assigned (admins away):
    no наряд, but a technician + вид + material. It must enter the queue as a LAB
    work — not be dropped (lost work) and not become a client named after its
    "вид" (анатомія). The наряд fills in on a later sync."""
    with make_session() as session:
        sync_tab(session, "T", [make_row(
            row_number=1, work_order_no="", technician_name="Іван",
            kind="анатомія", material_color="цирконій A2", sum3d_id="12-01-45",
            calculated="", milled="",
        )])
        session.commit()
        order = session.scalar(select(Order))
        assert order is not None  # not dropped
        assert order.source == "lab"
        assert order.client_name is None  # not a fake client
        assert order.work_order_no is None  # наряд not assigned yet
        assert order.kind == "анатомія"
        assert order.technician_name == "Іван"
        assert order.sum3d_id == "12-01-45"


def test_naryadless_lab_work_gains_its_naryad_without_duplicating():
    """When the наряд is finally assigned, the same order gains it — no second
    order, no lost Sum3D/status."""
    with make_session() as session:
        base = dict(row_number=1, work_order_no="", technician_name="Іван",
                    kind="анатомія", material_color="цирконій A2",
                    sum3d_id="12-01-45", calculated="", milled="")
        sync_tab(session, "T", [make_row(**base)])
        session.commit()
        age_orders(session)

        sync_tab(session, "T", [make_row(**{**base, "work_order_no": "24555"})],
                 deletion_grace_seconds=0)
        session.commit()

        orders = list(session.scalars(select(Order)))
        assert len(orders) == 1  # same order, not a duplicate
        assert orders[0].work_order_no == "24555"
        assert orders[0].sum3d_id == "12-01-45"


def test_clearing_naryad_on_an_active_lab_work_keeps_it_lab():
    """Clearing the наряд cell on an in-progress lab work (leaving технік + вид +
    material) must NOT flip it into a fake client and must NOT reset its status.
    Regression for the misclassification the client heuristic used to cause."""
    with make_session() as session:
        sync_tab(session, "T", [make_row(
            row_number=1, work_order_no="24122", technician_name="Технік",
            kind="анатомія", material_color="моно A2", sum3d_id="12-01-45",
            calculated="", milled="",
        )])
        session.commit()
        age_orders(session)
        order = session.scalar(select(Order))
        order.status = "прораховано"
        session.commit()

        # наряд cell cleared; технік + вид + material remain (no mill markers,
        # so the sheet reports no new progress — status must simply be preserved).
        sync_tab(session, "T", [make_row(
            row_number=1, work_order_no="", technician_name="Технік",
            kind="анатомія", material_color="моно A2", sum3d_id="12-01-45",
            calculated="", milled="",
        )], deletion_grace_seconds=0)
        session.commit()
        session.refresh(order)

        assert order.source == "lab"  # not flipped to sheet_client
        assert order.client_name is None
        assert order.status == "прораховано"  # progress not lost
        assert order.sum3d_id == "12-01-45"
        assert order.archived_at is None



def test_mistakenly_archived_work_returns_when_its_row_is_still_in_the_sheet():
    """Бойовий випадок 30.08.26: один тік синку заархівував 17 клієнтських
    робіт, які нікуди з таблиці не зникали (обірване читання), і ЖОДЕН
    наступний синк їх не повертав — гілка воскресіння спрацьовувала лише коли
    в рядку ІНША робота. Помилка ставала вічною.

    Тепер та сама робота, що стоїть у таблиці при архівованому замовленні,
    повертається з архіву — але лише коли архівації більше 10 хвилин, щоб не
    зламати гонку з бланкером (попередній тест)."""
    from datetime import datetime, timedelta

    session = make_session()
    order = Order(
        source="sheet_client", sheet_tab="27.08.26", row_number=61,
        client_name="Неда", material_color="mono a2", quantity="2",
        status="нове",
        archived_at=datetime.utcnow() - timedelta(hours=2),
    )
    session.add(order)
    session.commit()

    sync_tab(session, "27.08.26", [make_row(
        row_number=61, work_order_no="", material_color="mono a2",
        kind="Неда", quantity="2", job_code="", sum3d_id="",
        calculated="", milled="", technician_name="",
    )])
    session.commit()

    revived = session.get(Order, order.id)
    assert revived.archived_at is None, "робота, що стоїть у таблиці, не має жити в архіві"
    assert revived.client_name == "Неда"


def test_one_bad_read_cannot_archive_a_quarter_of_the_tab():
    """Другий кінець того ж бойового випадку: техніки чистять рядки по
    одному-два, а «зникнення» чверті вкладки за один тік — це майже напевно
    погане читання. Такий тік мусить пропустити архівацію цілком і голосно
    сказати про це, а не зняти з черги живі роботи."""
    session = make_session()
    for i in range(20):
        session.add(Order(
            source="lab", sheet_tab="27.08.26", row_number=10 + i,
            work_order_no=str(29000 + i), material_color="mono a3", status="нове",
        ))
    session.commit()

    # «Читання» повернуло лише перші 8 рядків із 20 — обрив на середині.
    partial = [make_row(
        row_number=10 + i, work_order_no=str(29000 + i), material_color="mono a3",
        kind="анатомія", quantity="1", job_code="", sum3d_id="",
        calculated="", milled="",
    ) for i in range(8)]
    result = sync_tab(session, "27.08.26", partial, deletion_grace_seconds=0)
    session.commit()

    assert result.deleted == 0, "масове зникнення не має архівувати нікого"
    still_active = session.query(Order).filter(Order.archived_at.is_(None)).count()
    assert still_active == 20

    # А звичайне точкове видалення (1 рядок із 20) працює як і працювало.
    normal = [make_row(
        row_number=10 + i, work_order_no=str(29000 + i), material_color="mono a3",
        kind="анатомія", quantity="1", job_code="", sum3d_id="",
        calculated="", milled="",
    ) for i in range(19)]
    result = sync_tab(session, "27.08.26", normal, deletion_grace_seconds=0)
    session.commit()
    assert result.deleted == 1


def test_force_reconcile_archives_a_confirmed_bulk_deletion():
    """Оператор СВІДОМО видалив пачку тестових рядків і підтвердив «звірити
    видалення» — обірване читання й справжнє масове видалення з одного читання
    не розрізнити, тому за явним підтвердженням поріг обходиться й рядки таки
    архівуються. Без force_reconcile той самий синк поріг тримає (тест вище).

    Бойовий сценарій 31.08.26: 34 клієнтські тестові рядки, видалені з аркуша,
    зависли в черзі, бо кожен фоновий/ручний тік бачив ті самі 40% «зниклих»."""
    session = make_session()
    for i in range(20):
        session.add(Order(
            source="sheet_client", sheet_tab="27.08.26", row_number=10 + i,
            client_name=f"Тест{i}", material_color="mono b1", status="нове",
        ))
    session.commit()

    # У таблиці лишилось лише 8 рядків із 20 — решту оператор видалив навмисне.
    remaining = [make_row(
        row_number=10 + i, work_order_no="", material_color="mono b1",
        kind=f"Тест{i}", quantity="1", job_code="", sum3d_id="",
        calculated="", milled="", technician_name="",
    ) for i in range(8)]

    # Звичайний синк — поріг тримає, нічого не архівує.
    guarded = sync_tab(session, "27.08.26", remaining, deletion_grace_seconds=0)
    session.commit()
    assert guarded.deleted == 0
    assert session.query(Order).filter(Order.archived_at.is_(None)).count() == 20

    # Підтверджена звірка — поріг обходиться, 12 видалених архівуються.
    forced = sync_tab(
        session, "27.08.26", remaining, deletion_grace_seconds=0, force_reconcile=True
    )
    session.commit()
    assert forced.deleted == 12
    active = session.query(Order).filter(Order.archived_at.is_(None)).all()
    assert len(active) == 8
    assert {o.client_name for o in active} == {f"Тест{i}" for i in range(8)}
