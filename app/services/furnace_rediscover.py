"""Піч змінила IP — знайти її в мережі цеху за MAC і оновити адресу.

Привід (власник 04.11.26): після вимкнення світла «Бочка» лишилась без кадру з
03:27, а її адреса `.61` відповідає відмовою, тобто там уже живий інший пристрій
або не піднявся VNC. Доступу до роутера у власника немає, тож закріпити адресу
там не можна — шукаємо самі. Той самий випадок, що `machine_rediscover` для
верстатів, але в печі немає агента з токеном, тому тотожність інша:

* **MAC відомий** (записується сам, поки піч читається) — стукаємо в кожну
  адресу /24 (спроба з'єднання заповнює ARP-таблицю Windows, навіть коли порт
  закритий), читаємо `arp -a` і шукаємо цей MAC. MAC на тій самій адресі —
  адреса правильна, не працює VNC на печі; адресу не чіпаємо, пишемо причину.
* **MAC ще невідомий** (перший раз) — адреси, де порт VNC відкритий: знімаємо
  кадр із паролем печі й віримо лише кадру, на якому `read_panel` впізнає табло.
  Рівно одна така адреса — наша. Кілька — не вгадуємо: хибна піч на екрані гірша
  за порожнє поле (§14 «Печі»).

Лише читання пристроїв: кадр по VNC, як у звичайному опитуванні.

Пошук іде у власному потоці й не частіше за `REDISCOVER_INTERVAL_SECONDS`:
вимкнена на ніч піч не має смикати мережу щохвилини. Знахідку пише в базу вже
воркер печей на наступному тіку власною сесією. Разом з адресою переїжджає
історія показань і пам'ять переходів Telegram — вони прив'язані до ключа-адреси.
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import log_throttle
from app.furnace_ocr import STATUS_UNKNOWN, read_panel
from app.furnace_vnc import capture
from app.models import Furnace, FurnaceReading, TelegramWatch
from app.services import furnace as furnace_service
from app.services.machine_rediscover import candidate_hosts
from app.settings_store import get_furnace_vnc_password

logger = logging.getLogger(__name__)

# Хвилина мовчання при тіку 6 с: короткий перезапуск печі пошуку не запускає.
REDISCOVER_AFTER_FAILURES = 10
REDISCOVER_INTERVAL_SECONDS = 600.0
SWEEP_CONNECT_TIMEOUT = 0.5
SWEEP_WORKERS = 32
VERIFY_TIMEOUT_SECONDS = 10.0
VERIFY_WORKERS = 4
# Звірка MAC читаючої печі: раз на годину, щоб помітити заміну карти чи ручну
# правку адреси, але не кликати `arp` на кожен кадр.
MAC_CHECK_INTERVAL_SECONDS = 3600.0
ARP_TIMEOUT_SECONDS = 5.0

_ARP_LINE = re.compile(
    r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})\b"
)

_lock = threading.Lock()
_last_scan: dict[int, float] = {}
_running: set[int] = set()
# furnace_id → (стара адреса, нова, MAC або None)
_found: dict[int, tuple[str, str, Optional[str]]] = {}
_verdicts: dict[int, str] = {}
_streaks: dict[int, tuple[str, int]] = {}
_last_mac_check: dict[int, float] = {}


def reset_for_tests() -> None:
    with _lock:
        _last_scan.clear()
        _running.clear()
        _found.clear()
        _verdicts.clear()
        _streaks.clear()
        _last_mac_check.clear()


def last_verdict(furnace_id: int) -> Optional[str]:
    """Чим закінчився останній пошук — для `kmill_devices`."""
    with _lock:
        return _verdicts.get(furnace_id)


# ── ARP ─────────────────────────────────────────────────────────────────────


def parse_arp(text: str) -> dict[str, str]:
    """Вивід `arp -a` → {адреса: mac}. Широкомовні й групові MAC відкидаються:
    кілька адрес ділять їх, і пічкою такий запис не буває."""
    table: dict[str, str] = {}
    for address, mac in _ARP_LINE.findall(text):
        clean = mac.lower().replace(":", "-")
        if clean == "ff-ff-ff-ff-ff-ff" or clean.startswith("01-00-5e") or clean.startswith("33-33"):
            continue
        table[address] = clean
    return table


def read_arp() -> dict[str, str]:
    """ARP-таблиця Windows. Порожньо — якщо прочитати не вдалось."""
    try:
        result = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            timeout=ARP_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("Таблицю ARP не прочитано: %s", exc)
        return {}
    return parse_arp(result.stdout.decode("ascii", "ignore"))


# ── Пошук ───────────────────────────────────────────────────────────────────


def port_open(host: str, port: int) -> bool:
    """Чи приймає адреса з'єднання. Сама спроба заповнює ARP, навіть при відмові."""
    try:
        with socket.create_connection((host, port), timeout=SWEEP_CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def looks_like_furnace(host: str, port: int, password: Optional[str]) -> bool:
    """Кадр із цієї адреси — табло печі: `read_panel` впізнав статус або
    температуру. Чужий VNC, інший пароль, будь-яка несподіванка — «ні»."""
    try:
        image = capture(host, port, password, timeout=VERIFY_TIMEOUT_SECONDS)
        reading = read_panel(image)
    except Exception as exc:  # noqa: BLE001 — чужий пристрій може відповісти будь-чим
        logger.debug("Адреса %s:%s — не піч: %s", host, port, exc)
        return False
    return reading.status != STATUS_UNKNOWN or reading.temp_c is not None


def search(
    host: str,
    port: int,
    mac: Optional[str],
    password: Optional[str],
    exclude: set[str],
    *,
    is_open: Callable[[str, int], bool] = port_open,
    arp: Callable[[], dict[str, str]] = read_arp,
    is_furnace: Callable[[str, int, Optional[str]], bool] = looks_like_furnace,
) -> tuple[Optional[str], Optional[str], str]:
    """Обійти мережу. Повертає (нова адреса або None, MAC, пояснення людською)."""
    hosts = candidate_hosts(host, exclude)
    if not hosts:
        return None, mac, f"адреса {host} не приватна IPv4 — у мережі не шукаємо"
    with ThreadPoolExecutor(max_workers=SWEEP_WORKERS, thread_name_prefix="furnace-rediscover") as pool:
        opened = [h for h, ok in zip(hosts, pool.map(lambda h: is_open(h, port), hosts)) if ok]

    if mac:
        at = [address for address, seen in arp().items() if seen == mac and address in hosts]
        if not at:
            return None, mac, f"MAC {mac} у мережі не відповідає — піч вимкнена"
        if host in at:
            return None, mac, (
                f"піч на своїй адресі {host} (MAC {mac}), але порт {port} не відповідає — "
                "не запущено VNC на печі, перезапустіть її"
            )
        return at[0], mac, f"MAC {mac} знайдено на {at[0]}"

    candidates = [h for h in opened if h != host]
    if not candidates:
        # Стан самої адреси відрізняє «порожня адреса» від «живий пристрій без VNC».
        seen = arp().get(host)
        own = (
            f"порт {port} відкритий" if host in opened
            else f"порт {port} закритий, пристрій {seen}" if seen
            else "пристрою немає в ARP (адреса порожня)"
        )
        return None, None, (
            f"у мережі {host}/24 немає іншої адреси з відкритим портом {port}; сама {host}: {own}"
        )
    with ThreadPoolExecutor(max_workers=VERIFY_WORKERS, thread_name_prefix="furnace-verify") as pool:
        verdicts = list(pool.map(lambda h: is_furnace(h, port, password), candidates))
    matches = [h for h, ok in zip(candidates, verdicts) if ok]
    if len(matches) > 1:
        return None, None, (
            f"табло печі видно на кількох адресах ({', '.join(matches)}) — не вгадуємо, "
            "впишіть адресу в Налаштуваннях"
        )
    if not matches:
        return None, None, (
            f"з {len(candidates)} адрес із відкритим портом {port} жодна не показала табло печі"
        )
    return matches[0], arp().get(matches[0]), f"табло печі знайдено на {matches[0]}"


def _start_search(
    furnace_id: int, name: str, host: str, port: int, mac: Optional[str],
    password: Optional[str], exclude: set[str],
) -> None:
    def run() -> None:
        try:
            found, seen_mac, verdict = search(host, port, mac, password, exclude)
        except Exception:  # noqa: BLE001 — пошук не має валити воркер
            logger.exception("Пошук печі %s у мережі впав", name)
            found, seen_mac, verdict = None, mac, "пошук упав, див. лог"
        with _lock:
            _running.discard(furnace_id)
            _verdicts[furnace_id] = verdict
            if found:
                _found[furnace_id] = (host, found, seen_mac)
        if found:
            logger.warning("Піч %s: %s (була %s)", name, verdict, host)
            return
        skipped = log_throttle.due(f"furnace-rediscover-miss:{furnace_id}")
        if skipped is not None:
            logger.info("Піч %s не знайдено за новою адресою: %s%s", name, verdict,
                        f" (ще {skipped} разів відтоді)" if skipped else "")

    threading.Thread(target=run, name=f"furnace-rediscover-{furnace_id}", daemon=True).start()


# ── Запис ───────────────────────────────────────────────────────────────────


def _key(host: str, port: int) -> str:
    return furnace_service.FurnaceTarget(name="", host=host, port=port).key


def _apply_found(db: Session) -> None:
    with _lock:
        found = dict(_found)
        _found.clear()
    for furnace_id, (old_host, new_host, mac) in found.items():
        row = db.get(Furnace, furnace_id)
        if row is None or row.host != old_host:
            continue  # піч прибрали або адресу вже змінили руками
        clash = db.scalar(
            select(Furnace).where(
                Furnace.host == new_host, Furnace.port == row.port, Furnace.id != furnace_id
            )
        )
        if clash is not None:
            logger.warning(
                "Піч %s знайдено на %s, але цю адресу в Налаштуваннях має «%s» — "
                "адресу не змінено, виправте вручну", row.name, new_host, clash.name,
            )
            continue
        old_key = _key(old_host, row.port)
        new_key = _key(new_host, row.port)
        row.host = new_host
        if mac:
            row.mac_address = mac
        # Історія й пам'ять переходів прив'язані до ключа-адреси: без переносу
        # графік дня обривався б, а Telegram вважав би піч новою.
        db.execute(update(FurnaceReading).where(FurnaceReading.host == old_key).values(host=new_key))
        old_watch = db.get(TelegramWatch, f"furnace:{old_key}")
        if old_watch is not None and db.get(TelegramWatch, f"furnace:{new_key}") is None:
            old_watch.key = f"furnace:{new_key}"
        db.commit()
        furnace_service.forget_state(old_key)
        log_throttle.clear(f"furnace-rediscover-miss:{furnace_id}")
        logger.warning(
            "Піч %s: адресу оновлено автоматично %s → %s (піч отримала нову IP від роутера)",
            row.name, old_host, new_host,
        )


def _learn_mac(db: Session, row: Furnace, moment: float, arp: Callable[[], dict[str, str]]) -> None:
    """Піч читається — отже адреса її. Запам'ятати MAC; при заміні карти чи
    ручній правці адреси оновити."""
    with _lock:
        last = _last_mac_check.get(row.id)
        if last is not None and moment - last < MAC_CHECK_INTERVAL_SECONDS:
            return
        _last_mac_check[row.id] = moment
    mac = arp().get(row.host)
    if mac and mac != row.mac_address:
        logger.info("Піч %s: MAC %s (був %s)", row.name, mac, row.mac_address or "невідомий")
        row.mac_address = mac
        db.commit()


def tick(
    db: Session,
    *,
    clock: Callable[[], float] = time.monotonic,
    arp: Callable[[], dict[str, str]] = read_arp,
    start: Callable[..., None] = _start_search,
    now: Optional[datetime] = None,
) -> None:
    """Прохід із воркера печей, ПІСЛЯ опитування: записати знахідки, запам'ятати
    MAC тих, що читаються, і запустити пошук для тих, що мовчать досить довго."""
    _apply_found(db)
    rows = furnace_service.list_furnaces(db, only_enabled=True)
    states = furnace_service.states_snapshot()
    moment = clock()
    all_hosts = {row.host for row in rows}
    for row in rows:
        state = states.get(_key(row.host, row.port))
        if state is None or state.attempted_at is None:
            continue
        if state.error is None:
            with _lock:
                _streaks.pop(row.id, None)
                _verdicts.pop(row.id, None)
            reading = state.reading
            if reading is not None and (reading.status != STATUS_UNKNOWN or reading.temp_c is not None):
                _learn_mac(db, row, moment, arp)
            continue
        with _lock:
            seen_at, count = _streaks.get(row.id, ("", 0))
            attempt = state.attempted_at.isoformat()
            if attempt != seen_at:
                count += 1
                _streaks[row.id] = (attempt, count)
            if count < REDISCOVER_AFTER_FAILURES:
                continue
            if row.id in _running or row.id in _found:
                continue
            last = _last_scan.get(row.id)
            if last is not None and moment - last < REDISCOVER_INTERVAL_SECONDS:
                continue
            _last_scan[row.id] = moment
            _running.add(row.id)
        target = furnace_service.target_of(row)
        start(
            row.id, row.name, row.host, row.port, row.mac_address,
            target.password or get_furnace_vnc_password(db), all_hosts - {row.host},
        )
