"""Верстат змінив IP — знайти його агента в мережі цеху й оновити адресу.

Привід (власник 28.10.26): телевізор «стабільно не показує» якийсь верстат.
250i-Sec мовчав добу, бо роутер видав ПК верстата нову адресу (.58 → .113),
а KuubMill і далі стукав за старою. RustDesk при цьому працював — він
вихідний, тож нічого не підказував. Адреса в Налаштуваннях правилась руками,
і кожен такий випадок був простоєм телевізора до першої скарги.

Як працює. Лише для верстатів із HTTP-агентом (у VNC-верстата немає чим себе
назвати) і лише поки верстат МОВЧИТЬ (`REDISCOVER_AFTER_FAILURES` невдач
поспіль) — не частіше за `REDISCOVER_INTERVAL_SECONDS` на верстат.
Обходимо ту саму /24 мережу, що й стара адреса, і питаємо кожну адресу
`GET /status` з ТОКЕНОМ ЦЬОГО верстата. Токен у кожного агента свій
(випадковий), тож відповідь 200 — це саме наш верстат, а не схожий:
чужий агент відповість 403, порожня адреса — нічим. Самого екрана ніхто не
читає й нічого на верстат не шле.

Пошук — у ВЛАСНОМУ потоці: обхід мережі триває секунди, а опитування решти
верстатів у воркері послідовне (§14 «Стук іде ОКРЕМИМ потоком»). Знахідку
пише в базу вже воркер власною сесією на наступному тіку (`tick`) — той самий
патерн, що `_flush_link_probe`: сесія лишається на одному потоці.

Нова адреса пишеться так само, як її записала б людина в Налаштуваннях
(`Machine.host`, порт не змінюється). Не пишемо, якщо адреса тим часом уже
змінилась руками або її займає ІНШИЙ верстат (два ПК помінялись адресами —
тоді хай вирішує людина, автоматичний обмін двох записів тут гірший за
пропуск). Відкритий запис обриву старої адреси закриваємо з поясненням.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import log_throttle
from app.models import Machine, MachineLinkEvent

logger = logging.getLogger(__name__)

# Хвилина мовчання при тіку 5 с. Менше — і короткий перезапуск ПК чи агента
# (кілька десятків секунд) запускав би обхід мережі даремно.
REDISCOVER_AFTER_FAILURES = 12
# Не частіше разу на 10 хв на верстат: вимкнений на вихідні ПК не має
# смикати всю мережу цеху кожну хвилину.
REDISCOVER_INTERVAL_SECONDS = 600.0
# Локальна мережа відповідає за мілісекунди; мовчазна адреса тримає з'єднання
# до таймауту, тому він короткий, а адрес обходимо багато паралельно.
SCAN_CONNECT_TIMEOUT = 0.5
SCAN_READ_TIMEOUT = 3.0
SCAN_WORKERS = 32

_lock = threading.Lock()
_last_scan: dict[int, float] = {}          # machine_id → monotonic старту обходу
_running: set[int] = set()
# Знахідки, які ще не записані в базу: machine_id → (стара адреса, нова).
_found: dict[int, tuple[str, str]] = {}


def reset_for_tests() -> None:
    with _lock:
        _last_scan.clear()
        _running.clear()
        _found.clear()


def candidate_hosts(host: str, exclude: set[str]) -> list[str]:
    """Усі адреси /24 мережі старої адреси, крім `exclude`. Порожньо — якщо
    адреса не приватний IPv4 (ім'я ПК, публічна): обходити нема що й не можна."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return []
    if ip.version != 4 or not ip.is_private:
        return []
    net = ipaddress.ip_network(f"{host}/24", strict=False)
    return [str(h) for h in net.hosts() if str(h) not in exclude]


def agent_answers(host: str, port: int, token: str) -> bool:
    """Чи на цій адресі агент САМЕ цього верстата: 200 на `/status` з його
    токеном і JSON з `hostname`. Будь-що інше (403 чужого агента, тиша,
    відмова, не JSON) — ні."""
    import requests

    try:
        resp = requests.get(
            f"http://{host}:{port}/status",
            headers={"X-Agent-Token": token},
            timeout=(SCAN_CONNECT_TIMEOUT, SCAN_READ_TIMEOUT),
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data, dict) and "hostname" in data
    except Exception:  # noqa: BLE001 — мовчання/відмова/битий JSON = «не він»
        return False


def scan(
    host: str,
    port: int,
    token: str,
    exclude: set[str],
    probe: Callable[[str, int, str], bool] = agent_answers,
) -> Optional[str]:
    """Обійти мережу й повернути адресу, де відповів агент цього верстата."""
    hosts = candidate_hosts(host, exclude | {host})
    if not hosts:
        return None
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="machine-rediscover") as pool:
        for candidate, ok in zip(hosts, pool.map(lambda h: probe(h, port, token), hosts)):
            if ok:
                return candidate
    return None


