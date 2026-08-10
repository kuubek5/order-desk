"""Tests for app/stats.py — pure statistics calculations."""

from datetime import datetime
from types import SimpleNamespace

from app.stats import average_new_to_milled_hours, parse_int_safe, summarize_rework_by_blame


def make_order(events):
    return SimpleNamespace(status_events=[SimpleNamespace(status=s, occurred_at=t) for s, t in events])


class TestParseIntSafe:
    def test_valid_number(self):
        assert parse_int_safe("5") == 5

    def test_strips_whitespace(self):
        assert parse_int_safe(" 12 ") == 12

    def test_none_returns_none(self):
        assert parse_int_safe(None) is None

    def test_non_numeric_returns_none(self):
        assert parse_int_safe("абц") is None


class TestSummarizeReworkByBlame:
    def test_groups_and_sums(self):
        records = [
            SimpleNamespace(blame="технік", redo_quantity="2"),
            SimpleNamespace(blame="технік", redo_quantity="3"),
            SimpleNamespace(blame="обладнання", redo_quantity="1"),
        ]
        result = summarize_rework_by_blame(records)
        by_blame = {g["blame"]: g for g in result}
        assert by_blame["технік"]["count"] == 2
        assert by_blame["технік"]["redo_quantity_sum"] == 5
        assert by_blame["обладнання"]["count"] == 1

    def test_missing_blame_grouped_as_not_specified(self):
        records = [SimpleNamespace(blame=None, redo_quantity="1")]
        result = summarize_rework_by_blame(records)
        assert result[0]["blame"] == "не вказано"

    def test_unparseable_redo_quantity_ignored_in_sum(self):
        records = [SimpleNamespace(blame="клієнт", redo_quantity="невідомо")]
        result = summarize_rework_by_blame(records)
        assert result[0]["redo_quantity_sum"] == 0
        assert result[0]["count"] == 1

    def test_empty_input(self):
        assert summarize_rework_by_blame([]) == []


class TestAverageNewToMilledHours:
    def test_single_order_computes_hours(self):
        order = make_order(
            [
                ("нове", datetime(2026, 7, 1, 8, 0)),
                ("відфрезеровано", datetime(2026, 7, 1, 20, 0)),
            ]
        )
        assert average_new_to_milled_hours([order]) == 12.0

    def test_averages_across_multiple_orders(self):
        order_a = make_order(
            [
                ("нове", datetime(2026, 7, 1, 8, 0)),
                ("відфрезеровано", datetime(2026, 7, 1, 20, 0)),
            ]
        )
        order_b = make_order(
            [
                ("нове", datetime(2026, 7, 2, 8, 0)),
                ("відфрезеровано", datetime(2026, 7, 3, 8, 0)),
            ]
        )
        assert average_new_to_milled_hours([order_a, order_b]) == 18.0

    def test_uses_earliest_occurrence_of_each_status(self):
        order = make_order(
            [
                ("нове", datetime(2026, 7, 2, 8, 0)),
                ("нове", datetime(2026, 7, 1, 8, 0)),
                ("відфрезеровано", datetime(2026, 7, 3, 8, 0)),
                ("відфрезеровано", datetime(2026, 7, 1, 20, 0)),
            ]
        )
        assert average_new_to_milled_hours([order]) == 12.0

    def test_order_missing_one_status_excluded(self):
        order = make_order([("нове", datetime(2026, 7, 1, 8, 0))])
        assert average_new_to_milled_hours([order]) is None

    def test_no_orders_returns_none(self):
        assert average_new_to_milled_hours([]) is None
