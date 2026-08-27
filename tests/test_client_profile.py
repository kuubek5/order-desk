"""Tests for app/client_profile.py — read-time fuzzy matching of Client to Order rows."""

from datetime import datetime
from types import SimpleNamespace

from app.client_profile import (
    count_matching_orders,
    find_matching_orders,
    index_orders_by_name,
    summarize_client_orders,
)


def make_order(client_name, material_color=None, created_at=None):
    return SimpleNamespace(
        id=1,
        client_name=client_name,
        material_color=material_color,
        created_at=created_at or datetime(2026, 1, 1),
    )


class TestFindMatchingOrders:
    def test_exact_match(self):
        orders = [make_order("Литвиненко Олег"), make_order("Хтось Інший")]
        result = find_matching_orders("Литвиненко Олег", orders)
        assert len(result) == 1
        assert result[0].client_name == "Литвиненко Олег"

    def test_case_and_whitespace_variant_matches(self):
        orders = [
            make_order("литвиненко олег"),
            make_order("Литвиненко Олег "),
            make_order("  ЛИТВИНЕНКО ОЛЕГ"),
        ]
        result = find_matching_orders("Литвиненко Олег", orders)
        assert len(result) == 3

    def test_no_match_for_different_name(self):
        orders = [make_order("Зовсім Інша Людина")]
        result = find_matching_orders("Литвиненко Олег", orders)
        assert result == []

    def test_orders_without_client_name_are_skipped(self):
        orders = [make_order(None), make_order("")]
        result = find_matching_orders("Литвиненко Олег", orders)
        assert result == []

    def test_empty_canonical_name_matches_nothing(self):
        orders = [make_order("Литвиненко Олег")]
        assert find_matching_orders("", orders) == []
        assert find_matching_orders("   ", orders) == []

    def test_typo_within_threshold_matches(self):
        orders = [make_order("Литвиненк Олег")]  # one character dropped
        result = find_matching_orders("Литвиненко Олег", orders)
        assert len(result) == 1

    def test_custom_threshold_is_respected(self):
        orders = [make_order("Зовсім не той клієнт")]
        # With threshold 0 everything matches, proving the parameter is used.
        result = find_matching_orders("Литвиненко Олег", orders, threshold=0.0)
        assert len(result) == 1


class TestSummarizeClientOrders:
    def test_empty_orders(self):
        summary = summarize_client_orders([])
        assert summary.total_count == 0
        assert summary.material_breakdown == []
        assert summary.last_order_date is None
        assert summary.recent_orders == []

    def test_counts_and_material_breakdown(self):
        orders = [
            make_order("Вова", material_color="пмма A2"),
            make_order("Вова", material_color="пмма A2"),
            make_order("Вова", material_color="титан корея"),
        ]
        summary = summarize_client_orders(orders)
        assert summary.total_count == 3
        assert summary.material_breakdown[0] == ("пмма A2", 2)

    def test_last_order_date_and_recent_ordering(self):
        orders = [
            make_order("Вова", created_at=datetime(2026, 1, 1)),
            make_order("Вова", created_at=datetime(2026, 3, 1)),
            make_order("Вова", created_at=datetime(2026, 2, 1)),
        ]
        summary = summarize_client_orders(orders)
        assert summary.last_order_date == datetime(2026, 3, 1)
        assert [o.created_at for o in summary.recent_orders] == [
            datetime(2026, 3, 1),
            datetime(2026, 2, 1),
            datetime(2026, 1, 1),
        ]

    def test_recent_orders_capped_at_limit(self):
        orders = [make_order("Вова", created_at=datetime(2026, 1, i)) for i in range(1, 15)]
        summary = summarize_client_orders(orders, recent_limit=5)
        assert len(summary.recent_orders) == 5


class TestCountMatchingOrders:
    """Індексований підрахунок мусить давати те саме число, що й повний прохід —
    інакше екран «Клієнти» показав би не ту кількість робіт."""

    ORDERS = [
        make_order("Литвиненко Олег"),
        make_order("литвиненко олег "),
        make_order("Литвиненко Олег"),
        make_order("Кривовид"),
        make_order("Кривовид кл"),
        make_order(None),
        make_order("Хтось Інший"),
    ]

    def test_matches_full_scan_for_every_name(self):
        index = index_orders_by_name(self.ORDERS)
        for name in ("Литвиненко Олег", "Кривовид", "Хтось Інший", "Невідомий", ""):
            assert count_matching_orders(name, index) == len(
                find_matching_orders(name, self.ORDERS)
            ), name

    def test_orders_without_client_name_are_not_indexed(self):
        index = index_orders_by_name(self.ORDERS)
        assert all(name for name in index)
