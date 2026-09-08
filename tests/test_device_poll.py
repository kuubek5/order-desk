"""Покинуті знімки не накопичуються: один на пристрій, пул один на застосунок.

Спільний строк на тік (аудит 08.09.26) прибрав головну біду — мертва піч більше
не тримає живих сусідів. Але сам по собі він тягне нову: кинути повільний
знімок недосить, бо потік, у якому той живе, доживає до власних 20 секунд. При
тіку раз на 6 секунд це ~3 покинуті потоки на КОЖНУ мертву піч, тобто 13 на
чотири; кожен тримає сокет, і всі вони затримують вимкнення застосунку, бо
`ThreadPoolExecutor` чекає на свої потоки при виході.

Ці тести стережуть обидва правила, якими та проблема закрита.
"""

import threading
import time

from app.services.device_poll import DevicePoller


def test_a_slow_device_does_not_hold_the_tick():
    poller = DevicePoller("test", max_workers=4)
    release = threading.Event()

    def job(key):
        if key == "мертвий":
            release.wait(10)
            return "пізно"
        return f"ок:{key}"

    started = time.monotonic()
    done = poller.gather(["живий", "мертвий"], job, deadline=0.3)
    elapsed = time.monotonic() - started

    assert elapsed < 3, f"тік чекав на мертвий пристрій {elapsed:.1f}с"
    assert done == {"живий": "ок:живий"}, "мертвий пристрій не має потрапити у відповідь"
    release.set()
    poller.shutdown()


def test_a_device_already_in_flight_is_not_asked_twice():
    """ГОЛОВНЕ правило. Без нього кожен тік ставив би в чергу ще один знімок
    того самого мертвого сокета, і покинуті потоки накопичувались би."""
    poller = DevicePoller("test", max_workers=4)
    release = threading.Event()
    calls = []

    def job(key):
        calls.append(key)
        release.wait(10)
        return "пізно"

    poller.gather(["мертвий"], job, deadline=0.2)
    assert poller.in_flight_count() == 1

    for _ in range(5):        # ще пʼять тіків поспіль, поки перший висить
        poller.gather(["мертвий"], job, deadline=0.2)

    assert len(calls) == 1, (
        f"знімок мертвого пристрою поставили {len(calls)} разів — покинуті "
        "потоки накопичуються"
    )
    assert poller.in_flight_count() == 1
    release.set()
    poller.shutdown()


def test_the_device_is_asked_again_once_it_answers():
    """Пропуск діє РІВНО поки знімок висить, інакше пристрій, що ожив, лишився
    б непитаним назавжди."""
    poller = DevicePoller("test", max_workers=2)
    calls = []

    def job(key):
        calls.append(key)
        return "ок"

    assert poller.gather(["піч"], job, deadline=1) == {"піч": "ок"}
    assert poller.gather(["піч"], job, deadline=1) == {"піч": "ок"}
    assert len(calls) == 2
    poller.shutdown()


def test_live_neighbours_keep_working_while_one_is_stuck():
    """Мертвий не має забирати місце в пулі в живих — саме заради цього
    пропуск і потрібен."""
    poller = DevicePoller("test", max_workers=2)
    release = threading.Event()

    def job(key):
        if key == "мертвий":
            release.wait(10)
            return "пізно"
        return f"ок:{key}"

    poller.gather(["мертвий"], job, deadline=0.2)
    for _ in range(3):
        done = poller.gather(["мертвий", "живий"], job, deadline=1)
        assert done.get("живий") == "ок:живий", "живий пристрій перестав опитуватись"

    release.set()
    poller.shutdown()


def test_shutdown_does_not_wait_for_a_stuck_capture():
    """Чекати на мертвий сокет означало б тримати вимкнення застосунку до 20 с
    на пристрій — та сама хвороба, що вже лікувалась у пулі запису в таблицю."""
    poller = DevicePoller("test", max_workers=2)
    release = threading.Event()

    poller.gather(["мертвий"], lambda k: release.wait(10), deadline=0.2)

    started = time.monotonic()
    poller.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"зупинка пулу чекала на страглера {elapsed:.1f}с"
    release.set()
