"""«Що не так» — проблеми застосунку людською мовою, в одному місці.

Навіщо. Дані про поломки в системі вже є: журнал синку, журнал обривів
зв'язку, звірка після оновлення, стан ліцензії. Але лежать вони на різних
екранах і написані для розробника: «sheet_to_db/skipped · вкладки за сьогодні
(10.09.26) немає серед датованих» — правда, але власник із неї нічого не
робить. Тут той самий факт перетворюється на «синхронізація не бачить
сьогоднішньої вкладки → черга за сьогодні порожня → перевір назву вкладки в
таблиці».

ФОРМАТ ТОЙ САМИЙ, ЩО ВЖЕ ПРАЦЮЄ на журналі обривів верстатів (§14): подія →
що це означає → що робити руками. Він себе виправдав, і вигадувати другий
немає причин.

НІЧОГО НЕ ЗАПИСУЄМО. Цей модуль лише ЧИТАЄ те, що підсистеми вже пишуть, і
перекладає. Окрема таблиця «проблем» означала б другу правду про ту саму
подію, яка рано чи пізно розійдеться з першою; а щоб проблема сюди
потрапила, підсистемі не треба знати про існування цього екрана.

ЧОГО ТУТ НЕМАЄ. Живих мережевих проб: екран відкривають, коли вже щось не
так, і двадцять секунд очікування — не те, що тоді потрібно. Для «перевір
зараз» є самоперевірка поруч, у «Стані системи».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_day import utc_now, utc_to_business
from app.models import MachineLinkEvent, SyncLog
from app.services import machine_link


# Скільки годин назад дивимось за замовчуванням. Доба — це «що сталось, поки
# мене не було»: зміна, ніч і ранок наступного дня.
DEFAULT_WINDOW_HOURS = 24

# Наскільки давно мусив пройти успішний синк, щоб це саме по собі стало
# проблемою. Фоновий тік — раз на хвилину; півгодини мовчання означає, що
# щось тримає його або він упав, і черга тихо застаріла.
SYNC_SILENCE_MINUTES = 30

# Рівні. Не кольори, а СТУПІНЬ ТЕРМІНОВОСТІ: «стоп» — працювати далі не можна,
# «проблема» — щось не працює зараз, «увага» — працює, але скоро зламається.
LEVEL_STOP = "stop"
LEVEL_PROBLEM = "problem"
LEVEL_WARN = "warn"

_LEVEL_ORDER = {LEVEL_STOP: 0, LEVEL_PROBLEM: 1, LEVEL_WARN: 2}


@dataclass
class Problem:
    """Одна проблема, готова до показу."""

    at: Optional[datetime]
    level: str
    # Заголовок — те, що сталось, мовою цеху. Без назв таблиць і кодів.
    title: str
    # Наслідок: що через це НЕ працює. Найважливіший рядок: саме він каже,
    # чи можна не звертати уваги до завтра.
    means: str
    # Кроки руками, у порядку, у якому їх варто робити.
    actions: list[str] = field(default_factory=list)
    # Сирий текст із журналу — для розробника, згорнутий у «Подробиці».
    raw: str = ""
    # Куди піти, щоб розібратись далі.
    link: Optional[str] = None
    link_text: str = ""
    # Скільки разів це повторилось за вікно і коли востаннє.
    times: int = 1
    last_at: Optional[datetime] = None
    # Ключ для згортання однакових. Не показується.
    key: str = ""


# ── Синхронізація з таблицею ────────────────────────────────────────────────

_TABS_RE = re.compile(r"\[(.*?)\]", re.S)


def _sync_problem(row: SyncLog) -> Optional[Problem]:
    """Один запис журналу синку → проблема, або None, якщо це не проблема."""
    message = (row.message or "").strip()
    when = utc_to_business(row.occurred_at) if row.occurred_at else None
    direction = row.direction or ""
    status = row.status or ""

    if status == "ok":
        return None

    if "вкладки за сьогодні" in message:
        # Перелік вкладок, які застосунок УПІЗНАВ, — це доказ: якщо там є
        # вчорашня, значить доступ до таблиці цілий і справа саме в назві.
        found = _TABS_RE.search(message)
        seen_tabs = found.group(1).replace("'", "").strip() if found else ""
        seen_line = f" Застосунок бачить вкладки: {seen_tabs}." if seen_tabs else ""
        return Problem(
            at=when,
            level=LEVEL_STOP,
            title="Синхронізація не бачить сьогоднішньої вкладки в таблиці",
            means=(
                "Роботи за сьогодні в чергу НЕ потраплять, поки це так. "
                "Вкладки на інші дати застосунок бачить, тобто доступ до "
                "таблиці є — не впізнається саме назва сьогоднішньої." + seen_line
            ),
            actions=[
                "Відкрити Google Таблицю і подивитись на назву сьогоднішньої вкладки.",
                "Назва мусить бути рівно у форматі 10.09.26 — без пробілу на "
                "початку чи в кінці, без зайвих символів. Саме пробіл на "
                "початку вже одного разу забрав цілий робочий день.",
                "Перейменувати вкладку — далі синхронізація підхопить її сама, "
                "чекати не треба.",
            ],
            raw=message,
            link="/journal/sync",
            link_text="Журнал синку",
            key="sheet_tab_missing",
        )

    if direction == "sheet_to_db" and status == "error":
        return Problem(
            at=when,
            level=LEVEL_STOP,
            title="Немає доступу до Google Таблиці",
            means=(
                "Нові роботи з таблиці не приходять, зміни статусів у таблицю "
                "не пишуться. Черга показує те, що встигло прийти раніше."
            ),
            actions=[
                "Налаштування → Стан системи → «Запустити самоперевірку»: "
                "перший рядок скаже точніше.",
                "Перевірити інтернет на цьому ПК.",
                "Якщо інтернет є — чи не забрали доступ сервісному акаунту в "
                "самій таблиці (кнопка «Поділитись»).",
            ],
            raw=message,
            link="/settings#state",
            link_text="Стан системи",
            key="sheets_error",
        )

    if direction == "db_to_sheet" and status == "error":
        return Problem(
            at=when,
            level=LEVEL_PROBLEM,
            title="Зміну не вдалось записати в Google Таблицю",
            means=(
                "У застосунку зміна збережена, а в таблиці її немає — і люди, "
                "які дивляться таблицю, її не побачать. Дані не втрачені."
            ),
            actions=[
                "Подивитись у журналі синку, якої роботи це стосується.",
                "Найчастіша причина — вкладку, у якій був той рядок, "
                "перейменували або видалили.",
            ],
            raw=message,
            link="/journal/sync",
            link_text="Журнал синку",
            key="sheet_write_error",
        )

    if direction.startswith("mail") and status == "error":
        return Problem(
            at=when,
            level=LEVEL_PROBLEM,
            title="Пошта не читається",
            means=(
                "Нові листи з роботами не з'являються в «Нові з пошти». "
                "Робота з таблиці й черга від цього не страждають."
            ),
            actions=[
                "Налаштування → Стан системи → «Запустити самоперевірку»: "
                "рядок про IMAP скаже точніше.",
                "Найчастіша причина — ukr.net скинув пароль для програм. "
                "Створити новий у налаштуваннях скриньки і вписати в "
                "Налаштування → Пошта.",
            ],
            raw=message,
            link="/settings#imap",
            link_text="Налаштування пошти",
            key="mail_error",
        )

    if status in ("error", "skipped"):
        # Незнайома поломка. Показуємо як є, чесно назвавши, що пояснення
        # немає: мовчати про неї гірше, ніж показати сирий текст.
        return Problem(
            at=when,
            level=LEVEL_PROBLEM if status == "error" else LEVEL_WARN,
            title=f"Синхронізація: {status}",
            means="Пояснення для цього випадку ще немає — покажіть текст розробнику.",
            actions=["Надіслати «Звіт для розробника» (Налаштування → Стан системи)."],
            raw=message,
            link="/journal/sync",
            link_text="Журнал синку",
            key=f"sync_{direction}_{status}",
        )
    return None


def _sync_problems(db: Session, since_utc: datetime) -> list[Problem]:
    rows = db.scalars(
        select(SyncLog)
        .where(SyncLog.occurred_at >= since_utc)
        .order_by(SyncLog.occurred_at.desc())
    ).all()
    out: list[Problem] = []
    for row in rows:
        problem = _sync_problem(row)
        if problem is not None:
            out.append(problem)
    return out


def _span(minutes: float) -> str:
    """Тривалість мовою людини. «1178 хв» технічно правда, але скільки це —
    доводиться рахувати в голові саме тоді, коли не до того."""
    total = int(minutes)
    if total < 60:
        return f"{total} хв"
    hours, rest = divmod(total, 60)
    if hours < 24:
        return f"{hours} год {rest} хв" if rest else f"{hours} год"
    days, hours = divmod(hours, 24)
    return f"{days} дн. {hours} год" if hours else f"{days} дн."


def _sync_silence(db: Session) -> Optional[Problem]:
    """Синк не падав — він просто мовчить. Це окрема поломка: у журналі немає
    НІЧОГО, і саме тому її не видно, поки хтось не помітить старі дані."""
    last_ok = db.scalar(
        select(SyncLog.occurred_at)
        .where(SyncLog.status == "ok", SyncLog.direction == "sheet_to_db")
        .order_by(SyncLog.occurred_at.desc())
        .limit(1)
    )
    if last_ok is None:
        return None
    quiet_minutes = (utc_now().replace(tzinfo=None) - last_ok).total_seconds() / 60
    if quiet_minutes < SYNC_SILENCE_MINUTES:
        return None
    when = utc_to_business(last_ok)
    return Problem(
        at=when,
        level=LEVEL_PROBLEM,
        title=f"Синхронізація мовчить {_span(quiet_minutes)}",
        means=(
            "Останній успішний прохід був о "
            f"{when.strftime('%H:%M')}. Черга показує дані з того часу; "
            "нові роботи з таблиці могли не дійти."
        ),
        actions=[
            "Перевірити, чи не поставлена синхронізація на паузу "
            "(Налаштування → Google Таблиця).",
            "Якщо не на паузі — «Запустити самоперевірку» у Стані системи.",
        ],
        link="/journal/sync",
        link_text="Журнал синку",
        key="sync_silent",
    )


# ── Верстати ────────────────────────────────────────────────────────────────


def _machine_problems(db: Session, since: datetime) -> list[Problem]:
    """Обриви зв'язку. Пояснення бере той самий `machine_link.explain`, що й
    журнал верстатів — двох різних пояснень одного обриву бути не може."""
    rows = db.scalars(
        select(MachineLinkEvent)
        .where(MachineLinkEvent.detected_at >= since)
        .order_by(MachineLinkEvent.detected_at.desc())
    ).all()
    out: list[Problem] = []
    for row in rows:
        view = machine_link.view_of(row)
        ex = view.explanation
        if view.is_open:
            means = f"Верстат «{view.name}» не відповідає ЗАРАЗ — на екрані його не видно."
            level = LEVEL_PROBLEM
        else:
            minutes = (view.seconds or 0) // 60
            means = (
                f"Верстат «{view.name}» був без зв'язку {minutes} хв і вже "
                "повернувся. Показує на випадок, якщо це повторюється."
            )
            level = LEVEL_WARN
        out.append(
            Problem(
                at=row.detected_at,
                level=level,
                # Заголовок беремо ЯК Є: `.lower()` перетворював «ПК» на «пк»
                # і псував абревіатури в чужому тексті.
                title=f"{view.name} — {ex.headline}",
                means=means,
                actions=list(ex.actions),
                raw=machine_link.strip_wrap(row.error or ""),
                link="/machines/diag",
                link_text="Журнал зв'язку",
                key=f"machine_{row.host}_{row.cause}",
            )
        )
    return out


# ── Стан застосунку ─────────────────────────────────────────────────────────


def _update_health_problem(db: Session) -> Optional[Problem]:
    """Після оновлення рядків стало менше. Найважча з усіх — тому «стоп»."""
    try:
        from app.services.health_snapshot import last_report

        report = last_report(db)
    except Exception:  # noqa: BLE001
        return None
    if not report or report.get("ok", True):
        return None
    lost = report.get("lost") or {}
    names = ", ".join(sorted(lost)) if lost else "деякі таблиці"
    return Problem(
        at=None,
        level=LEVEL_STOP,
        title="Після оновлення в базі стало менше рядків",
        means=(
            f"Порівняння до і після оновлення "
            f"{report.get('from_version')} → {report.get('to_version')} "
            f"показало втрату в таблицях: {names}. Це може означати втрату даних."
        ),
        actions=[
            "НЕ працювати далі з базою.",
            "Відновитись із резервної копії: Налаштування → Резервна копія.",
            "Надіслати «Звіт для розробника».",
        ],
        link="/settings#state",
        link_text="Стан системи",
        key="update_health",
    )


def _license_problem(db: Session) -> Optional[Problem]:
    try:
        from app.license import get_license_status

        status = get_license_status(db)
    except Exception:  # noqa: BLE001
        return None
    if status.expires_at is None:
        return None
    days = (status.expires_at - datetime.now()).days
    if days > 30:
        return None
    if days < 0:
        return Problem(
            at=status.expires_at, level=LEVEL_STOP,
            title="Ліцензія спливла",
            means="Застосунок працюватиме обмежено, поки ключ не поновлять.",
            actions=["Звернутись до розробника по новий ключ."],
            link="/license", link_text="Ліцензія", key="license",
        )
    return Problem(
        at=status.expires_at, level=LEVEL_WARN,
        title=f"Ліцензія спливає через {days} дн.",
        means="Поки що все працює. Далі знадобиться новий ключ.",
        actions=["Написати розробнику заздалегідь, не в останній день."],
        link="/license", link_text="Ліцензія", key="license",
    )


def _disk_problem() -> Optional[Problem]:
    import shutil
    from pathlib import Path

    from app.config import DB_PATH

    try:
        free_gb = shutil.disk_usage(Path(DB_PATH).parent).free / (1024 ** 3)
    except OSError:
        return None
    if free_gb >= 5:
        return None
    return Problem(
        at=None,
        level=LEVEL_STOP if free_gb < 2 else LEVEL_WARN,
        title=f"Мало місця на диску — {free_gb:.1f} ГБ",
        means=(
            "База, вкладення листів і резервні копії пишуться на цей диск. "
            "Коли місце скінчиться, застосунок перестане зберігати."
        ),
        actions=[
            "Прибрати старі файли або перенести теку вкладень.",
            "Перевірити, чи не розрослись резервні копії.",
        ],
        key="disk",
    )


# ── Збірка ──────────────────────────────────────────────────────────────────


def _collapse(problems: list[Problem]) -> list[Problem]:
    """Однакові проблеми — одним рядком із лічильником.

    Синк повторює ту саму поломку щохвилини: без згортання екран стає стрічкою
    з сорока однакових карток, і побачити на ньому ДРУГУ проблему неможливо.
    Згортання нічого не ховає — воно каже, скільки разів і коли востаннє.
    """
    merged: dict[str, Problem] = {}
    for problem in problems:
        seen = merged.get(problem.key)
        if seen is None:
            problem.times = 1
            problem.last_at = problem.at
            merged[problem.key] = problem
            continue
        seen.times += 1
        # Список відсортований найновішим уперед, тож перший — найсвіжіший;
        # найстаріший з групи задає «відколи це триває».
        if problem.at is not None:
            seen.at = problem.at
    return list(merged.values())


def collect(db: Session, *, hours: int = DEFAULT_WINDOW_HOURS) -> list[Problem]:
    """Усі проблеми за вікно, найтерміновіші зверху.

    Порядок саме за рівнем, а не за часом: екран відкривають з питанням «що
    робити», і «стоп» мусить бути в першому рядку, навіть якщо він старший за
    свіже попередження.
    """
    now = datetime.now()
    since_local = now - timedelta(hours=hours)
    since_utc = utc_now().replace(tzinfo=None) - timedelta(hours=hours)

    problems: list[Problem] = []

    def gather(name: str, source, *args):
        """Кожне джерело — окремо і під захистом.

        Один зламаний постачальник не сміє забрати з екрана решту проблем:
        це трапиться саме тоді, коли на екран і дивляться. Про власний збій
        мовчати теж не можна — він стає рядком нарівні з рештою.
        """
        try:
            found = source(*args)
        except Exception as exc:  # noqa: BLE001
            problems.append(
                Problem(
                    at=now,
                    level=LEVEL_WARN,
                    title=f"Частина діагностики не спрацювала ({name})",
                    means=(
                        "Решта перевірок відпрацювала й показана нижче, але цю "
                        "ділянку зараз не видно — тут могла бути ще одна проблема."
                    ),
                    actions=["Надіслати «Звіт для розробника» — це вада самої діагностики."],
                    raw=f"{type(exc).__name__}: {exc}",
                    key=f"selfbroken_{name}",
                )
            )
            return
        if found is None:
            return
        if isinstance(found, list):
            problems.extend(found)
        else:
            problems.append(found)

    gather("синк", _sync_problems, db, since_utc)
    gather("верстати", _machine_problems, db, since_local)
    gather("мовчання синку", _sync_silence, db)
    gather("звірка оновлення", _update_health_problem, db)
    gather("ліцензія", _license_problem, db)
    gather("диск", _disk_problem)

    collapsed = _collapse(problems)
    collapsed.sort(
        key=lambda p: (
            _LEVEL_ORDER.get(p.level, 9),
            -(p.last_at or p.at or datetime.min).timestamp(),
        )
    )
    return collapsed


def count(db: Session, *, hours: int = DEFAULT_WINDOW_HOURS) -> int:
    """Скільки проблем — для позначки в рейці. Попередження не рахуємо: рейка
    мусить світитись тоді, коли щось справді не працює, інакше на неї
    перестануть дивитись."""
    return sum(
        1 for p in collect(db, hours=hours) if p.level in (LEVEL_STOP, LEVEL_PROBLEM)
    )
