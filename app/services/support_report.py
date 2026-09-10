"""«Звіт для розробника» — один текстовий файл із усім, що потрібно для допомоги.

Навіщо. Власник працює з застосунком сам і звертається до розробника раз на
тиждень. Щоб відповісти на «щось не так», потрібні сім різних екранів: версія,
самоперевірка, журнал синку, журнал зв'язку верстатів, хвіст лога, кількість
рядків у таблицях, які налаштування задані. Кожен з них — окремий скріншот, і
половини все одно бракує; далі йде тиждень на уточнення. Цей звіт збирає все
за один клік.

ЖОДНОГО СЕКРЕТУ. Пароль пошти, ключ Google, токени агентів і ліцензійний ключ
у звіт не потрапляють — лише «задано / не задано». Файл їде в месенджер, а
месенджер — це чужий сервер; секрет, який туди поїхав, вважається розкритим.
Ідентифікатори (Sheet ID, логін пошти) маскуються серединою: їх треба ВПІЗНАТИ
(«так, це та сама таблиця»), а не прочитати.

ТІЛЬКИ ЧИТАННЯ. Звіт нічого не міняє й нічого не вмикає — його можна збирати
посеред зміни.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.__version__ import VERSION
from app.business_day import business_today, utc_to_business
from app.config import DATA_DIR, DB_PATH
from app.db import Base
from app.models import MachineLinkEvent, Order, SyncLog, User
from app.services import machine_link
from app.settings_store import SETTING_FIELDS, get_setting


# Скільки рядків лога брати в хвіст. Двісті — це приблизно остання година
# роботи на живому цеховому ПК: досить, щоб побачити, що передувало збою, і
# не настільки багато, щоб файл перестали читати.
LOG_TAIL_LINES = 200
# Окремо — рядки з ERROR/CRITICAL за останню добу. Вони можуть бути СТАРШІ за
# хвіст (після помилки застосунок міг ще годину писати INFO), і саме їх шукає
# розробник першими.
LOG_ERROR_HOURS = 24
LOG_ERROR_LINES = 40

_RULE = "─" * 66


def _mask(value: Optional[str], keep: int = 4) -> str:
    """Показати кінці, сховати середину: впізнати можна, скористатись — ні."""
    text = (value or "").strip()
    if not text:
        return "— не задано"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]} ({len(text)} симв.)"


def _head(title: str) -> list[str]:
    return ["", _RULE, title.upper(), _RULE]


def _kv(key: str, value: Any) -> str:
    return f"  {key:<36} {value}"


# ── Розділи звіту ───────────────────────────────────────────────────────────


def _section_app(db: Session) -> list[str]:
    lines = _head("застосунок")
    try:
        uptime = f"{(datetime.now() - _process_started()).total_seconds() / 3600:.1f} год"
    except Exception:  # noqa: BLE001
        uptime = "невідомо"
    lines += [
        _kv("Версія", VERSION),
        _kv("Компʼютер", socket.gethostname()),
        _kv("Система", f"{platform.system()} {platform.release()}"),
        _kv("Python", platform.python_version()),
        _kv("Час на ПК", datetime.now().strftime("%d.%m.%Y %H:%M:%S")),
        _kv("Робоча доба", business_today().strftime("%d.%m.%y")),
        _kv("Застосунок працює", uptime),
        _kv("Тека даних", str(DATA_DIR)),
        _kv("База", str(DB_PATH)),
    ]
    try:
        size_mb = Path(DB_PATH).stat().st_size / (1024 * 1024)
        lines.append(_kv("Розмір бази", f"{size_mb:.1f} МБ"))
    except OSError:
        lines.append(_kv("Розмір бази", "файл недоступний"))
    try:
        free_gb = shutil.disk_usage(Path(DB_PATH).parent).free / (1024 ** 3)
        lines.append(_kv("Вільно на диску", f"{free_gb:.1f} ГБ"))
    except OSError:
        pass
    return lines


def _process_started() -> datetime:
    """Коли стартував процес. Без psutil: беремо час створення файлу-локи або
    самого процесу через os — точність тут не критична, потрібен порядок."""
    try:
        import time as _t

        return datetime.fromtimestamp(_t.time() - os.times().elapsed)
    except Exception:  # noqa: BLE001
        return datetime.now()


def _section_selfcheck(results: Optional[Iterable[Any]]) -> list[str]:
    lines = _head("самоперевірка")
    if results is None:
        lines.append("  не запускалась (звіт зібрано без мережевих проб)")
        return lines
    rows = list(results)
    if not rows:
        lines.append("  проб немає")
        return lines
    passed = sum(1 for r in rows if r.ok)
    lines.append(f"  {passed} з {len(rows)} — ok")
    lines.append("")
    for r in rows:
        mark = "OK  " if r.ok and not r.warn else ("!   " if r.warn else "ЗБІЙ")
        lines.append(f"  [{mark}] {r.name}")
        lines.append(f"         {r.detail}  ({r.ms} мс)")
    return lines


def _section_settings(db: Session) -> list[str]:
    lines = _head("налаштування")
    lines.append("  секрети — лише «задано / не задано»")
    lines.append("")
    for field in SETTING_FIELDS:
        try:
            value = get_setting(db, field.key)
        except Exception:  # noqa: BLE001 — зіпсований шифр не має валити звіт
            lines.append(_kv(field.label, "!! не читається (ключ шифрування?)"))
            continue
        if getattr(field, "secret", False):
            shown = "задано" if (value or "").strip() else "— не задано"
        elif field.key in ("google_sheet_id", "imap_login"):
            shown = _mask(value)
        else:
            shown = (value or "").strip() or "— не задано"
        lines.append(_kv(field.label, shown))
    return lines


def _section_database(db: Session) -> list[str]:
    lines = _head("база даних")
    for table in sorted(Base.metadata.tables):
        try:
            n = db.scalar(select(func.count()).select_from(Base.metadata.tables[table]))
        except Exception:  # noqa: BLE001
            n = "?"
        lines.append(_kv(table, n))
    lines.append("")
    try:
        live = db.scalar(
            select(func.count()).select_from(Order).where(Order.archived_at.is_(None))
        )
        archived = db.scalar(
            select(func.count()).select_from(Order).where(Order.archived_at.isnot(None))
        )
        users = db.scalar(select(func.count()).select_from(User))
        lines += [
            _kv("Робіт живих", live),
            _kv("Робіт в архіві", archived),
            _kv("Операторів", users),
        ]
    except Exception:  # noqa: BLE001
        lines.append("  зведення не порахувалось")
    return lines


def _section_sync(db: Session, limit: int = 25) -> list[str]:
    """Останні записи синку, з ОДНАКОВИМИ ПОСПІЛЬ згорнутими в один рядок.

    Хворий синк повторює той самий текст щохвилини: на живому стенді 10 з 15
    рядків були буквально однакові, і розділ переставав читатись саме тоді,
    коли він найпотрібніший. Згортання нічого не ховає — воно називає, скільки
    разів це повторилось і до якого часу.
    """
    lines = _head("журнал синку")
    try:
        rows = db.scalars(
            select(SyncLog).order_by(SyncLog.occurred_at.desc()).limit(limit)
        ).all()
    except Exception:  # noqa: BLE001
        return lines + ["  журнал не читається"]
    if not rows:
        return lines + ["  порожній"]

    def stamp(row) -> str:
        # `occurred_at` — UTC (server_default); показуємо київський, інакше
        # звіт бреше на три години (CLAUDE.md §14).
        if row.occurred_at is None:
            return "—"
        return utc_to_business(row.occurred_at).strftime("%d.%m %H:%M:%S")

    def signature(row) -> tuple:
        return (row.direction, row.status, row.sheet_tab, (row.message or "").strip())

    groups: list[list] = []
    for row in rows:
        if groups and signature(groups[-1][0]) == signature(row):
            groups[-1].append(row)
        else:
            groups.append([row])

    for group in groups:
        first, last = group[0], group[-1]
        tab = f" [{first.sheet_tab}]" if first.sheet_tab else ""
        head = f"  {stamp(first)}  {first.direction}/{first.status}{tab}"
        if len(group) > 1:
            head += f"   x {len(group)} (до {stamp(last)})"
        lines.append(head)
        message = (first.message or "").replace(chr(10), " ")[:220]
        if message:
            lines.append(f"            {message}")
    return lines



def _section_machines(db: Session, limit: int = 10) -> list[str]:
    lines = _head(f"зв'язок з верстатами (останні {limit} обривів)")
    try:
        rows = db.scalars(
            select(MachineLinkEvent)
            .order_by(MachineLinkEvent.detected_at.desc())
            .limit(limit)
        ).all()
    except Exception:  # noqa: BLE001
        return lines + ["  журнал не читається"]
    if not rows:
        return lines + ["  обривів не було"]
    for row in rows:
        view = machine_link.view_of(row)
        when = row.detected_at.strftime("%d.%m %H:%M:%S") if row.detected_at else "—"
        span = "триває" if view.is_open else f"{(view.seconds or 0) // 60} хв {(view.seconds or 0) % 60} с"
        lines.append(f"  {when}  {row.name}  ({span})")
        lines.append(f"            {view.explanation.headline} · {row.cause}")
        if row.probe_verdict:
            lines.append(f"            {row.probe_verdict}")
    return lines


def _section_sections(db: Session) -> list[str]:
    lines = _head("доступ до розділів")
    try:
        from app.services.section_gate import SECTIONS, closed_sections

        closed = closed_sections(db)
    except Exception:  # noqa: BLE001
        return lines + ["  не читається"]
    if not closed:
        return lines + ["  усі відкриті"]
    for key, state in closed.items():
        lines.append(_kv(SECTIONS[key]["title"], f"зачинено ({state})"))
    return lines


def _log_path() -> Path:
    return DATA_DIR / "logs" / "kuubmill.log"


def _read_log_lines() -> list[str]:
    path = _log_path()
    if not path.exists():
        return []
    try:
        # Читаємо з кінця, а не цілий файл: він до 2 МБ, і тягти його в памʼять
        # заради двохсот рядків не варто.
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            chunk = min(size, 400_000)
            fh.seek(size - chunk)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()


def recent_errors(lines: Optional[list[str]] = None) -> list[str]:
    """Рядки ERROR/CRITICAL лога за останні LOG_ERROR_HOURS год.

    Спільне для розділу звіту й короткого підсумку в Telegram-боті — щоб
    «помилок 3» у повідомленні й у файлі рахувались однаково."""
    if lines is None:
        lines = _read_log_lines()
    cutoff = datetime.now() - timedelta(hours=LOG_ERROR_HOURS)
    hits: list[str] = []
    for line in lines:
        if " ERROR " not in line and " CRITICAL " not in line:
            continue
        stamp = line[:19]
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            hits.append(line)
            continue
        if when >= cutoff:
            hits.append(line)
    return hits


def _section_log_errors(lines: list[str]) -> list[str]:
    out = _head(f"помилки в лозі (за {LOG_ERROR_HOURS} год)")
    if not lines:
        return out + [f"  файл лога не знайдено: {_log_path()}"]
    hits = recent_errors(lines)
    if not hits:
        return out + ["  помилок немає"]
    for line in hits[-LOG_ERROR_LINES:]:
        out.append("  " + line[:300])
    if len(hits) > LOG_ERROR_LINES:
        out.append(f"  … всього {len(hits)}, показано останні {LOG_ERROR_LINES}")
    return out


def _section_log_tail(lines: list[str]) -> list[str]:
    out = _head(f"хвіст лога (останні {LOG_TAIL_LINES} рядків)")
    if not lines:
        return out + [f"  файл лога не знайдено: {_log_path()}"]
    for line in lines[-LOG_TAIL_LINES:]:
        out.append("  " + line[:300])
    return out


def build_report(db: Session, *, selfcheck_results: Optional[Iterable[Any]] = None) -> str:
    """Увесь звіт одним текстом.

    `selfcheck_results` віддає роут (проби ходять у мережу, і сервіс не має
    вирішувати, чи можна зараз чекати двадцять секунд). None = звіт без них,
    і розділ чесно про це каже, а не мовчить.
    """
    log_lines = _read_log_lines()
    parts: list[str] = [
        "ЗВІТ KUUBMILL ДЛЯ РОЗРОБНИКА",
        f"Зібрано {datetime.now().strftime('%d.%m.%Y %H:%M')} · версія {VERSION}",
        "Секретів у цьому файлі немає — лише «задано / не задано».",
    ]
    for section in (
        _section_app(db),
        _section_selfcheck(selfcheck_results),
        _section_settings(db),
        _section_sections(db),
        _section_database(db),
        _section_sync(db),
        _section_machines(db),
        _section_log_errors(log_lines),
        _section_log_tail(log_lines),
    ):
        parts.extend(section)
    parts.append("")
    parts.append(_RULE)
    parts.append("Кінець звіту.")
    return "\n".join(parts)


def report_filename(now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now()).strftime("%d.%m.%y_%H-%M")
    return f"kuubmill-zvit_{stamp}.txt"