def _start_scan(machine_id: int, name: str, host: str, port: int, token: str,
                exclude: set[str], probe: Callable[[str, int, str], bool]) -> None:
    def run() -> None:
        try:
            found = scan(host, port, token, exclude, probe)
        except Exception:  # noqa: BLE001 — пошук не має валити воркер
            logger.exception("Пошук верстата %s у мережі впав", name)
            found = None
        with _lock:
            _running.discard(machine_id)
            if found:
                _found[machine_id] = (host, found)
        if found:
            logger.warning("Верстат %s: агент знайдено за новою адресою %s (була %s)", name, found, host)
        else:
            skipped = log_throttle.due(f"machine-rediscover-miss:{machine_id}")
            if skipped is not None:
                logger.info(
                    "Верстат %s: агента в мережі %s/24 не знайдено — ПК вимкнено або агент не запущено%s",
                    name, host, f" (ще {skipped} разів відтоді)" if skipped else "",
                )

    threading.Thread(target=run, name=f"machine-rediscover-{machine_id}", daemon=True).start()


def _apply_found(db: Session, now: datetime) -> None:
    with _lock:
        found = dict(_found)
        _found.clear()
    for machine_id, (old_host, new_host) in found.items():
        machine = db.get(Machine, machine_id)
        if machine is None or machine.host != old_host:
            continue  # верстат прибрали або адресу вже змінили руками
        clash = db.scalar(
            select(Machine).where(
                Machine.host == new_host, Machine.port == machine.port, Machine.id != machine_id
            )
        )
        if clash is not None:
            logger.warning(
                "Верстат %s знайдено на %s, але цю адресу в Налаштуваннях має «%s» — "
                "адресу не змінено, виправте вручну",
                machine.name, new_host, clash.name,
            )
            continue
        old_key = f"{old_host}-{machine.port}"
        machine.host = new_host
        # Обрив старої адреси закриваємо: верстат не «досі лежить», він
        # переїхав. Інакше запис висів би відкритим до прибирання за 90 днів.
        for event in db.scalars(
            select(MachineLinkEvent).where(
                MachineLinkEvent.host == old_key, MachineLinkEvent.ended_at.is_(None)
            )
        ):
            event.ended_at = now
            event.probe_verdict = f"ПК змінив адресу: {old_host} → {new_host}, адресу оновлено автоматично"
        db.commit()
        log_throttle.clear(f"machine-rediscover-miss:{machine_id}")
        logger.warning(
            "Верстат %s: адресу оновлено автоматично %s → %s (ПК отримав нову IP від роутера)",
            machine.name, old_host, new_host,
        )


def tick(
    db: Session,
    *,
    now: Optional[datetime] = None,
    clock: Callable[[], float] = time.monotonic,
    probe: Callable[[str, int, str], bool] = agent_answers,
) -> None:
    """Один прохід із воркера верстатів, ПІСЛЯ опитування: записати знахідки
    й запустити пошук для верстатів, що мовчать досить довго."""
    from app.services import machines

    now = now or datetime.now()
    _apply_found(db, now)

    states = machines.states_snapshot()
    targets = machines.configured_targets(db)
    # Адреси, де зараз відповідають ІНШІ верстати, — там шукати нема чого.
    alive = {
        t.host for t in targets
        if (s := states.get(t.key)) is not None and s.fail_streak == 0 and s.last_ok_at is not None
    }
    moment = clock()
    for target in targets:
        if not target.is_agent or target.machine_id is None or not target.agent_token:
            continue
        state = states.get(target.key)
        if state is None or state.fail_streak < REDISCOVER_AFTER_FAILURES:
            continue
        with _lock:
            if target.machine_id in _running or target.machine_id in _found:
                continue
            last = _last_scan.get(target.machine_id)
            if last is not None and moment - last < REDISCOVER_INTERVAL_SECONDS:
                continue
            _last_scan[target.machine_id] = moment
            _running.add(target.machine_id)
        _start_scan(
            target.machine_id, target.name, target.host, target.port,
            target.agent_token, alive - {target.host}, probe,
        )
