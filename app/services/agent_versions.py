"""Яка версія агента стоїть на якому верстаті (17.11.26).

Агент (`agent/main.go`) віддає свою версію в `GET /status`, але KuubMill її досі
не читав: дізнатись, який білд стоїть на верстаті, можна було лише підійшовши до
ПК. Через це невстановлене оновлення агента нічим себе не видавало — функція
просто «не працювала» на трьох верстатах із десяти.

Опитування тут ОКРЕМЕ від опитування екранів (`machines.poll_all`): там кожна
зайва секунда старить кадри решти верстатів. Версія міняється раз на місяці,
тож її питає свій воркер — рідко, паралельно й з коротким тайм-аутом. Не
відповів — стара версія в базі лишається: «не знаю» не стирає «знав».
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from app.models import Machine
from app.services import machines

logger = logging.getLogger(__name__)

# Перший прохід — невдовзі після старту, далі раз на пів години.
FIRST_DELAY_SECONDS = 90
REFRESH_SECONDS = 30 * 60
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 3.0
WORKERS = 8
MAX_LENGTH = 60

# Білд CI штампує «рррр.мм.дд-<sha>»; усе інше («0.1.0», «dev») — старий або
# власноруч зібраний агент, вік якого з рядка не видно.
_DATED = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-[0-9a-f]{4,40}$")

Fetch = Callable[[str, int, str], Optional[str]]


def fetch_version(host: str, port: int, token: str) -> Optional[str]:
    """Версія агента за цією адресою або None (не відповів, чужий токен, не JSON)."""
    import requests

    try:
        resp = requests.get(
            f"http://{host}:{port}/status",
            headers={"X-Agent-Token": token},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 — тиша/відмова/битий JSON = «не знаю»
        logger.debug("Агент %s:%s не віддав версію", host, port, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    return version.strip()[:MAX_LENGTH]


def build_date(version: str) -> Optional[tuple[int, int, int]]:
    """Дата білда з рядка версії; None — версія без дати."""
    match = _DATED.match((version or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def newest(versions: list[str]) -> str:
    """Найсвіжіша версія серед відомих у цеху (за датою білда); "" — датованих немає."""
    dated = [(build_date(v), v) for v in versions if build_date(v)]
    if not dated:
        return ""
    return max(dated)[1]


def is_outdated(version: str, fleet: list[str]) -> bool:
    """Чи старіша ця версія за найсвіжішу в цеху.

    Порівнюємо ДАТИ білдів: два білди одного дня вважаються рівними (порядок
    комітів з рядка не видно — не вгадуємо). Версія без дати стара, щойно в
    цеху є хоч одна датована. Невідома версія ("") не позначається ніяк.
    """
    if not version:
        return False
    top = build_date(newest(fleet))
    if top is None:
        return False
    mine = build_date(version)
    return mine is None or mine < top


def stale_ids(rows: Iterable[Machine]) -> set[int]:
    """id верстатів зі старішим агентом — для екрана налаштувань (шаблон має
    перелік рядків, а не сесію). Вимкнений верстат у порівнянні участі не бере:
    його версія могла застигнути місяці тому."""
    listed = list(rows or [])
    fleet = [m.agent_version for m in listed if m.enabled and m.agent_version]
    return {m.id for m in listed if m.enabled and is_outdated(m.agent_version or "", fleet)}


def refresh_once(db: Session, fetch: Fetch = fetch_version, now: Optional[datetime] = None) -> dict[str, int]:
    """Спитати версію в усіх увімкнених верстатів з агентом і записати зміни.

    Мережа — паралельно й ПОЗА сесією бази; база — на цьому потоці. Повертає
    лічильники для логу й тестів.
    """
    moment = now or datetime.now()
    asked: list[tuple[int, str, str, int, str]] = []
    for machine in machines.list_machines(db, only_enabled=True):
        target = machines.target_of(machine)
        if target.agent_token:
            asked.append((machine.id, machine.name, machine.host, machine.port, target.agent_token))
    if not asked:
        return {"asked": 0, "answered": 0, "changed": 0}

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="agent-version") as pool:
        answers = list(pool.map(lambda row: fetch(row[2], row[3], row[4]), asked))

    answered = changed = 0
    for (machine_id, name, _host, _port, _token), version in zip(asked, answers):
        if not version:
            continue
        answered += 1
        row = db.get(Machine, machine_id)
        if row is None:
            continue
        if row.agent_version != version:
            if row.agent_version:
                logger.info("Верстат %s: версія агента %s → %s", name, row.agent_version, version)
            else:
                logger.info("Верстат %s: версія агента %s", name, version)
            row.agent_version = version
            changed += 1
        row.agent_version_at = moment
    db.commit()
    return {"asked": len(asked), "answered": answered, "changed": changed}
