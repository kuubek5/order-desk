"""Гонка навколо _viewed_days: воркер таблиці ітерує словник, поки
request-потоки в нього вставляють переглянуті дні.

До фіксу (28.08.26) `_hot_extra_days` сортував живий генератор
`_viewed_days.items()`, і вставка з іншого потоку валила тік синку на
«RuntimeError: dictionary changed size during iteration». Рідко (2 оператори,
тік 5с), тому й не ловилось юніт-тестом — лише стрес під конкуренцією.
"""

import threading
import time
from datetime import date, timedelta

import app.web as web
from app.sync_control import record_viewed_day


def test_no_dict_changed_size_under_concurrent_access():
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                record_viewed_day(date(2026, 8, 1) + timedelta(days=i % 400))
                i += 1
            except Exception as exc:  # noqa: BLE001 — фіксуємо будь-яку гонку
                errors.append(("writer", repr(exc)))
                return

    def reader():
        while not stop.is_set():
            try:
                web._hot_extra_days()
            except Exception as exc:  # noqa: BLE001
                errors.append(("reader", repr(exc)))
                return

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join()

    assert not errors, errors
