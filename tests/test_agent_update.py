"""Оновлення агента верстата з KuubMill: `app/services/agent_update.py`.

Ні мережі, ні верстатів: GitHub і агент підмінені функціями. Сам агент
(прийом файла, підпис, відкат) перевіряється своїми тестами на Go —
`agent/internal/upd/upd_test.go` і наскрізним `agent/e2e_test.go`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.crypto import encrypt_value
from app.models import Base, Machine
from app.services import agent_update as au

ROOT = Path(__file__).resolve().parents[1]
EXE = b"MZ" + b"new agent build " * 64


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


@pytest.fixture
def key(monkeypatch):
    """Тестовий ключ замість справжнього: приватної частини справжнього тут немає."""
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(au, "TRUSTED_KEYS", (public.hex(),))
    return private


def _signed(key, *, built: int = 200, version: str = "2026.11.17-abc1234", body: bytes = EXE, **extra):
    manifest = {
        "name": "kmill-agent", "version": version, "built": built,
        "sha256": hashlib.sha256(body).hexdigest(), "size": len(body), **extra,
    }
    raw = json.dumps(manifest).encode("utf-8")
    return raw, key.sign(raw)


def _machine(db: Session, *, token: str | None = "t", host: str = "192.168.1.10") -> Machine:
    machine = Machine(
        name="Tolik", host=host, port=8765, enabled=True,
        agent_token_encrypted=encrypt_value(token) if token else None,
        created_at=datetime(2026, 11, 1),
    )
    db.add(machine)
    db.commit()
    return machine


def _package(key, tmp_path: Path, **kw) -> au.Package:
    raw, sig = _signed(key, **kw)
    manifest = json.loads(raw)
    exe = tmp_path / "kmill-agent.exe"
    exe.write_bytes(kw.get("body", EXE))
    return au.Package(raw, sig, manifest["version"], manifest["built"], manifest["sha256"], manifest["size"], exe)


class _Github:
    """Підмінний GitHub: віддає три файли релізу й рахує звернення."""

    def __init__(self, raw: bytes, sig: bytes, exe: bytes, *, status: int = 200):
        self.files = {
            au.MANIFEST_ASSET: raw, au.SIGNATURE_ASSET: sig.hex().encode("ascii"), au.EXE_ASSET: exe,
        }
        self.status = status
        self.asked: list[str] = []

    def __call__(self, url: str, **kwargs):
        name = url.rsplit("/", 1)[-1]
        self.asked.append(name)
        body = self.files[name]
        return SimpleNamespace(
            status_code=self.status, content=body,
            iter_content=lambda size: [body[i:i + size] for i in range(0, len(body), size)],
        )


# ── ключ і підпис ─────────────────────────────────────────────────────────


def test_trusted_key_is_the_one_baked_into_the_agent():
    """Сервер і агент мусять довіряти одному ключу: розійдуться — сервер
    відмовиться слати справжнє оновлення або агент відхилить кожне."""
    go = (ROOT / "agent" / "internal" / "upd" / "upd.go").read_text(encoding="utf-8")
    block = go[go.index("var TrustedKeys"):]
    block = block[: block.index("}")]
    assert tuple(re.findall(r'"([0-9a-f]{64})"', block)) == au.TRUSTED_KEYS


def test_test_key_never_ships_in_a_normal_agent_build():
    """Тестовий ключ агента має відому приватну частину — він живе лише під
    тегом збірки e2e, і CI цей тег не ставить."""
    e2e = (ROOT / "agent" / "internal" / "upd" / "keys_e2e.go").read_text(encoding="utf-8")
    assert e2e.lstrip().startswith("//go:build e2e")
    workflow = (ROOT / ".github" / "workflows" / "agent-build.yml").read_text(encoding="utf-8")
    assert "-tags" not in workflow


def test_manifest_needs_our_signature(key):
    raw, sig = _signed(key)
    assert au.verify_manifest(raw, sig)["built"] == 200

    stranger = Ed25519PrivateKey.generate()
    with pytest.raises(au.AgentUpdateError, match="підпис"):
        au.verify_manifest(raw, stranger.sign(raw))
    with pytest.raises(au.AgentUpdateError, match="підпис"):
        au.verify_manifest(raw.replace(b'"built": 200', b'"built": 900'), sig)
    with pytest.raises(au.AgentUpdateError, match="підпис"):
        au.verify_manifest(raw, b"")


def test_signed_but_wrong_manifest_is_refused(key):
    for raw, sig in (
        _signed(key, built=0),
        _signed(key, version=None),
    ):
        with pytest.raises(au.AgentUpdateError, match="неповний"):
            au.verify_manifest(raw, sig)
    other = json.dumps({"name": "something-else", "built": 1}).encode()
    with pytest.raises(au.AgentUpdateError, match="не від агента"):
        au.verify_manifest(other, key.sign(other))


# ── файл з релізу ─────────────────────────────────────────────────────────


def test_fetch_package_downloads_once_and_reuses_the_file(key, tmp_path):
    raw, sig = _signed(key)
    github = _Github(raw, sig, EXE)

    package = au.fetch_package(github, tmp_path)

    assert (package.version, package.built, package.size) == ("2026.11.17-abc1234", 200, len(EXE))
    assert package.exe_path.read_bytes() == EXE
    assert package.exe_path.parent.name == "200"

    again = au.fetch_package(github, tmp_path)
    assert again.exe_path == package.exe_path
    # Маніфест і підпис питаються щоразу (вони і кажуть, чи є новіше), сам
    # файл — один раз.
    assert github.asked.count(au.EXE_ASSET) == 1
    assert github.asked.count(au.MANIFEST_ASSET) == 2

    # Наступна збірка витісняє попередню з кешу: агент перезбирається з кожним
    # релізом, і без цього тека росла б на файл за реліз.
    newer = EXE + b"!"
    raw3, sig3 = _signed(key, built=300, body=newer)
    third = au.fetch_package(_Github(raw3, sig3, newer), tmp_path)
    assert third.exe_path.read_bytes() == newer
    assert sorted(p.name for p in tmp_path.iterdir()) == ["300"]


def test_fetch_package_refuses_a_file_that_differs_from_the_manifest(key, tmp_path):
    raw, sig = _signed(key)
    with pytest.raises(au.AgentUpdateError, match="не збігається"):
        au.fetch_package(_Github(raw, sig, EXE[:-1] + b"!"), tmp_path)
    with pytest.raises(au.AgentUpdateError, match="довший"):
        au.fetch_package(_Github(raw, sig, EXE + b"tail"), tmp_path)
    # Ні готового файла, ні недокачаного — наступна спроба почне з нуля.
    assert not list(tmp_path.rglob("kmill-agent.exe*"))


def test_fetch_package_says_plainly_when_the_release_has_no_update(key, tmp_path):
    raw, sig = _signed(key)
    with pytest.raises(au.AgentUpdateError, match="ще немає підписаного"):
        au.fetch_package(_Github(raw, sig, EXE, status=404), tmp_path)


def test_fetch_package_survives_a_network_error(key, tmp_path):
    def down(url, **kwargs):
        raise ConnectionError("proxy")

    with pytest.raises(au.AgentUpdateError, match="GitHub"):
        au.fetch_package(down, tmp_path)


# ── оновлення одного верстата ─────────────────────────────────────────────


class _Agent:
    """Підмінний агент: відповідає на /status по черзі й запам'ятовує, що йому слали."""

    def __init__(self, *statuses, reply=(200, {"ok": True, "restart": True})):
        self.statuses = list(statuses)
        self.reply = reply
        self.sent: list[au.Package] = []

    def status(self, host, port, token):
        assert (host, port, token) == ("192.168.1.10", 8765, "t")
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def send(self, host, port, token, package):
        self.sent.append(package)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _run(db, machine, agent, package=None, *, milling=False, fetch=None):
    clock = _Clock()
    fetched = []

    def default_fetch():
        fetched.append(1)
        if package is None:
            raise AssertionError("файл оновлення не мав качатись")
        return package

    result = au.update_machine(
        db, machine.id, fetch=fetch or default_fetch, status=agent.status, send=agent.send,
        milling=lambda _db, _id: milling, sleep=clock.sleep, clock=clock,
    )
    return result, fetched


