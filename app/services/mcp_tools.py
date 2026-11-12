"""Інструменти MCP — структурований зріз живого стану KuubMill.

Навіщо. Діагностика цеху досі йшла через людину: «вставиш у PowerShell оце»,
«зроби скріншот Архіву», «перешли звіт». Кожне питання — коло через Рому, а
половина відповідей губилась по дорозі (інцидент 08.09.26: зниклий день
розбирався скріншотами півгодини, хоча в журналі синку відповідь лежала
готова). Тут ті самі факти віддаються машині, що питає, — у JSON, без
копіювання руками.

Межі, свідомо вузькі:

* **Тільки читання.** Жоден інструмент не пише ні в базу, ні в таблицю, ні в
  налаштування. Запис звідси не з'явиться «потім»: MCP ходить без сесії
  оператора, а історія роботи мусить знати, ХТО що зробив (CLAUDE.md §10).
* **Ніяких секретів.** Розшифровані значення налаштувань сюди не потрапляють —
  як і в Jinja-контекст (§14 «Секрети»). Віддається лише ознака «задано».
* **Час — як на екрані.** `SyncLog.occurred_at` у базі UTC; назовні йде
  київський через `utc_to_business`, інакше кожен рядок журналу брехав би на
  три години (§14 «Синк таблиці»).
* **День — робочий, не календарний.** «Сьогодні» = вкладка, у яку цех пише
  зараз (`business_tab_today`), тож у вихідні це п'ятниця (§4).

Транспорт (JSON-RPC, гейт, помилки) живе в `app/routers/mcp.py`: тут немає ні
`Request`, ні `Response`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import logging
import os
import re
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.__version__ import VERSION
from app.business_day import (
    business_tab_today,
    business_today,
    next_tab_day,
    prev_tab_day,
    utc_to_business,
)
from app.config import DATA_DIR, DB_PATH
from app.models import ActionLog, Attachment, Comment, Order, StatusEvent, SyncLog
from app import stats as stats_rules
from app.services import device_diag, stats_audit, sum3d_blank_shape, sum3d_deep, sum3d_files
from app.services.order_dates import parse_sheet_tab

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """Помилка, з якої видно, що робити далі.

    Правило з `mcp-builder`: «не зміг» без наступного кроку змушує того, хто
    питає, гадати. Тому текст завжди називає або допустимі значення, або
    наступну дію.
    """


# ── Дні ──────────────────────────────────────────────────────────────────────

_DAY_WORDS_TODAY = {"", "today", "сьогодні", "сегодня"}
_DAY_WORDS_PREV = {"yesterday", "вчора", "вчера"}
_DAY_WORDS_NEXT = {"tomorrow", "завтра"}
_DAY_FORMATS = ("%Y-%m-%d", "%d.%m.%y", "%d.%m.%Y")


def _resolve_day(value: str | None) -> date:
    """Слово або дата → робочий день вкладки.

    «Сьогодні» тут — `business_tab_today`, а не `date.today()`: у суботу й
    неділю цех пише в п'ятничну вкладку, і питання «що в черзі сьогодні» у
    вихідний мусить дати п'ятницю (§4, `weekends-write-into-friday`).
    """
    text = (value or "").strip().lower()
    today = business_tab_today()
    if text in _DAY_WORDS_TODAY:
        return today
    if text in _DAY_WORDS_PREV:
        return prev_tab_day(today)
    if text in _DAY_WORDS_NEXT:
        return next_tab_day(today)
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ToolError(
        f"день «{value}» не розпізнано. Допустимо: today / yesterday / tomorrow "
        "або дата як 2026-09-11 чи 11.09.26"
    )


def _readiness(order: Order) -> str:
    """Готовність із двох полів, як фільтр черги (§5). Окремого статусу немає."""
    if not (order.job_code or "").strip():
        return "не готово"
    if not (order.sum3d_id or "").strip():
        return "можна брати"
    return "в роботі"



def _order_email_files(db: Session, order: Order) -> dict | None:
    """Звідки поштова робота бере теку — або чому не бере.

    Дзеркало черги на екрані пошти показує іконку теки за файлами САМЕ ЦІЄЇ
    роботи (`Attachment.order_id`). Коли іконки немає, ззовні не видно, що саме
    порожнє: робота без листа, лист без вкладень, вкладення не переїхали чи
    файли зникли з диска. Питати це доводилось власника кліками по екрану
    (27.09.26) — тепер видно звідси.
    """
    if order.source != "email":
        return None
    own = db.scalars(
        select(Attachment).where(Attachment.order_id == order.id)
    ).all()
    loose: list[Attachment] = []
    if order.source_email_id is not None:
        loose = list(db.scalars(
            select(Attachment).where(
                Attachment.email_message_id == order.source_email_id,
                Attachment.order_id.is_(None),
            )
        ).all())
    existing = [a for a in own if Path(a.saved_path).exists()]
    return {
        "id_листа": order.source_email_id,
        # Головне джерело теки: прийняття листа записує сюди точний шлях, і
        # саме його читають видача й дзеркало черги на екрані пошти.
        "закріплена_тека": order.export_folder_path,
        "своїх_файлів": len(own),
        "з_них_на_диску": len(existing),
        "нерозібраних_у_листі": len(loose),
        "тека": str(Path(existing[0].saved_path).parent) if existing else None,
        "шлях_першого_файлу": own[0].saved_path if own else None,
    }

def _order_stl_height(db: Session, order: Order) -> dict[str, Any]:
    """Висота STL роботи — для калібрування усадки за парами «наряд →
    товщина в Sum3D» (власник 04.11.26). Збережену мірку фоновий прохід
    ставить лише свіжим роботам, тож для давніших теку міряємо тут-таки,
    тими самими `work_folder`/`measure_folder`. Лише читання: в базу нічого
    не пишемо, назв файлів (імена пацієнтів) не віддаємо."""
    from app.services import stl_height
    from app.settings_store import get_export_folder_path, get_technician_files_path

    family = order.material.name if order.material is not None else None
    out: dict[str, Any] = {
        "родина": family,
        "збережена_мм": order.stl_height_mm,
        "прогноз_товщини_мм": _round_or_none(stl_height.thickness_mm(order.stl_height_mm, family)),
        "усадка_в_прогнозі": stl_height.SHRINK.get(family or ""),
    }
    export_root = (get_export_folder_path(db) or "").strip()
    folder = stl_height.work_folder(
        order, get_technician_files_path(db), Path(export_root) if export_root else None
    )
    if folder is None:
        out["поміряно_зараз"] = "теки роботи не знайдено"
        return out
    measured = stl_height.measure_folder(folder)
    out["поміряно_зараз"] = {
        "висота_мм": _round_or_none(measured.height_mm, 3),
        "файлів_cad_stl": measured.files,
    }
    return out


def _round_or_none(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None else None


def _order_brief(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "рядок": order.row_number,
        "наряд": order.work_order_no,
        "клієнт": order.client_name,
        "технік": order.technician_name,
        "матеріал": order.material_color,
        "кількість": order.quantity,
        "вид": order.kind,
        "sum3d_id": order.sum3d_id,
        "номер_роботи": order.job_code,
        "статус": order.status,
        "готовність": _readiness(order),
        "джерело": order.source,
        "вкладка": order.sheet_tab,
        "коментар_cam": order.cam_comment,
    }


def _active_orders_of_day(db: Session, day: date) -> list[Order]:
    """Незаархівовані роботи однієї вкладки.

    Назва вкладки звіряється через `parse_sheet_tab`, а не рівністю рядків:
    вкладка `" 08.09.26"` з пробілом на початку вже одного разу зробила цілий
    день невидимим (§14 «Синк таблиці»), і порівняння рядків повторило б цю
    помилку тут.
    """
    rows = db.scalars(select(Order).where(Order.archived_at.is_(None))).all()
    return [o for o in rows if parse_sheet_tab(o.sheet_tab) == day]


# ── Інструменти ──────────────────────────────────────────────────────────────


def tool_health(db: Session, args: dict) -> dict[str, Any]:
    day = business_tab_today()
    of_day = _active_orders_of_day(db, day)
    last_sync = db.scalars(
        select(SyncLog).order_by(SyncLog.occurred_at.desc()).limit(1)
    ).first()

    paused: bool | None = None
    speed: str | None = None
    try:
        from app import sync_control

        paused = sync_control.is_paused()
        speed = sync_control.get_speed_preset()
    except Exception:  # noqa: BLE001 — діагностика не має падати через це
        logger.debug("MCP: стан синку недоступний", exc_info=True)

    api_minute: int | None = None
    try:
        from app.sheets import api_calls_last_minute

        api_minute = api_calls_last_minute()
    except Exception:  # noqa: BLE001
        logger.debug("MCP: лічильник Sheets недоступний", exc_info=True)

    return {
        "версія": VERSION,
        "база": DB_PATH,
        "тека_даних": str(DATA_DIR),
        "лог": {
            name: {
                "шлях": str(path),
                "є": path.exists(),
                "розмір_байт": path.stat().st_size if path.exists() else 0,
            }
            for name, path in _log_candidates().items()
        },
        "день": {
            "справжня_дата": business_today().isoformat(),
            "вкладка_сьогодні": day.isoformat(),
            "вихідні_на_пʼятниці": business_today() != day,
        },
        "синк": {
            "на_паузі": paused,
            "темп": speed,
            "запитів_до_sheets_за_хвилину": api_minute,
            "останній_запис": _sync_entry(last_sync) if last_sync else None,
        },
        "черга_вкладки": {
            "усього": len(of_day),
            "по_готовності": _count_by(of_day, _readiness),
            "по_статусу": _count_by(of_day, lambda o: o.status),
        },
    }


def tool_queue(db: Session, args: dict) -> dict[str, Any]:
    day = _resolve_day(args.get("day"))
    limit = _int_arg(args, "limit", default=50, low=1, high=300)
    rows = _active_orders_of_day(db, day)
    rows.sort(key=lambda o: (o.row_number or 10**6, o.id))
    return {
        "день": day.isoformat(),
        "вкладка": day.strftime("%d.%m.%y"),
        "усього": len(rows),
        "по_статусу": _count_by(rows, lambda o: o.status),
        "по_джерелу": _count_by(rows, lambda o: o.source),
        "по_готовності": _count_by(rows, _readiness),
        "одиниць": sum(_units(o) for o in rows),
        "показано": min(limit, len(rows)),
        "роботи": [_order_brief(o) for o in rows[:limit]],
    }


def tool_order(db: Session, args: dict) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("потрібен `query`: номер наряду, Sum3D ID (можна хвіст) або id роботи")

    found = db.scalars(
        select(Order)
        .where(Order.work_order_no == query)
        .order_by(Order.id.desc())
        .limit(10)
    ).all()
    if not found:
        found = db.scalars(
            select(Order)
            .where(Order.sum3d_id.like(f"%{query}%"))
            .order_by(Order.id.desc())
            .limit(10)
        ).all()
    if not found and query.isdigit():
        one = db.get(Order, int(query))
        found = [one] if one else []
    if not found:
        raise ToolError(
            f"за «{query}» нічого не знайдено. Спробуй номер наряду повністю, "
            "хвіст Sum3D ID (наприклад 12-01-45) або id роботи з черги"
        )

    return {"знайдено": len(found), "роботи": [_order_full(db, o) for o in found]}


def _order_full(db: Session, order: Order) -> dict[str, Any]:
    events = db.scalars(
        select(StatusEvent)
        .where(StatusEvent.order_id == order.id)
        .order_by(StatusEvent.occurred_at.desc())
        .limit(30)
    ).all()
    actions = db.scalars(
        select(ActionLog)
        .where(ActionLog.order_id == order.id)
        .order_by(ActionLog.created_at.desc())
        .limit(30)
    ).all()
    comments = db.scalars(
        select(Comment)
        .where(Comment.order_id == order.id)
        .order_by(Comment.created_at.desc())
        .limit(20)
    ).all()

    data = _order_brief(order)
    data.update(
        {
            "архівована": order.archived_at.isoformat() if order.archived_at else None,
            "прорахував": order.calculated_raw,
            "відфрезерував": order.milled_raw,
            "останнє_фрезерування": order.last_milled_date,
            "який_раз": order.mill_count,
            "опаків": order.opak_units,
            "видано_звідки": order.issued_source,
            "видача_закріплена": order.issue_locked,
            "sum3d_не_в_таблиці": order.sum3d_pending,
            "висота_stl": _order_stl_height(db, order),
            "лист": _order_email_files(db, order),
            "хронологія": [
                {
                    "коли": _local(e.occurred_at),
                    "статус": e.status,
                    "хто": e.actor,
                    "нотатка": e.note,
                }
                for e in events
            ],
            "дії": [
                {
                    "коли": _local(a.created_at),
                    "що": a.action_type,
                    "поле": a.field,
                    "було": a.old_value,
                    "стало": a.new_value,
                    "підпис": a.note,
                    "скасовано": _local(a.undone_at) if a.undone_at else None,
                }
                for a in actions
            ],
            "коментарі": [
                {
                    "коли": _local(c.created_at),
                    "звідки": c.source,
                    "автор": c.author,
                    "текст": c.text,
                }
                for c in comments
            ],
        }
    )
    return data


def tool_sync_journal(db: Session, args: dict) -> dict[str, Any]:
    limit = _int_arg(args, "limit", default=30, low=1, high=200)
    status = (args.get("status") or "").strip() or None
    stmt = select(SyncLog).order_by(SyncLog.occurred_at.desc()).limit(limit)
    if status:
        stmt = (
            select(SyncLog)
            .where(SyncLog.status == status)
            .order_by(SyncLog.occurred_at.desc())
            .limit(limit)
        )
    rows = db.scalars(stmt).all()
    return {
        "показано": len(rows),
        "записи": [_sync_entry(r) for r in rows],
    }


def _sync_entry(row: SyncLog) -> dict[str, Any]:
    return {
        "коли": _local(utc_to_business(row.occurred_at) if row.occurred_at else None),
        "напрям": row.direction,
        "вкладка": row.sheet_tab,
        "стан": row.status,
        "текст": row.message,
        "стертий_рядок": row.erased_row,
        "відновлено": _local(row.erased_restored_at) if row.erased_restored_at else None,
    }


# ── Лог ──────────────────────────────────────────────────────────────────────

_LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_QUOTA_SAMPLE = re.compile(r"Sheets API: (\d+) запит")
_SOFT_LIMIT = 45
_HARD_LIMIT = 60
_MAX_LINE = 400


def _log_candidates() -> dict[str, Path]:
    """Звідки можна читати лог — під іменами, а не довільним шляхом.

    Довільний шлях тут був би примітивом «прочитай будь-який файл з диска», і
    його не мусить давати інструмент діагностики. Тому вибір із двох відомих
    місць:

    * `поточний` — тека даних ЦЬОГО процесу (на проді це й є встановлений);
    * `встановлений` — `%LOCALAPPDATA%\KuubMill\logs` тієї самої машини.

    Dev-сервера тут свідомо немає: `dev_restart.ps1` складає лише access-лог
    uvicorn без часу, а рядки лічильника Sheets (INFO) на dev не пишуться
    нікуди — файл виглядав би як відповідь, а насправді давав би тишу.
    """
    found: dict[str, Path] = {"поточний": DATA_DIR / "logs" / "kuubmill.log"}
    local = os.environ.get("LOCALAPPDATA")
    if local:
        installed = Path(local) / "KuubMill" / "logs" / "kuubmill.log"
        if installed != found["поточний"]:
            found["встановлений"] = installed
    return found


def _update_logs() -> dict[str, Path]:
    """Що сталось під час встановлення. Окремо від `_log_candidates`: auto їх не бере,
    бо це не журнал застосунку й тиша в них нічого не означала б."""
    folder = DATA_DIR / "logs"
    return {"оновлення": folder / "update-watchdog.log", "інсталятор": folder / "update-installer.log"}


def _pick_log(choice: str | None) -> tuple[str, Path]:
    """Яким файлом відповідаємо — і назва їде у відповідь.

    Хибна тиша гірша за відмову: інструмент, який нічого не знайшов, бо дивився
    не в той файл, виглядає як «усе спокійно» (те саме правило, що з печами —
    порожнє поле краще за вгадане число).
    """
    candidates = _log_candidates()
    asked = (choice or "auto").strip().lower()
    if asked not in ("auto", ""):
        named = {**candidates, **_update_logs()}
        if asked not in named:
            raise ToolError(
                f"джерело «{choice}» невідоме. Доступні: {', '.join(named)} або auto"
            )
        path = named[asked]
        if not path.exists():
            raise ToolError(f"«{asked}»: файла {path} немає на цій машині")
        return asked, path
    for name, path in candidates.items():
        if path.exists():
            return name, path
    raise ToolError(
        "лога KuubMill на цій машині немає (шукав: "
        + "; ".join(str(p) for p in candidates.values())
        + "). kmill_quota і kmill_log працюють там, де запущено ВСТАНОВЛЕНУ CRM: "
        "dev-сервер пише лише access-лог uvicorn без часу й без рядків лічильника Sheets"
    )


def _log_files(base: Path) -> list[Path]:
    """Усі ротовані файли, від найстарішого. `.5` найстаріший, без суфікса — живий."""
    files = [base.with_name(base.name + f".{n}") for n in range(5, 0, -1)]
    files.append(base)
    return [p for p in files if p.exists()]


def _read_log(since: datetime | None, choice: str | None = None) -> tuple[str, list[tuple[datetime | None, str]]]:
    """Рядки лога з розібраним часом + назва джерела, яким відповіли.

    Файл читається з `errors="replace"`: прод пише в нього ОДНОЧАСНО, і
    обрізаний на півсимволу хвіст не має валити діагностику.
    """
    name, base = _pick_log(choice)
    out: list[tuple[datetime | None, str]] = []
    for path in _log_files(base):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(
                f"не вдалось прочитати {path.name}: {exc}. Перевір, що CRM запущена "
                "на ЦІЙ машині й тека логів на місці"
            ) from exc
        for line in text.splitlines():
            match = _LOG_TS.match(line)
            moment = None
            if match:
                try:
                    moment = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    moment = None
            if since and moment and moment < since:
                continue
            out.append((moment, line))
    return name, out


def tool_quota(db: Session, args: dict) -> dict[str, Any]:
    """Запити до Google за хвилину — з рядків, які пише сам лічильник.

    Замінює `scripts/quota_report.ps1`: та сама вибірка, але без вставляння
    скрипта в PowerShell на цеховому ПК. Ліміт Google — 60 на хвилину на кожен
    вид окремо, гальмо гарячого тіку спрацьовує на 45 (§14, `sheets.py`).
    """
    hours = _int_arg(args, "hours", default=24, low=1, high=24 * 14)
    since = datetime.now() - timedelta(hours=hours)
    source, lines = _read_log(since, args.get("log"))

    samples: list[tuple[datetime | None, int]] = []
    per_hour: dict[str, list[int]] = {}
    skipped = 0
    quota_errors: list[str] = []
    for moment, line in lines:
        sample = _QUOTA_SAMPLE.search(line)
        if sample:
            value = int(sample.group(1))
            samples.append((moment, value))
            if moment:
                per_hour.setdefault(moment.strftime("%Y-%m-%d %H"), []).append(value)
            continue
        if "Гарячий тік пропущено" in line:
            skipped += 1
        elif "Quota exceeded" in line:
            quota_errors.append(line[:_MAX_LINE])

    values = [v for _, v in samples]
    verdict = "квота не заважає"
    if quota_errors or skipped:
        verdict = "квота вже заважає: є пропущені тіки або відмови Google"
    elif values and max(values) >= _SOFT_LIMIT:
        verdict = "впритул до гальма (45/хв) — другого оператора підключати рано"
    elif values and max(values) >= 35:
        verdict = "запас є, але тонкий — перед другим оператором знизити темп"

    return {
        "лог": source,
        "рядків_прочитано": len(lines),
        "вікно_годин": hours,
        "проб": len(values),
        "середнє": round(sum(values) / len(values), 1) if values else None,
        "максимум": max(values) if values else None,
        "хвилин_від_45": sum(1 for v in values if v >= _SOFT_LIMIT),
        "хвилин_від_60": sum(1 for v in values if v >= _HARD_LIMIT),
        "пропущених_тіків": skipped,
        "відмов_google": len(quota_errors),
        "останні_відмови": quota_errors[-3:],
        "по_годинах": {
            key: {"середнє": round(sum(v) / len(v), 1), "максимум": max(v)}
            for key, v in sorted(per_hour.items())
        },
        "висновок": verdict,
        "довідка": "ліміт Google 60/хв на кожен вид; гальмо гарячого тіку — 45/хв",
    }


def tool_log(db: Session, args: dict) -> dict[str, Any]:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        raise ToolError("потрібен `pattern` — текст або регулярний вираз для пошуку в лозі")
    hours = _int_arg(args, "hours", default=6, low=1, high=24 * 14)
    limit = _int_arg(args, "limit", default=50, low=1, high=300)
    try:
        needle = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ToolError(
            f"не регулярний вираз: {exc}. Якщо шукаєш дужку чи крапку буквально — екрануй (\\.)"
        ) from exc

    since = datetime.now() - timedelta(hours=hours)
    source, lines = _read_log(since, args.get("log"))
    hits = [line for _, line in lines if needle.search(line)]
    return {
        "лог": source,
        "шаблон": pattern,
        "вікно_годин": hours,
        "знайдено": len(hits),
        "показано": min(limit, len(hits)),
        "рядки": [line[:_MAX_LINE] for line in hits[-limit:]],
    }


# ── Дрібні помічники ─────────────────────────────────────────────────────────


def _count_by(rows: list, key: Callable[[Any], Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        name = str(key(row) or "—")
        out[name] = out.get(name, 0) + 1
    return dict(sorted(out.items(), key=lambda pair: (-pair[1], pair[0])))


def _units(order: Order) -> int:
    digits = re.sub(r"\D", "", order.quantity or "")
    return int(digits) if digits else 0


def _local(moment: datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else None


def _int_arg(args: dict, name: str, *, default: int, low: int, high: int) -> int:
    raw = args.get(name, default)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"`{name}` мусить бути числом {low}…{high}, а не «{raw}»") from None
    if not low <= value <= high:
        raise ToolError(f"`{name}` поза межами {low}…{high}: {value}")
    return value


def tool_stats_audit(db: Session, args: dict) -> dict[str, Any]:
    """Звірка екрана «Статистика» рядок за рядком (`stats_audit`)."""
    view = str(args.get("view") or "overview")
    if view not in ("overview", "carryover", "rows"):
        raise ToolError(f"`view` буває overview, carryover або rows, а не «{view}»")
    limit = _int_arg(args, "limit", default=50, low=1, high=500)
    if view == "rows":
        if not args.get("day"):
            raise ToolError("для view=rows потрібен `day`: дата вкладки, напр. 2026-11-03")
        day = _resolve_day(args.get("day"))
        return stats_audit.day_rows(db, day, only_archived=bool(args.get("only_archived")), limit=limit)
    key, period = stats_rules.resolve_period(
        args.get("period"), args.get("from"), args.get("to"), business_today()
    )
    days = period.working_days()
    head = {"період": {"ключ": key, "від": period.start.isoformat(), "до": period.end.isoformat(), "робочих_днів": len(days)}}
    if view == "carryover":
        return {**head, **stats_audit.carryover(db, days, limit=limit)}
    min_quantity = _int_arg(args, "min_quantity", default=100, low=1, high=1_000_000)
    return {**head, **stats_audit.overview(db, days, min_quantity=min_quantity, limit=limit)}


def tool_handout_match(db: Session, args: dict) -> dict[str, Any]:
    """Діагностика зіставлення видачі для одного клієнта: чому робота показує
    (чи ні) STL-прев'ю. Тіло — у `handout_diag` (той самий ланцюг, що будує
    екран видачі). Імпорт локальний: тягне сканер export і stl_preview."""
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError(
            "потрібен `query`: ім'я клієнта (підрядок) або id/наряд роботи з видачі"
        )
    from app.services.handout_diag import diagnose_handout_client

    return diagnose_handout_client(db, query)


def tool_mail_material(db: Session, args: dict) -> dict[str, Any]:
    """Як картка листа розпізнала матеріал: той самий контекст (тема + слова
    замовника), той самий ланцюг `best_material`, покроково."""
    raw = args.get("email_id")
    try:
        email_id = int(str(raw))
    except (TypeError, ValueError):
        raise ToolError("потрібен `email_id` — номер листа (з адреси /mail?open=N)")
    from app.mail_body_view import letter_segments, useful_text
    from app.models import EmailMessage
    from app.services.material_suggest import explain_material

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise ToolError(f"листа {email_id} немає")
    # Той самий контекст, що `_material_context` у картці: тема + слова замовника.
    context = "\n".join([email.subject or "", useful_text(letter_segments(email.body_text))])
    out = explain_material(db, email.material_color_guess, context)
    out["лист"] = email_id
    return out


def tool_mail_units(db: Session, args: dict) -> dict[str, Any]:
    """Звірка «кількість з назв STL» на справжніх листах (STL_UNITS_BRIEF.md).

    Для кожної поштової роботи за останні `days` днів: кількість у роботі (її
    вписав або підтвердив оператор) проти порахованої з назв її файлів. Назви
    файлів НЕ віддаються — у них бувають імена пацієнтів; лише числа й зуби."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from app.business_day import business_today
    from app.services.order_dates import parse_sheet_tab
    from app.services.stl_units import count_units

    days = _int_arg(args, "days", default=3, low=1, high=30)
    low = business_today() - _td(days=days - 1)
    # Від робіт вікна (з запасом на прийняття заднім числом), не від усіх
    # вкладень бази: тих за роки — десятки тисяч.
    orders = list(db.scalars(
        select(Order).where(
            Order.source == "email",
            Order.created_at >= _dt.combine(low - _td(days=3), _dt.min.time()),
        )
    ))
    names: dict[int, list[str]] = {}
    if orders:
        for order_id, filename in db.execute(
            select(Attachment.order_id, Attachment.filename)
            .where(Attachment.order_id.in_([o.id for o in orders]))
        ):
            if order_id is not None:
                names.setdefault(order_id, []).append(filename)
    rows: list[dict[str, Any]] = []
    stats = {"збіг": 0, "розбіжність": 0, "без зубів у назвах": 0}
    for order in orders:
        day = parse_sheet_tab(order.sheet_tab)
        if day is None or day < low or order.id not in names:
            continue
        result = count_units(names[order.id])
        quantity = (order.quantity or "").strip()
        if not result.units:
            verdict = "без зубів у назвах"
        elif quantity.isdigit() and int(quantity) == result.units:
            verdict = "збіг"
        else:
            verdict = "розбіжність"
        stats[verdict] += 1
        rows.append({
            "id": order.id,
            "вкладка": order.sheet_tab,
            "клієнт": order.client_name,
            "кількість_у_роботі": quantity or None,
            "з_назв": result.units,
            "зуби": result.summary,
            "файлів_моделей": result.model_files,
            "без_номерів": len(result.without_teeth),
            "висновок": verdict,
        })
    rows.sort(key=lambda r: (r["висновок"] != "розбіжність", r["id"]))
    return {"днів": days, "підсумок": stats, "роботи": rows[:200]}


