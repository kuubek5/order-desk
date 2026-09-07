# -*- coding: utf-8 -*-
"""Верстати: коли знімати кадр екрана RemiCORE і що з ним робити.

Фаза 1 — ЖИВИЙ КАДР у CRM, без розпізнавання чисел. Це вже відповідає на
головне питання оператора («що зараз на верстаті?») без ходіння до RustDesk.
Фаза 2 — OCR відсотка/часу/імені програми тим самим конвеєром еталонів, що
читає табло печі; кадр і зони для неї вже будуть на місці.

Каркас свідомо повторює app/services/furnace.py: обидва модулі — «залізо з
екраном за VNC». Розбіжності теж свідомі:
- історії в базі немає (нема ще чисел, які варто зберігати);
- кадр через АГЕНТА знімається часто (5 с): відсоток на екрані міняється
  щохвилини, і оператор має бачити його майже наживо. VNC-верстати (без
  агента) все одно обмежені прогрівом ~8 с — для них тік просто рідший.

Читання і тільки читання: знімок іде через app/furnace_vnc.py, який фізично
не вміє слати ввід (перевірено стендом tests/fake_vnc_server.py).
"""

import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.furnace_vnc import DEFAULT_PORT, FurnaceVncError, capture
from app.machine_portraits import portrait_version
from app.machine_sisma import read_sisma, screen_is_sisma
from app.machine_ocr import (
    missing_caption_digits,
    pick_milling_program,
    read_progress_percent,
    screen_is_completed,
)
from app.models import Machine, Order, ReworkRecord
from app.services.furnace import (  # ті самі правила адреси й формат тривалості
    _HOST_RE,
    span_text,
    validate_address,
)
from app.config import MACHINE_CALIBRATION_PATH, MACHINE_FRAMES_PATH
from app.settings_store import get_machine_calibration_path, get_machine_vnc_password

logger = logging.getLogger(__name__)

# Як часто знімаємо кадр верстата. 5 с, а не 15: відставання відсотка від
# екрана — це 15с воркера + 10с віджета, і на 15-хвилинній програмі виходило
# ~5% (скарга власника 03.09.26). Дозволити собі це можна тому, що розбір
# кадру коштує ~56 мс — 10 верстатів це ~11% одного ядра.
POLL_INTERVAL_SECONDS = 5.0
# Мовчазний верстат (ПК вимкнено) тримає той самий дедлайн знімка, що й піч.
CAPTURE_TIMEOUT_SECONDS = 20.0
# «Заголовки ще ніхто не читав» — саме як окреме значення, а не None: None уже
# зайнято під «читали, агент не відповів», і плутати їх не можна.
_NOT_FETCHED = object()
# HTTP-агент відповідає за частки секунди — 20 с це спадок від VNC. При десяти
# верстатах кожен мовчазний ПК тримав би потік 20 с і старив живі сусіди.
# (з'єднатись, дочекатись кадру)
AGENT_TIMEOUT = (3.0, 8.0)
# Стеля на кадр і СУМАРНИЙ дедлайн. Таймаут читання в requests рахується МІЖ
# байтами, не на весь запит: агент, який віддає по байту раз на 7 с, тримав би
# потік нескінченно й ніколи не спрацював би на таймауті. Тому читаємо
# потоково, рахуємо байти й час самі. 24 МБ — з десятикратним запасом до
# реального PNG 1920×1200 (~0.2 МБ), тобто це запобіжник від збою, а не ліміт
# якості.
MAX_FRAME_BYTES = 24 * 1024 * 1024
AGENT_TOTAL_DEADLINE_SECONDS = 15.0
# Кадр на диск пишемо РІДШЕ, ніж аналізуємо: відсоток має бути свіжим (5 с), а
# картинка потрібна лише щоб глянути оком. Без цього 10 верстатів давали б
# ~35 ГБ запису на добу — місце не росте (файл один), але ресурс SSD витрачався
# б дарма.
FRAME_SAVE_INTERVAL_SECONDS = 15.0
# UltraVNC збирає екран полінгом ЛИШЕ поки клієнт підключений — перший кадр
# після конекту «недофарбований» (частина цифр біла, спіймано на 350i).
# Прогрів: тримаємо з'єднання, чекаємо і беремо другий кадр.
# 8 с, а не 2.5: на верстатних ПК стоїть Windows 7, де DDEngine (швидке
# читання відеобуфера) недоступний, а хуки вимкнено — вони трясли екран
# RemiCORE перед оператором. Лишається чистий полінг, і повний прохід
# 1152×864 займає в нього кілька секунд (виміряно кадрами 31.08: за 2.5 с
# правий і нижній краї стабільно лишались білими).
CAPTURE_WARMUP_SECONDS = 8.0
# Після цього мовчання плитка чесно каже «дані застаріли».
STALE_AFTER_SECONDS = 120.0
# Скільки НЕВДАЛИХ опитувань поспіль треба, щоб сказати «немає зв'язку».
# Раніше вистачало одного: загублений SYN у цеховій мережі або зайнятий
# фрезеруванням Windows 7, який не встиг відповісти за 3 с, — і плитка
# червоніла до наступного тіку. Оператор читав це як «зв'язок постійно
# обривається» (скарга 04.09.26). Три поспіль при інтервалі 5 с = справжній
# обрив видно за ~15 с, а поодиноке миготіння не показується взагалі.
# Числа при цьому НЕ підмінюються: за їхню свіжість і далі відповідає
# STALE_AFTER_SECONDS.
PROBLEM_AFTER_FAILURES = 3
# Скільки відсоток мусить простояти на 100, щоб це вважалось «завершено».
# Див. MachineCard.is_completed: витримка проти блимання галочкою.
COMPLETED_AFTER_SECONDS = 120.0
# Скільки останніх обривів памʼятаємо на верстат. Десяти вистачає, щоб побачити
# закономірність («рветься щогодини» / «один раз уночі»), і вони нічого не
# важать для памʼяті.
MAX_OUTAGES = 10


class MachineConfigError(Exception):
    """Адресу верстата написано неправильно. Повідомлення — для оператора."""


@dataclass(frozen=True)
class MachineTarget:
    """Один верстат у налаштуваннях."""

    name: str
    host: str
    port: int = DEFAULT_PORT
    password: Optional[str] = None
    # Непорожній → читаємо кадр через HTTP-агент (Go), а не VNC.
    agent_token: Optional[str] = None
    # id рядка `machines` — для фото верстата (`/machines/portrait/{id}.jpg`).
    # None у тестах і для цілей без рядка: тоді картка бере дефолт моделі.
    machine_id: Optional[int] = None
    # Обраний портрет (ключ з MACHINE_MODELS); "" = вгадати за назвою.
    portrait_model: str = ""
    # Ручний режим калібрування: відкладати кадри за часом (див. Machine).
    collect_calibration: bool = False

    @property
    def key(self) -> str:
        # Та сама логіка, що в печі: адреса як стабільний ідентифікатор.
        return f"{self.host}-{self.port}" if self.port != DEFAULT_PORT else self.host

    @property
    def is_agent(self) -> bool:
        return bool(self.agent_token)


