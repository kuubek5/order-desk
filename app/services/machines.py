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
from app.machine_ocr import (
    missing_caption_digits,
    pick_milling_program,
    read_progress_percent,
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
# HTTP-агент відповідає за частки секунди — 20 с це спадок від VNC. При десяти
# верстатах кожен мовчазний ПК тримав би потік 20 с і старив живі сусіди.
# (з'єднатись, дочекатись кадру)
AGENT_TIMEOUT = (3.0, 8.0)
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
    # Коли кадр востаннє лягав на диск (аналізуємо частіше, ніж пишемо).
    frame_saved_at: Optional[datetime] = None
    # Що саме фрезерується: ім'я .iso із заголовка вікна RemiCORE і витягнутий
    # з нього Sum3D ID (хвіст HH-MM-SS) — ключ до рядка черги.
    iso_name: Optional[str] = None
    sum3d_id: Optional[str] = None
    program_at: Optional[datetime] = None


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
        frames = sum(1 for _ in root.glob("*/pct-*.png"))
    return {"active": bool(missing), "missing": missing, "frames": frames}


def calibration_zip_bytes() -> bytes:
    """Усі калібрувальні кадри одним zip — щоб оператор забрав їх із робочого
    ПК одним файлом і надіслав. Порожньо, якщо нічого не зібрано."""
    import io
    import zipfile

    root = Path(MACHINE_CALIBRATION_PATH)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if root.exists():
            for png in sorted(root.glob("*/pct-*.png")):
                # Ім'я в архіві: <верстат>/<файл>, шлях на диску не розкриваємо.
                archive.write(png, arcname=f"{png.parent.name}/{png.name}")
    return buffer.getvalue()


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
    path = frame_path(key)
    tmp = path.with_suffix(".tmp")
    image.save(tmp, format="PNG")
    tmp.replace(path)
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
    resp = requests.get(url, headers={"X-Agent-Token": token}, timeout=AGENT_TIMEOUT)
    if resp.status_code == 403:
        raise RuntimeError("агент відхилив токен (403) — звір токен у налаштуваннях")
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


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
) -> MachineState:
    """Один знімок одного верстата: кадр → диск → стан у пам'яті."""
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
        state.error = error
        state.error_at = now
        return state

    state.frame_at = now
    state.error = None
    # Диск чіпаємо не частіше ніж раз на FRAME_SAVE_INTERVAL_SECONDS: свіжість
    # потрібна ВІДСОТКУ (він у пам'яті), а картинку дивляться оком.
    due = (
        state.frame_saved_at is None
        or (now - state.frame_saved_at).total_seconds() >= FRAME_SAVE_INTERVAL_SECONDS
    )
    if due:
        try:
            save_frame(target.key, frame)
            state.frame_saved_at = now
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
    if percent != state.percent or state.percent_changed_at is None:
        state.percent_changed_at = now
    state.percent = percent
    state.percent_at = now

    # Поки шрифт підпису неповний, відкладаємо кадр із новим відсотком для
    # навчання. `percent` тут — геометрія (підпис ще не читається, бо саме його
    # й калібруємо), тобто правильна мітка. Коли всі цифри вивчено —
    # collect_calibration_frame сам нічого не робить.
    if percent is not None:
        collect_calibration_frame(target.key, frame, percent)

    # Що фрезерується — лише через агента (заголовок вікна). У VNC такого
    # каналу немає, і вигадувати його з картинки ми не будемо.
    if target.is_agent:
        titles = _fetch_titles(target.host, target.port, target.agent_token)
        if titles is not None:  # агент відповів — довіряємо результату
            program = pick_milling_program(titles)
            # Порожньо/немає програми = вікно закрилось → знімаємо прив'язку,
            # інакше «фрезерується Кривовид» висіло б після завершення.
            state.iso_name = program.iso_name if program else None
            state.sum3d_id = program.sum3d_id if program else None
            state.program_at = now
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
            return target, image, None
        except FurnaceVncError as exc:
            return target, None, str(exc)
        except Exception as exc:  # noqa: BLE001
            return target, None, f"Знімок не вдався: {exc}"

    results = []
    # Усі верстати ПАРАЛЕЛЬНО: при десяти й пулі на вісім виходило два заходи,
    # і мовчазний ПК у першому старив живі з другого.
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        for target, image, error in pool.map(grab, targets):
            results.append(
                poll_target(db, target, shared, now=now, frame=image, error=error)
            )
    return results


# ── Картки для екранів ──────────────────────────────────────────────────────


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
        return bool(self.state and self.state.error)

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
