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


def _peek_answer(text, *, tail=None, offset=0, truncated=False, extra=None):
    def answer(params):
        payload = {"path": params["path"], "size": len(text), "mtime": "2026-11-16T03:21:41-08:00",
                   "truncated": truncated, "data": base64.b64encode(text.encode()).decode()}
        if tail is not None:
            payload.update(tail=tail, offset=offset)
        payload.update(extra or {})
        return _Resp(payload=payload)
    return answer


def test_tail_reads_the_end_drops_the_cut_line_and_greps(monkeypatch):
    """SmartControl на 150i-Olejka (16.11.26): свіже в журналі — у кінці, а
    рядок довший за 160 символів (номер фрези й рядка програми — у хвості)."""
    long_tail = "|Line:4321|Tool:7|Length:-47.123"
    text = "cut-in-the-middle\n" + "\n".join(
        f"2026-11-16 01:{m:02d}:00 | INFO | M3.cnc" + " " * 150 + long_tail for m in range(30))
    seen = {}

    def peek(params):
        seen.update(params)
        return _peek_answer(text, tail=True, offset=5000, truncated=True)(params)

    _agent(monkeypatch, {"/peek": peek})
    out = machine_files.peek(_target(), r"C:\ProgramData\imes-icore\Server.log", tail=True, limit=3,
                             grep="01:2[0-9]", width=400)
    assert seen["tail"] == "1"
    assert out["кінець_файлу"] is True and out["збігів"] == 10
    assert [line[:19] for line in out["рядки"]] == ["2026-11-16 01:27:00", "2026-11-16 01:28:00",
                                                    "2026-11-16 01:29:00"], "останні три збіги, по порядку"
    assert all(line.endswith(long_tail) for line in out["рядки"]), "хвіст рядка не обрізано"
    assert out["рядків_прочитано"] == 30, "перший, обрізаний посередині рядок відкинуто"


def test_tail_on_an_old_agent_says_it_got_the_head(monkeypatch):
    _agent(monkeypatch, {"/peek": _peek_answer("first\nsecond")})
    out = machine_files.peek(_target(), r"C:\imes\a.log", tail=True)
    assert "агент_старий" in out and out["рядки"] == ["first", "second"]


def test_bad_grep_is_explained(monkeypatch):
    _agent(monkeypatch, {"/peek": _peek_answer("x")})
    with pytest.raises(machine_files.AgentError, match="регулярний"):
        machine_files.peek(_target(), r"C:\imes\a.log", grep="(")


def test_zip_lists_entries_then_reads_one_by_number(monkeypatch):
    monkeypatch.setattr(machine_files, "_zips", {})
    entries = [{"name": "Server.Default.2026-11-14.log", "size": 900, "mtime": "2026-11-14T23:59:00Z", "text": True},
               {"name": "Server.Default.2026-11-15.log", "size": 950, "mtime": "2026-11-15T23:59:00Z", "text": True}]
    seen = []

    def peek(params):
        seen.append(dict(params))
        if "entry" not in params:
            return _Resp(payload={"path": params["path"], "size": 400, "mtime": "", "entries": entries,
                                  "entries_total": 2})
        return _peek_answer("a\nb\nToolChange T7", tail=True, extra={"entry": params["entry"]})(params)

    _agent(monkeypatch, {"/peek": peek})
    target = _target()
    with pytest.raises(machine_files.AgentError, match="спершу перелік архіву"):
        machine_files.zip_peek(target, r"C:\imes\logs.zip", 1)
    listing = machine_files.zip_entries(target, r"C:\imes\logs.zip")
    assert [f["назва"] for f in listing["файли"]] == ["Server.Default.2026-11-15.log",
                                                      "Server.Default.2026-11-14.log"], "свіжіші вгорі"
    out = machine_files.zip_peek(target, r"C:\imes\logs.zip", 1, tail=True, grep="tool")
    assert seen[-1]["entry"] == "Server.Default.2026-11-15.log" and seen[-1]["tail"] == "1"
    assert out["рядки"] == ["ToolChange T7"] and out["збігів"] == 1


def test_zip_on_an_old_agent_asks_for_a_new_agent(monkeypatch):
    _agent(monkeypatch, {"/peek": _Resp(status=403, text="не текстовий файл")})
    with pytest.raises(machine_files.AgentError, match="відмовив"):
        machine_files.zip_entries(_target(), r"C:\imes\logs.zip")
    with pytest.raises(machine_files.AgentError, match="mode=zip"):
        machine_files.peek(_target(), r"C:\imes\logs.zip")


def test_list_find_shows_every_matching_file_with_its_number(monkeypatch):
    files = [_file(r"C:\imes\logs\a.zip", 10, "2026-11-01T00:00:00Z"),
             _file(r"C:\imes\bin\x.dll", 10),
             _file(r"C:\imes\logs\b.zip", 10, "2026-11-15T00:00:00Z")]
    _agent(monkeypatch, {"/discover": _Resp(payload={"roots": [], "files": files, "dirs": 2,
                                                       "truncated": False, "elapsed_ms": 3})})
    out = machine_files.discover(_target(), now=NOW, find=".ZIP")
    assert [(f["№"], f["файл"].rsplit("\\", 1)[-1]) for f in out["знайдено"]] == [(3, "b.zip"), (1, "a.zip")]
    assert out["знайдено_усього"] == 2
    assert "знайдено" not in machine_files.discover(_target(), now=NOW)


def test_tool_passes_tail_grep_width_and_zip_through(monkeypatch, db_session):
    from app.services import machines

    monkeypatch.setattr(machine_files, "_zips", {})
    olejka = SimpleNamespace(name="150i-Olejka", enabled=True)
    monkeypatch.setattr(machines, "list_machines", lambda db: [olejka])
    monkeypatch.setattr(machines, "target_of", lambda m: _target(name=m.name))
    files = [_file(r"C:\imes\logs\Server.log", 10), _file(r"C:\imes\logs\old.zip", 10)]
    line = "x" * 300 + "END"

    def peek(params):
        if params["path"].endswith(".zip") and "entry" not in params:
            return _Resp(payload={"path": params["path"], "size": 1, "mtime": "",
                                  "entries": [{"name": "a.log", "size": 1, "mtime": "", "text": True}],
                                  "entries_total": 1})
        return _peek_answer(line, tail=True)(params)

    _agent(monkeypatch, {"/discover": _Resp(payload={"roots": [], "files": files, "dirs": 1,
                                                       "truncated": False, "elapsed_ms": 1}),
                         "/peek": peek})
    run = mcp_tools_machine_files.tool_machine_files
    assert run(db_session, {"machine": "olejka", "find": "zip"})["знайдено"][0]["№"] == 2
    out = run(db_session, {"machine": "olejka", "mode": "peek", "file": 1, "tail": True, "width": 400})
    assert out["рядки"] == [line] and out["кінець_файлу"] is True
    assert len(run(db_session, {"machine": "olejka", "mode": "peek", "file": 1})["рядки"][0]) == 160
    assert run(db_session, {"machine": "olejka", "mode": "zip", "file": 2})["файли"][0]["назва"] == "a.log"
    got = run(db_session, {"machine": "olejka", "mode": "zip", "file": 2, "entry": 1, "tail": True, "width": 1000})
    assert got["рядки"] == [line]
