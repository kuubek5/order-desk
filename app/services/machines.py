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
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.furnace_vnc import DEFAULT_PORT, FurnaceVncError, capture
from app.machine_portraits import portrait_version
from app.machine_ocr import (
    missing_caption_digits,
    pick_milling_program,
    read_progress_percent,
    screen_is_completed,
)
from app.models import Machine, Order
from app.services.furnace import _HOST_RE, validate_address  # ті самі правила адреси
from app.config import MACHINE_CALIBRATION_PATH, MACHINE_FRAMES_PATH
from app.settings_store import get_machine_vnc_password

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
# Ручний (за часом) збір: не частіше ніж раз на стільки секунд, щоб за програму
# набрати РІЗНІ кадри, а не сотні однакових.
CALIBRATION_TIMED_INTERVAL_SECONDS = 15.0
_calib_last_timed: dict[str, float] = {}
_calib_lock = threading.Lock()


def _sanitize_key(key: str) -> str:
    """Ключ верстата у безпечний сегмент шляху (адреса вже валідна, це пасок
    безпеки: у назву теки не має потрапити ані роздільник, ані «..»)."""
    return "".join(ch if (ch.isalnum() or ch in ".-") else "_" for ch in key)


def calibration_status() -> dict:
    """Стан збору калібрувальних кадрів — для банера на екрані «Верстати».

    Каже операторові рівно те, що йому треба знати: скільки кадрів уже
    відкладено й чи ще збираємо. Коли шрифт повний — `active=False`, банер
    ховається, і збирати більше нема потреби.
    """
    missing = sorted(missing_caption_digits())
    root = Path(MACHINE_CALIBRATION_PATH)
    frames = 0
    if root.exists():
        # І кадри по відсотку (pct-*), і зібрані за часом (t-*).
        frames = sum(1 for _ in root.glob("*/*.png"))
    # Банер показуємо, доки RemiCORE-цифри неповні АБО вже є зібрані кадри
    # (у т.ч. з ручного режиму для нового покоління) — щоб кнопка «Скачати»
    # була доступна навіть коли RemiCORE-шрифт уже повний.
    return {"active": bool(missing) or frames > 0, "missing": missing, "frames": frames}


def calibration_zip_bytes() -> bytes:
    """Усі калібрувальні кадри одним zip — щоб оператор забрав їх із робочого
    ПК одним файлом і надіслав. Порожньо, якщо нічого не зібрано."""
    import io
    import zipfile

    root = Path(MACHINE_CALIBRATION_PATH)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if root.exists():
            for png in sorted(root.glob("*/*.png")):
                # Ім'я в архіві: <верстат>/<файл>, шлях на диску не розкриваємо.
                archive.write(png, arcname=f"{png.parent.name}/{png.name}")
    return buffer.getvalue()


def collect_calibration_frame_timed(key: str, frame: "Image.Image") -> None:
    """Відкласти кадр за ЧАСОМ (ручний режим калібрування, `collect_calibration`).

    Для верстата, де відсоток ще не читається (нове покоління, інша розкладка),
    дедуп за відсотком неможливий — тож беремо кадр не частіше ніж раз на
    CALIBRATION_TIMED_INTERVAL_SECONDS. За програму набереться спред кадрів на
    різних відсотках, оператор їх качає кнопкою «Скачати кадри», як і раніше.
    Кап той самий. Ніколи не кидає — збір це зручність, не робота.
    """
    try:
        now = time.monotonic()
        with _calib_lock:
            last = _calib_last_timed.get(key)
            if last is not None and now - last < CALIBRATION_TIMED_INTERVAL_SECONDS:
                return
            _calib_last_timed[key] = now
        folder = Path(MACHINE_CALIBRATION_PATH) / _sanitize_key(key)
        folder.mkdir(parents=True, exist_ok=True)
        if sum(1 for _ in folder.glob("*.png")) >= CALIBRATION_MAX_FRAMES:
            return
        # Мілісекунди в імені — унікальність навіть за кількох збережень в одну
        # секунду (у проді інтервал 15 с, але хай ім'я не колізить ніколи).
        stamp = datetime.now().strftime("%H%M%S%f")[:-3]
        target = folder / f"t-{stamp}.png"
        if target.exists():
            return
        tmp = folder / f".t-{stamp}.tmp.png"
        frame.save(tmp, format="PNG")
        tmp.replace(target)
    except Exception:  # noqa: BLE001 — збір не має валити опитування
        logger.debug("Калібрувальний кадр (час) верстата %s не збережено", key, exc_info=True)