OLD = {"version": "2026.11.01-aaaaaaa", "built": 100, "self_update": True, "pid": 1}
NEW = {"version": "2026.11.17-abc1234", "built": 200, "self_update": True, "pid": 2, "updated": True}


def test_update_reaches_the_new_version_and_remembers_it(db, key, tmp_path):
    machine = _machine(db)
    agent = _Agent(OLD, None, NEW)

    result, _ = _run(db, machine, agent, _package(key, tmp_path))

    assert (result.tone, result.version) == ("ok", "2026.11.17-abc1234")
    assert "2026.11.01-aaaaaaa → 2026.11.17-abc1234" in result.message
    assert len(agent.sent) == 1
    db.refresh(machine)
    assert machine.agent_version == "2026.11.17-abc1234"
    assert machine.agent_version_at is not None


def test_no_agent_configured_or_silent_agent_sends_nothing(db, key, tmp_path):
    bare = _machine(db, token=None, host="192.168.1.9")
    agent = _Agent(OLD)
    result, fetched = _run(db, bare, agent)
    assert result.tone == "bad" and "не задано агента" in result.message

    silent = _Agent(None)
    result, fetched = _run(db, _machine(db), silent)
    assert result.tone == "bad" and "не відповідає" in result.message
    assert not fetched and not agent.sent and not silent.sent


def test_old_agent_without_self_update_gets_a_clear_answer(db):
    """Агент до самооновлення віддає лише версію — йому файл не шлемо й не
    качаємо: він його не прийме, а відповість 404."""
    agent = _Agent({"version": "0.1.0"})
    result, fetched = _run(db, _machine(db), agent)
    assert result.tone == "warn"
    assert "0.1.0" in result.message and "інсталятором" in result.message
    assert not fetched and not agent.sent