def tool_sum3d_files(db: Session, args: dict) -> dict[str, Any]:
    """Формат файлів дисків `.blk` і проєктів `.cam` Sum3D — щоб знайти, де
    лежить коефіцієнт усадки (`app/services/sum3d_files.py`). Лише числа й
    назви ключів: текстових значень і назв STL не віддає."""
    from app.settings_store import get_setting, get_sum3d_projects_path

    kind = str(args.get("kind") or "both").strip().lower()
    if kind not in ("both", "blk", "cam", "settings", "cam_deep", "cam_compare", "blk_shape", "board_disc"):
        raise ToolError(
            "`kind` — blk, cam, both, settings, cam_deep, cam_compare, blk_shape або board_disc (типово both)"
        )
    limit = _int_arg(args, "limit", default=2, low=1, high=5)
    blanks_root = (get_setting(db, "cam_blanks_path") or "").strip()
    projects_root = (get_sum3d_projects_path(db) or "").strip()
    if kind == "settings":
        return {
            "налаштування": sum3d_deep.scan_settings(sum3d_deep.settings_roots(blanks_root)),
            "реєстр": sum3d_deep.scan_registry(),
        }
    if kind == "blk_shape":
        return _sum3d_blank_shape(blanks_root, str(args.get("brand") or ""), args)
    if kind == "board_disc":
        return _sum3d_board_disc(db, blanks_root, projects_root)
    if kind in ("cam_deep", "cam_compare") and not projects_root:
        raise ToolError("тека проєктів Sum3D не задана (Налаштування → Шляхи)")
    if kind == "cam_deep":
        return _sum3d_cam_deep(db, projects_root, args, limit)
    if kind == "cam_compare":
        plain, shrunk = _sum3d_cam_groups(db, projects_root, limit)
        return sum3d_deep.compare_groups(plain, shrunk)
    out: dict[str, Any] = {
        "тека_дисків": blanks_root or None,
        "тека_проєктів": projects_root or None,
    }
    if kind in ("both", "blk"):
        if not blanks_root:
            out["диски"] = "тека дисків CAM не задана («Нові диски» → нижня смуга)"
        else:
            found = sum3d_files.newest_blanks(
                blanks_root, brand=str(args.get("brand") or ""), limit=limit
            )
            out["диски"] = [_sum3d_file_entry(f) for f in found] or "файлів .blk не знайдено"
            # Схема формату — слова, спільні для КІЛЬКОХ дисків; назва STL з
            # іменем пацієнта своя в кожному диску й у перетин не потрапить.
            if len(found) >= 3:
                out["спільні_слова_дисків"] = sum3d_files.common_words(f.path for f in found)[:200]
            elif found:
                out["спільні_слова_дисків"] = "потрібно ≥3 дисків (limit=3…5)"
            # ПММА без усадки проти цирконію — де в ПММА 1.0, там коефіцієнт.
            if len(found) >= 2:
                out["порівняння_заголовків"] = sum3d_files.compare_headers(
                    [(f.name, f.path) for f in found]
                )
    if kind in ("both", "cam"):
        if not projects_root:
            out["проєкти"] = "тека проєктів Sum3D не задана (Налаштування → Шляхи)"
        else:
            found = sum3d_files.newest_projects(
                projects_root, sum3d_id=str(args.get("sum3d_id") or ""), limit=limit
            )
            out["проєкти"] = [_sum3d_file_entry(f) for f in found] or "файлів .cam не знайдено"
    return out


