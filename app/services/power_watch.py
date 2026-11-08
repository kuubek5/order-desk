"""Чи вимикався ПК цеху — і чому (власник 08.11.26: «коли зникало світло»).

Бот живе на тому самому ПК, тож поки світла немає, він мовчить. Сказати про
вимкнення можна лише ПІСЛЯ — на старті. Для цього програма раз на хвилину
ставить мітку «живий» (`app_alive_at`), а при штатному виході — мітку виходу
(`app_clean_exit_at`). На старті три факти дають відповідь:

* розрив між останньою міткою «живий» і зараз — скільки нас не було;
* час завантаження Windows (`psutil.boot_time`) пізніший за мітку — ПК
  вимикався, а не лише програма;
* запис Windows «попереднє завершення роботи було несподіваним» (System,
  EventID 6008 чи Kernel-Power 41) після мітки — вимкнення аварійне, тобто
  майже напевно зникало світло.

Висновок без доказу не робимо: журнал Windows не прочитався — кажемо «ПК
вимикався», а не «зникало світло». Хибне «світло» гірше за чесне «не знаю».
Короткий розрив (`MIN_GAP`) не повідомляється: це звичайний перезапуск.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

ALIVE_KEY = "app_alive_at"
CLEAN_EXIT_KEY = "app_clean_exit_at"
LAST_OUTAGE_KEY = "power_last_outage"

BEAT_SECONDS = 60
# Коротший розрив — перезапуск програми чи оновлення, про це не пишемо.
MIN_GAP = timedelta(minutes=3)
# Мітка виходу, поставлена не раніше ніж за стільки до останньої мітки
# «живий», означає, що ЦЕЙ вихід був штатним.
_CLEAN_SLACK = timedelta(minutes=2)
# Скільки рядок «⚡» тримається в головному меню бота.
SHOW_FOR = timedelta(hours=24)

KIND_POWER = "power"        # ПК вимкнувся аварійно — схоже, світло
KIND_PC = "pc"              # ПК вимикали чи перезавантажували штатно
KIND_PC_UNKNOWN = "pc_unknown"  # ПК вимикався, причину Windows не назвала
KIND_APP_CRASH = "app_crash"    # ПК працював, а програма зупинилась аварійно
KIND_APP = "app"            # штатний перезапуск програми — не повідомляємо


@dataclass(frozen=True)
class Outage:
    kind: str
    gone_at: datetime        # остання мітка «живий»
    back_at: datetime        # коли ПК чи програма знову запрацювали
    started_at: datetime     # старт програми (зараз)

    @property
    def duration(self) -> timedelta:
        return self.back_at - self.gone_at

    @property
    def notify(self) -> bool:
        return self.kind != KIND_APP


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def beat(db: Session, now: datetime) -> None:
    set_setting(db, ALIVE_KEY, now.isoformat(timespec="seconds"))


def mark_clean_exit(db: Session, now: datetime) -> None:
    set_setting(db, CLEAN_EXIT_KEY, now.isoformat(timespec="seconds"))
    beat(db, now)


def classify(
    *,
    last_alive: Optional[datetime],
    clean_exit: Optional[datetime],
    boot: Optional[datetime],
    now: datetime,
    unexpected: Optional[bool],
    update_at: Optional[datetime] = None,
) -> Optional[Outage]:
    """Що сталося між останньою міткою «живий» і стартом. None — нічого
    вартого уваги (перший запуск, короткий розрив, годинник пішов назад)."""
    if last_alive is None or now - last_alive < MIN_GAP:
        return None
    rebooted = boot is not None and boot > last_alive
    if rebooted:
        assert boot is not None
        back = min(boot, now)
        if unexpected is True:
            kind = KIND_POWER
        elif unexpected is False:
            kind = KIND_PC
        else:
            kind = KIND_PC_UNKNOWN
        return Outage(kind, last_alive, back, now)
    clean = clean_exit is not None and clean_exit >= last_alive - _CLEAN_SLACK
    # Оновлення закриває програму й може добити її примусово (без мітки
    # виходу) — це не аварія, а встановлення, яке запустив адмін.
    updating = update_at is not None and last_alive - _CLEAN_SLACK <= update_at <= now
    return Outage(KIND_APP if clean or updating else KIND_APP_CRASH, last_alive, now, now)


# ── Факти з Windows ─────────────────────────────────────────────────────────

# Час події в XML журналу — UTC, із сімома знаками після секунд
# («2026-11-08T03:15:22.1234567Z»): беремо лише до секунд.
_EVENT_TIME = re.compile(r"SystemTime=['\"](\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def boot_time() -> Optional[datetime]:
    try:
        import psutil

        return datetime.fromtimestamp(psutil.boot_time())
    except Exception:  # noqa: BLE001
        logger.warning("Час завантаження Windows не прочитано", exc_info=True)
        return None


def unexpected_shutdown_since(since: datetime) -> Optional[bool]:
    """Чи є в журналі System запис про несподіване завершення після `since`.
    None — журнал не прочитався (не Windows, немає прав, wevtutil мовчить)."""
    if sys.platform != "win32":
        return None
    query = "*[System[(EventID=6008 or (EventID=41 and Provider[@Name='Microsoft-Windows-Kernel-Power']))]]"
    try:
        result = subprocess.run(
            ["wevtutil", "qe", "System", f"/q:{query}", "/c:5", "/rd:true", "/f:xml"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Журнал Windows не прочитано", exc_info=True)
        return None
    if result.returncode != 0:
        logger.warning("wevtutil повернув %s: %s", result.returncode, (result.stderr or "")[:200])
        return None
    return any_event_after(result.stdout or "", since)


def any_event_after(xml: str, since: datetime) -> bool:
    """Чи є в XML журналу подія не раніше за `since` (місцевий час)."""
    since_utc = since.astimezone(timezone.utc)
    for raw in _EVENT_TIME.findall(xml):
        moment = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        if moment >= since_utc:
            return True
    return False


# ── Старт ───────────────────────────────────────────────────────────────────


def _duration_text(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    bits = []
    if days:
        bits.append(f"{days} д")
    if hours:
        bits.append(f"{hours} год")
    if minutes and not days:
        bits.append(f"{minutes} хв")
    return " ".join(bits)


def _when(moment: datetime, now: datetime) -> str:
    if moment.date() == now.date():
        return f"сьогодні {moment:%H:%M}"
    if moment.date() == (now - timedelta(days=1)).date():
        return f"вчора {moment:%H:%M}"
    return f"{moment:%d.%m %H:%M}"


_HEADLINE = {
    KIND_POWER: "⚡ <b>ПК цеху був вимкнений аварійно</b>",
    KIND_PC: "⏻ <b>ПК цеху вимикали</b>",
    KIND_PC_UNKNOWN: "⏻ <b>ПК цеху був вимкнений</b>",
    KIND_APP_CRASH: "⚠️ <b>KuubMill зупинявся</b>",
}
_REASON = {
    KIND_POWER: "Windows не завершувала роботу, ПК стартував заново. Схоже, зникало світло.",
    KIND_PC: "Windows завершила роботу штатно: ПК вимкнули чи перезавантажили.",
    KIND_PC_UNKNOWN: "Чому — Windows не сказала: журнал подій не прочитався.",
    KIND_APP_CRASH: "ПК працював, а програма не відповідала й запустилась заново.",
}


def message(outage: Outage) -> str:
    now = outage.started_at
    gone = "Зник" if outage.kind == KIND_POWER else "Перестав працювати"
    lines = [
        _HEADLINE[outage.kind],
        "",
        f"{gone}: <b>{_when(outage.gone_at, now)}</b>",
        f"Знову працює: <b>{_when(outage.back_at, now)}</b> · перерва <b>{_duration_text(outage.duration)}</b>",
        "",
        _REASON[outage.kind],
        "За цей час печі й верстати не стежились, пошта й таблиця не синхронізувались.",
    ]
    return "\n".join(lines)


def home_line(db: Session, now: datetime) -> str:
    """Рядок для головного меню бота, поки вимкнення свіже."""
    raw = get_setting(db, LAST_OUTAGE_KEY)
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        kind = data["kind"]
        gone = datetime.fromisoformat(data["gone_at"])
        back = datetime.fromisoformat(data["back_at"])
    except (ValueError, KeyError, TypeError):
        return ""
    if kind not in (KIND_POWER, KIND_PC, KIND_PC_UNKNOWN) or now - back > SHOW_FOR:
        return ""
    word = "Світло зникало" if kind == KIND_POWER else "ПК був вимкнений"
    span = f"{_when(gone, now)}–{back:%H:%M}"
    return f"⚡ {word} {span} ({_duration_text(back - gone)})"


def _update_started_at(db: Session) -> Optional[datetime]:
    """Коли адмін востаннє запускав встановлення оновлення (знімок перед ним)."""
    from app.services.health_snapshot import BEFORE_KEY

    try:
        return _parse(json.loads(get_setting(db, BEFORE_KEY) or "{}").get("at"))
    except (ValueError, AttributeError):
        return None


def check_on_start(db: Session, now: Optional[datetime] = None) -> Optional[Outage]:
    """Розібрати перерву перед цим стартом, запамʼятати й поставити сповіщення.
    Викликається один раз, до першої мітки «живий» цього запуску."""
    from app.services import telegram_bot

    now = now or datetime.now()
    last_alive = _parse(get_setting(db, ALIVE_KEY))
    clean_exit = _parse(get_setting(db, CLEAN_EXIT_KEY))
    boot = boot_time()
    unexpected = None
    if last_alive is not None and boot is not None and boot > last_alive:
        unexpected = unexpected_shutdown_since(last_alive)
    outage = classify(last_alive=last_alive, clean_exit=clean_exit, boot=boot, now=now,
                      unexpected=unexpected, update_at=_update_started_at(db))
    beat(db, now)
    if outage is None:
        db.commit()
        return None
    logger.info("Перерва перед стартом: %s, %s → %s", outage.kind,
                outage.gone_at.isoformat(timespec="minutes"), outage.back_at.isoformat(timespec="minutes"))
    if outage.notify:
        data = {k: (v.isoformat(timespec="seconds") if isinstance(v, datetime) else v)
                for k, v in asdict(outage).items()}
        set_setting(db, LAST_OUTAGE_KEY, json.dumps(data, ensure_ascii=False))
        if telegram_bot.bot_enabled(db):
            telegram_bot.broadcast(
                db, event_key=f"outage:{outage.gone_at.isoformat(timespec='minutes')}",
                kind="outage", text=message(outage), now=now, ttl=timedelta(hours=12),
            )
    db.commit()
    return outage
