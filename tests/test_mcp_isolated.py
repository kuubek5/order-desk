"""Важкі інструменти MCP — окремим процесом (`app/mcp_isolated.py`, 18.11.26).

18.11.26 два виклики `kmill_sum3d_files kind=cam` у процесі CRM поклали прод
на дві хвилини. Тут стережемо: хто йде окремо, що процес убивається за межею
часу, що другий важкий запит отримує «зайнято», і що відповідь (зокрема
`_image`) доїжджає цілою."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from app import mcp_isolated
from app.services.mcp_common import Tool, ToolError
from app.services.mcp_tools import TOOLS_BY_NAME


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(mcp_isolated, "INLINE", False)


def _python(code: str):
    """Підміна дочірнього процесу: короткий python, що отримує шляхи запиту й відповіді."""

    def command(request: Path, result: Path):
        return [sys.executable, "-c", code, str(request), str(result)], None

    return command


def _tool(name: str = "kmill_fake") -> Tool:
    return Tool(name=name, description="", schema={}, run=lambda db, args: {}, isolate=lambda args: True)


# ── Хто йде окремо ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("kmill_sum3d_files", {}, True),  # типово kind=both — читає й .cam
        ("kmill_sum3d_files", {"kind": "cam"}, True),
        ("kmill_sum3d_files", {"kind": "cam_deep", "sum3d_id": "13-12-29"}, True),
        ("kmill_sum3d_files", {"kind": "cam_compare"}, True),
        # Читають верстати з памʼяті процесу — в окремому їх немає.
        ("kmill_sum3d_files", {"kind": "board_disc"}, False),
        ("kmill_sum3d_files", {"kind": "disc_link"}, False),
        ("kmill_sum3d_files", {"kind": "iso_link"}, False),
        ("kmill_sum3d_files", {"kind": "blk_shape"}, False),
        ("kmill_disc_fit_replay", {}, True),
        ("kmill_stl_shapes", {"kind": "міст"}, True),
        ("kmill_stl_shapes", {}, False),
        ("kmill_mail_learn", {"days": 7}, True),
        # Нумерація «№» з what=files живе в памʼяті процесу — лишається вдома.
        ("kmill_sum3d_explore", {"what": "files"}, False),
        ("kmill_health", {}, False),
    ],
)
def test_which_tools_run_in_their_own_process(isolated, tool, args, expected):
    assert mcp_isolated.should_isolate(TOOLS_BY_NAME[tool], args) is expected


def test_tests_run_tools_inline_by_default():
    # Автофікстура conftest: дочірній процес не бачить бази в памʼяті.
    assert mcp_isolated.should_isolate(TOOLS_BY_NAME["kmill_disc_fit_replay"], {}) is False


# ── Процес ──────────────────────────────────────────────────────────────────

_ANSWER = (
    "import json, sys; "
    "req = json.load(open(sys.argv[1], encoding='utf-8')); "
    "json.dump({'ok': True, 'result': {'інструмент': req['tool'], 'args': req['args'], "
    "'_image': {'data': 'QUJD', 'mimeType': 'image/png'}}}, "
    "open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)"
)


def test_answer_with_image_comes_back_whole(isolated):
    got = mcp_isolated.run(_tool(), {"kind": "cam", "limit": 2}, command=_python(_ANSWER))
    assert got == {
        "інструмент": "kmill_fake",
        "args": {"kind": "cam", "limit": 2},
        "_image": {"data": "QUJD", "mimeType": "image/png"},
    }


def test_slow_tool_is_killed_at_the_deadline(isolated):
    started = time.monotonic()
    with pytest.raises(ToolError, match="не вклався"):
        mcp_isolated.run(_tool(), {}, timeout=1, command=_python("import time; time.sleep(60)"))
    assert time.monotonic() - started < 15
    # Після вбитого процесу замок вільний — наступний запит іде.
    assert mcp_isolated.run(_tool(), {}, command=_python(_ANSWER))["інструмент"] == "kmill_fake"


def test_second_heavy_request_is_refused_while_one_runs(isolated):
    errors: list[str] = []

    def slow():
        try:
            mcp_isolated.run(_tool(), {}, timeout=20, command=_python("import time; time.sleep(3)"))
        except ToolError as exc:  # пустий процес без відповіді — теж ToolError
            errors.append(str(exc))

    first = threading.Thread(target=slow)
    first.start()
    try:
        deadline = time.monotonic() + 5
        while not mcp_isolated._busy.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        with pytest.raises(ToolError, match="уже йде інший важкий запит"):
            mcp_isolated.run(_tool("kmill_other"), {}, command=_python(_ANSWER))
        assert time.monotonic() - started < 1  # відмова одразу, не очікування
    finally:
        first.join()


def test_process_without_answer_is_a_tool_error(isolated):
    with pytest.raises(ToolError, match="без відповіді"):
        mcp_isolated.run(_tool(), {}, command=_python("import sys; sys.exit(3)"))


def test_tool_error_inside_the_process_keeps_its_text(isolated):
    code = (
        "import json, sys; json.dump({'ok': False, 'tool_error': 'потрібен sum3d_id'}, "
        "open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)"
    )
    with pytest.raises(ToolError, match="потрібен sum3d_id"):
        mcp_isolated.run(_tool(), {}, command=_python(code))


# ── Тіло дочірнього процесу ─────────────────────────────────────────────────


def test_child_runs_the_named_tool_with_its_own_session(tmp_path, db_engine, monkeypatch):
    from sqlalchemy.orm import Session

    from app.services import mcp_tools

    seen: dict = {}

    def run(db, args):
        seen["db"] = db
        return {"ok_args": args}

    monkeypatch.setitem(mcp_tools.TOOLS_BY_NAME, "kmill_fake", Tool("kmill_fake", "", {}, run))
    request, result = tmp_path / "request.json", tmp_path / "result.json"
    request.write_text(json.dumps({"tool": "kmill_fake", "args": {"limit": 1}}), encoding="utf-8")

    code = mcp_isolated.run_child(str(request), str(result), session_factory=lambda: Session(db_engine))

    assert code == 0
    assert json.loads(result.read_text(encoding="utf-8")) == {"ok": True, "result": {"ok_args": {"limit": 1}}}
    assert isinstance(seen["db"], Session)


def test_child_crash_still_writes_an_answer(tmp_path, db_engine, monkeypatch):
    from sqlalchemy.orm import Session

    from app.services import mcp_tools

    def run(db, args):
        raise RuntimeError("бум")

    monkeypatch.setitem(mcp_tools.TOOLS_BY_NAME, "kmill_fake", Tool("kmill_fake", "", {}, run))
    request, result = tmp_path / "request.json", tmp_path / "result.json"
    request.write_text(json.dumps({"tool": "kmill_fake", "args": {}}), encoding="utf-8")

    assert mcp_isolated.run_child(str(request), str(result), session_factory=lambda: Session(db_engine)) == 1
    assert json.loads(result.read_text(encoding="utf-8"))["crash"] == "RuntimeError: бум"


def test_rpc_routes_heavy_tool_through_the_isolated_runner(isolated, monkeypatch, db_session):
    from app.routers.mcp import handle_rpc

    calls: list = []
    monkeypatch.setattr(mcp_isolated, "run", lambda tool, args: calls.append((tool.name, args)) or {"x": 1})
    response = handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "kmill_sum3d_files", "arguments": {"kind": "cam", "limit": 2}}},
        db_session,
    )
    assert calls == [("kmill_sum3d_files", {"kind": "cam", "limit": 2})]
    assert json.loads(response.body)["result"]["structuredContent"] == {"x": 1}
