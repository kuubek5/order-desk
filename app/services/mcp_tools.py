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
from typing import Any, Callable

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
from app.models import ActionLog, Comment, Order, StatusEvent, SyncLog
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


def _pick_log(choice: str | None) -> tuple[str, Path]:
    """Яким файлом відповідаємо — і назва їде у відповідь.

    Хибна тиша гірша за відмову: інструмент, який нічого не знайшов, бо дивився
    не в той файл, виглядає як «усе спокійно» (те саме правило, що з печами —
    порожнє поле краще за вгадане число).
    """
    candidates = _log_candidates()
    asked = (choice or "auto").strip().lower()
    if asked not in ("auto", ""):
        if asked not in candidates:
            raise ToolError(
                f"джерело «{choice}» невідоме. Доступні: {', '.join(candidates)} або auto"
            )
        path = candidates[asked]
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
                    "description": "Звідки читати: auto (типово), поточний, встановлений.",
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
                    "description": "Звідки читати: auto (типово), поточний, встановлений.",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        run=tool_log,
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