def _sum3d_blank_shape(root: str, brand: str, args: dict) -> dict[str, Any]:
    """Геометрія файлу диска: полілінії вирізів і службові проміжки
    (`sum3d_blank_shape`). Один диск за виклик — відповідь велика; два —
    лише на явне `limit=2`, щоб звірити новий диск з ужитим."""
    if not root:
        raise ToolError("тека дисків CAM не задана («Нові диски» → нижня смуга)")
    limit = _int_arg(args, "limit", default=1, low=1, high=2)
    found = sum3d_files.newest_blanks(root, brand=brand, limit=limit)
    return {
        "тека_дисків": root,
        "диски": [
            {
                "файл": f.name,
                "розмір": f.size,
                "змінено": datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M:%S"),
                **sum3d_blank_shape.describe_shape(f.path),
            }
            for f in found
        ] or "файлів .blk не знайдено",
    }


def _sum3d_board_disc(db: Session, blanks_root: str, projects_root: str) -> dict[str, Any]:
    """Що табло покаже як диск кожного верстата (`sum3d_disc`, `sum3d_names`):
    повна назва програми (для CORiTEC — з проєкту Sum3D, синхронно), файл
    диска, обриси, вільна частка і малюнок дисків — звірити з Sum3D оком."""
    from pathlib import Path

    from PIL import Image, ImageDraw

    from app.services import sum3d_disc, sum3d_names
    from app.services.device_diag import _encode
    from app.services.machines import snapshot

    rows: list[dict[str, Any]] = []
    tiles: list[Any] = []
    for card in snapshot(db):
        if card.is_sisma_machine:
            continue
        iso = getattr(card, "iso_name", None)
        program, how = iso, "екран/заголовок"
        parts = sum3d_names.bare_parts(iso)
        if parts and projects_root:
            try:
                found = sum3d_names.find_in_project(Path(projects_root) / f"{parts[0]}_{parts[1]}.cam", *parts)
            except OSError as exc:
                found, how = None, f"проєкт не прочитано: {exc.__class__.__name__}"
            if found:
                program, how = found, "з проєкту Sum3D"
            elif how == "екран/заголовок":
                how = "у проєкті назви не знайдено"
        path = sum3d_disc.find_blank(blanks_root, program)
        shape = sum3d_disc.shape_of(path) if path else None
        row: dict[str, Any] = {
            "верстат": card.target.name,
            "програма_з_екрана": iso,
            "програма_для_табло": program,
            "звідки_назва": how,
            "ключ_диска": sum3d_disc.disc_key(program),
            "файл_диска": path.name if path else None,
        }
        if shape is not None:
            row.update({
                "обрисів": len(shape.contours),
                "радіус_мм": shape.radius,
                "вільно_%": round(shape.free_share * 100),
                "кадр": f"д{len(tiles) + 1}",
            })
            img = Image.new("RGB", (300, 300), (24, 20, 14))
            draw = ImageDraw.Draw(img)
            k = 130 / shape.radius
            draw.ellipse([20, 20, 280, 280], outline=(255, 60, 220), width=2)
            for contour in shape.contours:
                draw.polygon([(150 + x * k, 150 - y * k) for x, y in contour], outline=(255, 212, 137))
            draw.text((8, 6), f"{row['кадр']} {card.target.name}", fill=(240, 220, 160))
            tiles.append(img)
        rows.append(row)
    result: dict[str, Any] = {"тека_дисків": blanks_root or None, "тека_проєктів": projects_root or None,
                              "верстати": rows}
    if tiles:
        cols = min(3, len(tiles))
        sheet = Image.new("RGB", (cols * 300, ((len(tiles) + cols - 1) // cols) * 300), (12, 13, 18))
        for i, tile in enumerate(tiles):
            sheet.paste(tile, ((i % cols) * 300, (i // cols) * 300))
        note: list[str] = []
        result["_image"] = _encode(sheet, note)
    return result


def _sum3d_cam_deep(db: Session, root: str, args: dict, limit: int) -> dict[str, Any]:
    """Розвідка одного проєкту: масштаб у матрицях, задане число, вершини STL
    робіт цього проєкту (`sum3d_deep.probe_cam`)."""
    tail = str(args.get("sum3d_id") or "").strip()
    if not tail:
        raise ToolError("для `cam_deep` потрібен `sum3d_id` (13-12-29)")
    # Клієнт зі старою схемою інструмента шле число рядком (прод 04.11.26).
    raw = args.get("value")
    if isinstance(raw, str):
        try:
            raw = float(raw.strip().replace(",", "."))
        except ValueError:
            raise ToolError("`value` — число (1.25)") from None
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float))):
        raise ToolError("`value` — число (1.25)")
    found = sum3d_files.newest_projects(root, sum3d_id=tail, limit=limit)
    if not found:
        return {"проєкти": "файлів .cam не знайдено"}
    return {
        "проєкти": [
            {
                "файл": f.name,
                **sum3d_deep.probe_cam(
                    f.path,
                    stl_paths=_sum3d_project_stls(db, f.name, tail),
                    expected=float(raw) if raw is not None else None,
                ),
            }
            for f in found
        ]
    }


def _cam_day(name: str) -> Optional[date]:
    try:
        return datetime.strptime(name[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _sum3d_project_stls(db: Session, cam_name: str, tail: str) -> list[str]:
    """STL робіт проєкту. Sum3D ID — лише час доби, тож робота мусить бути
    з дня проєкту (±1), як і скрізь, де його зіставляють."""
    from app.services import stl_height
    from app.settings_store import get_export_folder_path, get_technician_files_path

    day = _cam_day(cam_name)
    export_root = (get_export_folder_path(db) or "").strip()
    tech_root = get_technician_files_path(db)
    paths: list[str] = []
    orders = db.scalars(select(Order).where(Order.sum3d_id.like(f"%{tail}%"))).all()
    for order in orders:
        tab = parse_sheet_tab(order.sheet_tab)
        if day is not None and (tab is None or abs((tab - day).days) > 1):
            continue
        folder = stl_height.work_folder(order, tech_root, Path(export_root) if export_root else None)
        if folder is not None:
            paths += [str(p) for p in stl_height.rotated_stls(folder)]
    return paths[:5]


def _sum3d_cam_groups(
    db: Session, root: str, limit: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Проєкти ПММА (без усадки) і цирконію — родина береться з роботи."""
    by_name = {f.name: f for f in sum3d_files.newest_projects(root, limit=10**6)}
    plain: dict[str, str] = {}
    shrunk: dict[str, str] = {}
    orders = db.scalars(
        select(Order).where(Order.sum3d_id.isnot(None)).order_by(Order.id.desc()).limit(1000)
    ).all()
    for order in orders:
        family = order.material.name if order.material is not None else None
        target = {"ПММА": plain, "Цирконій": shrunk}.get(family or "")
        day = parse_sheet_tab(order.sheet_tab)
        if target is None or day is None or len(target) >= limit:
            continue
        tail = (order.sum3d_id or "").strip()[-8:]
        for delta in (0, -1, 1):
            name = f"{day + timedelta(days=delta):%Y-%m-%d}_{tail}.cam"
            if name in by_name:
                target.setdefault(name, by_name[name].path)
                break
    return sorted(plain.items()), sorted(shrunk.items())


def _sum3d_file_entry(found: sum3d_files.FoundFile) -> dict[str, Any]:
    return {
        "файл": found.name,
        "розмір": found.size,
        "змінено": datetime.fromtimestamp(found.mtime).strftime("%Y-%m-%d %H:%M:%S"),
        **sum3d_files.describe_file(found.path),
    }


# ── Реєстр ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[Session, dict], dict[str, Any]]


_DAY_PROP = {
    "type": "string",
    "description": "today / yesterday / tomorrow або дата (2026-09-11, 11.09.26). "
    "Робочий день вкладки: у вихідні «today» = пʼятниця.",
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="kmill_health",
        description=(
            "Стан KuubMill одним запитом: версія, шлях до бази й лога, який день "
            "вважається сьогоднішньою вкладкою, чи синк на паузі та з яким темпом, "
            "скільки запитів до Google за останню хвилину, останній запис журналу "
            "синку, скільки робіт у черзі вкладки за статусом і готовністю. "
            "Починати діагностику будь-якої скарги варто саме звідси."
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=tool_health,
    ),
    Tool(
        name="kmill_queue",
        description=(
            "Черга одного дня: лічильники за статусом, джерелом і готовністю "
            "(не готово / можна брати / в роботі), сума одиниць і самі рядки в "
            "порядку Google Таблиці. Відповідає на «скільки робіт сьогодні», "
            "«що не взяли в роботу», «чи зійшлась кількість із таблицею»."
        ),
        schema={
            "type": "object",
            "properties": {
                "day": _DAY_PROP,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "Скільки рядків віддати (типово 50). Лічильники завжди по всьому дню.",
                },
            },
            "additionalProperties": False,
        },
        run=tool_queue,
    ),
    Tool(
        name="kmill_order",
        description=(
            "Паспорт роботи за номером наряду, хвостом Sum3D ID (12-01-45) або id: "
            "усі поля, хронологія статусів із іменем оператора, журнал дій "
            "(що, було→стало, чи скасовано) і коментарі. Для питань «хто це зробив», "
            "«коли вона стала видана», «чому Sum3D не в таблиці»."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Номер наряду (24122), хвіст Sum3D ID (12-01-45) або id роботи.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        run=tool_order,
    ),
    Tool(
        name="kmill_handout_match",
        description=(
            "Чому робота на видачі не показує STL-прев'ю. Для клієнта (ім'я-підрядок "
            "або id/наряд роботи) показує: як зіставилось ім'я з текою в export "
            "(впевненість, кандидати), які партії знайшов сканер (тека, ДАТА "
            "СТВОРЕННЯ = робочий день партії, матеріал, к-сть STL), і по кожній "
            "роботі — зіставлено з текою чи ні і ЧОМУ (немає партії того дня, "
            "старіша тека, матеріал не збігся, ім'я не знайшло теки)."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Ім'я клієнта (підрядок, напр. «Голій») або id/наряд роботи.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        run=tool_handout_match,
    ),
    Tool(
        name="kmill_mail_material",
        description=(
            "Чому картка листа підставила (чи ні) матеріал і колір: здогад з листа, "
            "які слова тексту зіставились із каноном цеху і чи прийнято кожне, "
            "знайдені відтінки, чіпи-підказки й підсумкове значення поля. Текст "
            "листа цілком не віддається — лише слова, що зіставились."
        ),
        schema={
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "integer",
                    "description": "Номер листа (з адреси /mail?open=N або з kmill-логу).",
                }
            },
            "required": ["email_id"],
            "additionalProperties": False,
        },
        run=tool_mail_material,
    ),
    Tool(
        name="kmill_sync_journal",
        description=(
            "Останні записи Журналу синку з київським часом: що синк робив, які "
            "вкладки читав, на що скаржився, які рядки стирав. Перше місце, куди "
            "дивитись, коли «день зник», «робота не приїхала», «Sum3D не записався»."
        ),
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "status": {
                    "type": "string",
                    "description": "Фільтр за станом запису (наприклад «помилка»). Пусто — усі.",
                },
            },
            "additionalProperties": False,
        },
        run=tool_sync_journal,
    ),
    Tool(
        name="kmill_quota",
        description=(
            "Скільки запитів до Google Таблиці CRM робить за хвилину — з рядків "
            "лічильника в лозі: середнє, максимум, розкладка по годинах, пропущені "
            "через квоту тіки й відмови Google, плюс готовий висновок. Саме цим "
            "міряється запас перед підключенням другого оператора."
        ),
        schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 336,
                    "description": "Вікно в годинах назад (типово 24).",
                },
                "log": {
                    "type": "string",
                    "description": "Звідки читати: auto (типово), поточний, встановлений, оновлення (сторож встановлення), інсталятор.",
                },
            },
            "additionalProperties": False,
        },
        run=tool_quota,
    ),
    Tool(
        name="kmill_log",
        description=(
            "Пошук у лозі CRM (усі ротовані файли) за текстом або регулярним "
            "виразом у заданому вікні годин. Для «що там упало», «чи був 429», "
            "«коли агент переставав відповідати»."
        ),
        schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Текст або regex, без урахування регістру."},
                "hours": {"type": "integer", "minimum": 1, "maximum": 336},
                "limit": {"type": "integer", "minimum": 1, "maximum": 300},
                "log": {
                    "type": "string",
                    "description": "Звідки читати: auto (типово), поточний, встановлений, оновлення (сторож встановлення), інсталятор.",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        run=tool_log,
    ),
    Tool(
        name="kmill_devices",
        description=(
            "Печі й верстати: статус, що прочиталось з екрана, помилка звʼязку, "
            "а головне — ВІК найсвіжішого кадру й де він лежить. Плюс кадри "
            "«екран JOBS видно, а назву не прочитано». Звідси починається розбір "
            "будь-якого «піч не показує температуру» чи «верстат не бачить роботу»."
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=device_diag.devices,
    ),
    Tool(
        name="kmill_frame",
        description=(
            "Сам кадр пристрою картинкою — весь або виріз названої зони печі "
            "(temp, status, step, command, clock, remaining, button). За ключем "
            "пристрою — лише найсвіжіший кадр; старіші — через аргумент `puzzle` "
            "(id зі скриньки kmill_screens). Потрібен, коли треба ПОБАЧИТИ, що "
            "написано на табло, а не повірити на слово."
        ),
        schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Ключ пристрою зі списку kmill_devices (зазвичай адреса).",
                },
                "zone": {
                    "type": "string",
                    "description": "Назва зони печі для вирізу. Пусто — весь кадр.",
                },
                "puzzle": {
                    "type": "integer",
                    "description": (
                        "id загадки зі скриньки невідомих екранів (kmill_screens). "
                        "Задано — віддається ЇЇ кадр, а `key` не потрібен."
                    ),
                },
                "part": {
                    "type": "string",
                    "description": "Для загадки: frame (типово) або zone — виріз зони.",
                },
            },
            "additionalProperties": False,
        },
        run=device_diag.frame,
    ),
    Tool(
        name="kmill_zones",
        description=(
            "Розбір найсвіжішого кадру: що читається в кожній зоні, сирий рядок "
            "із «?» замість невпізнаних символів, і ЧОМУ поле лишилось порожнім — "
            "обрізано зоною, немає еталона чи не той шаблон. Для верстатів — "
            "чому не прочитано назву програми з екрана JOBS. Еталони через MCP "
            "не додаються: навчання лишається скриптом із тестом."
        ),
        schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Ключ пристрою зі списку kmill_devices.",
                }
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        run=device_diag.zones,
    ),
    Tool(
        name="kmill_screens",
        description=(
            "Скринька невідомих екранів: кадри, на яких читач спіткнувся, по "
            "одному рядку на РІЗНИЙ екран із лічильником «бачено N разів», "
            "причиною і підписом, який лишила людина. Підпис — дані, не правило: "
            "зону, еталон чи правило статусу з нього роблять у репозиторії з "
            "тестом. Картинку бери через kmill_frame з аргументом `puzzle`."
        ),
        schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Лише загадки цього пристрою. Пусто — усі.",
                },
                "include_dismissed": {
                    "type": "boolean",
                    "description": "Показати й позначені «це неважливо».",
                },
            },
            "additionalProperties": False,
        },
        run=device_diag.puzzles,
    ),
)

TOOLS = TOOLS + (
    Tool(
        name="kmill_mail_units",
        description=(
            "Кількість одиниць з назв STL проти кількості в поштових роботах за "
            "останні дні: «збіг / розбіжність / без зубів у назвах» по кожній "
            "роботі, з порахованими зубами. Назви файлів не віддаються (у них "
            "бувають імена пацієнтів). Для звірки підрахунку зубів на даних цеху."
        ),
        schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 30,
                         "description": "Скільки останніх робочих днів (типово 3)."},
            },
            "additionalProperties": False,
        },
        run=tool_mail_units,
    ),
    Tool(
        name="kmill_sum3d_files",
        description=(
            "Що всередині файлів Sum3D: дисків `.blk` (тека «Нові диски») і "
            "проєктів `.cam` (Cam-work) — формат, назви ключів і ЧИСЛА, окремо ті, "
            "що біля слів shrink/scale/factor і в межах 1.10–1.40. Бінарний файл: "
            "ентропія, zlib/gzip-потоки всередині (розпаковані), слова в "
            "ASCII/UTF-16LE/BE; у малому файлі — усі «круглі» double/float (обидва "
            "порядки байтів) як коефіцієнт, обернений (0.8) чи відсотки; для ≥3 "
            "дисків — слова, спільні для всіх; для ≥2 — звірка початку файлів "
            "число-в-число (де в ПММА 1.0, а в цирконії 1.1–1.4). kind=settings — "
            "налаштування Sum3D і реєстр; kind=cam_deep (sum3d_id, value) — матриці "
            "масштабу, задане число, вершини STL у проєкті; kind=cam_compare — "
            "проєкти ПММА проти цирконію. Для пошуку, де Sum3D тримає "
            "коефіцієнт усадки диска. kind=blk_shape (brand — підрядок назви "
            "диска, limit 1–2) — геометрія диска: ряди чисел, полілінії вирізів "
            "на одній висоті (межі, площа, проріджені точки) і службові проміжки "
            "між ними шістнадцятково, літери затерто; щоб розібрати, де на диску "
            "вже вирізано. kind=board_disc — що табло покаже як диск кожного "
            "верстата: повна назва програми (CORiTEC — з проєкту Sum3D), файл "
            "диска, обриси, вільно %, малюнок дисків. Лише читання; текстових значень і назв STL "
            "(імена пацієнтів) не віддає."
        ),
        schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "blk, cam, both (типово), settings, cam_deep, cam_compare, blk_shape, board_disc."},
                "value": {"type": "number",
                          "description": "Для cam_deep: очікуваний коефіцієнт (1.25) — точний пошук і вершини STL × value."},
                "brand": {"type": "string",
                          "description": "Підрядок назви диска (monolith, emo) — фільтр .blk; "
                                         "кілька через кому (pmma,monolith) — по limit кожного."},
                "sum3d_id": {"type": "string",
                             "description": "Хвіст Sum3D ID (13-12-29) — конкретний проєкт .cam."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5,
                          "description": "Скільки найновіших файлів кожного виду (типово 2; "
                                         "для blk_shape типово 1, не більше 2)."},
            },
            "additionalProperties": False,
        },
        run=tool_sum3d_files,
    ),
)

TOOLS = TOOLS + (
    Tool(
        name="kmill_stats_audit",
        description=(
            "Звірка екрана «Статистика» рядок за рядком, разом з архівними роботами, яких "
            "не показує kmill_queue. overview: підсумок екрана, що дало б кожне правило "
            "(із Sum3D, без переробок, архівні), дні, великі кількості й дивні рядки. "
            "carryover: наряди лаби на кількох вкладках. rows: усі рядки одного дня. "
            "Для питань «чому статистика показує таке число»."
        ),
        schema={
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["overview", "carryover", "rows"], "description": "Типово overview."},
                "period": {"type": "string", "enum": ["7", "30", "90", "custom"], "description": "Як на екрані, типово 30. custom потребує from і to."},
                "from": {"type": "string", "description": "Початок для custom: 2026-10-01."},
                "to": {"type": "string", "description": "Кінець для custom: 2026-10-31."},
                "day": _DAY_PROP,
                "only_archived": {"type": "boolean", "description": "Для rows: лише архівні."},
                "min_quantity": {"type": "integer", "minimum": 1, "maximum": 1000000, "description": "Від якої кількості рядок вважати великим, типово 100."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Скільки рядків віддати в кожному списку, типово 50. Лічильники завжди по всьому."},
            },
            "additionalProperties": False,
        },
        run=tool_stats_audit,
    ),
)


def tool_client_facts(db: Session, args: dict) -> dict[str, Any]:
    """Наскільки витяг із листів працює на цих даних. Текстів листів тут немає."""
    from app.services import client_insights

    return client_insights.audit(db, _int_arg(args, "limit", default=30, low=1, high=200))


TOOLS = TOOLS + (
    Tool(
        name="kmill_client_facts",
        description=(
            "Замір витягу для картки клієнта: скільки клієнтів мають листи, у кого знайдено телефон і "
            "доставку. Телефони замасковані, текстів листів і імен немає. Для питань «чи працює витяг»."
        ),
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Скільки клієнтів показати, типово 30."},
            },
            "additionalProperties": False,
        },
        run=tool_client_facts,
    ),
)

def tool_stl_shapes(db: Session, args: dict) -> dict[str, Any]:
    """STL робіт цеху для 3D на табло: заміри й малюнок, без назв файлів і тек
    (`app/services/stl_shapes.py`). Без `kind` — які види робіт є за період."""
    import time
    from collections import Counter
    from datetime import datetime, timedelta

    from PIL import Image
    from sqlalchemy import or_

    from app.models import Order
    from app.services import stl_shapes
    from app.services.device_diag import _encode
    from app.services.stl_height import work_folder
    from app.settings_store import get_export_folder_path, get_technician_files_path

    days = _int_arg(args, "days", default=21, low=1, high=120)
    since = datetime.now() - timedelta(days=days)
    kind = str(args.get("kind") or "").strip().lower()
    if not kind:
        rows = db.execute(
            select(Order.kind, Order.material_color).where(Order.created_at >= since)
        ).all()
        kinds = Counter((k or "—").strip().lower() for k, _m in rows)
        return {
            "днів": days,
            "робіт": len(rows),
            "види_робіт": dict(kinds.most_common(40)),
            "підказка": "kind — частина виду роботи або матеріалу (абатмент, міст, вкладка, капа, віск).",
        }
    works_limit = _int_arg(args, "works", default=3, low=1, high=6)
    files_limit = _int_arg(args, "files", default=2, low=1, high=4)
    export_raw = (get_export_folder_path(db) or "").strip()
    export_root = Path(export_raw) if export_raw else None
    tech_root = get_technician_files_path(db)
    pattern = f"%{kind}%"
    candidates = db.scalars(
        select(Order)
        .where(Order.created_at >= since,
               or_(Order.kind.ilike(pattern), Order.material_color.ilike(pattern)))
        .order_by(Order.created_at.desc())
        .limit(200)
    ).all()
    started = time.monotonic()
    works: list[dict[str, Any]] = []
    tiles: list[Any] = []
    seen_folders: set[Path] = set()
    no_folder = no_stl = 0
    for order in candidates:
        if len(works) >= works_limit or time.monotonic() - started > 40:
            break
        folder = work_folder(order, tech_root, export_root)
        if folder is None:
            no_folder += 1
            continue
        if folder in seen_folders:
            continue
        seen_folders.add(folder)
        files = stl_shapes.stl_files(folder, files_limit)
        if not files:
            no_stl += 1
            continue
        number = len(works) + 1
        described = []
        for f_index, path in enumerate(files, 1):
            info, poses = stl_shapes.describe(path)
            label = f"р{number}·ф{f_index}"
            info["кадр"] = label
            described.append(info)
            if poses is not None:
                as_file, stood = poses
                tiles.append(stl_shapes.render(as_file, label=f"{label} файл"))
                tiles.append(stl_shapes.render(stood, label=f"{label} поворот"))
        works.append({
            "№": number,
            "id": order.id,
            "вид_роботи": order.kind,
            "матеріал": order.material_color,
            "кількість": order.quantity,
            "джерело": order.source,
            "файли": described,
        })
    result: dict[str, Any] = {
        "вид": kind,
        "днів": days,
        "кандидатів": len(candidates),
        "без_теки": no_folder,
        "тека_без_stl": no_stl,
        "роботи": works,
    }
    if tiles:
        cols = min(4, len(tiles))
        rows_n = (len(tiles) + cols - 1) // cols
        side = tiles[0].size[0]
        sheet = Image.new("RGB", (cols * side, rows_n * side), (12, 13, 18))
        for i, tile in enumerate(tiles):
            sheet.paste(tile, ((i % cols) * side, (i // cols) * side))
        note: list[str] = []
        result["_image"] = _encode(sheet, note)
        if note:
            result["примітка"] = note
    return result


TOOLS = TOOLS + (
    Tool(
        name="kmill_stl_shapes",
        description=(
            "STL робіт цеху для 3D на табло: для свіжих робіт заданого виду — чи модель замкнена, "
            "чи знайдено край, габарити, висота, тип Exocad, і малюнок кожної моделі двічі: як лежить "
            "у файлі і після повороту за краєм (пари «файл»/«поворот», по два кадри на файл). "
            "Без kind — які види робіт є за період. Назв файлів, тек і клієнтів не віддає. Лише читання."
        ),
        schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "Частина виду роботи або матеріалу: абатмент, міст, вкладка, капа, віск, анатомія."},
                "days": {"type": "integer", "minimum": 1, "maximum": 120, "description": "За скільки днів шукати, типово 21."},
                "works": {"type": "integer", "minimum": 1, "maximum": 6, "description": "Скільки робіт, типово 3."},
                "files": {"type": "integer", "minimum": 1, "maximum": 4, "description": "Скільки STL з кожної роботи, типово 2."},
            },
            "additionalProperties": False,
        },
        run=tool_stl_shapes,
    ),
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}

# Анотації однакові для всіх: інструментів, що пишуть, тут немає й не буде
# (див. докстрінг модуля). `openWorldHint=False` — дані беруться з власної бази
# й лога, не з інтернету.
TOOL_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def tool_descriptors() -> list[dict[str, Any]]:
    """Опис інструментів у форматі `tools/list`."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.schema,
            "annotations": dict(TOOL_ANNOTATIONS, title=tool.name),
        }
        for tool in TOOLS
    ]
