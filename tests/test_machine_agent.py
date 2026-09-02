"""HTTP-агент верстата: CRM тягне кадр по /capture з токеном (не VNC)."""
from __future__ import annotations

import io

from PIL import Image

from app.services import machines as ms


class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _png_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


def test_capture_http_returns_image(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp(_png_bytes())

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    img = ms._capture_http("192.168.1.85", 8765, "tok123")
    assert isinstance(img, Image.Image)
    assert captured["url"] == "http://192.168.1.85:8765/capture"
    assert captured["headers"]["X-Agent-Token"] == "tok123"


def test_capture_http_403_explains(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(b"", 403))
    try:
        ms._capture_http("h", 8765, "bad")
        assert False, "мало кинути виняток на 403"
    except RuntimeError as exc:
        assert "токен" in str(exc).lower()


def test_target_is_agent_flag():
    vnc = ms.MachineTarget(name="a", host="h", port=5900)
    agent = ms.MachineTarget(name="b", host="h", port=8765, agent_token="t")
    assert not vnc.is_agent
    assert agent.is_agent


def test_poll_all_uses_http_for_agent_targets(monkeypatch):
    """Сторож бойового багу 02.09.26: poll_all має ВЛАСНУ grab(), і в ній
    бракувало розвилки транспорту — воркер (він ходить саме через poll_all)
    опитував агентний верстат по VNC і давав «not a VNC server», попри
    правильний токен. Обидві гілки мусять дивитись на target.is_agent."""
    import requests

    calls = {"http": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["http"] += 1
        return _Resp(_png_bytes())

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ms, "capture", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("poll_all пішов у VNC замість HTTP-агента")))
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)
    monkeypatch.setattr(ms, "get_machine_vnc_password", lambda db: None)
    target = ms.MachineTarget(name="350i", host="192.168.1.85", port=8765, agent_token="tok")
    monkeypatch.setattr(ms, "configured_targets", lambda db: [target])

    states = ms.poll_all(None)
    assert calls["http"] == 1
    assert states and states[0].error is None


def test_poll_target_uses_http_when_token(monkeypatch):
    import requests

    calls = {"http": 0, "vnc": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["http"] += 1
        return _Resp(_png_bytes())

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ms, "capture", lambda *a, **k: (_ for _ in ()).throw(AssertionError("VNC не має викликатись")))
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)

    target = ms.MachineTarget(name="350i", host="192.168.1.85", port=8765, agent_token="tok")
    state = ms.poll_target(None, target, password=None)
    assert calls["http"] == 1
    assert state.error is None
