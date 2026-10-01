"""Робота на верстаті за Sum3D — свіжі збіги, якщо вони є (01.10.26).

Sum3D ID — лише час доби й повторюється між днями. Табло цеху показало на
150i-Olejka (програма `23-21-26`) сьогоднішнього Середюка (×2, вкладка
01.10.26) і Ковальчука з 01.09.26 — роботу, яка давно видана, але неархівна.
"""

from datetime import date, datetime
from types import SimpleNamespace

from app.services.machines import SCREEN_PROGRAM_DAYS_BACK, _prefer_recent

TODAY = date(2026, 10, 1)


def _o(oid, tab, client):
    return SimpleNamespace(id=oid, sheet_tab=tab, created_at=datetime(2026, 1, 1), client_name=client)


def test_shop_case_old_namesake_is_dropped_when_todays_work_exists():
    orders = [_o(5025, "01.10.26", "Середюк"), _o(5024, "01.10.26", "Середюк"), _o(2499, "01.09.26", "Ковальчук")]
    assert [o.id for o in _prefer_recent(orders, TODAY)] == [5025, 5024]


def test_only_old_candidates_are_kept_as_before():
    """Свіжих немає — показуємо давню, як і раніше (не «нічого»)."""
    orders = [_o(2499, "01.09.26", "Ковальчук"), _o(2500, "02.09.26", "Інший")]
    assert [o.id for o in _prefer_recent(orders, TODAY)] == [2499, 2500]


def test_window_edges():
    edge = TODAY.toordinal() - SCREEN_PROGRAM_DAYS_BACK
    in_window = date.fromordinal(edge).strftime("%d.%m.%y")
    orders = [_o(1, in_window, "А"), _o(2, "01.09.26", "Б"), _o(3, "02.10.26", "В")]
    assert [o.id for o in _prefer_recent(orders, TODAY)] == [1, 3]


def test_single_match_is_untouched():
    one = [_o(2499, "01.09.26", "Ковальчук")]
    assert _prefer_recent(one, TODAY) is one