@dataclass
class MachineState:
    """Останнє, що ми знаємо про верстат. Живе в пам'яті процесу."""

    target: MachineTarget
    frame_at: Optional[datetime] = None
    error: Optional[str] = None
    error_at: Optional[datetime] = None
    # Відсоток виконання програми зі смуги RemiCORE (Фаза 2). None — смуги на
    # кадрі не видно (верстат стоїть, інший екран, кадр не читається): краще
    # нічого, ніж хибне число — той самий принцип, що на пічках.
    percent: Optional[int] = None
    percent_at: Optional[datetime] = None
    # Коли відсоток востаннє ЗМІНИВСЯ — див. poll_target. Замороженого числа
    # достатньо, щоб відрізнити верстат у роботі від зупиненого, не читаючи з
    # кадру ні подачу, ні оберти шпинделя.
    percent_changed_at: Optional[datetime] = None
    # Програма ЗАВЕРШЕНА: на екрані нового покоління стоїть підсумок SUMMARY
    # («Completed», Duration/Blanks/Jobs). Смуги прогресу там немає зовсім, тож
    # без цього прапорця завершений верстат виглядав так само, як зупинений
    # («—»), — а для цеху це різні речі: завершений треба розвантажити.
    completed: bool = False
    # Скільки опитувань поспіль не вдалось. Нуль = останнє було успішним.
    fail_streak: int = 0
    # Коли верстат востаннє ВІДПОВІВ — для тривалості обриву в логу.
    last_ok_at: Optional[datetime] = None
    # Історія обривів: (початок, кінець або None якщо триває, причина).
    # У памʼяті процесу, останні MAX_OUTAGES. Потрібна, щоб на питання «як
    # часто рветься» відповідати цифрами, а не відчуттям, — і щоб оператор
    # бачив це в картці, а не шукав у лог-файлі (скарга 04.09.26).
    outages: list = field(default_factory=list)
    polls_ok: int = 0
    polls_failed: int = 0
    # Коли кадр востаннє лягав на диск (аналізуємо частіше, ніж пишемо).
    frame_saved_at: Optional[datetime] = None
    # Що саме фрезерується: ім'я .iso із заголовка вікна RemiCORE і витягнутий
    # з нього Sum3D ID (хвіст HH-MM-SS) — ключ до рядка черги.
    iso_name: Optional[str] = None
    sum3d_id: Optional[str] = None
    program_at: Optional[datetime] = None
    # ЩО САМЕ віддав агент у /titles. Потрібне для діагностики «верстат не
    # показує роботу»: без цього не відрізнити «агент не має /titles» від
    # «заголовки є, але .iso серед них немає» — а це різні причини й різні
    # виправлення. Тримаємо кілька останніх, обрізаних: показуємо адміну.
    titles_seen: Optional[list[str]] = None
    # ── SLM-принтер SISMA (окремий тип екрана, 06.09.26) ────────────────────
    # У нього немає ні смуги RemiCORE, ні .iso в заголовку — зате він пише
    # точні числа: шар N з M і власний прогноз кінця роботи. Поля окремі, а не
    # втиснуті в percent/iso_name, бо це інші сутності: «шар 250 з 1049» це не
    # відсоток фрезерування, а `ends_at` — не наша оцінка, а слово машини.
    layer: Optional[int] = None
    layers_total: Optional[int] = None
    started_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    # Фаза лазера: True — пише шар, False — розрівнює порошок між шарами.
    # Друк іде в ОБОХ випадках (див. app/machine_sisma).
    lasing: Optional[bool] = None
    is_sisma: bool = False


_states: dict[str, MachineState] = {}
_states_lock = threading.Lock()


def list_machines(db: Session, *, only_enabled: bool = False) -> list[Machine]:
    """Верстати з таблиці, у порядку оператора (як пічки)."""
    query = select(Machine).order_by(Machine.sort_order, Machine.id)
    if only_enabled:
        query = query.where(Machine.enabled.is_(True))
    return list(db.scalars(query))


def _safe_decrypt(value: Optional[str], name: str, what: str) -> Optional[str]:
    if not value:
        return None
    try:
        from app.crypto import decrypt_value

        return decrypt_value(value) or None
    except Exception:  # noqa: BLE001 — зіпсований шифр не має валити опитування
        logger.warning("Не вдалося розшифрувати %s верстата %s", what, name)
        return None


def target_of(machine: Machine) -> MachineTarget:
    password = _safe_decrypt(machine.password_encrypted, machine.name, "пароль")
    agent_token = _safe_decrypt(
        getattr(machine, "agent_token_encrypted", None), machine.name, "токен агента"
    )
    return MachineTarget(
        name=machine.name, host=machine.host, port=machine.port,
        password=password, agent_token=agent_token,
        collect_calibration=bool(getattr(machine, "collect_calibration", False)),
        machine_id=machine.id,
        portrait_model=getattr(machine, "portrait_model", "") or "",
    )


def configured_targets(db: Session) -> list[MachineTarget]:
    return [target_of(m) for m in list_machines(db, only_enabled=True)]


def is_configured(db: Session) -> bool:
    return bool(configured_targets(db))


# ── Кадри на диску ──────────────────────────────────────────────────────────
# Той самий контракт, що в печі: ОДИН файл на верстат, перезапис атомарний,
# ключ у URL звіряється зі станом процесу, а не підставляється у шлях.


def frames_root() -> Path:
    root = Path(MACHINE_FRAMES_PATH)
    root.mkdir(parents=True, exist_ok=True)
    return root


# Скільки калібрувальних кадрів щонайбільше тримаємо на верстат. За змістом їх
# буде ~101 (по одному на відсоток), але це запобіжник від патологічного
# накопичення, якщо геометрія почне стрибати. Диск локальний, кадр ~50КБ.
CALIBRATION_MAX_FRAMES = 130
# Ручний збір: не частіше ніж раз на стільки секунд. Це не «крок збору», а
# стеля навантаження — кадр однаково візьмуть лише якщо він НЕСХОЖИЙ на вже
# збережені (див. collect_calibration_frame_timed).
CALIBRATION_TIMED_INTERVAL_SECONDS = 15.0

# ── Відбір за НЕСХОЖІСТЮ ────────────────────────────────────────────────────
# Навіщо. Кадри збирають, щоб навчити читач РІДКІСНИХ екранів: помилка,
# завершення, діалог. Збір «раз на 15 секунд» їх якраз і губить: доба простою
# забиває кільце однаковими кадрами, а двохвилинна аварія о третій ночі до
# ранку витісняється (бойовий випадок 06.09.26 — 107 кадрів простою зі 130).
#
# Тому кадр відкладається, лише якщо він не схожий на жоден уже збережений, а
# коли тека повна — витісняється не найстаріший, а НАЙЗАЙВІШИЙ: той, що
# найближчий до свого сусіда. Набір сам себе тримає різноманітним, і рідкісний
# екран не зникає ніколи, бо йому нема на що бути схожим.
#
# Підпис кадру — та сама мініатюра, що в scripts/machine_frame_clusters.py:
# цифри на ній зникають, розкладка лишається. Тобто «той самий екран з іншим
# відсотком» дублем НЕ вважається помилково — він і є дубль.
CALIBRATION_SIGNATURE_SIZE = (64, 48)
# Середня різниця яскравості (0..255), нижче якої кадри вважаємо однаковими.
# 6 підібрано на 130 бойових кадрах SISMA: простій і друк розходяться на 10.2,
# сусідні кадри одного стану — на частки одиниці.
CALIBRATION_NOVELTY_THRESHOLD = 6.0

_calib_last_timed: dict[str, float] = {}
# Підписи вже збережених кадрів: ключ верстата → {ім'я файлу: підпис}.
# Тримаємо в памʼяті, щоб не перечитувати теку на кожному опитуванні;
# перший доступ підіймає з диска (файлів щонайбільше CALIBRATION_MAX_FRAMES).
_calib_signatures: dict[str, dict[str, tuple[int, ...]]] = {}
_calib_lock = threading.Lock()


def _frame_signature(frame: "Image.Image") -> tuple[int, ...]:
    """Мініатюра кадру як плаский підпис яскравості."""
    small = frame.convert("L").resize(CALIBRATION_SIGNATURE_SIZE, Image.BILINEAR)
    return tuple(small.tobytes())


