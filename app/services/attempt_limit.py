"""Лічильник невдалих спроб у пам'яті процесу — для входу і ПІНа «Виробітку».

Навмисно без БД і без блокування акаунта: на спільному цеховому ПК помилитись
може сам оператор, тому пауза мусить минати сама, а перезапуск застосунку —
скидати все. Мета — прибрати перебір (4-значний ПІН підбирався скриптом за
секунди), а не покарати за одруківку.

Потоко-безпечний: фоновий синк і веб-запити живуть у різних потоках.
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    failures: int = 0
    blocked_until: float = 0.0
    last_failure: float = 0.0


@dataclass
class AttemptLimiter:
    """Ковзний лічильник: `free_attempts` вільних спроб, далі пауза.

    `block_seconds` — пауза після перевищення; `reset_after_seconds` — час
    тиші, після якого лічильник забуває старі невдачі (одна одруківка вранці
    не має додаватись до одруківки ввечері).
    """

    free_attempts: int = 5
    block_seconds: float = 300.0
    reset_after_seconds: float = 900.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def retry_after(self, key: str) -> int:
        """Скільки секунд лишилось чекати; 0 — можна пробувати."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return 0
            if bucket.blocked_until > now:
                return int(bucket.blocked_until - now) + 1
            if now - bucket.last_failure > self.reset_after_seconds:
                self._buckets.pop(key, None)
            return 0

    def register_failure(self, key: str) -> int:
        """Порахувати невдачу. Повертає секунди паузи (0 — паузи ще нема)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or now - bucket.last_failure > self.reset_after_seconds:
                bucket = _Bucket()
                self._buckets[key] = bucket
            bucket.failures += 1
            bucket.last_failure = now
            if bucket.failures > self.free_attempts:
                bucket.blocked_until = now + self.block_seconds
                return int(self.block_seconds)
            return 0

    def reset(self, key: str) -> None:
        """Успішний вхід стирає історію невдач."""
        with self._lock:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


def block_message(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"Забагато невдалих спроб. Спробуйте за {minutes} хв."


# Вхід у застосунок: ключ = логін + IP.
login_limiter = AttemptLimiter(free_attempts=5, block_seconds=300.0)
# ПІН «Виробітку»: він захищає зарплатні цифри від самих операторів, тож
# вільних спроб менше.
pin_limiter = AttemptLimiter(free_attempts=5, block_seconds=300.0)
# Форма зворотного зв'язку: захищає не таємницю, а диск і Telegram — до
# звернення можна причепити скріншоти, і цикл у скрипті (чи просто затиснута
# кнопка) забив би шару картинками й вичерпав ліміт бота. Вільних спроб більше:
# це робочий інструмент, а не гейт (ревʼю 07.09.26, K.9).
feedback_limiter = AttemptLimiter(free_attempts=20, block_seconds=120.0)
