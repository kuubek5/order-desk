"""Глушник повторів у лозі: перший раз чути, далі рідше, замовкати назовсім не можна."""

from app import log_throttle


def test_first_time_speaks_and_repeat_stays_quiet():
    assert log_throttle.due("k") == 0
    assert log_throttle.due("k") is None
    assert log_throttle.due("k") is None


def test_after_the_window_it_speaks_again_and_says_how_many_were_skipped():
    assert log_throttle.due("k", every_seconds=0) == 0
    log_throttle.due("k")          # пропущено 1
    log_throttle.due("k")          # пропущено 2
    # Вікно минуло — говоримо знову й повідомляємо, скільки промовчали.
    assert log_throttle.due("k", every_seconds=0) == 2
    # Лічильник пропущених починається заново.
    assert log_throttle.due("k", every_seconds=0) == 0


def test_keys_do_not_glue_together():
    """Один зіпсований верстат не має глушити попередження про сусідній."""
    assert log_throttle.due("machines.calib_full:192.168.1.27-8765") == 0
    assert log_throttle.due("machines.calib_full:192.168.1.81-8765") == 0
    assert log_throttle.due("machines.calib_full:192.168.1.27-8765") is None


def test_clear_makes_the_next_event_speak_immediately():
    """Стан минув (теку звільнили) — наступна така подія вже нова."""
    assert log_throttle.due("k") == 0
    assert log_throttle.due("k") is None
    log_throttle.clear("k")
    assert log_throttle.due("k") == 0