def test_milling_machine_is_left_alone(db, key, tmp_path):
    agent = _Agent(OLD)
    result, fetched = _run(db, _machine(db), agent, _package(key, tmp_path), milling=True)
    assert result.tone == "warn" and "фрезерує" in result.message
    assert not fetched and not agent.sent


def test_already_newest_sends_nothing(db, key, tmp_path):
    machine = _machine(db)
    agent = _Agent(NEW)
    result, _ = _run(db, machine, agent, _package(key, tmp_path))
    assert result.tone == "ok" and "вже найновіша" in result.message
    assert not agent.sent
    db.refresh(machine)
    assert machine.agent_version == "2026.11.17-abc1234"


def test_download_problem_is_reported_and_nothing_is_sent(db):
    agent = _Agent(OLD)

    def broken():
        raise au.AgentUpdateError("не вдалося взяти оновлення з GitHub: ConnectionError")

    result, _ = _run(db, _machine(db), agent, fetch=broken)
    assert result.tone == "bad" and "GitHub" in result.message
    assert not agent.sent


def test_agent_refusal_is_shown_in_its_own_words(db, key, tmp_path):
    machine = _machine(db)
    agent = _Agent(OLD, reply=(400, {"ok": False, "error": "підпис не збігається"}))
    result, _ = _run(db, machine, agent, _package(key, tmp_path))
    assert result.tone == "bad" and "підпис не збігається" in result.message

    agent = _Agent(OLD, reply=(409, {"ok": False, "error": "агент уже має цю або новішу версію"}))
    result, _ = _run(db, machine, agent, _package(key, tmp_path))
    assert result.tone == "ok" and "вже найновіша" in result.message


def test_link_broken_while_sending(db, key, tmp_path):
    agent = _Agent(OLD, reply=ConnectionError("reset"))
    result, _ = _run(db, _machine(db), agent, _package(key, tmp_path))
    assert result.tone == "bad" and "не дійшов" in result.message


def test_elevated_agent_takes_the_file_but_waits_for_the_next_logon(db, key, tmp_path):
    note = "нова версія запрацює після наступного входу в Windows: агент запущено з правами адміністратора"
    agent = _Agent(OLD, reply=(200, {"ok": True, "restart": False, "note": note}))
    result, _ = _run(db, _machine(db), agent, _package(key, tmp_path))
    assert result.tone == "warn" and "наступного входу" in result.message


def test_new_version_that_did_not_take_reports_the_agents_reason(db, key, tmp_path):
    """Агент відкотився сам — показуємо ЙОГО причину й версію, на якій він лишився."""
    machine = _machine(db)
    back = dict(OLD, pid=7, update_note="позначена несправною: не відповіла за 45s після запуску")
    agent = _Agent(OLD, None, back)

    result, _ = _run(db, machine, agent, _package(key, tmp_path))

    assert result.tone == "bad"
    assert "не прижилась" in result.message and "не відповіла за 45s" in result.message
    assert "2026.11.01-aaaaaaa" in result.message
    db.refresh(machine)
    assert machine.agent_version == "2026.11.01-aaaaaaa"


def test_stale_note_from_the_same_process_does_not_end_the_wait(db, key, tmp_path):
    """Процес, що приймав файл, міг нести примітку про ПОПЕРЕДНЄ невдале
    оновлення — це не відповідь про нинішнє: чекаємо далі на нову версію."""
    before = dict(OLD, update_note="позначена несправною: стара історія")
    agent = _Agent(before, before, None, NEW)
    result, _ = _run(db, _machine(db), agent, _package(key, tmp_path))
    assert result.tone == "ok"


def test_agent_silent_after_the_push(db, key, tmp_path):
    agent = _Agent(OLD, None)
    result, _ = _run(db, _machine(db), agent, _package(key, tmp_path))
    assert result.tone == "bad" and "не відповідає" in result.message and "сторож" in result.message


# ── роут і кнопка ─────────────────────────────────────────────────────────


def test_route_is_a_plain_function_for_admins_only():
    from app.routers.settings import devices

    route = devices.update_machine_agent
    # Чекає до хвилини на перезапуск агента — мусить іти в threadpool.
    assert not inspect.iscoroutinefunction(route)
    assert "_require_admin_machines(request, db)" in inspect.getsource(route)


def test_button_is_shown_to_the_admin_only(db):
    from app.routers.deps import templates

    machine = _machine(db)
    machine.agent_version = "2026.11.01-aaaaaaa"
    db.commit()
    rows = [machine]
    template = templates.env.get_template("_settings_machines.html")

    admin = template.render(machines=rows, user=SimpleNamespace(role="адмін"))
    operator = template.render(machines=rows, user=SimpleNamespace(role="оператор"))

    assert f'hx-post="/settings/machines/{machine.id}/agent-update"' in admin
    assert 'class="mc-agent-out"' in admin
    assert "agent-update" not in operator
