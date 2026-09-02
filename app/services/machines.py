# -*- coding: utf-8 -*-
"""Верстати: коли знімати кадр екрана RemiCORE і що з ним робити.

Фаза 1 — ЖИВИЙ КАДР у CRM, без розпізнавання чисел. Це вже відповідає на
головне питання оператора («що зараз на верстаті?») без ходіння до RustDesk.
Фаза 2 — OCR відсотка/часу/імені програми тим самим конвеєром еталонів, що
читає табло печі; кадр і зони для неї вже будуть на місці.

Каркас свідомо повторює app/services/furnace.py: обидва модулі — «залізо з
екраном за VNC». Розбіжності теж свідомі:
- історії в базі немає (нема ще чисел, які варто зберігати);
- кадр знімається рідше (15 с): екран RemiCORE 1920×1080 — це вчетверо
  більший framebuffer, ніж табло печі, а десять верстатів — не чотири печі.

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
from app.models import Machine
from app.services.furnace import _HOST_RE, validate_address  # ті самі правила адреси
from app.config import MACHINE_FRAMES_PATH
from app.settings_store import get_machine_vnc_password

logger = logging.getLogger(__name__)

# Кадр раз на 15 с: RemiCORE міняє відсоток нечасто, а framebuffer великий.
POLL_INTERVAL_SECONDS = 15.0
# Мовчазний верстат (ПК вимкнено) тримає той самий дедлайн знімка, що й піч.
CAPTURE_TIMEOUT_SECONDS = 20.0
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
    resp = requests.get(
        url, headers={"X-Agent-Token": token}, timeout=CAPTURE_TIMEOUT_SECONDS
    )
    if resp.status_code == 403:
        raise RuntimeError("агент відхилив токен (403) — звір токен у налаштуваннях")
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


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

    try:
        save_frame(target.key, frame)
        state.frame_at = now
        state.error = None
    except OSError:
        logger.exception("Кадр верстата %s не збережено", target.host)
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
        try:
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
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
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
