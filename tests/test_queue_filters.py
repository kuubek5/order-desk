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
        job_code_folder_uri: str | None = None,
        export_folder_uri: str | None = None,
    ):
        self.job_code = job_code
        self.job_code_folder_uri = job_code_folder_uri
        self.export_folder_uri = export_folder_uri


def test_filter_by_readiness_all_returns_everything_unchanged():
    orders = [_FakeOrder(job_code_folder_uri="file:///ready"), _FakeOrder()]

    result = filter_by_readiness(orders, "all")

    assert result == orders


def test_filter_by_readiness_ready_keeps_only_orders_with_resolved_folder():
    ready_order = _FakeOrder(job_code_folder_uri="file:///ready")
    not_ready_order = _FakeOrder("2026-07-21_00016-007")

    result = filter_by_readiness([ready_order, not_ready_order], "ready")

    assert result == [ready_order]


def test_filter_by_readiness_not_ready_keeps_only_orders_without_resolved_folder():
    ready_order = _FakeOrder(export_folder_uri="file:///mail-ready")
    not_ready_order = _FakeOrder("2026-07-21_00016-007")

    result = filter_by_readiness([ready_order, not_ready_order], "not_ready")

    assert result == [not_ready_order]


def test_filter_by_readiness_treats_unresolved_job_code_as_not_ready():
    order = _FakeOrder("")

    assert filter_by_readiness([order], "ready") == []
    assert filter_by_readiness([order], "not_ready") == [order]


def test_filter_by_readiness_unknown_value_behaves_like_all():
    orders = [_FakeOrder(job_code_folder_uri="file:///ready"), _FakeOrder()]

    result = filter_by_readiness(orders, "bogus")

    assert result == orders


def test_count_by_readiness_splits_correctly():
    orders = [
        _FakeOrder(job_code_folder_uri="file:///ready-1"),
        _FakeOrder(export_folder_uri="file:///ready-2"),
        _FakeOrder("2026-07-21_00018-001"),
    ]

    counts = count_by_readiness(orders)

    assert counts == {"all": 3, "ready": 2, "not_ready": 1}


def test_count_by_readiness_empty_list():
    assert count_by_readiness([]) == {"all": 0, "ready": 0, "not_ready": 0}


def test_filter_by_source_email_keeps_only_email_orders():
    lab = SimpleNamespace(source="lab")
    email = SimpleNamespace(source="email")

    assert filter_by_source([lab, email], "email") == [email]


def test_filter_by_source_lab_keeps_only_lab_orders():
    lab = SimpleNamespace(source="lab")
    email = SimpleNamespace(source="email")

    assert filter_by_source([lab, email], "lab") == [lab]


def test_filter_by_source_all_and_unknown_return_everything():
    orders = [SimpleNamespace(source="lab"), SimpleNamespace(source="email")]

    assert filter_by_source(orders, "all") == orders
    assert filter_by_source(orders, "bogus") == orders


def test_count_by_source_splits_known_sources():
    orders = [
        SimpleNamespace(source="lab"),
        SimpleNamespace(source="email"),
        SimpleNamespace(source="lab"),
    ]

    assert count_by_source(orders) == {"all": 3, "lab": 2, "email": 1}


def test_count_by_source_empty_list():
    assert count_by_source([]) == {"all": 0, "lab": 0, "email": 0}


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
