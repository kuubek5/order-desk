"""Вимірювання затримок, які відчуває оператор.

Навіщо. Скарга власника 03.09.26 — «перемикання між вкладками ~5 секунд» —
досі не має числа, бо міряти було нічим. У логу лежало рівно два рядки:
«Slow request: GET / took 4.17s» (сума, без розкладки) і розбивка SQL/скан
лише для черги. Решта доданків не мірялась узагалі:

    клік → запит → [сесія · ліцензія · SQL · мережева шара · рендер шаблону]
         → передача → свап HTMX → перемальовка браузером → видно

Оператор відчуває СУМУ. Якщо міряти лише серверну частину, легко побачити
«0.4 с» і не зрозуміти, звідки решта — а на таблиці в кілька сотень рядків
свап і перемальовка коштують стільки ж, скільки весь сервер.

Правило модуля: **вимірювання не має коштувати помітно**. Тут лише
`perf_counter` і словник на запит; жодного IO, жодної бази. Проби тримаються
в кільцевому буфері в пам'яті процесу — вони діагностичні, а не облікові, і
переживати рестарт не мусять.

Читається все на екрані `/diag/perf` (лише адмін), звідки можна скопіювати
текстом. Логи для цього не годяться: прод стоїть на робочому ПК, і «відкрий
файл і знайди рядок» — це не те, що робиться між двома роботами.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

# Скільки проб тримаємо. 400 — це приблизно година активної роботи оператора
# (полл черги раз на 15 с плюс кліки), а важить менше за один кадр печі.
MAX_SAMPLES = 400

# Фази коротші за це в розкладку не потрапляють: інакше корисні рядки тонуть
# у шумі з нулями.
MIN_PHASE_SECONDS = 0.002


@dataclass
class Sample:
    """Одна виміряна взаємодія."""

    method: str
    path: str
    query: str
    status: int
    server_seconds: float
    phases: dict[str, float]
    at: float  # time.time() — для показу «коли»
    note: str = ""
    # Заповнюється клієнтом через /diag/perf/client: те, що сталося ПІСЛЯ
    # відповіді сервера й чого сервер не бачить.
    client: dict[str, float] = field(default_factory=dict)
    request_id: str = ""


class _Recorder:
    """Таймери однієї HTTP-обробки. Живе в contextvar, тому потоки не змішуються."""

    __slots__ = ("phases", "started", "request_id")

    def __init__(self, request_id: str = "") -> None:
        self.phases: dict[str, float] = {}
        self.started = time.perf_counter()
        self.request_id = request_id

    def add(self, name: str, seconds: float) -> None:
        # Складаємо, а не перезаписуємо: та сама фаза може траплятись кілька
        # разів за запит (напр. два звернення до мережевої теки), і цікава
        # саме СУМА — вона і є те, що чекає оператор.
        self.phases[name] = self.phases.get(name, 0.0) + seconds


_current: ContextVar[_Recorder | None] = ContextVar("perf_recorder", default=None)

_lock = threading.Lock()
_samples: list[Sample] = []
_by_request_id: dict[str, Sample] = {}


def start(request_id: str = "") -> _Recorder:
    recorder = _Recorder(request_id)
    _current.set(recorder)
    return recorder


def current_request_id() -> str:
    """Ідентифікатор поточного запиту — щоб сторінка могла назвати себе.

    Потрібен для ЗВИЧАЙНОЇ навігації: там немає XHR із заголовком, тож
    клієнтський лічильник бере id з `<body data-perf-id>`. Поза запитом —
    порожній рядок, і клієнт просто мовчить.
    """
    recorder = _current.get()
    return recorder.request_id if recorder is not None else ""


def finish() -> None:
    _current.set(None)


@contextmanager
def span(name: str) -> Iterator[None]:
    """Заміряти шматок роботи всередині запиту.

    Поза запитом (фоновий воркер, скрипт) — тихий no-op, щоб інструмент можна
    було ставити в спільний код, не думаючи, хто його викликав.
    """
    recorder = _current.get()
    if recorder is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        recorder.add(name, time.perf_counter() - started)


def add(name: str, seconds: float) -> None:
    """Додати вже виміряний шматок (там, де час рахують своїм таймером)."""
    recorder = _current.get()
    if recorder is not None:
        recorder.add(name, seconds)


def mark_route_entry() -> None:
    """Зафіксувати момент, коли керування дійшло до обробника роута.

    Різниця з початком запиту — це весь каркас до нашого коду: ланцюг
    middleware плюс очікування вільного потоку в пулі. На завантаженому ПК
    друге переважає: синхронні сторінки йдуть у threadpool, а фонові воркери
    (синк таблиці, печі, верстати) там же. Оператор бачить «сторінка думає»,
    хоча наш код ще навіть не почався.
    """
    recorder = _current.get()
    if recorder is not None and "before-route" not in recorder.phases:
        recorder.phases["before-route"] = time.perf_counter() - recorder.started


# Службова фаза: не «робота», а точка відліку. Не входить у суму заміряного,
# бо позначає межу, а не відрізок.
ENTRY_PHASE = "before-route"


def note_rows(count: int) -> None:
    """Скільки рядків малюємо. Без цього числа розкладка не читається:
    «рендер 1.2 с» означає різне на 30 і на 600 рядках."""
    add("rows", float(count))


def record(
    *,
    method: str,
    path: str,
    query: str,
    status: int,
    server_seconds: float,
    phases: dict[str, float],
    request_id: str,
    note: str = "",
) -> Sample:
    sample = Sample(
        method=method,
        path=path,
        query=query,
        status=status,
        server_seconds=server_seconds,
        phases={k: v for k, v in phases.items() if v >= MIN_PHASE_SECONDS or k == "rows"},
        at=time.time(),
        note=note,
        request_id=request_id,
    )
    with _lock:
        _samples.append(sample)
        _by_request_id[request_id] = sample
        while len(_samples) > MAX_SAMPLES:
            dropped = _samples.pop(0)
            _by_request_id.pop(dropped.request_id, None)
    return sample


def attach_client(request_id: str, client: dict[str, float]) -> bool:
    """Долучити клієнтські числа до вже записаної серверної проби.

    Клієнт присилає їх ПІСЛЯ того, як усе намальовано, тобто свідомо пізніше
    за серверний запис — тому це окремий крок, а не поле в `record`.
    """
    with _lock:
        sample = _by_request_id.get(request_id)
        if sample is None:
            return False
        sample.client = client
        return True


def samples() -> list[Sample]:
    with _lock:
        return list(_samples)


def clear() -> None:
    with _lock:
        _samples.clear()
        _by_request_id.clear()


def server_timing_header(phases: dict[str, float], total: float) -> str:
    """Заголовок `Server-Timing` — його показує панель «Мережа» в браузері.

    Корисно навіть без нашого екрана: вкладка Network показує розкладку
    поруч із рештою чисел запиту.
    """
    parts = [f"total;dur={total * 1000:.1f}"]
    for name, seconds in phases.items():
        if name == "rows":
            continue
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
        parts.append(f"{safe};dur={seconds * 1000:.1f}")
    return ", ".join(parts)
