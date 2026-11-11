"""Графік відключень світла для черги цеху (11.11.26, лише читання).

Навіщо. Цех живе за графіком відключень: коли дадуть світло, коли вимкнуть і
чи встигне програма верстата. Власник попросив віджет — плашку в шапці
«Черги» й «Нових з пошти», сітку годин по кліку і смугу на табло цеху.

Звідки дані. Графік у Києві свій на кожну ЧЕРГУ (їх 60), тож у налаштуваннях
лежить лише номер черги (`4.1`), адреса не зберігається й нікуди не йде.
Головне джерело — публічні запити Yasno (сервіс ДТЕК, без входу): проміжки з
точністю до хвилини і статус дня («діє графік», «чекаємо графік», «аварійні»).
Запасне — збірник outage-data-ua на GitHub (погодинно). Обидва неофіційні:
формат можуть змінити без попередження, тому розбір нічого не вгадує, а
незрозуміла відповідь — це «джерело не відповіло».

Три правила, заради яких модуль саме такий:

* **Старий графік гірший за жодного.** Коли обидва джерела мовчать довше
  `STALE_AFTER` або в знімку немає сьогоднішнього дня — віджет каже «графіка
  немає», а не показує вчорашні години як сьогоднішні.
* **«Світла немає» — звичайний стан, не тривога.** У день зі жорстким
  графіком світла немає більшу частину доби. Тон тривоги — лише перед
  вимкненням (`SOON_MINUTES`) і на аварійних відключеннях.
* **Аварійних у графіку немає.** Статус «аварійні» джерело ставить саме;
  раптове зникнення світла ловить `power_watch`, не цей модуль.

Тексти складає сервер: шаблони нічого не рахують.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from app.business_day import BUSINESS_TIMEZONE, aware_to_business, business_now

logger = logging.getLogger(__name__)

#: Київ і ДТЕК Київські електромережі в довіднику Yasno (`…/addresses/v2/regions`).
REGION_ID = 25
DSO_ID = 902
YASNO_URL = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/"
    f"regions/{REGION_ID}/dsos/{DSO_ID}/planned-outages"
)
BACKUP_URL = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv.json"

#: Номер черги (`4.1`). Порожньо — віджета немає ніде й запити не йдуть.
GROUP_KEY = "power_group"
#: Смуга на табло цеху: «1» — показувати. Окремо від CRM (там — шестерня акаунта).
BOARD_KEY = "power_board_enabled"

REFRESH_SECONDS = 600
#: Скільки знімок вважається живим без жодного вдалого оновлення.
STALE_AFTER = timedelta(hours=3)
#: За скільки хвилин до вимкнення вмикається попередження.
SOON_MINUTES = 30
DAY = 1440

_YASNO_STATUS = {
    "ScheduleApplies": "schedule",
    "NoOutages": "none",
    "WaitingForSchedule": "waiting",
    "EmergencyShutdowns": "emergency",
}


class SourceError(Exception):
    """Джерело відповіло не тим, що ми вміємо читати."""


@dataclass(frozen=True)
class Day:
    """Один день черги. `off` — проміжки без світла, хвилини від початку доби."""

    date: date
    status: str                      # schedule | none | waiting | emergency
    off: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    group: str
    days: tuple[Day, ...]
    fetched_at: datetime             # київський, без поясу — як `business_now()`
    source: str                      # yasno | backup
    updated_on: Optional[datetime] = None

    def day(self, wanted: date) -> Optional[Day]:
        for item in self.days:
            if item.date == wanted:
                return item
        return None


def _naive(moment: datetime) -> datetime:
    """Київський час без поясу. `business_now()` віддає час ІЗ поясом, а дати з
    відповідей джерел і тестів — без; різниця таких двох падає TypeError-ом,
    тож усередині модуля всі моменти — без поясу."""
    return moment.replace(tzinfo=None)


# ── Розбір відповідей ────────────────────────────────────────────────────────

def clean_group(raw: Any) -> str:
    """`4.1` з того, що ввела людина (`4,1`, ` 4.1 `); інше — порожньо."""
    text = str(raw or "").strip().replace(",", ".")
    head, _, tail = text.partition(".")
    if head.isdigit() and tail.isdigit() and 0 < int(head) <= 99 and 0 < int(tail) <= 9:
        return f"{int(head)}.{int(tail)}"
    return ""


def _merge(spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        start, end = max(0, start), min(DAY, end)
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return tuple(out)


def parse_yasno(payload: Any, group: str, now: datetime) -> Snapshot:
    """Відповідь `planned-outages`: ключ — черга, у ній `today` і `tomorrow`.

    Без світла — лише проміжки типу `Definite`. Інші типи («можливо») у графік
    не беремо: віджет обіцяє час, а не ймовірність.
    """
    entry = payload.get(group) if isinstance(payload, dict) else None
    if not isinstance(entry, dict):
        raise SourceError(f"у відповіді Yasno немає черги {group}")
    days: list[Day] = []
    for key in ("today", "tomorrow"):
        block = entry.get(key)
        if not isinstance(block, dict):
            continue
        try:
            day_date = date.fromisoformat(str(block.get("date"))[:10])
        except ValueError as exc:
            raise SourceError(f"Yasno: незрозуміла дата в {key}") from exc
        status = _YASNO_STATUS.get(str(block.get("status")))
        if status is None:
            raise SourceError(f"Yasno: невідомий статус {block.get('status')!r}")
        spans: list[tuple[int, int]] = []
        for slot in block.get("slots") or ():
            if not isinstance(slot, dict) or slot.get("type") != "Definite":
                continue
            try:
                spans.append((int(slot["start"]), int(slot["end"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceError("Yasno: проміжок без меж") from exc
        days.append(Day(day_date, status, _merge(spans) if status == "schedule" else ()))
    if not days:
        raise SourceError("Yasno: у черзі немає ні сьогодні, ні завтра")
    updated = None
    try:
        updated = aware_to_business(datetime.fromisoformat(str(entry.get("updatedOn"))))
    except ValueError:
        updated = None
    return Snapshot(group, tuple(days), now, "yasno", updated)


def parse_backup(payload: Any, group: str, now: datetime) -> Snapshot:
    """Збірник outage-data-ua: `fact.data[<північ, Unix>]["GPV4.1"]["1".."24"]`.

    Стани години: `no` — немає, `first`/`second` — немає першу чи другу
    половину; усе інше («можливо») — світло є, з тієї ж причини, що й у Yasno.
    Статусу дня тут немає, тож аварійні відключення це джерело не покаже.
    """
    try:
        data = payload["fact"]["data"]
    except (KeyError, TypeError) as exc:
        raise SourceError("збірник: немає fact.data") from exc
    days: list[Day] = []
    for stamp, groups in (data or {}).items():
        hours = groups.get("GPV" + group) if isinstance(groups, dict) else None
        if not isinstance(hours, dict):
            continue
        try:
            day_date = datetime.fromtimestamp(int(stamp), BUSINESS_TIMEZONE).date()
        except (TypeError, ValueError, OSError) as exc:
            raise SourceError("збірник: незрозумілий ключ дня") from exc
        spans: list[tuple[int, int]] = []
        for hour in range(1, 25):
            state, base = hours.get(str(hour)), (hour - 1) * 60
            if state == "no":
                spans.append((base, base + 60))
            elif state == "first":
                spans.append((base, base + 30))
            elif state == "second":
                spans.append((base + 30, base + 60))
        off = _merge(spans)
        days.append(Day(day_date, "schedule" if off else "none", off))
    if not days:
        raise SourceError(f"у збірнику немає черги {group}")
    updated = None
    try:
        updated = datetime.strptime(str(payload["fact"].get("update")), "%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        updated = None
    return Snapshot(group, tuple(sorted(days, key=lambda d: d.date)), now, "backup", updated)


# ── Знімок у памʼяті й оновлення ─────────────────────────────────────────────

@dataclass
class _State:
    snapshot: Optional[Snapshot] = None
    failed_since: Optional[datetime] = None
    last_error: str = ""


_state = _State()
_lock = threading.Lock()


def reset() -> None:
    """Для тестів і для зміни черги: забути знімок."""
    with _lock:
        _state.snapshot, _state.failed_since, _state.last_error = None, None, ""


def current() -> tuple[Optional[Snapshot], Optional[datetime]]:
    with _lock:
        return _state.snapshot, _state.failed_since


def refresh(group: str, fetch_json: Callable[[str], Any], now: Optional[datetime] = None) -> bool:
    """Один прохід: Yasno, а коли він не відповів — збірник. True — знімок свіжий.

    Невдача НЕ стирає попередній знімок: він ще годиться, доки не постаріє
    (`STALE_AFTER`). Зате запамʼятовується, відколи джерела мовчать — це й
    покаже віджет.
    """
    now = _naive(now or business_now())
    group = clean_group(group)
    if not group:
        reset()
        return False
    errors: list[str] = []
    for name, url, parse in (("yasno", YASNO_URL, parse_yasno), ("backup", BACKUP_URL, parse_backup)):
        try:
            snapshot = parse(fetch_json(url), group, now)
        except (SourceError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Мережа (`requests` — нащадки OSError), JSON, формат: «джерело не відповіло».
            errors.append(f"{name}: {exc.__class__.__name__}: {exc}")
            continue
        with _lock:
            _state.snapshot, _state.failed_since, _state.last_error = snapshot, None, ""
        if errors:
            logger.warning("Графік світла взято із запасного джерела: %s", "; ".join(errors))
        return True
    with _lock:
        if _state.failed_since is None:
            _state.failed_since = now
        _state.last_error = "; ".join(errors)
    logger.warning("Графік світла не оновлено (черга %s): %s", group, "; ".join(errors))
    return False


# ── Що показати ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Segment:
    left: float      # % ширини доби
    width: float


@dataclass(frozen=True)
class Cell:
    """Година в сітці: on | off | first | second | wait."""

    kind: str
    past: bool = False
    now: bool = False


@dataclass
class PowerView:
    group: str
    kind: str                         # off | on | soon | alarm | void
    say: str
    sub: str = ""
    left: str = ""                    # «38 хв», «1 год 20 хв»
    meta: str = ""                    # коротко, одним рядком
    at: str = ""                      # час найближчої зміни, «15:00»
    change_iso: str = ""              # той самий момент — для живого відліку
    count_label: str = ""             # «до світла» / «до вимкнення»
    back: str = ""                    # для попередження: «повернуть о 04:30»
    now_pct: Optional[float] = None
    segments: list[Segment] = field(default_factory=list)
    today_cells: list[Cell] = field(default_factory=list)
    tomorrow_cells: list[Cell] = field(default_factory=list)
    today_sum: str = ""
    tomorrow_sum: str = ""
    updated: str = ""

    @property
    def tone(self) -> str:
        return {"soon": "is-soon", "alarm": "is-alarm"}.get(self.kind, "")

    @property
    def lit(self) -> bool:
        return self.kind in ("on", "soon")

    @property
    def has_bar(self) -> bool:
        return self.kind in ("off", "on", "soon")


def clock(minute: int) -> str:
    minute %= DAY
    return f"{minute // 60:02d}:{minute % 60:02d}"


def span_text(minutes: int) -> str:
    hours, rest = divmod(max(0, minutes), 60)
    if not hours:
        return f"{rest} хв"
    return f"{hours} год" + (f" {rest} хв" if rest else "")


def _cells(day: Optional[Day], now_minute: Optional[int]) -> list[Cell]:
    if day is None or day.status not in ("schedule", "none"):
        return [Cell("wait") for _ in range(24)]
    cells: list[Cell] = []
    for hour in range(24):
        base = hour * 60
        first = sum(max(0, min(end, base + 30) - max(start, base)) for start, end in day.off)
        second = sum(max(0, min(end, base + 60) - max(start, base + 30)) for start, end in day.off)
        # Половина години «без світла», коли відключення накриває її хоча б на чверть години.
        kind = ("off" if first >= 15 and second >= 15 else "first" if first >= 15
                else "second" if second >= 15 else "on")
        cells.append(Cell(
            kind,
            past=now_minute is not None and now_minute >= base + 60,
            now=now_minute is not None and base <= now_minute < base + 60,
        ))
    return cells


def _sum_text(day: Optional[Day]) -> str:
    if day is None or day.status == "waiting":
        return "ще не опубліковано"
    if day.status == "emergency":
        return "аварійні відключення"
    total = sum(end - start for start, end in day.off)
    return f"без світла {span_text(total)}" if total else "без відключень"


def _updated_text(snapshot: Snapshot, today: date) -> str:
    moment = snapshot.updated_on or snapshot.fetched_at
    if moment.date() == today:
        when = "сьогодні"
    elif moment.date() == today - timedelta(days=1):
        when = "вчора"
    else:
        when = moment.strftime("%d.%m")
    return f"оновлено {when} о {moment:%H:%M}"


def build_view(
    group: str,
    snapshot: Optional[Snapshot],
    failed_since: Optional[datetime],
    now: Optional[datetime] = None,
) -> Optional[PowerView]:
    """Готовий до показу стан; None — черга не задана, віджета немає."""
    group = clean_group(group)
    if not group:
        return None
    now = _naive(now or business_now())
    today_date, minute = now.date(), now.hour * 60 + now.minute

    def void(sub: str) -> PowerView:
        return PowerView(group, "void", "Графіка немає", sub=sub, meta="немає графіка",
                         today_cells=_cells(None, None), tomorrow_cells=_cells(None, None),
                         today_sum="немає даних", tomorrow_sum="немає даних")

    if snapshot is None or snapshot.group != group:
        return void("джерело ще не відповіло" if failed_since is None
                    else f"джерело мовчить з {failed_since:%H:%M}")
    if now - _naive(snapshot.fetched_at) > STALE_AFTER:
        return void(f"джерело мовчить з {(failed_since or snapshot.fetched_at):%H:%M}")
    today, tomorrow = snapshot.day(today_date), snapshot.day(today_date + timedelta(days=1))
    if today is None or today.status == "waiting":
        return void("на сьогодні ще не опубліковано")

    view = PowerView(
        group, "on", "Світло є",
        today_cells=_cells(today, minute), tomorrow_cells=_cells(tomorrow, None),
        today_sum=_sum_text(today), tomorrow_sum=_sum_text(tomorrow),
        updated=_updated_text(snapshot, today_date),
    )
    if today.status == "emergency":
        view.kind, view.say, view.sub, view.meta = "alarm", "Аварійні відключення", "графік не діє", "аварійні відключення"
        view.today_cells = _cells(None, None)
        return view

    view.now_pct = round(minute / DAY * 100, 3)
    view.segments = [Segment(round(s / DAY * 100, 3), round((e - s) / DAY * 100, 3)) for s, e in today.off]
    next_off = tuple(tomorrow.off) if tomorrow is not None and tomorrow.status == "schedule" else None
    midnight = datetime.combine(today_date, datetime.min.time())

    def change(at_minute: int, label: str) -> None:
        view.at = clock(at_minute)
        view.left = span_text(at_minute - minute)
        view.change_iso = (midnight + timedelta(minutes=at_minute)).isoformat()
        view.count_label = label

    current_off = next((s for s in today.off if s[0] <= minute < s[1]), None)
    if current_off is not None:
        view.kind, view.say = "off", "Світла немає"
        end = current_off[1]
        if end == DAY and next_off and next_off[0][0] == 0:
            end = DAY + next_off[0][1]                # відключення тягнеться через північ
        if end == DAY and (tomorrow is None or tomorrow.status == "waiting"):
            view.sub, view.meta = "до кінця доби", "немає до кінця доби"
        else:
            change(end, "до світла")
            view.sub, view.meta = f"дадуть о {view.at}", f"немає до {view.at}"
        return view

    upcoming = next((s for s in today.off if s[0] > minute), None)
    if upcoming is None and next_off:
        upcoming = (DAY + next_off[0][0], DAY + next_off[0][1])
    if upcoming is None:
        tail = "сьогодні більше не вимикають" if today.off else "сьогодні без відключень"
        view.sub, view.meta = tail, "є, " + tail
        return view
    change(upcoming[0], "до вимкнення")
    if upcoming[0] - minute <= SOON_MINUTES:
        view.kind, view.say = "soon", "Скоро вимкнуть"
        view.sub, view.meta = f"о {view.at}", f"вимкнуть о {view.at}"
        view.back = "до кінця доби" if upcoming[1] in (DAY, 2 * DAY) else f"повернуть о {clock(upcoming[1])}"
    else:
        view.sub, view.meta = f"вимкнуть о {view.at}", f"є до {view.at}"
    return view


# ── Налаштування й живий стан для шаблонів ───────────────────────────────────
# Шаблони питають стан на КОЖЕН рендер (плашка в шапці, табло раз на 30 с), тож
# номер черги й перемикач табло лежать у памʼяті: їх кладе фоновий прохід і
# роут налаштувань, а рендер не ходить по них у базу.

@dataclass
class _Config:
    group: str = ""
    board: bool = False


_config = _Config()
#: Розбудити фоновий прохід раніше строку — щойно змінили чергу.
wake = threading.Event()


def configure(group: Any, board: Any) -> None:
    new_group = clean_group(group)
    with _lock:
        changed = new_group != _config.group
        _config.group, _config.board = new_group, bool(board)
    if changed:
        reset()           # графік попередньої черги — чужий
        wake.set()


def load_config(db) -> str:
    """Прочитати налаштування з бази; повертає чергу (порожньо — вимкнено)."""
    from app.settings_store import get_setting

    configure(get_setting(db, GROUP_KEY), (get_setting(db, BOARD_KEY) or "") == "1")
    return _config.group


def configured_group() -> str:
    return _config.group


def board_enabled() -> bool:
    return _config.board


def live_view(now: Optional[datetime] = None) -> Optional[PowerView]:
    """Стан для CRM; None — черга не задана, віджета немає."""
    snapshot, failed_since = current()
    return build_view(_config.group, snapshot, failed_since, now)


def board_view(now: Optional[datetime] = None) -> Optional[PowerView]:
    """Стан для табло цеху; None — смугу вимкнено в налаштуваннях або черги немає."""
    return live_view(now) if _config.board else None


_session = None


def http_fetch_json(url: str) -> Any:
    """GET через ту саму сесію, що й решта вихідних запитів (TLS-проксі лаби).
    Назва клієнта загальна: джерелу не треба знати, хто питає."""
    global _session
    if _session is None:
        from app.sheets import new_legacy_session

        _session = new_legacy_session()
    response = _session.get(url, timeout=(10, 30),
                            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    return response.json()
