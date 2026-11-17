"""Оновлення агента на ПК верстата з KuubMill, без походу до верстата (17.11.26).

Досі нова версія агента ставилась лише інсталятором на кожному ПК верстата з
правами адміністратора, тому доходила не всюди (0.21.140 чекав на трьох
верстатах). Агент навчився оновлюватись сам (`agent/selfupdate.go`): приймає
новий файл через `POST /update` і запускає його лише з дійсним підписом, а
якщо нова версія не запрацювала — лишається на попередній.

Цей модуль — сторона KuubMill: взяти підписаний файл з релізу `agent-latest`,
перевірити його ЩЕ ТУТ і надіслати на верстат за кнопкою адміністратора.

Що тут свідомо НЕ робиться:
  - нічого не оновлюється само: лише за кнопкою, по одному верстату;
  - приватного ключа тут немає й бути не може — підписує збірка в CI, тут
    лише публічний ключ для перевірки (той самий, що зашито в агента);
  - на верстат, який зараз фрезерує, файл не шлеться: агент лише читає екран,
    але перезапуск на кілька секунд забирає в табло відсоток і назву програми.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models import Machine
from app.services import machines

logger = logging.getLogger(__name__)

RELEASE_TAG = "agent-latest"
EXE_ASSET = "kmill-agent.exe"
MANIFEST_ASSET = "kmill-agent.manifest.json"
SIGNATURE_ASSET = "kmill-agent.manifest.sig"

# Публічні ключі ed25519 (hex) — ТІ САМІ, що в agent/internal/upd/upd.go
# (TrustedKeys); збіг стереже tests/test_agent_update.py. Перевірка тут не
# замінює перевірку на агенті, а лише не дає слати на верстат файл, який він
# однаково відхилить.
TRUSTED_KEYS = ("20bd5af8a0e0adfe4b42f3fe0fb4cb64bb1293cbafd5d0f469ad7cc446fca344",)

PRODUCT_NAME = "kmill-agent"
MAX_BYTES = 64 << 20
DOWNLOAD_TIMEOUT = (5.0, 60.0)
STATUS_TIMEOUT = (2.0, 3.0)
PUSH_TIMEOUT = (3.0, 180.0)
# Скільки чекати, поки агент підніметься в новій версії. Сам він чекає на неї
# до 45 с і лише потім відкочується, тож менше ждати немає сенсу.
CONFIRM_SECONDS = 60.0
CONFIRM_STEP = 2.0


class AgentUpdateError(Exception):
    """Оновлення неможливе; текст — для адміністратора на екрані."""


@dataclass(frozen=True)
class Package:
    raw_manifest: bytes
    signature: bytes
    version: str
    built: int
    sha256: str
    size: int
    exe_path: Path


@dataclass(frozen=True)
class Result:
    tone: str  # ok | warn | bad
    message: str
    version: str = ""


# ---------------------------------------------------------------------------
# Підписаний файл з релізу.
# ---------------------------------------------------------------------------


def verify_manifest(raw: bytes, signature: bytes) -> dict:
    """Перевіряє підпис СИРИХ байтів маніфесту й лише потім розбирає їх."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signed = False
    for key_hex in TRUSTED_KEYS:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(signature, raw)
            signed = True
            break
        except (InvalidSignature, ValueError):
            continue
    if not signed:
        raise AgentUpdateError("підпис файла оновлення не збігається — файл не надіслано")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentUpdateError("маніфест оновлення не читається") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != PRODUCT_NAME:
        raise AgentUpdateError("маніфест не від агента верстата")
    built, size, sha = manifest.get("built"), manifest.get("size"), manifest.get("sha256")
    if (
        not isinstance(built, int) or built <= 0
        or not isinstance(size, int) or not 0 < size <= MAX_BYTES
        or not isinstance(sha, str) or len(sha) != 64
        or not isinstance(manifest.get("version"), str)
    ):
        raise AgentUpdateError("маніфест оновлення неповний")
    return manifest


def cache_dir() -> Path:
    from app.config import DATA_DIR

    return Path(DATA_DIR) / "agent_update"


def _asset_url(name: str) -> str:
    from app.update_check import GITHUB_REPO

    return f"https://github.com/{GITHUB_REPO}/releases/download/{RELEASE_TAG}/{name}"


def _default_get(url: str, **kwargs):
    # Та сама сесія, що в перевірки оновлень: без неї GitHub за проксі цеху
    # віддає SSLEOFError.
    from app.update_check import _http_get

    return _http_get(url, **kwargs)


