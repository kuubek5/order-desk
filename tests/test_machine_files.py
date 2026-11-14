"""Розвідка файлів верстата через агента (`machine_files`, `kmill_machine_files`)."""

import base64
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import machine_files, mcp_tools_machine_files
from app.services.mcp_common import ToolError

NOW = datetime(2026, 11, 14, 12, 0, 0)


def _target(**extra):
    fields = dict(name="350i-Boris", host="10.0.0.5", port=8765, agent_token="t",
                  is_agent=True, machine_id=7)
    fields.update(extra)
    return SimpleNamespace(**fields)


def _file(path, size, mtime="2026-11-14T11:30:00+02:00"):
    return {"path": path, "size": size, "mtime": mtime}


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _fresh_memory(monkeypatch):
    monkeypatch.setattr(machine_files, "_last", {})


def _agent(monkeypatch, answers):
    """`answers`: шлях агента → _Resp (або функція від params)."""
    import requests

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {}), dict(headers or {})))
        answer = answers[url.rsplit(":8765", 1)[1]]
        return answer(params) if callable(answer) else answer

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_candidates_rank_tool_files_first_and_mask_names():
    files = [
        _file(r"C:\RemiCORE\Photos\pic.png", 900),
        _file(r"C:\RemiCORE\cfg\ToolProperties.INI", 2048),
        _file(r"C:\RemiCORE\cfg\Іваненко_job.ini", 10, "2026-10-01T10:00:00+02:00"),
        _file(r"C:\RemiCORE\Spindle.LOG", 5000),
    ]
    report = machine_files.summarize(files, None, now=NOW)
    names = [c["файл"] for c in report["кандидати"]]
    assert {n.rsplit("\\", 1)[-1] for n in names[:2]} == {"ToolProperties.INI", "Spindle.LOG"}
    assert {c["№"] for c in report["кандидати"][:2]} == {2, 4}
    assert not any("pic.png" in n for n in names), "картинка не кандидат"
    assert not any("Іваненко" in str(report) for _ in [0]), "кирилиця в назвах затерта"
    assert report["за_розширенням"][".ini"] == 2
    assert "змінилось_з_минулого_переліку" not in report


def test_second_listing_shows_what_changed(monkeypatch):
    first = [_file(r"C:\RemiCORE\Toolstat.INI", 100, "2026-11-14T10:00:00+02:00"),
             _file(r"C:\RemiCORE\Same.ini", 5, "2026-11-01T10:00:00+02:00")]
    second = [_file(r"C:\RemiCORE\Toolstat.INI", 120, "2026-11-14T11:50:00+02:00"),
              _file(r"C:\RemiCORE\Same.ini", 5, "2026-11-01T10:00:00+02:00"),
              _file(r"C:\RemiCORE\New.log", 1)]
    listings = iter([first, second])
    calls = _agent(monkeypatch, {"/discover": lambda params: _Resp(payload={
        "roots": [r"C:\RemiCORE"], "files": next(listings), "dirs": 1, "truncated": False, "elapsed_ms": 12})})
    one = machine_files.discover(_target(), now=NOW)
    assert one["минулий_перелік"] is None and "змінилось_усього" not in one
    two = machine_files.discover(_target(), now=NOW)
    changed = {c["файл"].rsplit("\\", 1)[-1]: c for c in two["змінилось_з_минулого_переліку"]}
    assert set(changed) == {"Toolstat.INI", "New.log"} and two["змінилось_усього"] == 2
    assert changed["Toolstat.INI"]["було"]["розмір"] == 100 and changed["New.log"]["було"] == "не було"
    assert calls[0][2] == {"X-Agent-Token": "t"}


def test_peek_by_number_masks_text_and_collects_numeric_keys(monkeypatch):
    text = "[Tool1]\r\nActRunTime=125.5\r\nMaxRunTime = 600\r\nName=Фреза кульова\r\n"
    seen = {}

    def peek(params):
        seen.update(params)
        return _Resp(payload={"path": params["path"], "size": len(text), "mtime": "2026-11-14T11:00:00+02:00",
                              "truncated": False,
                              "data": base64.b64encode(text.encode("cp1251")).decode()})

    _agent(monkeypatch, {
        "/discover": _Resp(payload={"roots": [], "files": [_file(r"C:\RemiCORE\ToolProperties.INI", 60)],
                                    "dirs": 1, "truncated": False, "elapsed_ms": 1}),
        "/peek": peek,
    })
    target = _target()
    machine_files.discover(target, now=NOW)
    out = machine_files.peek(target, machine_files.path_by_number(target, 1))
    assert seen["path"] == r"C:\RemiCORE\ToolProperties.INI"
    assert out["числові_ключі"] == ["ActRunTime = 125.5", "MaxRunTime = 600"]
    assert out["рядки"][0] == "[Tool1]" and "Фреза" not in str(out) and "•" in out["рядки"][3]


def test_peek_needs_a_listing_first_and_a_real_number():
    with pytest.raises(machine_files.AgentError, match="спершу перелік"):
        machine_files.path_by_number(_target(), 1)


def test_binary_file_is_not_dumped(monkeypatch):
    _agent(monkeypatch, {"/peek": _Resp(payload={
        "path": "x", "size": 9, "mtime": "", "truncated": False,
        "data": base64.b64encode(b"\x01\x00\x02\x00binary").decode()})})
    out = machine_files.peek(_target(), r"C:\RemiCORE\tool.dat")
    assert out.get("двійковий") is True and "рядки" not in out


@pytest.mark.parametrize("status, words", [
    (404, "агент старий"), (429, "уже обходить"), (503, "призупинено"), (403, "відмовив"),
])
def test_agent_refusals_say_what_to_do(monkeypatch, status, words):
    _agent(monkeypatch, {"/discover": _Resp(status=status, text="поза теками")})
    with pytest.raises(machine_files.AgentError, match=words):
        machine_files.discover(_target())


def test_vnc_machine_and_network_error_are_explained(monkeypatch):
    import requests

    with pytest.raises(machine_files.AgentError, match="немає агента"):
        machine_files.discover(SimpleNamespace(is_agent=False))

    def boom(*a, **k):
        raise requests.ConnectionError("x")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(machine_files.AgentError, match="не відповів"):
        machine_files.discover(_target())


def test_tool_lists_machines_without_touching_the_network(monkeypatch, db_session):
    import requests

    from app.services import machines

    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("мережі бути не мусить"))
    boris = SimpleNamespace(name="350i-Boris", enabled=True)
    titan = SimpleNamespace(name="350i_Titan", enabled=False)
    monkeypatch.setattr(machines, "list_machines", lambda db: [boris, titan])
    monkeypatch.setattr(machines, "target_of", lambda m: _target(name=m.name) if m is boris
                        else SimpleNamespace(name=m.name, is_agent=False))
    out = mcp_tools_machine_files.tool_machine_files(db_session, {})
    assert [(m["верстат"], m["агент"]) for m in out["верстати"]] == [("350i-Boris", True), ("350i_Titan", False)]
    with pytest.raises(ToolError, match="рівно один верстат"):
        mcp_tools_machine_files.tool_machine_files(db_session, {"machine": "350i"})
    with pytest.raises(ToolError, match="350i_Titan: у верстата немає агента"):
        mcp_tools_machine_files.tool_machine_files(db_session, {"machine": "titan"})
    with pytest.raises(ToolError, match="потрібен `file`"):
        mcp_tools_machine_files.tool_machine_files(db_session, {"machine": "boris", "mode": "peek"})