def collect_calibration_frame(key: str, frame: "Image.Image", geometry_percent: int) -> None:
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
        folder = Path(MACHINE_CALIBRATION_PATH) / _sanitize_key(key)
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
            f"агент {host}:{port} не відповідає — ПК вимкнено або порт закрито брандмауером"
        ) from exc
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(f"агент {host}:{port} не віддав кадр за {AGENT_TIMEOUT[1]:.0f} с") from exc
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"агент {host}:{port} недоступний — ПК вимкнено або агент не запущено"
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
            streak = state.fail_streak
            since = state.last_ok_at
        # Пишемо в лог САМЕ ПЕРЕХІД, а не кожен невдалий тік: інакше мертвий
        # верстат за ніч насипле 17 тисяч рядків. Один рядок на обрив дає
        # відповідь на «як часто рветься» цифрами, а не відчуттям.
        if streak == PROBLEM_AFTER_FAILURES:
            logger.warning(
                "Верстат %s: обрив зв'язку (остання відповідь %s) — %s",
                target.name,
                since.strftime("%H:%M:%S") if since else "невідомо",
                error,
            )
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
        state.fail_streak = 0
        state.last_ok_at = now
        if percent != state.percent or state.percent_changed_at is None:
            state.percent_changed_at = now
        state.percent = percent
        state.percent_at = now
        state.completed = completed
        state.frame_at = now
        state.error = None
        state.frame_saved_at = saved_at

    # Поки шрифт підпису неповний, відкладаємо кадр із новим відсотком для
    # навчання. `percent` тут — геометрія (підпис ще не читається, бо саме його
    # й калібруємо), тобто правильна мітка. Коли всі цифри вивчено —
    # collect_calibration_frame сам нічого не робить.
    if percent is not None:
        collect_calibration_frame(target.key, frame, percent)
    # Ручний режим: збираємо кадри за часом навіть коли відсоток НЕ читається —
    # саме для верстатів, де читача ще нема (нове покоління, інша розкладка).
    if target.collect_calibration:
        collect_calibration_frame_timed(target.key, frame)

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
    order: Optional[Order] = None

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
        """Програма завершена — на екрані підсумок SUMMARY («Completed»).

        Читається зі СВІЖОГО кадру, як і відсоток: показувати «завершено» з
        протухлого кадру означало б те саме «хибне число», якого ми уникаємо.
        Взаємно виключне з `percent` за побудовою детектора."""
        if not (self.state and self.state.completed):
            return False
        return not (self.stale or self.has_problem)

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
    процесу, до верстатів не ходить."""
    return {"machine_cards": snapshot(db), "machine_summary": strip_summary(db)}


def strip_summary(db: Session) -> dict:
    """Підсумок для шапки віджета: скільки фрезерує / без зв'язку.

    Годується з тих самих карток, що й сам віджет — два ПОГЛЯДИ на одне
    значення це нормально, два ДЖЕРЕЛА ні (правило зі смуги пічок)."""
    cards = snapshot(db)
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
        rows = db.scalars(
            select(Order).where(
                Order.sum3d_id.in_(wanted), Order.archived_at.is_(None)
            )
        ).all()
        by_id: dict[str, Order] = {}
        for row in rows:
            # Той самий ID у двох роботах — не вгадуємо, лишаємо без зв'язки.
            by_id[row.sum3d_id] = None if row.sum3d_id in by_id else row
        for card in cards:
            if card.sum3d_id:
                card.order = by_id.get(card.sum3d_id)
    return cards


def reset_state_for_tests() -> None:
    with _states_lock:
        _states.clear()


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
