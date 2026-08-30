"""Печі спікання: опитування екранів, збереження показань, стан для екрана.

Межа модуля (правило ARCHITECTURE_PLAN.md): тут немає ні Request, ні Response.
HTTP живе в app/routers/furnace.py, знімок кадру — в app/furnace_vnc.py,
читання пікселів — в app/furnace_ocr.py. Тут — що і коли ми з цим робимо.

Три рішення, які варто пам'ятати:

1. **Кадр і рядок у базі — різні частоти.** Картинка оновлюється кожні кілька
   секунд, щоб екран був живим; рядок пишеться лише на зміну (або раз на
   хвилину як «я живий»). Без цього одна піч давала б ~17 тис. рядків на добу
   про те, що нічого не змінилось.

2. **Кадр на диску — один на піч, перезаписується.** Історія картинок нікому
   не потрібна: числа лежать у базі. Запис атомарний (тимчасовий файл +
   заміна), інакше роут віддав би недописаний PNG.

3. **Усе read-only.** Ні тут, ні глибше немає коду, який шле печі хоч один
   байт вводу. Керування піччю — свідомо поза застосунком.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import FURNACE_FRAMES_PATH
from app.furnace_ocr import (
    EYE_CROPS,
    PanelReading,
    STATUS_RUN,
    STATUS_UNKNOWN,
    STATUS_WAIT,
    format_remaining,
    read_panel,
)
from app.furnace_vnc import DEFAULT_PORT, FurnaceVncError, capture
from app.models import Furnace, FurnaceReading
from app.services.order_dates import BUSINESS_TIMEZONE
from app.crypto import decrypt_value
from app.settings_store import get_furnace_vnc_password

logger = logging.getLogger(__name__)

# Як часто знімати кадр. Компроміс: табло оновлює «срок» раз на секунду, але
# екран печей стоїть відкритим цілий день, а кожен кадр — це повний
# framebuffer 800×600 по мережі цеху.
POLL_INTERVAL_SECONDS = 6.0
# Не частіше цього в базу не пишемо навіть при змінах — секундний тик «срок»
# інакше писав би рядок на кожен кадр.
MIN_DB_INTERVAL_SECONDS = 60.0
# Помилки (піч вимкнена на ніч) записуємо рідко: інакше вимкнена на вихідні
# піч дала б тисячі однакових рядків.
ERROR_DB_INTERVAL_SECONDS = 15 * 60.0
# Показання старші за це прибираються — це оперативні дані, а не архів.
READINGS_RETENTION_DAYS = 30
# За скільки мовчання плитка визнає, що опитування СТАЛО. Поріг мусить бути
# більший за «інтервал + дедлайн знімка» (6 + 20 с), інакше кожна недоступна
# піч блимала б «стоїть» просто тому, що знімок довго не відповідав.
STALE_AFTER_SECONDS = 90.0

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class FurnaceConfigError(Exception):
    """Перелік печей написано неправильно. Повідомлення — для оператора."""


@dataclass(frozen=True)
class FurnaceTarget:
    """Одна піч у налаштуваннях."""

    name: str
    host: str
    port: int = DEFAULT_PORT
    # Власний пароль пічки. None означає «спільний з налаштувань» — так буде
    # майже завжди, бо пароль заводський, один на модель.
    password: Optional[str] = None

    @property
    def key(self) -> str:
        """Стабільний ідентифікатор для URL і імені файлу кадру.

        Це адреса, а не назва: назву можна перейменувати, і посилання на кадр
        не мають від цього ламатись. Крапки в шляху нам не шкодять — сам рядок
        перевірено _HOST_RE, тобто ні слешів, ні «..» в ньому бути не може.
        """
        return f"{self.host}-{self.port}" if self.port != DEFAULT_PORT else self.host


@dataclass
class FurnaceState:
    """Останнє, що ми знаємо про піч. Живе в пам'яті процесу.

    Навіщо пам'ять, коли є база: база зберігає зміни, а екрану потрібен
    останній кадр і час останньої СПРОБИ (зокрема невдалої) — це стан процесу,
    а не історія.
    """

    target: FurnaceTarget
    reading: Optional[PanelReading] = None
    captured_at: Optional[datetime] = None
    error: Optional[str] = None
    attempted_at: Optional[datetime] = None
    stored_at: Optional[datetime] = None
    # Окремо від stored_at: успішні показання й записи про недоступність
    # притримуються з різною частотою (хвилина проти чверті години), і одне
    # спільне поле означало б, що вимкнена на ніч піч глушить перший же кадр
    # після ввімкнення.
    error_stored_at: Optional[datetime] = None
    frame_at: Optional[datetime] = None

    # Кожна властивість нижче читає self.reading РІВНО ОДИН раз, у локальну
    # змінну. Це не стиль: `self.reading.temp_c if self.reading else None` —
    # два окремі читання поля, а між ними воркер печей (інший потік) може
    # поставити None, коли піч у цю мить зникла з мережі. Тоді сторінка
    # падала б з AttributeError просто тому, що піч вимкнули під час запиту.
    # Той самий стан читає HTTP-потік і пише фоновий, тому знімок посилання —
    # найдешевший спосіб не мати гонки взагалі.

    @property
    def status(self) -> str:
        reading = self.reading
        return reading.status if reading else STATUS_UNKNOWN

    @property
    def temp_c(self) -> Optional[int]:
        reading = self.reading
        return reading.temp_c if reading else None

    @property
    def remaining_seconds(self) -> Optional[int]:
        reading = self.reading
        return reading.remaining_seconds if reading else None

    @property
    def remaining_text(self) -> str:
        return format_remaining(self.remaining_seconds)

    @property
    def done_at(self) -> Optional[datetime]:
        """Коли програма добіжить — за київським часом.

        Рахується від часу кадру, а не «зараз»: між знімком і показом сторінки
        минає час, і оператор має бачити момент, а не залишок, який уже трохи
        протух.

        Київ, а не «час цієї машини»: оператор звіряє число з годинником на
        стіні, і якщо ПК колись опиниться в іншому поясі (RDP, машину
        переставили, годинник збили на UTC), «відкриється о 17:54» мусить
        лишитись київським. `astimezone()` без аргументу підставляє поясу
        системи саме те, що є, — це і є перетворення «наївний локальний → з
        поясом», після якого переклад у Київ чесний.
        """
        captured_at = self.captured_at
        remaining = self.remaining_seconds
        if captured_at is None or not remaining:
            return None
        finish = captured_at + timedelta(seconds=remaining)
        if BUSINESS_TIMEZONE is None:
            return finish
        return finish.astimezone().astimezone(BUSINESS_TIMEZONE)

    @property
    def warnings(self) -> list[str]:
        reading = self.reading
        return list(reading.warnings) if reading else []


# Стан усіх печей процесу. Читає HTTP-потік, пише фоновий воркер — тому лок.
_states: dict[str, FurnaceState] = {}
_states_lock = threading.Lock()


def list_furnaces(db: Session, *, only_enabled: bool = False) -> list[Furnace]:
    """Пічки з таблиці, у порядку, який задав оператор.

    Порядок його, а не наш: пічки в цеху стоять у відомій людині
    послідовності, і сортування за id чи назвою її ламає.
    """
    query = select(Furnace).order_by(Furnace.sort_order, Furnace.id)
    if only_enabled:
        query = query.where(Furnace.enabled.is_(True))
    return list(db.scalars(query))


def target_of(furnace: Furnace) -> FurnaceTarget:
    """Рядок таблиці → ціль опитування, з розшифрованим власним паролем."""
    password = None
    if furnace.password_encrypted:
        try:
            password = decrypt_value(furnace.password_encrypted)
        except Exception:  # noqa: BLE001 — зіпсований шифротекст не валить опитування
            logger.warning("Не вдалось розшифрувати пароль пічки %s", furnace.host)
    return FurnaceTarget(
        name=furnace.name, host=furnace.host, port=furnace.port, password=password
    )


def validate_address(host: str, port: str | int | None) -> tuple[str, int]:
    """Перевірити адресу пічки перед збереженням.

    Суворо навмисно: адреса, яка не схожа на адресу, — це друкарська помилка в
    налаштуваннях, і сказати про неї одразу дешевше, ніж потім довго дивитись
    на плитку «немає зв'язку».
    """
    clean = (host or "").strip()
    if not _HOST_RE.match(clean):
        raise FurnaceConfigError(f"Некоректна адреса пічки: «{host}»")
    raw_port = str(port or "").strip() or str(DEFAULT_PORT)
    try:
        number = int(raw_port)
    except ValueError as exc:
        raise FurnaceConfigError(f"Некоректний порт пічки: «{port}»") from exc
    if not 1 <= number <= 65535:
        raise FurnaceConfigError(f"Некоректний порт пічки: «{port}»")
    return clean, number


def configured_targets(db: Session) -> list[FurnaceTarget]:
    """Увімкнені пічки. Вимкнена лишається в переліку зі своїми
    налаштуваннями, але не опитується й ніде не показується — це стан «на
    ремонті», а не видалення."""
    return [target_of(furnace) for furnace in list_furnaces(db, only_enabled=True)]


def config_error(db: Session) -> Optional[str]:
    """Лишилось як шов для екрана: тепер адреси перевіряються на збереженні,
    тож дійти до екрана криве значення вже не може."""
    return None


def is_configured(db: Session) -> bool:
    return bool(configured_targets(db))


# ── Кадри на диску ──────────────────────────────────────────────────────────


def frames_root() -> Path:
    return Path(FURNACE_FRAMES_PATH)


def frame_path(key: str) -> Path:
    return frames_root() / f"{key}.png"


def save_frame(key: str, image: Image.Image) -> Path:
    """Записати кадр атомарно: спершу тимчасовий файл, потім заміна.

    Без цього роут, який віддає PNG, рано чи пізно натрапив би на файл у
    процесі запису й показав оператору биту картинку.
    """
    root = frames_root()
    root.mkdir(parents=True, exist_ok=True)
    final = root / f"{key}.png"
    tmp = root / f"{key}.png.tmp"
    image.save(tmp, format="PNG")
    os.replace(tmp, final)
    return final


def resolve_frame(key: str) -> Optional[Path]:
    """Шлях до кадру для роута. Ключ звіряється з налаштованими печами, а не
    підставляється у шлях: інакше довільний рядок з URL ліз би в файлову
    систему."""
    with _states_lock:
        known = key in _states
    if not known:
        return None
    path = frame_path(key)
    return path if path.exists() else None


def eye_crop(key: str, name: str) -> Optional[Image.Image]:
    """Смужка кадру для звірки очима — те, за чим оператор бачить, що
    розпізнане число справді написане на табло."""
    rect = EYE_CROPS.get(name)
    if rect is None:
        return None
    path = resolve_frame(key)
    if path is None:
        return None
    with Image.open(path) as image:
        return image.convert("RGB").crop(rect)


# ── Опитування ──────────────────────────────────────────────────────────────


def _should_store(state: FurnaceState, reading: PanelReading, now: datetime) -> bool:
    """Чи писати рядок у базу. «Зміна або раз на хвилину» — див. модульний
    докстрінг."""
    if state.stored_at is None:
        return True
    if (now - state.stored_at).total_seconds() >= MIN_DB_INTERVAL_SECONDS:
        return True
    previous = state.reading
    if previous is None:
        return True
    return (
        previous.status != reading.status
        or previous.temp_c != reading.temp_c
        or (previous.remaining_seconds is None) != (reading.remaining_seconds is None)
    )


def _store(db: Session, target: FurnaceTarget, reading: PanelReading, now: datetime) -> None:
    db.add(
        FurnaceReading(
            # Ключ, а не гола адреса: дві печі за одним хостом на різних портах
            # (стенд, проброшений порт) інакше зливали б історію в одну.
            host=target.key,
            captured_at=now,
            status=reading.status,
            temp_c=reading.temp_c,
            remaining_seconds=reading.remaining_seconds,
            elapsed_seconds=reading.elapsed_seconds,
            command=reading.command,
            raw_temp=(reading.fields.get("temp").raw if reading.fields.get("temp") else None),
            raw_remaining=(
                reading.fields.get("remaining").raw if reading.fields.get("remaining") else None
            ),
        )
    )
    db.commit()


def _store_error(db: Session, target: FurnaceTarget, message: str, now: datetime) -> None:
    db.add(
        FurnaceReading(
            host=target.key,
            captured_at=now,
            status=STATUS_UNKNOWN,
            error=message[:300],
        )
    )
    db.commit()


def grab(target: FurnaceTarget, password: Optional[str]) -> tuple[Optional[Image.Image], Optional[str]]:
    """Знімок без бази — щоб кілька печей можна було знімати одночасно.

    Повертає (кадр, None) або (None, пояснення). Виключення не летить далі:
    недоступна піч — робочий стан, а не збій програми.
    """
    try:
        return capture(target.host, target.port, password), None
    except FurnaceVncError as exc:
        return None, str(exc)


def poll_target(
    db: Session,
    target: FurnaceTarget,
    password: Optional[str],
    now: Optional[datetime] = None,
    frame: Optional[Image.Image] = None,
    error: Optional[str] = None,
) -> FurnaceState:
    """Один цикл для однієї печі: кадр → читання → диск → база.

    `frame`/`error` дають передати вже знятий кадр (див. poll_all, який знімає
    печі паралельно). Без них кадр знімається тут же.

    Ніколи не кидає: недоступна піч — це нормальний робочий стан (вимкнена на
    ніч, від'єднали кабель), і фоновий воркер має пережити його мовчки.
    """
    now = now or datetime.now()
    with _states_lock:
        state = _states.setdefault(target.key, FurnaceState(target=target))
        state.target = target
    state.attempted_at = now

    image = frame
    if image is None and error is None:
        image, error = grab(target, password)
    if image is None:
        message = error or "Кадр не знято"
        state.error = message
        state.reading = None
        last_error_row = state.error_stored_at
        if (
            last_error_row is None
            or (now - last_error_row).total_seconds() >= ERROR_DB_INTERVAL_SECONDS
        ):
            _store_error(db, target, message, now)
            state.error_stored_at = now
        return state

    reading = read_panel(image)
    try:
        save_frame(target.key, image)
        state.frame_at = now
    except OSError:
        # Кадр не записався (немає місця, тека лише для читання) — числа все
        # одно живі, тому це не привід гасити піч на екрані.
        logger.exception("Кадр печі %s не збережено", target.host)

    if _should_store(state, reading, now):
        _store(db, target, reading, now)
        state.stored_at = now
        state.error_stored_at = None

    state.reading = reading
    state.captured_at = now
    state.error = None
    return state


def poll_all(db: Session, now: Optional[datetime] = None) -> list[FurnaceState]:
    """Опитати всі печі. Знімки — паралельно, запис у базу — послідовно.

    Паралельність тут не про швидкість, а про правду: вимкнена піч мовчить до
    самого дедлайну (20 с), і послідовний обхід чотирьох печей означав би
    хвилину з гаком на тік — тобто екран показував би позаминулий стан живих
    печей через мертві. Сесія БД лишається на одному потоці: SQLAlchemy-сесія
    не для спільного користування.
    """
    targets = configured_targets(db)
    if not targets:
        return []
    shared = get_furnace_vnc_password(db)

    def password_for(target: FurnaceTarget) -> Optional[str]:
        return target.password or shared

    if len(targets) == 1:
        return [poll_target(db, targets[0], password_for(targets[0]), now=now)]
    with ThreadPoolExecutor(max_workers=min(len(targets), 6)) as pool:
        grabbed = list(pool.map(lambda target: grab(target, password_for(target)), targets))
    return [
        poll_target(db, target, password_for(target), now=now, frame=frame, error=error)
        for target, (frame, error) in zip(targets, grabbed)
    ]


def prune_readings(db: Session, now: Optional[datetime] = None) -> int:
    """Прибрати показання, старші за вікно зберігання."""
    cutoff = (now or datetime.now()) - timedelta(days=READINGS_RETENTION_DAYS)
    rows = db.scalars(
        select(FurnaceReading).where(FurnaceReading.captured_at < cutoff)
    ).all()
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


# ── Стан для екрана ─────────────────────────────────────────────────────────


@dataclass
class FurnaceCard:
    """Те, що бачить оператор на плитці печі."""

    target: FurnaceTarget
    state: Optional[FurnaceState]
    history: list[FurnaceReading] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.target.key

    @property
    def status(self) -> str:
        return self.state.status if self.state else STATUS_UNKNOWN

    @property
    def is_running(self) -> bool:
        return self.status == STATUS_RUN

    @property
    def is_idle(self) -> bool:
        return self.status == STATUS_WAIT

    @property
    def offline(self) -> bool:
        return bool(self.state and self.state.error)

    @property
    def has_data(self) -> bool:
        """Чи є з печі свіжий кадр із числами."""
        state = self.state
        return bool(state and state.reading and state.error is None)

    @property
    def has_problem(self) -> bool:
        """Піч, яку опитували і не вийшло: немає зв'язку або опитувач стоїть.

        Раніше такі пічки просто зникали з віджета — «немає даних, отже нема
        що показувати». На робочому місці це найгірший з можливих варіантів:
        налаштована піч, яка злетіла вночі, поводилась рівно так само, як
        піч, якої ніколи не існувало, і помітити різницю було ніяк. Тепер
        збій лишається на екрані й називає причину.
        """
        return self.offline or self.stale()

    @property
    def problem_text(self) -> str:
        """Причина збою людською мовою, без адреси на початку.

        Адреса вже стоїть у назві печі й у налаштуваннях; у вузькому чіпі
        вона з'їдає рівно те місце, де мала б бути причина.
        """
        if self.stale():
            return "опитування стоїть"
        state = self.state
        error = state.error if state else None
        if not error:
            return "немає зв'язку"
        host = self.target.host
        if host and error.startswith(f"Піч {host} "):
            return error[len(f"Піч {host} "):]
        return error

    @property
    def never_polled(self) -> bool:
        return self.state is None or self.state.attempted_at is None

    def stale(self, now: Optional[datetime] = None) -> bool:
        """Чи замовк сам опитувач.

        Без цієї ознаки смерть фонового потоку виглядала б як спокійна піч:
        числа лишились би на екрані, час кадру просто перестав би йти — а
        оператор дивиться на числа, не на час у підвалі. Пульс печі свідомо
        НЕ вішається на спільний heartbeat пошти й таблиці: там пара
        «джерело робіт», а тут стан кожної печі окремий, і одна мовчазна піч
        не має позначати решту здоровими чи хворими.
        """
        if self.state is None or self.state.attempted_at is None:
            return False
        return ((now or datetime.now()) - self.state.attempted_at).total_seconds() > STALE_AFTER_SECONDS


def snapshot(db: Session) -> list[FurnaceCard]:
    """Плитки для екрана «Печі» — по одній на налаштовану піч.

    Піч, яку ще жодного разу не опитали (застосунок щойно стартував), теж
    отримує плитку: порожня плитка з написом «чекаємо перший кадр» чесніша за
    відсутність печі на екрані.
    """
    cards = []
    with _states_lock:
        states = dict(_states)
    for target in configured_targets(db):
        cards.append(FurnaceCard(target=target, state=states.get(target.key)))
    return cards


@dataclass
class StripSummary:
    """Що показує згорнута смуга печей: скільки в роботі й котра відкриється
    найближче. Згорнутий стан має лишатись корисним — інакше оператор його
    просто не згортатиме."""

    running: int
    total: int
    nearest_done_at: Optional[datetime] = None
    # Скільки печей зі збоєм. Мусить бути ТУТ, а не лише в розгорнутих чіпах:
    # згорнута смуга — це стан, у якому оператор проводить більшість дня, і
    # якби збій було видно лише розгорнутою, він так само тихо губився б.
    broken: int = 0

    @property
    def nearest_text(self) -> str:
        return self.nearest_done_at.strftime("%H:%M") if self.nearest_done_at else ""


def strip_summary(cards: list["FurnaceCard"]) -> StripSummary:
    finishes = [card.state.done_at for card in cards if card.state and card.state.done_at]
    return StripSummary(
        running=sum(1 for card in cards if card.is_running),
        total=len(cards),
        nearest_done_at=min(finishes) if finishes else None,
        broken=sum(1 for card in cards if card.has_problem),
    )


def strip_cards(db: Session) -> list[FurnaceCard]:
    """Пічки для віджета: з показаннями АБО зі збоєм.

    Було «лише ті, у яких є показання» — і піч, яка злетіла, тихо зникала з
    головного екрана. Прохання власника прямо про це: налаштована піч, яка
    перестала відповідати, мусить лишитись на видноті й назвати причину, а не
    прикинутись, що її ніколи не було.

    Ще не опитану піч (перші секунди після старту застосунку) далі не
    показуємо: це не збій, а нормальний проміжний стан, і блимати нею на
    кожному рестарті означало б привчити не читати цю смугу.
    """
    return [card for card in snapshot(db) if card.has_data or card.has_problem]


def recent_readings(db: Session, host: str, limit: int = 40) -> list[FurnaceReading]:
    return list(
        db.scalars(
            select(FurnaceReading)
            .where(FurnaceReading.host == host)
            .order_by(FurnaceReading.captured_at.desc())
            .limit(limit)
        )
    )


def reset_state_for_tests() -> None:
    """Очистити стан процесу між тестами — інакше піч, «побачена» одним
    тестом, лишалась би видимою в наступному."""
    with _states_lock:
        _states.clear()
