"""Робота на верстаті за Sum3D — свіжі збіги, якщо вони є (01.10.26).

Sum3D ID — лише час доби й повторюється між днями. Табло цеху показало на
150i-Olejka (програма `23-21-26`) сьогоднішнього Середюка (×2, вкладка
01.10.26) і Ковальчука з 01.09.26 — роботу, яка давно видана, але неархівна.
"""

from datetime import date, datetime
from types import SimpleNamespace

from app.services.machines import SCREEN_PROGRAM_DAYS_BACK, _prefer_recent

TODAY = date(2026, 10, 1)


def _o(oid, tab, client, status="прораховано"):
    return SimpleNamespace(id=oid, sheet_tab=tab, created_at=datetime(2026, 1, 1), client_name=client, status=status)


def test_shop_case_found_work_inside_window_is_dropped():
    """17.11.26, 350i_Titan, програма `20-56-55`: лабораторна 40378 (вкладка
    17.11) і Zahar Pushkar з 12.11 — п'ять днів, у вікні, але вже знайдена
    при видачі. На табло мала лишитись тільки лабораторна."""
    orders = [_o(8241, "12.11.26", "Zahar Pushkar", "знайдено при видачі"), _o(8547, "17.11.26", None)]
    assert [o.id for o in _prefer_recent(orders, date(2026, 11, 17))] == [8547]


def test_issued_namesake_is_dropped_too():
    orders = [_o(1, "16.11.26", "А", "видано"), _o(2, "17.11.26", "Б", "у фрезеруванні")]
    assert [o.id for o in _prefer_recent(orders, date(2026, 11, 17))] == [2]


def test_only_finished_candidates_are_kept_as_before():
    """Усі кандидати вже знайдені/видані — показуємо їх, як і без правила."""
    orders = [_o(1, "16.11.26", "А", "видано"), _o(2, "17.11.26", "Б", "знайдено при видачі")]
    assert [o.id for o in _prefer_recent(orders, date(2026, 11, 17))] == [1, 2]


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
