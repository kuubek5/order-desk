"""Спільний пул знімків пристроїв: печі й верстати опитуються через нього.

Навіщо окремий модуль. Аудит 08.09.26 дав тіку опитування СПІЛЬНИЙ СТРОК: одна
вимкнена піч більше не тримає живих сусідів (`pool.map` віддавав результат лише
коли договорила остання, тож нічний тік розтягувався з 6 до 20 с). Але саме по
собі це рішення тягне за собою нову проблему, і саме її вирішує цей модуль.

ПРОБЛЕМА СТРАГЛЕРІВ. Кинути повільний знімок недосить — потік, у якому він
живе, нікуди не дівається: він доживає до свого власного дедлайну (20 с). При
тіку раз на 6 секунд це означає, що на КОЖНУ мертву піч у повітрі постійно
висить близько трьох покинутих потоків, а на чотири мертві печі — тринадцять.
Кожен тримає сокет. Гірше: `ThreadPoolExecutor` реєструє свої потоки в
`atexit`, який ЧЕКАЄ на них при виході — тобто стагнуючий знімок затримував би
вимкнення застосунку далеко за дозволені 10 секунд (та сама хвороба, що вже
лікувалась у пулі запису в таблицю).

РІШЕННЯ — два правила:

1. **Один пул на сімʼю пристроїв, а не новий на кожен тік.** Потоки
   переюзуються, пули не плодяться.
2. **Один знімок на пристрій одночасно.** Поки попередній знімок цієї печі ще
   в польоті, новий НЕ ставиться в чергу: другий однаково впреться в той самий
   мертвий сокет, а місце в пулі забере. Пропущений тік для мертвого пристрою
   нічого не коштує — він і так мовчить.

Друге правило заразом робить перше безпечним: без нього черга пулу росла б
швидше, ніж розсмоктується, і живі пристрої чекали б за мертвими.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as PoolTimeout
from typing import Callable, Hashable, Iterable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DevicePoller:
    """Пул знімків для однієї сімʼї пристроїв (печі АБО верстати).

    Живе весь час роботи застосунку. `max_workers` — стеля одночасних знімків;
    брати її більшою за кількість пристроїв немає сенсу.
    """

    def __init__(self, name: str, max_workers: int) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=name
        )
        self._lock = threading.Lock()
        # Ключ пристрою → знімок, який ЩЕ не завершився.
        self._in_flight: dict[Hashable, Future] = {}

    def gather(
        self,
        keys: Iterable[Hashable],
        job: Callable[[Hashable], T],
        *,
        deadline: float,
    ) -> dict[Hashable, T]:
        """Зібрати результати за `deadline` секунд. Хто не встиг — того немає.

        Повертає лише те, що ЗАВЕРШИЛОСЬ вчасно. Пристрій, чий знімок не
        долетів, просто відсутній у відповіді: викликач лишає його попередній
        стан недоторканим. Вигаданих значень звідси не береться ніколи —
        інваріант печей (CLAUDE.md §14).
        """
        keys = list(keys)
        with self._lock:
            self._forget_finished()
            fresh: dict[Hashable, Future] = {}
            for key in keys:
                if key in self._in_flight:
                    # Попередній знімок цього пристрою ще висить у мертвому
                    # сокеті. Другий поставив би в чергу ту саму приреченість і
                    # зайняв би місце, потрібне живим сусідам.
                    logger.debug("Знімок %s ще в польоті — тік пропускає його", key)
                    continue
                future = self._pool.submit(job, key)
                self._in_flight[key] = future
                fresh[key] = future

        done: dict[Hashable, T] = {}
        if not fresh:
            return done
        try:
            for future in as_completed(fresh.values(), timeout=deadline):
                for key, candidate in fresh.items():
                    if candidate is future:
                        done[key] = future.result()
                        break
        except PoolTimeout:
            logger.debug(
                "Тік закрито за строком: встигли %d з %d", len(done), len(fresh)
            )
        return done

    def _forget_finished(self) -> None:
        """Прибрати з обліку знімки, які вже завершились. Викликається під
        локом."""
        for key in [k for k, f in self._in_flight.items() if f.done()]:
            del self._in_flight[key]

    def in_flight_count(self) -> int:
        """Скільки знімків зараз висить. Для тестів і діагностики."""
        with self._lock:
            self._forget_finished()
            return len(self._in_flight)

    def shutdown(self) -> None:
        """Закрити пул, НЕ чекаючи на страглерів.

        `wait=False` тут свідомо: чекати на мертвий сокет означало б тримати
        вимкнення застосунку до 20 с на пристрій.
        """
        self._pool.shutdown(wait=False, cancel_futures=True)