def _signature_distance(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    if len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _known_signatures(folder: Path, key: str) -> dict[str, tuple[int, ...]]:
    """Підписи кадрів, що вже лежать у теці цього верстата.

    Читаємо диск ОДИН раз на процес: далі словник підтримується записами й
    витісненнями. Кадр, який не відкрився, просто пропускаємо — зіпсований
    файл не привід зупинити збір.
    """
    known = _calib_signatures.get(key)
    if known is not None:
        return known
    known = {}
    for png in sorted(folder.glob("d-*.png")):
        try:
            with Image.open(png) as frame:
                known[png.name] = _frame_signature(frame)
        except Exception:  # noqa: BLE001 — битий файл не має валити збір
            logger.debug("Калібрувальний кадр %s не прочитався", png, exc_info=True)
    _calib_signatures[key] = known
    return known


def _most_redundant(known: dict[str, tuple[int, ...]]) -> Optional[str]:
    """Ім'я найзайвішого кадру — того, що найближчий до свого сусіда.

    Саме він, а не найстаріший: набір мусить лишатись різноманітним, а вік
    кадру про його цінність нічого не каже.
    """
    if len(known) < 2:
        return None
    names = list(known)
    best_name, best_gap = None, None
    for i, name in enumerate(names):
        gap = min(
            _signature_distance(known[name], known[other])
            for j, other in enumerate(names)
            if i != j
        )
        if best_gap is None or gap < best_gap:
            best_name, best_gap = name, gap
    return best_name


def calibration_root(root: Optional[str] = None) -> Path:
    """Корінь калібрувальних кадрів.

    `root` приходить із налаштувань (Налаштування → Верстати, ключ
    `machine_calibration_path`) — оператор може покласти кадри на іншу теку,
    коли на системному диску тісно. Порожньо = типова тека застосунку.
    Резолвиться ТУТ, а не в кожній функції, щоб «звідки беруться кадри» мало
    одну відповідь: банер, zip, збирач і кнопка «Відкрити теку» мусять
    дивитись в одне місце, інакше оператор шукає кадри там, де їх нема.
    """
    return Path(root or MACHINE_CALIBRATION_PATH)


def _sanitize_key(key: str) -> str:
    """Ключ верстата у безпечний сегмент шляху (адреса вже валідна, це пасок
    безпеки: у назву теки не має потрапити ані роздільник, ані «..»)."""
    return "".join(ch if (ch.isalnum() or ch in ".-") else "_" for ch in key)


def calibration_status(root: Optional[str] = None) -> dict:
    """Стан збору калібрувальних кадрів — для банера на екрані «Верстати».

    Каже операторові рівно те, що йому треба знати: скільки кадрів уже
    відкладено й чи ще збираємо. Коли шрифт повний — `active=False`, банер
    ховається, і збирати більше нема потреби.
    """
    missing = sorted(missing_caption_digits())
    folder = calibration_root(root)
    frames = 0
    # Час НАЙСВІЖІШОГО кадру — єдиний чесний доказ, що збір живий.
    # Кількість таким доказом БУТИ НЕ МОЖЕ: кап кільцевий, тека стоїть на
    # межі, і число завмирає назавжди. Оператор читав це як «зламалось»
    # (скарга 06.09.26) — і мав рацію, бо сигнал справді нічого не казав.
    newest: Optional[datetime] = None
    capped = False
    if folder.exists():
        # І кадри по відсотку (pct-*), і зібрані за часом (t-*).
        newest_ts = 0.0
        for png in folder.glob("*/*.png"):
            frames += 1
            try:
                newest_ts = max(newest_ts, png.stat().st_mtime)
            except OSError:
                continue
        if newest_ts:
            newest = datetime.fromtimestamp(newest_ts)
        capped = any(
            sum(1 for _ in sub.glob("*.png")) >= CALIBRATION_MAX_FRAMES
            for sub in folder.iterdir()
            if sub.is_dir()
        )
    # Банер показуємо, доки RemiCORE-цифри неповні АБО вже є зібрані кадри
    # (у т.ч. з ручного режиму для нового покоління) — щоб кнопка «Скачати»
    # була доступна навіть коли RemiCORE-шрифт уже повний.
    return {
        "active": bool(missing) or frames > 0,
        "missing": missing,
        "frames": frames,
        "newest": newest,
        "capped": capped,
        "cap": CALIBRATION_MAX_FRAMES,
    }


def calibration_zip_bytes(root: Optional[str] = None) -> bytes:
    """Усі калібрувальні кадри одним zip — щоб оператор забрав їх із робочого
    ПК одним файлом і надіслав. Порожньо, якщо нічого не зібрано."""
    import io
    import zipfile

    folder = calibration_root(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if folder.exists():
            for png in sorted(folder.glob("*/*.png")):
                # Ім'я в архіві: <верстат>/<файл>, шлях на диску не розкриваємо.
                archive.write(png, arcname=f"{png.parent.name}/{png.name}")
    return buffer.getvalue()


def collect_calibration_frame_timed(
    key: str, frame: "Image.Image", root: Optional[str] = None
) -> None:
    """Відкласти кадр, ЯКЩО ВІН НЕСХОЖИЙ на вже зібрані (ручний режим, галка
    «Калібр.»).

    Ім'я лишилось історичним, а правило змінилось: час тепер лише стеля
    навантаження (не частіше ніж раз на CALIBRATION_TIMED_INTERVAL_SECONDS), а
    рішення «писати чи ні» ухвалює несхожість. Причина — у розділі
    CALIBRATION_NOVELTY_THRESHOLD: збір за годинником губить рідкісні екрани,
    заради яких його й вмикають.

    Ніколи не кидає: збір — зручність, а не робота.
    """
    try:
        now = time.monotonic()
        with _calib_lock:
            last = _calib_last_timed.get(key)
            if last is not None and now - last < CALIBRATION_TIMED_INTERVAL_SECONDS:
                return
            _calib_last_timed[key] = now

        folder = calibration_root(root) / _sanitize_key(key)
        folder.mkdir(parents=True, exist_ok=True)
        signature = _frame_signature(frame)

        with _calib_lock:
            known = _known_signatures(folder, key)
            if known:
                nearest = min(_signature_distance(signature, s) for s in known.values())
                if nearest <= CALIBRATION_NOVELTY_THRESHOLD:
                    return  # такий екран уже є — писати нема сенсу

            # Тека повна: звільняємо місце, викидаючи НАЙЗАЙВІШИЙ кадр (той,
            # що найближчий до сусіда), а не найстаріший. Так набір лишається
            # різноманітним, і давня аварія переживе тиждень простою.
            #
            # Рахуємо ВСІ png (кап на теку спільний), а видаляємо лише свої
            # `d-*`: кадри `pct-*` — по одному на відсоток, вони незамінні.
            if sum(1 for _ in folder.glob("*.png")) >= CALIBRATION_MAX_FRAMES:
                victim = _most_redundant(known)
                if victim is None:
                    return
                try:
                    (folder / victim).unlink()
                except OSError:
                    logger.debug("Кадр %s не видалився", victim, exc_info=True)
                    return
                known.pop(victim, None)

            stamp = datetime.now().strftime("%H%M%S%f")[:-3]
            target = folder / f"d-{stamp}.png"
            if target.exists():
                return
            tmp = folder / f".d-{stamp}.tmp.png"
            frame.save(tmp, format="PNG")
            tmp.replace(target)
            known[target.name] = signature
    except Exception:  # noqa: BLE001 — збір не має валити опитування
        logger.debug("Калібрувальний кадр верстата %s не збережено", key, exc_info=True)


def collect_calibration_frame(
    key: str, frame: "Image.Image", geometry_percent: int, root: Optional[str] = None
) -> None:
    """Відкласти кадр для навчання шрифту — САМЕ доти, доки шрифт неповний.

    Викликається з опитування щоразу, коли з кадру знялась геометрія. Пише
    лише НОВИЙ відсоток (один файл на число), тож за програму-дві набирається
    весь набір цифр, а коли всі десять вивчено — не пише більше нічого. Робочий
    ПК так сам готує матеріал; оператор його лише скачує (zip), навчання
    робиться на машині розробки.

    Ніколи не кидає: збір кадрів — зручність, а не робота, і не має права
    завалити опитування верстата.
    """
    try:
        if not missing_caption_digits():
            return  # шрифт уже повний — збирати нема потреби
        if not (0 <= geometry_percent <= 100):
            return
        folder = calibration_root(root) / _sanitize_key(key)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"pct-{geometry_percent:03d}.png"
        if target.exists():
            return  # цей відсоток уже є
        if sum(1 for _ in folder.glob("pct-*.png")) >= CALIBRATION_MAX_FRAMES:
            return  # запобіжник переповнення
        tmp = folder / f".{geometry_percent:03d}.tmp.png"
        frame.save(tmp, format="PNG")
        tmp.replace(target)
    except Exception:  # noqa: BLE001 — збір не має валити опитування
        logger.debug("Калібрувальний кадр верстата %s не збережено", key, exc_info=True)


def frame_path(key: str) -> Path:
    return frames_root() / f"{key}.png"


def save_frame(key: str, image: Image.Image) -> Path:
    """Кадр на диск атомарно: tmp у ТІЙ САМІЙ теці + replace.

    Імʼя tmp УНІКАЛЬНЕ на виклик. Детерміноване (`key.tmp`) ламалось на двох
    одночасних писачах — фоновий тік і ручне «Оновити» цілком можуть збігтись,
    бо екран «Верстати» тримають відкритим цілий день: обидва писали в один
    файл, і на диск міг лягти напівзаписаний кадр. Сусідні функції збору
    калібрувальних кадрів роблять tmp унікальним — тут цього бракувало
    (знайдено рев'ю 04.09.26). Свій tmp прибираємо за собою, якщо запис упав."""
    path = frame_path(key)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident():x}.tmp")
    try:
        image.save(tmp, format="PNG")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def resolve_frame(key: str) -> Optional[Path]:
    """Шлях до кадру ЛИШЕ для відомого процесу ключа — інакше None."""
    with _states_lock:
        known = key in _states
    if not known or not _HOST_RE.match(key.replace("-", "")):
        # ключ або невідомий, або містить те, чого в адресі бути не може
        if not known:
            return None
    path = frame_path(key)
    return path if path.exists() else None


# ── Опитування ──────────────────────────────────────────────────────────────


def _capture_http(host: str, port: int, token: str) -> Image.Image:
    """Кадр екрана через HTTP-агент (Go kmill-agent): GET /capture з токеном.

    На відміну від VNC, агент бачить синю смугу % RemiCORE. Помилки (мережа,
    невірний токен, не PNG) піднімаються як виняток — їх ловить poll_target і
    показує причину на екрані «Верстати», а не тихе порожнє поле."""
    import io

    import requests

    url = f"http://{host}:{port}/capture"
    # Мережеві збої — людською: сирий текст requests («HTTPConnectionPool…
    # Max retries exceeded… NewConnectionError…») лягав у плитку на екрані
    # «Верстати» шістьма рядками. Оператору треба лише «хто» і «що робити».
    started = time.monotonic()
    try:
        resp = requests.get(
            url, headers={"X-Agent-Token": token}, timeout=AGENT_TIMEOUT, stream=True
        )
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError(
            f"ПК {host} мовчить на порту {port} — вимкнено або порт закрито брандмауером"
        ) from exc
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(f"агент {host}:{port} не віддав кадр за {AGENT_TIMEOUT[1]:.0f} с") from exc
    except requests.exceptions.ConnectionError as exc:
        # РІЗНИЦЯ ТУТ ВИРІШАЛЬНА для діагностики, тому розділяємо явно.
        # «Відмовлено у зʼєднанні» = ПК ЖИВИЙ і сам відповів відмовою: слухати
        # нема кому, тобто агент помер. Це зовсім інша поломка, ніж мовчання
        # мережі, і лікується інакше (перевстановити агента, а не шукати
        # кабель). Обидва повідомлення раніше починались з «ПК вимкнено» і
        # змазували саме цю різницю (04.09.26, нічне випадання одного верстата).
        if _is_refused(exc):
            raise RuntimeError(
                f"ПК {host} працює, але на порту {port} ніхто не слухає — агент не запущено"
            ) from exc
        raise RuntimeError(
            f"ПК {host} не відповідає в мережі — вимкнено, спить або кабель"
        ) from exc
    with resp:
        if resp.status_code == 403:
            raise RuntimeError("агент відхилив токен (403) — звір токен у налаштуваннях")
        resp.raise_for_status()
        buf = io.BytesIO()
        for chunk in resp.iter_content(64 * 1024):
            buf.write(chunk)
            if buf.tell() > MAX_FRAME_BYTES:
                raise RuntimeError(
                    f"агент {host}:{port} віддає завеликий кадр (>{MAX_FRAME_BYTES // 1024 // 1024} МБ)"
                )
            if time.monotonic() - started > AGENT_TOTAL_DEADLINE_SECONDS:
                raise RuntimeError(
                    f"агент {host}:{port} віддає кадр надто повільно — обрив за {AGENT_TOTAL_DEADLINE_SECONDS:.0f} с"
                )
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _is_refused(exc: BaseException) -> bool:
    """Чи це саме «відмовлено у зʼєднанні» (ПК живий, слухача немає).

    requests загортає купу різних бід в один ConnectionError, тож дивимось у
    ланцюг причин: ConnectionRefusedError означає, що хост ВІДПОВІВ відмовою —
    отже він у мережі, а помер саме агент."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, ConnectionRefusedError):
            return True
        if getattr(exc, "errno", None) == 10061:  # WSAECONNREFUSED
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _fetch_titles(host: str, port: int, token: str) -> list[str] | None:
    """Заголовки вікон з агента (GET /titles).

    Розрізняє ДВА випадки, і це важливо для очищення:
    * `[]` — агент відповів, але потрібного вікна немає (програма завершилась,
      RemiCORE закрито) → знімаємо стару прив'язку;
    * `None` — агент не відповів / старий агент без ендпоінта / битий JSON →
      НЕ чіпаємо: ми просто не знаємо, а не «нічого не фрезерується»."""
    import requests

    try:
        resp = requests.get(
            f"http://{host}:{port}/titles",
            headers={"X-Agent-Token": token},
            timeout=AGENT_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        titles = data.get("titles")
        if titles is None:
            return None
        return [str(t) for t in titles]
    except Exception:  # noqa: BLE001 — мережа/старий агент/битий JSON
        return None


def poll_target(
    db: Session,
    target: MachineTarget,
    password: Optional[str],
    now: Optional[datetime] = None,
    frame: Optional[Image.Image] = None,
    error: Optional[str] = None,
    titles: object = _NOT_FETCHED,
) -> MachineState:
    """Один знімок одного верстата: кадр → диск → стан у пам'яті.

    `titles` — заголовки вікон, ЯКЩО їх уже прочитав хтось інший. Це не
    оптимізація, а виправлення відмови: читання заголовків — ДРУГИЙ мережевий
    виклик, і поки він жив тут, він виконувався послідовно в потоці-виклику.
    Паралелізм `poll_all` рятував лише кадр, а мовчазний ПК усе одно тримав
    обхід на заголовках (3 с на підключення × кількість мертвих верстатів при
    інтервалі полінгу 5 с). `_NOT_FETCHED` відрізняє «ніхто не читав» від
    «читали, агент не відповів»: у другому випадку прив'язку програми чіпати
    не можна — ми не знаємо, а не «нічого не фрезерується»."""
    now = now or datetime.now()
    with _states_lock:
        state = _states.setdefault(target.key, MachineState(target=target))
        state.target = target

    if frame is None and error is None:
        try:
            if target.is_agent:
                frame = _capture_http(target.host, target.port, target.agent_token)
            else:
                frame = capture(
                    target.host,
                    port=target.port,
                    password=target.password or password,
                    timeout=CAPTURE_TIMEOUT_SECONDS,
                    warmup=CAPTURE_WARMUP_SECONDS,
                )
        except FurnaceVncError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 — мережа цеху вміє дивувати
            error = f"Знімок не вдався: {exc}"

    if error is not None:
        with _states_lock:
            state.error = error
            state.error_at = now
            state.fail_streak += 1
            state.polls_failed += 1
            streak = state.fail_streak
            since = state.last_ok_at
        # Пишемо в лог САМЕ ПЕРЕХІД, а не кожен невдалий тік: інакше мертвий
        # верстат за ніч насипле 17 тисяч рядків. Один рядок на обрив дає
        # відповідь на «як часто рветься» цифрами, а не відчуттям.
        if streak == PROBLEM_AFTER_FAILURES:
            with _states_lock:
                state.outages.append([since or now, None, error])
                del state.outages[:-MAX_OUTAGES]
            logger.warning(
                "Верстат %s: обрив зв'язку (остання відповідь %s) — %s",
                target.name,
                since.strftime("%H:%M:%S") if since else "невідомо",
                error,
            )
        if streak >= PROBLEM_AFTER_FAILURES:
            record_state(target.key, "off", now)
        return state

    # Диск чіпаємо не частіше ніж раз на FRAME_SAVE_INTERVAL_SECONDS: свіжість
    # потрібна ВІДСОТКУ (він у пам'яті), а картинку дивляться оком.
    due = (
        state.frame_saved_at is None
        or (now - state.frame_saved_at).total_seconds() >= FRAME_SAVE_INTERVAL_SECONDS
    )
    saved_at = state.frame_saved_at
    if due:
        try:
            save_frame(target.key, frame)
            saved_at = now
        except OSError:
            logger.exception("Кадр верстата %s не збережено", target.host)

    # Відсоток — з ТОГО САМОГО кадру. Стоїть тут (а не в grab), бо poll_target —
    # спільна лійка обох шляхів опитування: фонового (poll_all) і разового.
    # Саме розходження цих шляхів дало баг 0.6.13, повторювати його не будемо.
    # SISMA має власний екран і власного читача. Пробуємо його ПЕРШИМ і, якщо
    # це справді SISMA, детектори смуги RemiCORE навіть не запускаємо: шукати
    # синю смугу на чужому інтерфейсі — рівно той шлях, яким беруться числа з
    # повітря (шматок шпалер уже одного разу став «смугою на 100%»).
    sisma = read_sisma(frame) if screen_is_sisma(frame) else None
    if sisma is not None:
        percent = sisma.percent
    else:
        try:
            percent = read_progress_percent(frame)
        except Exception:  # noqa: BLE001 — читання кадру не має валити опитування
            logger.exception("Відсоток верстата %s не прочитано", target.host)
            percent = None
    # ЗАВЖДИ пишемо результат СВІЖОГО кадру, навіть None. Інакше, коли програма
    # завершилась і смуга зникла з екрана, старий відсоток залипав назавжди —
    # робота показувала «85%», хоч давно готова (бойовий випадок 03.09.26).
    # Новий кадр без смуги = «зараз не фрезерується», а не «лишилось 85%».
    # Коли число ЗМІНИЛОСЬ (а не коли ми його востаннє прочитали). Бойові
    # кадри 03.09.26: верстат показував 81% шість хвилин поспіль — і це була
    # правда, машина стояла (vl 0.0 mm/min, шпиндель 0 U/min, інструмент 17 у
    # помилці). Оператор же прочитав це як «CRM залипла». Різниця між
    # «фрезерує» і «стоїть на 81%» видима лише в ЧАСІ, тому запамʼятовуємо
    # момент зміни: percent_at каже, наскільки свіже читання, а
    # percent_changed_at — наскільки живий верстат.
    # Екран підсумку — з того самого кадру й тією ж лійкою, що й відсоток.
    # Взаємно виключні за побудовою: на 285 бойових кадрах чотирьох верстатів
    # жоден не дав одночасно число і SUMMARY (перевірено 04.09.26).
    if sisma is not None:
        # У SISMA свій підсумок — вікно «Workzone Report», яке машина відкриває
        # САМА після останнього шару (підтвердив власник 07.09.26). Лічильник
        # шарів для цього не годиться: 1049/1049 на екрані не буває, принтер
        # одразу скидається в підготовку. Шукати тут SUMMARY нового покоління
        # RemiCORE теж нічого — то інший верстат.
        completed = sisma.finished
    else:
        try:
            completed = screen_is_completed(frame)
        except Exception:  # noqa: BLE001 — читання кадру не має валити опитування
            logger.exception("Екран верстата %s не розпізнано", target.host)
            completed = False

    # Усе, що прочитали з ОДНОГО кадру, лягає в стан ОДНИМ кроком під локом.
    # Раніше поля писались по черзі, а між ними стояли дискове I/O і мережевий
    # виклик — і читач (віджет, /machines, milling_now) міг зловити свіжий
    # відсоток у парі зі старим Sum3D ID, тобто показати прогрес не тієї
    # роботи. Плюс фоновий тік і ручне «Оновити» — це два потоки на один
    # об'єкт стану (знайдено рев'ю 04.09.26).
    with _states_lock:
        if state.fail_streak >= PROBLEM_AFTER_FAILURES:
            gap = (now - state.last_ok_at).total_seconds() if state.last_ok_at else None
            logger.warning(
                "Верстат %s: зв'язок відновлено%s",
                target.name,
                f" після {gap / 60:.0f} хв" if gap else "",
            )
        if state.outages and state.outages[-1][1] is None:
            state.outages[-1][1] = now
        state.fail_streak = 0
        state.last_ok_at = now
        state.polls_ok += 1
        if percent != state.percent or state.percent_changed_at is None:
            state.percent_changed_at = now
        state.percent = percent
        state.percent_at = now
        state.completed = completed
        # Поля SISMA пишемо ЗАВЖДИ (навіть None): та сама причина, що з
        # відсотком — інакше після завершення роботи на екрані залипли б
        # старі шари й старий час кінця, і картка показувала б давно знятий
        # друк як живий.
        state.is_sisma = sisma is not None
        state.layer = sisma.layer if sisma else None
        state.layers_total = sisma.layers_total if sisma else None
        state.started_at = sisma.started_at if sisma else None
        state.ends_at = sisma.ends_at if sisma else None
        state.lasing = sisma.lasing if sisma else None
        state.frame_at = now
        state.error = None
        state.frame_saved_at = saved_at

    record_state(
        target.key,
        observed_kind(percent=percent, completed=completed, error=False),
        now,
    )

    # Поки шрифт підпису неповний, відкладаємо кадр із новим відсотком для
    # навчання. `percent` тут — геометрія (підпис ще не читається, бо саме його
    # й калібруємо), тобто правильна мітка. Коли всі цифри вивчено —
    # collect_calibration_frame сам нічого не робить.
    #
    # Теку резолвимо ЛИШЕ коли справді збираємо: це запит до налаштувань, а
    # poll_target крутиться на кожен верстат кожні 15 с.
    if percent is not None or target.collect_calibration:
        calib_root = get_machine_calibration_path(db)
        if percent is not None:
            collect_calibration_frame(target.key, frame, percent, calib_root)
        # Ручний режим: збираємо кадри за часом навіть коли відсоток НЕ
        # читається — саме для верстатів, де читача ще нема (нове покоління,
        # інша розкладка).
        if target.collect_calibration:
            collect_calibration_frame_timed(target.key, frame, calib_root)

    # Що фрезерується — лише через агента (заголовок вікна). У VNC такого
    # каналу немає, і вигадувати його з картинки ми не будемо.
    if target.is_agent:
        if titles is _NOT_FETCHED:
            # Одиничний виклик (ручне «Оновити» одного верстата) — читаємо самі.
            titles = _fetch_titles(target.host, target.port, target.agent_token)
        if titles is not None:  # агент відповів — довіряємо результату
            program = pick_milling_program(titles)
            # Порожньо/немає програми = вікно закрилось → знімаємо прив'язку,
            # інакше «фрезерується Кривовид» висіло б після завершення.
            with _states_lock:
                state.iso_name = program.iso_name if program else None
                state.sum3d_id = program.sum3d_id if program else None
                state.program_at = now
                state.titles_seen = [str(x)[:120] for x in titles[:12]]
        else:
            with _states_lock:
                state.titles_seen = None
    return state


def poll_all(db: Session, now: Optional[datetime] = None) -> list[MachineState]:
    """Знімки всіх верстатів ПАРАЛЕЛЬНО — той самий урок, що з печами:
    мовчазний ПК тримає дедлайн 20 с, і послідовний обхід десяти верстатів
    означав би, що живі старіють через мертві. Сесія БД лишається на цьому
    потоці — у знімальні потоки йде лише мережа."""
    now = now or datetime.now()
    targets = configured_targets(db)
    if not targets:
        return []
    shared = get_machine_vnc_password(db)

    def grab(target: MachineTarget):
        # ТА САМА розвилка транспорту, що в poll_target: воркер ходить саме
        # сюди, тож без неї верстат з HTTP-агентом опитувався б по VNC і давав
        # «not a VNC server» (бойовий випадок 02.09.26).
        #
        # ОБИДВА мережеві виклики — тут, у потоці. Заголовки колись читались у
        # poll_target, тобто вже послідовно, і мовчазний ПК тримав обхід на
        # них: 3 с на підключення × кількість мертвих при інтервалі 5 с.
        # Паралелізм рятував лише кадр — рівно та відмова, від якої він мав
        # захищати (знайдено рев'ю 04.09.26).
        titles: object = _NOT_FETCHED
        try:
            if target.is_agent:
                image = _capture_http(target.host, target.port, target.agent_token)
            else:
                image = capture(
                    target.host,
                    port=target.port,
                    password=target.password or shared,
                    timeout=CAPTURE_TIMEOUT_SECONDS,
                    warmup=CAPTURE_WARMUP_SECONDS,
                )
        except FurnaceVncError as exc:
            return target, None, str(exc), titles
        except Exception as exc:  # noqa: BLE001
            return target, None, f"Знімок не вдався: {exc}", titles
        # Заголовки лише для агентних верстатів і лише коли кадр уже є: до
        # мертвого ПК другий раз не ходимо — він щойно відмовив.
        if target.is_agent:
            titles = _fetch_titles(target.host, target.port, target.agent_token)
        return target, image, None, titles

    results = []
    # Усі верстати ПАРАЛЕЛЬНО: при десяти й пулі на вісім виходило два заходи,
    # і мовчазний ПК у першому старив живі з другого.
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        for target, image, error, titles in pool.map(grab, targets):
            results.append(
                poll_target(
                    db, target, shared, now=now, frame=image, error=error, titles=titles
                )
            )
    return results


# ── Картки для екранів ──────────────────────────────────────────────────────


# Чотири портрети цеху (з реальних фото власника, 04.09.26): ключ → підпис у
# селекторі Налаштувань. Ключ = суфікс файлу app/static/img/machine-portrait-*.jpg.
MACHINE_MODELS: tuple[tuple[str, str], ...] = (
    ("350i", "350i"),
    ("350i-loader", "350i loader"),
    ("250i", "250i"),
    ("250i-dry", "250i dry"),
)
MACHINE_MODEL_KEYS = frozenset(key for key, _ in MACHINE_MODELS)


def machine_model_key(name: str, chosen: str = "") -> str:
    """Портрет картки: ОБРАНИЙ у Налаштуваннях, а без вибору — здогад за
    моделлю в назві (loader / dry / 250 / решта 350i). Невідомий ключ у
    `chosen` (стара БД, чужа форма) не ламає нічого — просто здогад."""
    if chosen in MACHINE_MODEL_KEYS:
        return chosen
    lowered = (name or "").lower()
    if "loader" in lowered or "лоадер" in lowered:
        return "350i-loader"
    if "250" in lowered:
        return "250i-dry" if "dry" in lowered else "250i"
    return "350i"


@dataclass
class MachineCard:
    target: MachineTarget
    state: Optional[MachineState]
    now: datetime = field(default_factory=datetime.now)
    # Робота з черги, знайдена за sum3d_id програми. Заповнює snapshot() ОДНИМ
    # запитом на всі картки: запит усередині property дав би N+1 у циклі
    # рендера (той самий урок, що з focused_ids).
    # УСІ роботи з цим Sum3D ID, не одна. Один проєкт Sum3D цілком може містити
    # кілька робіт, що фрезеруються з однієї заготовки — власник підтвердив
    # 04.09.26. Раніше код у цьому випадку свідомо не вгадував і не показував
    # НІЧОГО, хоч правильна відповідь — показати всі.
    orders: list = field(default_factory=list)
    # ЧОМУ пари немає. «немає в черзі» одним написом покривало три різні
    # причини — не знайдено взагалі, знайдено в кількох роботах (не вгадуємо),
    # знайдено лише серед архівних. Оператор бачив однакове й не міг зрозуміти,
    # що робити (скарга 04.09.26). Порожньо = пара є або ID не читається.
    match_note: str = ""

    @property
    def key(self) -> str:
        return self.target.key

    @property
    def has_frame(self) -> bool:
        return bool(self.state and self.state.frame_at)

    @property
    def frame_at(self) -> Optional[datetime]:
        return self.state.frame_at if self.state else None

    @property
    def has_problem(self) -> bool:
        """«Немає зв'язку» — лише після PROBLEM_AFTER_FAILURES невдач поспіль.

        Одна невдача — це ще не обрив: у цеховій мережі губиться пакет, а ПК
        верстата під фрезеруванням не завжди відповідає за 3 с. Показувати за
        нею червону плитку означало миготіти на очах в оператора кожні кілька
        хвилин (скарга 04.09.26)."""
        return bool(
            self.state
            and self.state.error
            and self.state.fail_streak >= PROBLEM_AFTER_FAILURES
        )

    @property
    def problem_text(self) -> str:
        if not (self.state and self.state.error):
            return ""
        text = self.state.error
        # Адреса в тексті причини зайва — вона вже є в назві й налаштуваннях.
        return text.split(": ", 1)[-1] if text.startswith("Піч ") else text

    @property
    def stale(self) -> bool:
        if not (self.state and self.state.frame_at):
            return False
        return (self.now - self.state.frame_at).total_seconds() > STALE_AFTER_SECONDS

    @property
    def percent(self) -> Optional[int]:
        """Відсоток зі СВІЖОГО кадру. Протухлий кадр числа не дає: показувати
        старий відсоток як поточний — це і є «хибне число»."""
        if not (self.state and self.state.percent is not None):
            return None
        if self.stale or self.has_problem:
            return None
        return self.state.percent

    @property
    def is_running(self) -> bool:
        """Програма йде: є відсоток і він ще не 100."""
        pct = self.percent
        return pct is not None and pct < 100

    # ── SLM-принтер SISMA ───────────────────────────────────────────────
    # Свіжість перевіряється так само, як для відсотка: протухлий кадр не дає
    # ні шарів, ні часу кінця. Показати вчорашній «закінчить о 20:58» гірше,
    # ніж не показати нічого — за цим числом планують зміну.

    @property
    def is_sisma_machine(self) -> bool:
        """Це SLM-принтер — за ОСТАННІМ упізнаним екраном, без огляду на
        свіжість. Саме за цим полем віджет вирішує, кого показувати: інакше
        принтер, який щойно втратив зв'язок, зник би з екрана разом із
        повідомленням про обрив — тобто саме тоді, коли він найпотрібніший."""
        return bool(self.state and self.state.is_sisma)

    @property
    def is_sisma(self) -> bool:
        """Дані принтера можна ЧИТАТИ — кадр свіжий і зв'язок є."""
        return self.is_sisma_machine and not (self.stale or self.has_problem)

    @property
    def layers(self) -> Optional[tuple[int, int]]:
        """Шар N з M — точні числа з екрана, а не оцінка."""
        if not self.is_sisma:
            return None
        if not (self.state.layer and self.state.layers_total):
            return None
        return (self.state.layer, self.state.layers_total)

    @property
    def started_at(self) -> Optional[datetime]:
        """Коли машина почала друк — її ж слова з екрана."""
        return self.state.started_at if self.is_sisma else None

    @property
    def ends_at(self) -> Optional[datetime]:
        """Коли машина обіцяє закінчити — ЇЇ прогноз, не наш."""
        return self.state.ends_at if self.is_sisma else None

    @property
    def lasing(self) -> Optional[bool]:
        """True — пише шар, False — розрівнює порошок. Обидва = працює."""
        return self.state.lasing if self.is_sisma else None

    @property
    def left_text(self) -> str:
        """«лишилось 3 год 55 хв» — різниця між прогнозом машини і зараз.

        Порожньо, коли термін уже минув: «лишилось -12 хв» гірше за тишу, а
        прогноз машини на паузах цілком може відстати від годинника."""
        ends = self.ends_at
        if ends is None:
            return ""
        seconds = (ends - self.now).total_seconds()
        if seconds <= 0:
            return ""
        hours, minutes = divmod(int(seconds) // 60, 60)
        if hours and minutes:
            return f"лишилось {hours} год {minutes} хв"
        if hours:
            return f"лишилось {hours} год"
        return f"лишилось {minutes} хв"

    @property
    def phase_text(self) -> str:
        """Фаза словами. Порожньо, якщо фаза не прочиталась."""
        if not self.is_sisma or self.layers is None:
            return ""
        if self.lasing is True:
            return "пише шар"
        if self.lasing is False:
            return "розрівнює порошок"
        return ""

    @property
    def sum3d_id(self) -> Optional[str]:
        """Sum3D ID програми на верстаті — зі свіжого читання."""
        if not (self.state and self.state.sum3d_id):
            return None
        return None if (self.stale or self.has_problem) else self.state.sum3d_id

    @property
    def iso_name(self) -> Optional[str]:
        if not (self.state and self.state.iso_name):
            return None
        return None if (self.stale or self.has_problem) else self.state.iso_name

    @property
    def order(self) -> Optional[Order]:
        """Перша зі знайдених робіт — для місць, де вміщується лише одна."""
        return self.orders[0] if self.orders else None

    @property
    def link_report(self) -> Optional[str]:
        """Підсумок звʼязку: скільки обривів і скільки часу верстат мовчав.

        Показуємо, лише якщо обриви БУЛИ — на здоровому верстаті це зайвий шум.
        Рахуємо від старту застосунку: історія в памʼяті процесу, і це чесно
        видно з формулювання."""
        if not (self.state and self.state.outages):
            return None
        done = [o for o in self.state.outages if o[1] is not None]
        total = sum((o[1] - o[0]).total_seconds() for o in done)
        parts = [f"обривів: {len(self.state.outages)}"]
        if done:
            longest = max((o[1] - o[0]).total_seconds() for o in done)
            parts.append(f"найдовший {longest / 60:.0f} хв")
            parts.append(f"разом {total / 60:.0f} хв")
        if self.state.outages[-1][1] is None:
            parts.append("зараз триває")
        return " · ".join(parts)

    @property
    def link_outages(self) -> list[str]:
        """Обриви рядками, найновіші зверху."""
        out = []
        for start, end, reason in reversed(self.state.outages if self.state else []):
            when = start.strftime("%H:%M")
            if end is None:
                out.append(f"{when} — триває · {reason}")
            else:
                out.append(f"{when}–{end.strftime('%H:%M')} ({(end - start).total_seconds() / 60:.0f} хв)")
        return out

    @property
    def titles_report(self) -> Optional[str]:
        """Що агент бачить у заголовках вікон — рядком для адміна.

        Відповідає на «верстат не показує, яка робота фрезерується»: причин дві
        і вони різні. Або агент старий і /titles у нього немає взагалі, або
        заголовки є, але імені `.iso` серед них немає — так буває на пласких
        CORiTEC, де назва програми стоїть НА ЕКРАНІ, а не в заголовку вікна
        (бойовий випадок 150i, 04.09.26). Без цього рядка їх не розрізнити."""
        if not (self.state and self.target.is_agent):
            return None
        if self.sum3d_id:
            return None  # усе працює, діагностика зайва
        seen = self.state.titles_seen
        if seen is None:
            return "агент не віддає заголовки вікон (старий агент або немає звʼязку)"
        if not seen:
            return "агент віддав порожній список вікон"
        return f"вікон: {len(seen)} · імені .iso серед них немає"

    @property
    def titles_list(self) -> list[str]:
        return list(self.state.titles_seen or []) if self.state else []

    @property
    def is_completed(self) -> bool:
        """Програма завершена. Два незалежні шляхи, і обидва потрібні.

        (1) Екран підсумку SUMMARY («Completed») — є лише на верстатах нового
        покоління. Саме тому галочку досі бачив ОДИН верстат із чотирьох, а
        решта показували «100%» і мовчали (скарга власника 06.09.26).

        (2) Відсоток стоїть на 100 довше COMPLETED_AFTER_SECONDS. Пауза тут не
        косметична: смуга торкається сотні й на мить перед зміною програми, і
        без витримки галочка блимала б на здоровому верстаті. Дві хвилини —
        свідомо більше, ніж будь-який такий доторк, і менше, ніж час, за який
        оператор дійде знімати роботу.

        Читається зі СВІЖОГО кадру, як і відсоток: «завершено» з протухлого
        кадру — те саме хибне число, якого ми уникаємо."""
        if not self.state or self.stale or self.has_problem:
            return False
        if self.state.completed:
            return True
        if self.state.percent != 100 or self.state.percent_changed_at is None:
            return False
        held = (self.now - self.state.percent_changed_at).total_seconds()
        return held >= COMPLETED_AFTER_SECONDS

    @property
    def has_program(self) -> bool:
        """Чи завантажена програма на верстаті — за заголовком вікна RemiCORE
        (`...ім'я.iso`), який агент читає НЕЗАЛЕЖНО від того, яку вкладку
        показує RemiCORE. Потрібно, щоб не брехати «програма не йде», коли
        відсотка на поточному екрані просто не видно (сітка інструментів
        замість смуги «NN%»), а програма насправді йде — саме на це скаржився
        власник 04.09.26 (верстат .76 на іншій вкладці)."""
        return bool(self.iso_name or self.sum3d_id)

    @property
    def portrait_url(self) -> Optional[str]:
        """Фото САМЕ ЦЬОГО верстата (Налаштування → Фото), або None → дефолт
        моделі. mtime у URL — щоб нове фото не перекрив кеш браузера."""
        mid = self.target.machine_id
        if mid is None:
            return None
        version = portrait_version(mid)
        return None if version is None else f"/machines/portrait/{mid}.jpg?v={version}"

    @property
    def model_key(self) -> str:
        return machine_model_key(self.target.name, self.target.portrait_model)


def milling_now() -> dict[str, dict]:
    """Sum3D ID → {machine, percent} для робіт, що ЗАРАЗ фрезеруються.

    Годує підсвітку рядка черги. Читає лише памʼять процесу (жодного запиту й
    жодного походу до верстата): черга рендерить сотні рядків, і будь-який
    запит на рядок був би N+1 — той самий урок, що з `focused_ids`.

    Протухлий кадр і верстат без звʼязку не потрапляють сюди взагалі: показати
    «фрезерується» для роботи, яку зняли пів години тому, гірше, ніж не
    показати нічого.
    """
    now = datetime.now()
    out: dict[str, dict] = {}
    with _states_lock:
        states = list(_states.values())
    for state in states:
        if not (state.sum3d_id and state.frame_at) or state.error:
            continue
        if (now - state.frame_at).total_seconds() > STALE_AFTER_SECONDS:
            continue
        # Той самий ID на двох верстатах — не вгадуємо, прибираємо обидва.
        if state.sum3d_id in out:
            out[state.sum3d_id] = None
            continue
        out[state.sum3d_id] = {
            "machine": state.target.name,
            "percent": state.percent,
            "stalled": _percent_is_stalled(state, now),
        }
    return {k: v for k, v in out.items() if v}


STALLED_AFTER_SECONDS = 300.0
"""Скільки відсоток має простояти без змін, щоб назвати верстат зупиненим.

П'ять хвилин, бо повільні фінішні проходи на цирконії справді дають хвилини
без зміни цілого відсотка (смуга ~128px, тобто крок ≈ 0.8%). Менший поріг
чіпляв би живий верстат, а це рівно та брехня, якої тут не можна: краще
сказати «стоїть» на п'ять хвилин пізніше, ніж сказати це помилково."""


def _percent_is_stalled(state: "MachineState", now: datetime) -> bool:
    """Число завмерло? Лише для НЕПОРОЖНЬОГО відсотка: без смуги немає що
    заморожувати, а «зупинився на невідомо чому» — не повідомлення."""
    if state.percent is None or state.percent_changed_at is None:
        return False
    if state.percent >= 100:
        return False  # завершено — це не зупинка
    return (now - state.percent_changed_at).total_seconds() >= STALLED_AFTER_SECONDS


def machine_side_context(db: Session) -> dict:
    """Контекст віджета верстатів у бічній панелі черги.

    Спільний для роута полла (/machines/side) і для першого рендера черги —
    щоб два входи не розійшлись (урок віджета пічок). Читає лише памʼять
    процесу, до верстатів не ходить.

    SLM-принтери сюди НЕ потрапляють: у фрезерного відсоток програми, у
    принтера — шари й час завершення, і в одному ряду вони читаються як
    однакові речі (рішення власника 06.09.26). Принтер живе у власному
    віджеті над чергою.
    """
    return {
        "machine_cards": [c for c in snapshot(db) if not c.is_sisma_machine],
        "machine_summary": strip_summary(db),
    }


def sisma_context(db: Session) -> dict:
    """Контекст віджета SLM-принтерів над чергою.

    Той самий контракт, що в machine_side_context: лише памʼять процесу, і
    ОДИН вхід для першого рендера й для полла."""
    return {"sisma_cards": [c for c in snapshot(db) if c.is_sisma_machine]}


def strip_summary(db: Session) -> dict:
    """Підсумок для шапки віджета: скільки фрезерує / без зв'язку.

    Годується з тих самих карток, що й сам віджет — два ПОГЛЯДИ на одне
    значення це нормально, два ДЖЕРЕЛА ні (правило зі смуги пічок).

    SISMA сюди не входить — так само, як `machine_side_context` не кладе її
    в картки: принтер, що друкує, інакше рахувався б як «фрезерує», а
    відключений — як «без звʼязку» серед фрезерних."""
    cards = [c for c in snapshot(db) if not c.is_sisma_machine]
    return {
        "total": len(cards),
        "running": sum(1 for c in cards if c.is_running),
        "broken": sum(1 for c in cards if c.has_problem),
    }


def snapshot(db: Session) -> list[MachineCard]:
    """Картки ВСІХ налаштованих верстатів — читає лише пам'ять процесу.

    До верстата з потоку запиту не ходимо: мовчазний ПК тримав би сторінку
    двадцять секунд (та сама причина, що в /furnaces/side)."""
    now = datetime.now()
    cards = []
    with _states_lock:
        states = dict(_states)
    for target in configured_targets(db):
        cards.append(MachineCard(target=target, state=states.get(target.key), now=now))

    # Зв'язка «верстат ↔ наряд»: ОДИН запит на всі картки (не N+1). Шукаємо
    # серед НЕархівних робіт — програма на верстаті завжди з робочого вікна.
    wanted = {c.sum3d_id for c in cards if c.sum3d_id}
    if wanted:
        by_id: dict[str, list[Order]] = {}

        def offer(key: Optional[str], order: Order) -> None:
            if not key:
                return
            bucket = by_id.setdefault(key, [])
            if all(o.id != order.id for o in bucket):
                bucket.append(order)

        for row in db.scalars(
            select(Order).where(
                Order.sum3d_id.in_(wanted), Order.archived_at.is_(None)
            )
        ).all():
            offer(row.sum3d_id, row)

        # ПЕРЕРОБКИ. Для переробленої роботи живий ID лежить не в Order, а в
        # її ReworkRecord (колонка W таблиці) — саме туди пише оператор, і саме
        # його показує рядок черги. Пошук лише по Order означав, що верстат,
        # який фрезерує переробку, чесно читав ID з екрана і так само чесно
        # казав «немає в черзі», хоч робота лежала поруч (бойовий випадок
        # 250-New, 04.09.26). Фільтр черги цю різницю враховує давно
        # (queue_filters._has_sum3d) — тут вона загубилась.
        for rec in db.scalars(
            select(ReworkRecord)
            .join(Order, Order.id == ReworkRecord.order_id)
            .where(ReworkRecord.sum3d_id.in_(wanted), Order.archived_at.is_(None))
            .options(selectinload(ReworkRecord.order))
        ).all():
            if rec.order is not None:
                offer(rec.sum3d_id, rec.order)

        # Чи існує така робота взагалі — включно з архівними. Окремий, дешевий
        # запит лише для тих ID, що не знайшлись: без нього «робота є, але вона
        # в архіві» і «такої роботи немає» виглядають однаково.
        missing = {c.sum3d_id for c in cards if c.sum3d_id and not by_id.get(c.sum3d_id)}
        archived: set[str] = set()
        if missing:
            archived = {
                sid
                for (sid,) in db.execute(
                    select(Order.sum3d_id).where(
                        Order.sum3d_id.in_(missing), Order.archived_at.is_not(None)
                    )
                ).all()
                if sid
            }

        for card in cards:
            if not card.sum3d_id:
                continue
            card.orders = by_id.get(card.sum3d_id, [])
            if card.orders:
                continue
            if card.sum3d_id in archived:
                card.match_note = "робота з цим ID уже в архіві"
            else:
                card.match_note = "жодна робота в черзі не має цього ID"
    return cards


# ── Історія стану за добу ───────────────────────────────────────────────────
#
# Питання оператора: «верстат стояв уночі чи фрезерував?». Відповіді не було
# ніде — картка показує лише «просто зараз».
#
# Стрічка живе В ПАМ'ЯТІ ПРОЦЕСУ, а не в базі, і це свідомо. Показання верстата
# в базу не пишуться взагалі (на відміну від печей), тому «показати вже зібране»
# і «завести таблицю показань» — дві різні зміни, і робити їх однією означало б
# протягнути нову схему даних під виглядом графіка. Той самий вибір уже зроблено
# для `outages` («рахуємо від старту застосунку, і це чесно видно з
# формулювання»), і графік каже те саме прямо: до першого спостереження — не
# «стояв», а «немає даних».

# Скільки історії тримаємо. Довше не треба: це оперативна довідка на зміну.
HISTORY_HOURS = 24
# Скільки тиші вже вважаємо діркою. Кадр знімається раз на 5 с, тож хвилина без
# жодного спостереження — це не пауза, а справжня відсутність даних (застосунок
# перезапустили, опитувач став).
TIMELINE_GAP_SECONDS = 60.0
# Стеля відрізків на верстат. Верстат, який блимає між станами, інакше ріс би
# в пам'яті без межі.
HISTORY_MAX_SPANS = 600
TIMELINE_WIDTH = 360
TIMELINE_HEIGHT = 18

# Що саме показував верстат. Порядок = порядок легенди на екрані.
KIND_LABELS = {
    "run": "фрезерує",
    "done": "програма завершена",
    "idle": "стоїть",
    "off": "немає зв'язку",
    "gap": "немає даних",
}

# key → список [початок, кінець, стан]. Окремий лок, а не _states_lock:
# запис у стрічку йде ПІСЛЯ того, як стан уже зафіксовано, і чіпати заради
# нього лок гарячого шляху не треба.
_history: dict[str, list] = {}
_history_lock = threading.Lock()


def record_state(key: str, kind: str, now: datetime) -> None:
    """Додати спостереження в денну стрічку верстата.

    Спостереження, а не подію: ми записуємо те, що бачили В ЦЮ СЕКУНДУ, і
    тягнемо відрізок далі, поки стан не змінився. Якщо між спостереженнями
    минуло більше за TIMELINE_GAP_SECONDS, відрізок НЕ подовжується, а
    починається новий — інакше застосунок, який стояв ніч, «дорисував» би
    верстату вісім годин фрезерування, яких ніхто не бачив.
    """
    if kind not in KIND_LABELS:  # захист від друкарської помилки в новому виклику
        return
    with _history_lock:
        spans = _history.setdefault(key, [])
        last = spans[-1] if spans else None
        fresh = last is not None and (now - last[1]).total_seconds() <= TIMELINE_GAP_SECONDS
        if last is not None and fresh and last[2] == kind:
            last[1] = now
        else:
            if last is not None and fresh:
                # Стан змінився саме тут — попередній тягнеться до цієї миті,
                # щоб між відрізками не з'явилась дірка на порожньому місці.
                last[1] = now
            spans.append([now, now, kind])
        cutoff = now - timedelta(hours=HISTORY_HOURS)
        while len(spans) > 1 and spans[0][1] < cutoff:
            spans.pop(0)
        del spans[:-HISTORY_MAX_SPANS]


def observed_kind(*, percent, completed, error: bool) -> str:
    """Що записати у стрічку за результатом одного опитування."""
    if error:
        return "off"
    if completed:
        return "done"
    if percent is None:
        return "idle"
    return "run" if percent < 100 else "done"


@dataclass(frozen=True)
class TimelineSpan:
    """Один прямокутник стрічки. `kind` == "gap" — це саме відсутність даних,
    а не окремий стан верстата."""

    x: float
    width: float
    kind: str
    title: str


@dataclass(frozen=True)
class TimelineTick:
    pos: float
    label: str


@dataclass(frozen=True)
class DayTimeline:
    started_at: datetime
    ended_at: datetime
    width: int = TIMELINE_WIDTH
    height: int = TIMELINE_HEIGHT
    spans: list[TimelineSpan] = field(default_factory=list)
    ticks: list[TimelineTick] = field(default_factory=list)
    # Скільки часу верстат провів у кожному стані, у секундах.
    totals: dict = field(default_factory=dict)
    # Від якої миті взагалі є спостереження (старт застосунку або поява
    # верстата в налаштуваннях). Раніше цього моменту — не «стояв», а «не знаємо».
    since: Optional[datetime] = None

    @property
    def has_data(self) -> bool:
        return any(span.kind != "gap" for span in self.spans)

    @property
    def summary(self) -> list[tuple]:
        """(підпис, тривалість) для кожного стану, що траплявся. Порядок —
        як у KIND_LABELS, щоб рядок не переставлявся від тіку до тіку."""
        out = []
        for kind, label in KIND_LABELS.items():
            seconds = self.totals.get(kind, 0.0)
            if seconds > 0:
                out.append((label, span_text(seconds), kind))
        return out


def build_day_timeline(spans: list, *, started_at: datetime, ended_at: datetime) -> DayTimeline:
    """Відрізки з пам'яті → геометрія стрічки. Чиста функція, тому й тестована."""
    span_seconds = max(1.0, (ended_at - started_at).total_seconds())

    def x_of(moment: datetime) -> float:
        share = (moment - started_at).total_seconds() / span_seconds
        return round(min(1.0, max(0.0, share)) * TIMELINE_WIDTH, 1)

    out: list[TimelineSpan] = []
    totals: dict[str, float] = {}

    def add(start: datetime, end: datetime, kind: str) -> None:
        x = x_of(start)
        seconds = (end - start).total_seconds()
        totals[kind] = totals.get(kind, 0.0) + seconds
        out.append(
            TimelineSpan(
                x=x,
                width=max(1.0, round(x_of(end) - x, 1)),
                kind=kind,
                title=(
                    f"{KIND_LABELS[kind]} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
                    f" · {span_text(seconds)}"
                ),
            )
        )

    cursor = started_at
    since: Optional[datetime] = None
    for start, end, kind in spans:
        start = max(start, started_at)
        end = min(end, ended_at)
        if end < start:
            continue
        # «Від якої миті ми взагалі дивимось» рахується ДО відсіву нульових
        # відрізків: одне-єдине спостереження — це вже початок нагляду, і
        # підпис під стрічкою має його назвати.
        if since is None:
            since = start
        if end == start:
            # Миттєве спостереження без тривалості. Малювати з нього
            # прямокутник означало б приписати верстату кілька хвилин у
            # стані, який ми бачили одну мить.
            continue
        # Дірка перед цим відрізком — окремим прямокутником, а не розтягнутим
        # сусідом: невідомість не має виглядати як стан.
        if (start - cursor).total_seconds() > TIMELINE_GAP_SECONDS:
            add(cursor, start, "gap")
        cursor = max(cursor, end)
        add(start, end, kind)
    if (ended_at - cursor).total_seconds() > TIMELINE_GAP_SECONDS:
        add(cursor, ended_at, "gap")

    ticks: list[TimelineTick] = []
    mark = started_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while mark < ended_at:
        if mark.hour % 3 == 0:
            ticks.append(TimelineTick(pos=x_of(mark), label=mark.strftime("%H:%M")))
        mark += timedelta(hours=1)

    return DayTimeline(
        started_at=started_at,
        ended_at=ended_at,
        spans=out,
        ticks=ticks,
        totals=totals,
        since=since,
    )


def day_timeline(key: str, *, now: Optional[datetime] = None, hours: int = HISTORY_HOURS) -> DayTimeline:
    """Стрічка «працює/стоїть» одного верстата за останню добу."""
    ended_at = now or datetime.now()
    started_at = ended_at - timedelta(hours=hours)
    with _history_lock:
        spans = [list(span) for span in _history.get(key, [])]
    return build_day_timeline(spans, started_at=started_at, ended_at=ended_at)


def reset_state_for_tests() -> None:
    with _states_lock:
        _states.clear()
    with _history_lock:
        _history.clear()


__all__ = [
    "CAPTURE_TIMEOUT_SECONDS",
    "MachineCard",
    "MachineConfigError",
    "MachineTarget",
    "MachineState",
    "POLL_INTERVAL_SECONDS",
    "configured_targets",
    "frames_root",
    "is_configured",
    "list_machines",
    "poll_all",
    "poll_target",
    "resolve_frame",
    "snapshot",
    "target_of",
    "validate_address",
]
