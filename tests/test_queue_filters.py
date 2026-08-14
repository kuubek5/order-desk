from types import SimpleNamespace

from app.queue_filters import (
    count_by_readiness,
    count_by_source,
    count_client_groups_by_source,
    filter_by_readiness,
    filter_by_source,
    filter_client_groups_by_source,
    group_has_email_order,
)


class _FakeOrder:
    """Stand-in carrying transient folder attributes attached by web.py."""

    def __init__(
        self,
        job_code: str | None = None,
        *,
        sum3d_id: str | None = None,
        active_rework=None,
        job_code_folder_uri: str | None = None,
        export_folder_uri: str | None = None,
    ):
        self.job_code = job_code
        self.sum3d_id = sum3d_id
        self.active_rework = active_rework
        self.job_code_folder_uri = job_code_folder_uri
        self.export_folder_uri = export_folder_uri


def test_filter_by_readiness_all_returns_everything_unchanged():
    orders = [_FakeOrder("2026-07-21_00016-007"), _FakeOrder()]

    result = filter_by_readiness(orders, "all")

    assert result == orders


def test_filter_by_readiness_can_take_needs_path_and_no_sum3d():
    # «Можна набрати»: technician filled the path, operator has not set a Sum3D
    # ID yet — the job is waiting to be picked up.
    can_take = _FakeOrder("2026-07-21_00016-007")
    in_work = _FakeOrder("2026-07-21_00016-008", sum3d_id="PRJ-1")
    not_ready = _FakeOrder()

    result = filter_by_readiness([can_take, in_work, not_ready], "can_take")

    assert result == [can_take]


def test_filter_by_readiness_in_work_needs_path_and_sum3d():
    # «В роботі»: path filled AND Sum3D ID set — operator already took it.
    can_take = _FakeOrder("2026-07-21_00016-007")
    in_work = _FakeOrder("2026-07-21_00016-008", sum3d_id="PRJ-1")
    not_ready = _FakeOrder()

    result = filter_by_readiness([can_take, in_work, not_ready], "in_work")

    assert result == [in_work]


def test_filter_by_readiness_not_ready_keeps_only_orders_without_job_code():
    ready_order = _FakeOrder("2026-07-21_00016-007")
    not_ready_order = _FakeOrder()

    result = filter_by_readiness([ready_order, not_ready_order], "not_ready")

    assert result == [not_ready_order]


def test_filter_by_readiness_treats_blank_job_code_as_not_ready():
    order = _FakeOrder("   ")

    assert filter_by_readiness([order], "can_take") == []
    assert filter_by_readiness([order], "not_ready") == [order]


def test_filter_by_readiness_blank_sum3d_still_can_take():
    # A whitespace-only Sum3D ID is not a real ID → still «можна набрати».
    order = _FakeOrder("2026-07-21_00016-007", sum3d_id="  ")

    assert filter_by_readiness([order], "can_take") == [order]
    assert filter_by_readiness([order], "in_work") == []


def test_filter_by_readiness_rework_uses_rework_sum3d_not_base():
    # A rework whose redo has NOT been calculated yet (rework.sum3d_id empty)
    # reads as «можна набрати», even though the base run's Sum3D is filled —
    # matches the ID the operator sees in the queue row (column W).
    redo_pending = _FakeOrder(
        "2026-07-20_01939-011",
        sum3d_id="18-23-34",  # base run — must be ignored for a rework
        active_rework=SimpleNamespace(sum3d_id=None),
    )
    redo_taken = _FakeOrder(
        "2026-07-20_01939-012",
        sum3d_id=None,
        active_rework=SimpleNamespace(sum3d_id="20-01-05"),
    )

    assert filter_by_readiness([redo_pending, redo_taken], "can_take") == [redo_pending]
    assert filter_by_readiness([redo_pending, redo_taken], "in_work") == [redo_taken]


def test_filter_by_readiness_ignores_resolved_folder_without_job_code():
    # A resolved on-disk folder is no longer the readiness signal — only a
    # filled job_code is.
    folder_only = _FakeOrder(job_code_folder_uri="file:///ready")

    assert filter_by_readiness([folder_only], "can_take") == []
    assert filter_by_readiness([folder_only], "not_ready") == [folder_only]


