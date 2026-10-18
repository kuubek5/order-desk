"""«Скачати за посиланням» на листі з багатьма посиланнями (власник 18.10.26:
«кнопка скачати все не завжди скачує все»).

Проходимо справжній роут `/mail/{id}/fetch-link` для КОЖНОГО посилання листа —
так, як його шле черга браузера, — зі справжнім `download_link`; підставна лише
мережа (сесія requests) і паузи між повторами. Жодного звернення в мережу.
"""

import json
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.settings_store as settings_store_mod
import app.sheets as sheets_mod
import app.web as web
from app import link_attachments as la
from app.db import Base
from app.link_attachments import undownloaded_links
from app.models import Attachment, EmailMessage, User
from app.routers import mail as mail_router_mod
from app.services.mail_accept import accept_blocker

IDS = [f"1Drive{n:02d}xxxxxxxxxxxxxxxxxxxxxx" for n in range(6)]


class _Resp:
    def __init__(self, url, *, status=200, headers=None, text="", chunks=None):
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self.cookies = {}
        self.text = text
        self._chunks = chunks or [b""]
        self.is_redirect = False

    def iter_content(self, _size):
        yield from self._chunks

    def close(self):
        pass


class _Drive:
    """Підставний Google Drive: для кожного id — сценарій відповідей по черзі
    («503», «reset», «private», «ok»); вичерпаний сценарій = «ok»."""

    def __init__(self, scripts):
        self.scripts = {fid: list(steps) for fid, steps in scripts.items()}
        self.calls: list[str] = []
        self.cookies = {}

    def __call__(self):  # new_legacy_session() → ця сама сесія
        return self

    def get(self, url, params=None, stream=False, timeout=None, allow_redirects=None):
        fid = (params or {}).get("id")
        self.calls.append(fid)
        steps = self.scripts.get(fid) or []
        step = steps.pop(0) if steps else "ok"
        if step == "503":
            return _Resp(url, status=503)
        if step == "reset":
            raise requests.exceptions.ConnectionError("connection reset")
        if step == "private":
            return _Resp(url, headers={"content-type": "text/html"},
                         text="<html>You need access</html>")
        return _Resp(
            url,
            headers={"content-type": "application/octet-stream",
                     "content-disposition": f'attachment; filename="{fid}.stl"'},
            chunks=[b"solid ", fid.encode()],
        )


@pytest.fixture
def mail_env(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings_store_mod, "MAIL_ATTACHMENTS_PATH", str(tmp_path / "mail"))
    monkeypatch.setattr(la, "_RETRY_DELAYS", (0.0, 0.0))
    rows = []

    def _fake_tr(request, template, context):
        rows.append(context)
        return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(web.templates, "TemplateResponse", _fake_tr)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="operator", password_hash="unused", full_name="Operator")
        email = EmailMessage(
            uid="501", uid_validity="1700", status="нове", attachments_status="ready",
            body_text="\n".join(f"<https://drive.google.com/file/d/{fid}/view>" for fid in IDS),
        )
        db.add_all([user, email])
        db.commit()
        yield SimpleNamespace(db=db, user=user, email=email, rows=rows,
                              spool=tmp_path / "mail" / "1700_501")


def _download_all(env, monkeypatch, drive):
    monkeypatch.setattr(sheets_mod, "new_legacy_session", drive)
    request = SimpleNamespace(session={"user_id": env.user.id},
                              client=SimpleNamespace(host="127.0.0.1"))
    statuses = {}
    for link in undownloaded_links(env.email) or la.extract_download_links(env.email.body_text):
        ref = link.file_id or link.url
        mail_router_mod.fetch_email_link(request=request, email_id=env.email.id, ref=ref, db=env.db)
        statuses[ref] = env.rows[-1]["link_status"]
    return statuses


def test_every_link_arrives_despite_transient_failures(mail_env, monkeypatch):
    drive = _Drive({IDS[0]: ["503"], IDS[2]: ["reset", "503"], IDS[4]: ["reset"]})
    statuses = _download_all(mail_env, monkeypatch, drive)

    assert set(statuses.values()) == {"done"}
    assert sorted(p.name for p in mail_env.spool.iterdir()) == sorted(f"{fid}.stl" for fid in IDS)
    db = mail_env.db
    assert db.query(Attachment).count() == len(IDS)
    db.refresh(mail_env.email)
    assert undownloaded_links(mail_env.email) == []
    assert accept_blocker(mail_env.email) is None


def test_permanent_failure_isolated_status_shown_and_gate_holds(mail_env, monkeypatch):
    drive = _Drive({IDS[1]: ["private", "private", "private"], IDS[3]: ["503"]})
    statuses = _download_all(mail_env, monkeypatch, drive)

    assert statuses[IDS[1]] == "error"
    failed_row = next(r for r in mail_env.rows if r["link_status"] == "error")
    assert "закритий" in failed_row["link_message"]
    # «Закритий» не повторюємо — результат той самий.
    assert drive.calls.count(IDS[1]) == 1
    # Решта п'ять скачались — одна відмова не зупиняє інших.
    assert [s for ref, s in statuses.items() if ref != IDS[1]] == ["done"] * 5
    db = mail_env.db
    db.refresh(mail_env.email)
    left = undownloaded_links(mail_env.email)
    assert [lnk.file_id for lnk in left] == [IDS[1]]
    blocker = accept_blocker(mail_env.email)
    assert blocker and "1 файл(ів) за посиланням не скачано" in blocker


def test_rerun_after_partial_failure_adds_no_duplicates(mail_env, monkeypatch):
    first = _Drive({IDS[5]: ["503", "503", "503"]})
    _download_all(mail_env, monkeypatch, first)
    db = mail_env.db
    db.refresh(mail_env.email)
    assert [lnk.file_id for lnk in undownloaded_links(mail_env.email)] == [IDS[5]]

    # Повторне «Скачати за посиланням» качає лише нескачане…
    second = _Drive({})
    _download_all(mail_env, monkeypatch, second)
    assert second.calls == [IDS[5]]
    # …а повторний клік по вже скачаному рядку — «такий файл уже є», без «(2)».
    request = SimpleNamespace(session={"user_id": mail_env.user.id},
                              client=SimpleNamespace(host="127.0.0.1"))
    mail_router_mod.fetch_email_link(request=request, email_id=mail_env.email.id, ref=IDS[0], db=db)
    assert mail_env.rows[-1]["link_status"] == "skip"

    names = sorted(p.name for p in mail_env.spool.iterdir())
    assert names == sorted(f"{fid}.stl" for fid in IDS)
    assert db.query(Attachment).count() == len(IDS)
    db.refresh(mail_env.email)
    assert sorted(json.loads(mail_env.email.handled_link_refs)) == sorted(IDS)