def _sha256_of(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def fetch_package(http_get: Optional[Callable] = None, root: Optional[Path] = None) -> Package:
    """Свіжий підписаний агент з релізу. Сам файл лежить у кеші за часом збірки,
    тож на другий верстат він уже не качається."""
    get = http_get or _default_get
    root = root or cache_dir()
    try:
        raw = _small(get, _asset_url(MANIFEST_ASSET))
        sig_text = _small(get, _asset_url(SIGNATURE_ASSET))
    except AgentUpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 — мережа, проксі, GitHub
        logger.warning("Агент: маніфест оновлення не скачався", exc_info=True)
        raise AgentUpdateError(f"не вдалося взяти оновлення з GitHub: {type(exc).__name__}") from exc
    try:
        signature = bytes.fromhex(sig_text.decode("ascii").strip())
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentUpdateError("підпис оновлення не читається") from exc
    manifest = verify_manifest(raw, signature)
    sha = manifest["sha256"].lower()

    folder = root / str(manifest["built"])
    exe = folder / EXE_ASSET
    if not (exe.is_file() and _sha256_of(exe) == (sha, manifest["size"])):
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / (EXE_ASSET + ".part")
        try:
            _download(get, _asset_url(EXE_ASSET), part, manifest["size"])
        except AgentUpdateError:
            part.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            part.unlink(missing_ok=True)
            logger.warning("Агент: файл оновлення не скачався", exc_info=True)
            raise AgentUpdateError(f"файл оновлення не скачався: {type(exc).__name__}") from exc
        if _sha256_of(part) != (sha, manifest["size"]):
            part.unlink(missing_ok=True)
            # Буває, коли реліз саме перезбирається: маніфест уже новий, файл
            # ще старий. Через хвилину збігатиметься.
            raise AgentUpdateError("скачаний файл не збігається з підписаним маніфестом — спробувати за хвилину")
        part.replace(exe)
        _drop_older_builds(root, keep=folder.name)
    return Package(
        raw_manifest=raw, signature=signature, version=manifest["version"],
        built=manifest["built"], sha256=sha, size=manifest["size"], exe_path=exe,
    )


def _drop_older_builds(root: Path, keep: str) -> None:
    """Агент перезбирається з кожним релізом KuubMill, тож без прибирання кеш
    ріс би на файл за реліз. Лишається лише щойно скачана збірка."""
    import shutil

    for entry in root.iterdir():
        if entry.is_dir() and entry.name.isdigit() and entry.name != keep:
            shutil.rmtree(entry, ignore_errors=True)


def _small(get: Callable, url: str) -> bytes:
    resp = get(url, timeout=DOWNLOAD_TIMEOUT)
    if resp.status_code == 404:
        raise AgentUpdateError("у релізі агента ще немає підписаного файла оновлення")
    if resp.status_code != 200:
        raise AgentUpdateError(f"GitHub відповів {resp.status_code} на запит оновлення агента")
    body = resp.content
    if len(body) > 64 * 1024:
        raise AgentUpdateError("маніфест оновлення завеликий")
    return body


def _download(get: Callable, url: str, dest: Path, expected: int) -> None:
    resp = get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    if resp.status_code != 200:
        raise AgentUpdateError(f"GitHub відповів {resp.status_code} на запит файла агента")
    written = 0
    with dest.open("wb") as fh:
        for chunk in resp.iter_content(1 << 20):
            written += len(chunk)
            if written > expected:
                raise AgentUpdateError("файл оновлення довший, ніж у маніфесті")
            fh.write(chunk)


# ---------------------------------------------------------------------------
# Розмова з агентом.
# ---------------------------------------------------------------------------


def agent_status(host: str, port: int, token: str) -> Optional[dict]:
    """`GET /status` агента або None (не відповів, чужий токен, не JSON)."""
    import requests

    try:
        resp = requests.get(
            f"http://{host}:{port}/status",
            headers={"X-Agent-Token": token}, timeout=STATUS_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 — тиша/відмова/битий JSON = «не відповів»
        logger.debug("Агент %s:%s не відповів на /status", host, port, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def push(host: str, port: int, token: str, package: Package) -> tuple[int, dict]:
    """Надсилає файл агентові. Повертає код відповіді й розібране тіло."""
    import requests

    with package.exe_path.open("rb") as fh:
        resp = requests.post(
            f"http://{host}:{port}/update",
            data=fh,
            headers={
                "X-Agent-Token": token,
                "X-Update-Manifest": base64.b64encode(package.raw_manifest).decode("ascii"),
                "X-Update-Signature": package.signature.hex(),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(package.size),
            },
            timeout=PUSH_TIMEOUT,
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def is_milling(db: Session, machine_id: int) -> bool:
    """Чи верстат зараз фрезерує — тим самим станом, що й табло."""
    for card in machines.snapshot(db):
        if card.target.machine_id == machine_id:
            return card.is_running
    return False


def update_machine(
    db: Session,
    machine_id: int,
    *,
    fetch: Callable[[], Package] = fetch_package,
    status: Callable[[str, int, str], Optional[dict]] = agent_status,
    send: Callable[[str, int, str, Package], tuple[int, dict]] = push,
    milling: Callable[[Session, int], bool] = is_milling,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Result:
    """Оновити агента одного верстата й дочекатись, що він піднявся в новій
    версії. Нічого не кидає: будь-яка відмова — це `Result` з поясненням."""
    machine = db.get(Machine, machine_id)
    if machine is None:
        return Result("bad", "верстат не знайдено")
    target = machines.target_of(machine)
    if not target.host or not target.agent_token:
        return Result("bad", "у цього верстата не задано агента (адреса й токен)")
    host, port, token = target.host, target.port, target.agent_token

    before = status(host, port, token)
    if before is None:
        return Result("bad", "агент не відповідає — оновити зараз не можна")
    current = str(before.get("version") or "")
    if not before.get("self_update"):
        return Result(
            "warn",
            f"агент {current or 'цієї версії'} ще не вміє оновлюватись сам — "
            "один раз поставити свіжий інсталятором на ПК верстата",
            current,
        )
    if milling(db, machine_id):
        return Result("warn", "верстат зараз фрезерує — оновити, коли зупиниться", current)

    try:
        package = fetch()
    except AgentUpdateError as exc:
        return Result("bad", str(exc), current)

    try:
        agent_built = int(before.get("built") or 0)
    except (TypeError, ValueError):
        agent_built = 0
    if agent_built >= package.built:
        _remember(db, machine, current)
        return Result("ok", f"вже найновіша версія ({current})", current)

    try:
        code, body = send(host, port, token, package)
    except Exception as exc:  # noqa: BLE001 — обрив мережі посеред надсилання
        logger.warning("Агент %s: файл оновлення не надіслано", machine.name, exc_info=True)
        return Result("bad", f"файл не дійшов до верстата: {type(exc).__name__}", current)
    if code == 409:
        _remember(db, machine, current)
        return Result("ok", f"вже найновіша версія ({current})", current)
    if code != 200 or not body.get("ok"):
        why = str(body.get("error") or f"код {code}")
        return Result("bad", f"агент відхилив оновлення: {why}", current)
    if not body.get("restart"):
        note = str(body.get("note") or "нова версія запрацює після наступного входу в Windows")
        return Result("warn", f"файл прийнято; {note}", current)

    # Агент перезапускається. Чекаємо на НОВУ версію, а не просто на відповідь:
    # відповісти може й попередня, якщо нова не прижилась.
    deadline = clock() + CONFIRM_SECONDS
    last: Optional[dict] = None
    while clock() < deadline:
        sleep(CONFIRM_STEP)
        last = status(host, port, token)
        if last is not None and str(last.get("version") or "") == package.version:
            _remember(db, machine, package.version)
            logger.info("Агент %s оновлено: %s → %s", machine.name, current, package.version)
            return Result("ok", f"оновлено: {current or '?'} → {package.version}", package.version)
        if last is not None and last.get("update_note") and last.get("pid") != before.get("pid"):
            # Відповів уже НОВИЙ процес і сам сказав, чому нова версія не
            # працює, — далі чекати нічого. Той самий процес, що приймав файл,
            # міг нести стару примітку про попереднє оновлення.
            break
    if last is None:
        return Result(
            "bad",
            "після оновлення агент не відповідає — сторож на верстаті підніме його "
            "за хвилину; перевірити пізніше",
            current,
        )
    stayed = str(last.get("version") or "")
    why = str(last.get("update_note") or "причина не названа")
    _remember(db, machine, stayed)
    logger.warning("Агент %s: нова версія %s не прижилась (%s)", machine.name, package.version, why)
    return Result("bad", f"нова версія не прижилась ({why}); агент працює на {stayed}", stayed)


def _remember(db: Session, machine: Machine, version: str) -> None:
    """Версію, яку агент щойно назвав, одразу в базу — не чекати пів години
    до наступного проходу `agent_versions`."""
    if not version:
        return
    machine.agent_version = version[:60]
    machine.agent_version_at = datetime.now()
    db.commit()