def test_filter_by_readiness_unknown_value_behaves_like_all():
    orders = [_FakeOrder("2026-07-21_00016-007"), _FakeOrder()]

    result = filter_by_readiness(orders, "bogus")

    assert result == orders


def test_count_by_readiness_splits_correctly():
    orders = [
        _FakeOrder("2026-07-21_00018-001"),  # can_take
        _FakeOrder("2026-07-21_00018-002", sum3d_id="PRJ-9"),  # in_work
        _FakeOrder(),  # not_ready
    ]

    counts = count_by_readiness(orders)

    assert counts == {"all": 3, "can_take": 1, "in_work": 1, "not_ready": 1}


def test_count_by_readiness_empty_list():
    assert count_by_readiness([]) == {
        "all": 0,
        "can_take": 0,
        "in_work": 0,
        "not_ready": 0,
    }


def test_filter_by_source_client_keeps_email_and_sheet_client_orders():
    lab = SimpleNamespace(source="lab")
    email = SimpleNamespace(source="email")
    sheet_client = SimpleNamespace(source="sheet_client")

    # "Клієнти" groups both email-client sources, lab is excluded.
    assert filter_by_source([lab, email, sheet_client], "client") == [email, sheet_client]


def test_filter_by_source_lab_keeps_only_lab_orders():
    lab = SimpleNamespace(source="lab")
    email = SimpleNamespace(source="email")
    sheet_client = SimpleNamespace(source="sheet_client")

    assert filter_by_source([lab, email, sheet_client], "lab") == [lab]


def test_filter_by_source_all_and_unknown_return_everything():
    orders = [SimpleNamespace(source="lab"), SimpleNamespace(source="email")]

    assert filter_by_source(orders, "all") == orders
    assert filter_by_source(orders, "bogus") == orders


def test_count_by_source_splits_lab_and_client():
    orders = [
        SimpleNamespace(source="lab"),
        SimpleNamespace(source="email"),
        SimpleNamespace(source="sheet_client"),
        SimpleNamespace(source="lab"),
    ]

    assert count_by_source(orders) == {"all": 4, "lab": 2, "client": 2}


def test_count_by_source_empty_list():
    assert count_by_source([]) == {"all": 0, "lab": 0, "client": 0}


def test_group_has_email_order_true_when_any_order_is_from_email():
    orders = [SimpleNamespace(source="lab"), SimpleNamespace(source="email")]

    assert group_has_email_order(orders) is True


def test_group_has_email_order_false_when_no_email_orders():
    orders = [SimpleNamespace(source="lab"), SimpleNamespace(source="lab")]

    assert group_has_email_order(orders) is False


def test_group_has_email_order_false_for_empty_orders():
    assert group_has_email_order([]) is False


def test_filter_client_groups_by_source_email_keeps_only_matching_groups():
    lab_group = {"client_name": "Іван", "orders": [SimpleNamespace(source="lab")]}
    email_group = {"client_name": "Олена", "orders": [SimpleNamespace(source="email")]}
    mixed_group = {
        "client_name": "Петро",
        "orders": [SimpleNamespace(source="lab"), SimpleNamespace(source="email")],
    }

    result = filter_client_groups_by_source([lab_group, email_group, mixed_group], "email")

    assert result == [email_group, mixed_group]


def test_filter_client_groups_by_source_all_and_unknown_return_everything():
    groups = [
        {"client_name": "Іван", "orders": [SimpleNamespace(source="lab")]},
        {"client_name": "Олена", "orders": [SimpleNamespace(source="email")]},
    ]

    assert filter_client_groups_by_source(groups, "all") == groups
    assert filter_client_groups_by_source(groups, "bogus") == groups


def test_count_client_groups_by_source_splits_correctly():
    groups = [
        {"client_name": "Іван", "orders": [SimpleNamespace(source="lab")]},
        {"client_name": "Олена", "orders": [SimpleNamespace(source="email")]},
        {
            "client_name": "Петро",
            "orders": [SimpleNamespace(source="lab"), SimpleNamespace(source="email")],
        },
    ]

    assert count_client_groups_by_source(groups) == {"all": 3, "email": 2}


def test_count_client_groups_by_source_empty_list():
    assert count_client_groups_by_source([]) == {"all": 0, "email": 0}
