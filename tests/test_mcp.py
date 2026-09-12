"""MCP по HTTP (`POST /mcp`) — протокол, гейт і інструменти.

Що ламається тихо саме тут:

* **Рукостискання.** Клієнт (Claude Code) відвалюється без жодного сліду в
  CRM, якщо `initialize` віддав не ту форму. Тест тримає форму.
* **Гейт.** Роут працює без сесії оператора свідомо, тож єдина межа —
  loopback. Якщо вона поїде, зріз черги стане доступний по мережі.
* **Помилка інструмента ≠ помилка протоколу.** Невдалий аргумент мусить
  приїхати результатом із `isError`, інакше той, хто питає, бачить збій
  транспорту й не знає, що саме виправити.
* **День — робочий.** `kmill_queue` без аргументів мусить дивитись на вкладку
  (`business_tab_today`), а не на календарну дату: у вихідні це пʼятниця.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models import Order, SyncLog
from app.services import mcp_tools
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура


def _call(client: MiniClient, method: str, params: dict | None = None, rid=1):
    status, _, body = client.post_json(
        "/mcp",
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    return status, body


def _json(body: str) -> dict:
    import json

    return json.loads(body)


def _tool(client: MiniClient, name: str, args: dict | None = None) -> dict:
    status, body = _call(client, "tools/call", {"name": name, "arguments": args or {}})
    assert status == 200, body
    return _json(body)


# ── Протокол ────────────────────────────────────────────────────────────────


def test_initialize_returns_server_info(app_db):  # noqa: F811
    app, _ = app_db
    status, body = _call(MiniClient(app), "initialize", {"protocolVersion": "2025-06-18"})
    assert status == 200, body
    result = _json(body)["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "kuubmill"


def test_unknown_protocol_version_falls_back_to_ours(app_db):  # noqa: F811
    app, _ = app_db
    _, body = _call(MiniClient(app), "initialize", {"protocolVersion": "1999-01-01"})
    assert _json(body)["result"]["protocolVersion"] == mcp_tools_protocol()


def mcp_tools_protocol() -> str:
    from app.routers.mcp import PROTOCOL_VERSION

    return PROTOCOL_VERSION


def test_notification_gets_202_and_no_body(app_db):  # noqa: F811
    app, _ = app_db
    status, _, body = MiniClient(app).post_json(
        "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert status == 202
    assert body.strip() == ""


def test_tools_list_shape(app_db):  # noqa: F811
    app, _ = app_db
    _, body = _call(MiniClient(app), "tools/list")
    tools = _json(body)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == set(mcp_tools.TOOLS_BY_NAME)
    for tool in tools:
        assert tool["description"].strip(), tool["name"]
        assert tool["inputSchema"]["type"] == "object"
        # Анотації — обіцянка клієнту, що виклик нічого не змінить.
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False


def test_unknown_method_is_jsonrpc_error(app_db):  # noqa: F811
    app, _ = app_db
    _, body = _call(MiniClient(app), "resources/list")
    assert _json(body)["error"]["code"] == -32601


def test_batch_request_refused(app_db):  # noqa: F811
    app, _ = app_db
    status, _, body = MiniClient(app).post_json("/mcp", [{"jsonrpc": "2.0", "method": "ping"}])
    assert status == 200
    assert _json(body)["error"]["code"] == -32600


def test_remote_client_is_refused(app_db, monkeypatch):  # noqa: F811
    """Гейт — loopback. Сесії тут немає, тож це єдина межа.

    Підміняємо саме той символ, який читає роут: після переносу коду
    `monkeypatch.setattr` у старому місці став би тихим no-op (§14).
    """
    app, _ = app_db
    import app.routers.mcp as mcp_router

    monkeypatch.setattr(mcp_router, "is_loopback_request", lambda request: False)
    status, body = _call(MiniClient(app), "tools/list")
    assert status == 403
    assert "127.0.0.1" in _json(body)["error"]["message"]


# ── Інструменти ─────────────────────────────────────────────────────────────


def _seed(session_factory, tab: str, rows: list[dict]):
    with session_factory() as db:
        for index, row in enumerate(rows, start=7):
            db.add(Order(source="sheet_client", sheet_tab=tab, row_number=index, **row))
        db.commit()


def test_queue_counts_by_readiness_and_units(app_db):  # noqa: F811
    app, session_factory = app_db
    tab = mcp_tools.business_tab_today().strftime("%d.%m.%y")
    _seed(
        session_factory,
        tab,
        [
            # технік не здав → «не готово»
            dict(work_order_no="1", quantity="3", material_color="mono a2"),
            # здав, не прораховано → «можна брати»
            dict(work_order_no="2", quantity="2", job_code="2026-07-21_00016"),
            # оператор узяв → «в роботі»
            dict(work_order_no="3", quantity="4", job_code="23203", sum3d_id="12-01-45"),
        ],
    )
    data = _tool(MiniClient(app), "kmill_queue")["result"]["structuredContent"]
    assert data["вкладка"] == tab
    assert data["усього"] == 3
    assert data["по_готовності"] == {"в роботі": 1, "можна брати": 1, "не готово": 1}
    assert data["одиниць"] == 9
    assert [r["наряд"] for r in data["роботи"]] == ["1", "2", "3"]


def test_queue_ignores_archived_and_other_days(app_db):  # noqa: F811
    app, session_factory = app_db
    today = mcp_tools.business_tab_today()
    other = (today - timedelta(days=3)).strftime("%d.%m.%y")
    _seed(session_factory, today.strftime("%d.%m.%y"), [dict(work_order_no="тут", quantity="1")])
    _seed(session_factory, other, [dict(work_order_no="інший день", quantity="1")])
    with session_factory() as db:
        db.add(
            Order(
                source="sheet_client",
                sheet_tab=today.strftime("%d.%m.%y"),
                work_order_no="в архіві",
                archived_at=datetime(2026, 9, 1, 10, 0),
            )
        )
        db.commit()
    data = _tool(MiniClient(app), "kmill_queue")["result"]["structuredContent"]
    assert [r["наряд"] for r in data["роботи"]] == ["тут"]


def test_queue_finds_tab_with_leading_space(app_db):  # noqa: F811
    """Вкладка `" 11.09.26"` з пробілом уже забирала цілий день у невидимість
    (§14 «Синк таблиці»). Порівняння рядків повторило б ту саму помилку."""
    app, session_factory = app_db
    tab = " " + mcp_tools.business_tab_today().strftime("%d.%m.%y")
    _seed(session_factory, tab, [dict(work_order_no="з пробілом", quantity="1")])
    data = _tool(MiniClient(app), "kmill_queue")["result"]["structuredContent"]
    assert [r["наряд"] for r in data["роботи"]] == ["з пробілом"]


def test_queue_bad_day_is_tool_error_not_transport_error(app_db):  # noqa: F811
    app, _ = app_db
    payload = _tool(MiniClient(app), "kmill_queue", {"day": "позавчора"})
    result = payload["result"]
    assert result["isError"] is True
    # Текст мусить казати, ЩО саме прийнятне — інакше виклик не виправити.
    assert "today" in result["content"][0]["text"]
    assert "error" not in payload


def test_queue_limit_out_of_range_is_named(app_db):  # noqa: F811
    app, _ = app_db
    result = _tool(MiniClient(app), "kmill_queue", {"limit": 9999})["result"]
    assert result["isError"] is True
    assert "limit" in result["content"][0]["text"]


def test_order_passport_by_sum3d_tail(app_db):  # noqa: F811
    app, session_factory = app_db
    _seed(
        session_factory,
        mcp_tools.business_tab_today().strftime("%d.%m.%y"),
        [dict(work_order_no="24122", sum3d_id="2026-09-11_12-01-45", client_name="Лагус")],
    )
    data = _tool(MiniClient(app), "kmill_order", {"query": "12-01-45"})["result"][
        "structuredContent"
    ]
    assert data["знайдено"] == 1
    assert data["роботи"][0]["наряд"] == "24122"
    assert data["роботи"][0]["клієнт"] == "Лагус"


def test_order_not_found_says_what_to_try(app_db):  # noqa: F811
    app, _ = app_db
    result = _tool(MiniClient(app), "kmill_order", {"query": "нічого"})["result"]
    assert result["isError"] is True
    assert "Sum3D" in result["content"][0]["text"]


def test_unknown_tool_lists_available(app_db):  # noqa: F811
    app, _ = app_db
    _, body = _call(MiniClient(app), "tools/call", {"name": "kmill_nope", "arguments": {}})
    error = _json(body)["error"]
    assert error["code"] == -32602
    assert "kmill_queue" in error["message"]


def test_sync_journal_shows_kyiv_time(app_db):  # noqa: F811
    """`SyncLog.occurred_at` у базі UTC. Якщо віддати як є, кожен рядок
    журналу бреше на три години (§14 «Синк таблиці»)."""
    app, session_factory = app_db
    stamped = datetime(2026, 9, 11, 6, 0, 0)
    with session_factory() as db:
        db.add(
            SyncLog(
                direction="sheet→crm",
                sheet_tab="11.09.26",
                status="ok",
                message="прочитано 92 рядки",
                occurred_at=stamped,
            )
        )
        db.commit()
    data = _tool(MiniClient(app), "kmill_sync_journal", {"limit": 5})["result"][
        "structuredContent"
    ]
    entry = data["записи"][0]
    assert entry["текст"] == "прочитано 92 рядки"
    expected = mcp_tools.utc_to_business(stamped).strftime("%Y-%m-%d %H:%M:%S")
    assert entry["коли"] == expected
    assert entry["коли"] != stamped.strftime("%Y-%m-%d %H:%M:%S")


def test_health_reports_version_and_tab_day(app_db):  # noqa: F811
    app, _ = app_db
    data = _tool(MiniClient(app), "kmill_health")["result"]["structuredContent"]
    from app.__version__ import VERSION

    assert data["версія"] == VERSION
    assert data["день"]["вкладка_сьогодні"] == mcp_tools.business_tab_today().isoformat()
    # Секрети тут не з'являються ні в якому вигляді.
    assert "pass" not in str(data).lower()


# ── Лог: квота й пошук ──────────────────────────────────────────────────────


@pytest.fixture
def fake_log(tmp_path, monkeypatch):
    """Підміняємо саме файл лога, а не функцію розбору: інакше тест доводив би
    лише сам себе (`green-test-dead-feature`)."""
    log = tmp_path / "logs" / "kuubmill.log"
    log.parent.mkdir(parents=True)
    monkeypatch.setattr(mcp_tools, "_log_candidates", lambda: {"поточний": log})
    return log


def _log_line(minute: int, text: str, day: date | None = None) -> str:
    moment = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=minute)
    return f"{moment:%Y-%m-%d %H:%M:%S},000 INFO app.sheets: {text}"


def test_quota_reads_counter_lines(app_db, fake_log):  # noqa: F811
    app, _ = app_db
    fake_log.write_text(
        "\n".join(
            [
                _log_line(30, "Sheets API: 12 запитів за останні 60 с"),
                _log_line(20, "Sheets API: 48 запитів за останні 60 с"),
                _log_line(10, "Гарячий тік пропущено: 47 запитів до Sheets за останню хвилину"),
                "рядок без часу — не має валити розбір",
            ]
        ),
        encoding="utf-8",
    )
    data = _tool(MiniClient(app), "kmill_quota", {"hours": 2})["result"]["structuredContent"]
    assert data["проб"] == 2
    assert data["максимум"] == 48
    assert data["хвилин_від_45"] == 1
    assert data["пропущених_тіків"] == 1
    assert "заважає" in data["висновок"]


def test_quota_window_cuts_old_lines(app_db, fake_log):  # noqa: F811
    app, _ = app_db
    fake_log.write_text(
        "\n".join(
            [
                _log_line(60 * 5, "Sheets API: 59 запитів за останні 60 с"),
                _log_line(5, "Sheets API: 7 запитів за останні 60 с"),
            ]
        ),
        encoding="utf-8",
    )
    data = _tool(MiniClient(app), "kmill_quota", {"hours": 1})["result"]["structuredContent"]
    assert data["проб"] == 1
    assert data["максимум"] == 7


def test_quota_names_the_file_it_read(app_db, fake_log):  # noqa: F811
    """Інструмент мусить казати, ЯКИЙ файл читав.

    Інакше тиша «0 проб» виглядає як «у цеху спокійно», хоч насправді дивились
    не в той файл — та сама пастка, що з порожнім числом на печі."""
    app, _ = app_db
    fake_log.write_text(_log_line(5, "Sheets API: 11 запитів за останні 60 с"), encoding="utf-8")
    data = _tool(MiniClient(app), "kmill_quota")["result"]["structuredContent"]
    assert data["лог"] == "поточний"
    assert data["рядків_прочитано"] == 1
    assert data["максимум"] == 11


def test_quota_without_any_log_refuses_instead_of_lying(app_db, tmp_path, monkeypatch):  # noqa: F811
    """Файла немає → відмова з поясненням, а не «квота не заважає».

    Саме це й вилізло на dev: там лежить лише access-лог uvicorn, без часу й без
    рядків лічильника, тож «0 проб» означало б «все спокійно» на порожньому місці."""
    app, _ = app_db
    monkeypatch.setattr(
        mcp_tools, "_log_candidates", lambda: {"поточний": tmp_path / "немає.log"}
    )
    result = _tool(MiniClient(app), "kmill_quota")["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "ВСТАНОВЛЕНУ" in text and "dev" in text


def test_quota_unknown_source_lists_options(app_db, fake_log):  # noqa: F811
    app, _ = app_db
    result = _tool(MiniClient(app), "kmill_quota", {"log": "цех"})["result"]
    assert result["isError"] is True
    assert "поточний" in result["content"][0]["text"]


def test_log_search_reads_rotated_files(app_db, fake_log):  # noqa: F811
    app, _ = app_db
    fake_log.with_name(fake_log.name + ".1").write_text(
        _log_line(40, "Quota exceeded for quota metric 'Read requests'"), encoding="utf-8"
    )
    fake_log.write_text(_log_line(5, "усе добре"), encoding="utf-8")
    data = _tool(MiniClient(app), "kmill_log", {"pattern": "quota exceeded", "hours": 3})[
        "result"
    ]["structuredContent"]
    assert data["знайдено"] == 1
    assert "Quota exceeded" in data["рядки"][0]


def test_log_bad_regex_is_actionable(app_db, fake_log):  # noqa: F811
    app, _ = app_db
    fake_log.write_text(_log_line(1, "байдуже"), encoding="utf-8")
    result = _tool(MiniClient(app), "kmill_log", {"pattern": "("})["result"]
    assert result["isError"] is True
    assert "екрануй" in result["content"][0]["text"]


def test_quota_parses_a_line_written_by_the_real_logger(app_db, fake_log, monkeypatch):  # noqa: F811
    """Прилад звіряється з ДІЙСНИМ рядком лога, не з моїм текстом.

    Рядок складає сам `app.sheets._record_api_call` через той самий форматтер,
    яким пише прод (`runtime.LOG_FORMAT`). Якби тест писав текст руками, він
    пережив би і зміну формулювання, і зміну формату часу — тобто замовк би
    саме тоді, коли потрібен (`green-test-dead-feature`).
    """
    import logging

    from app import sheets
    from app.runtime import LOG_FORMAT

    handler = logging.FileHandler(fake_log, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger = logging.getLogger("app.sheets")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    monkeypatch.setattr(sheets, "_rate_calls", sheets.deque())
    monkeypatch.setattr(sheets, "_rate_reported_at", 0.0, raising=False)
    try:
        # Лічильник пише рядок не частіше разу на 60 с, і в рядку — скільки
        # викликів у вікні НА ТУ МИТЬ. Тому два звіти: перший на холодному
        # старті (1 виклик), другий через хвилину (у вікні лишаються 101 і 161).
        for tick in (100.0, 100.5, 101.0, 161.0):
            sheets._record_api_call(tick)
    finally:
        logger.removeHandler(handler)
        handler.close()

    written = fake_log.read_text(encoding="utf-8")
    assert "Sheets API" in written, written

    data = _tool(MiniClient(app_db[0]), "kmill_quota", {"hours": 1})["result"][
        "structuredContent"
    ]
    assert data["проб"] == written.count("Sheets API"), written
    assert data["максимум"] == 2, written
