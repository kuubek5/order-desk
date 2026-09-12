"""MCP по HTTP — `POST /mcp`. Транспорт для інструментів з `services/mcp_tools.py`.

Чому HTTP, а не stdio. Звичайний MCP-сервер говорить через stdin/stdout, але
прод зібраний БЕЗ консолі (`KuubMill.spec`, `console=False`), і там
`sys.stdout is None` — та сама граблі, на якій табло печей не відкривало порт
(§14). Тому сервер живе всередині застосунку, який і так слухає
`127.0.0.1:8000`: на цеховому ПК нічого додатково ставити не треба, ні Python,
ні другий exe, а версія інструментів завжди дорівнює версії CRM.

Гейт — `is_loopback_request`, як у тринадцяти інших дій (§ROADMAP хід 17). Сесії
оператора тут немає свідомо: MCP-клієнт не носить куку, а всі інструменти лише
читають. Межа така: хто може запустити процес на цій машині, той і так читає
файл бази поруч. По мережі — 403, навіть після того, як застосунок почне
слухати `0.0.0.0`.

Одна приємна дрібниця з безпеки: протокол вимагає заголовок
`MCP-Protocol-Version`, а «нестандартний» заголовок змушує браузер робити
preflight. Тож чужа сторінка в браузері оператора не може тихо постукати сюди
з фонового скрипта — CORS ми не відкриваємо.

Відповіді — звичайний `application/json`: потокові нотифікації нам не потрібні,
кожен інструмент віддає готовий зріз.

Входів два, розбір один. Крім роута головного застосунку тут живе
`create_mcp_app()` — окремий крихітний застосунок для другого порту
(`app/services/mcp_gateway.py`), куди запит приходить з ІНШОЇ машини, і межа там
токен, а не loopback. Сам протокол обидва входи проганяють через одну
`handle_rpc`, щоб відповідь не розійшлась між ними.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.__version__ import VERSION
from app.routers.deps import get_db, is_loopback_request
from app.services.mcp_gateway import gateway_enabled, token_matches
from app.services.mcp_tools import TOOLS_BY_NAME, ToolError, tool_descriptors

logger = logging.getLogger(__name__)

router = APIRouter()

SERVER_NAME = "kuubmill"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05", "2026-07-28")

# Межа тексту однієї відповіді. Журнал синку чи лог на 300 рядків роздувається
# легко, а цілий екран тексту в контексті коштує дорожче, ніж другий виклик із
# меншим `limit`. Обрізаємо з ХВОСТА, бо свіже важливіше.
_MAX_TEXT = 60_000

# JSON-RPC коди (specification 2.0) — числа не свої, не міняти.
_ERR_INVALID_REQUEST = -32600
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602
_ERR_INTERNAL = -32603


def _result(request_id: Any, payload: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": payload})


def _error(request_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _tool_text(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) > _MAX_TEXT:
        text = (
            text[:_MAX_TEXT]
            + "\n… обрізано. Повтори виклик із меншим `limit` або вужчим вікном."
        )
    return text


def _tool_result(request_id: Any, payload: dict) -> JSONResponse:
    return _result(
        request_id,
        {
            "content": [{"type": "text", "text": _tool_text(payload)}],
            "structuredContent": payload,
        },
    )


def _tool_failure(request_id: Any, message: str) -> JSONResponse:
    """Помилка ІНСТРУМЕНТА, не протоколу.

    За специфікацією така помилка їде як звичайний результат із `isError`: той,
    хто питає, мусить побачити текст і виправити виклик, а не отримати збій
    транспорту, по якому незрозуміло, що робити.
    """
    return _result(
        request_id,
        {"content": [{"type": "text", "text": message}], "isError": True},
    )


def handle_rpc(payload: Any, db: Session) -> Response:
    """Розбір одного запиту JSON-RPC — без жодного гейта.

    Винесено з роута, бо входів тепер ДВА: `POST /mcp` головного застосунку
    (межа — loopback) і окремий слухач на `GATEWAY_PORT` для запиту з іншої
    машини (межа — токен, `app/routers/mcp.py::create_mcp_app`). Протокол мусить
    бути той самий байт у байт, тож він живе в одному місці: гейт — справа
    входу, розбір — справа цієї функції.
    """
    if isinstance(payload, list):
        return _error(
            None,
            _ERR_INVALID_REQUEST,
            "пакет запитів не підтримується — надсилай по одному",
        )
    if not isinstance(payload, dict):
        return _error(None, _ERR_INVALID_REQUEST, "очікується обʼєкт JSON-RPC 2.0")

    method = str(payload.get("method") or "")
    request_id = payload.get("id")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    # Нотифікації (`notifications/initialized` і подібні) відповіді не мають.
    if method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return _result(
            request_id,
            {
                "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": "KuubMill — стан цеху (лише читання)",
                    "version": VERSION,
                },
            },
        )

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": tool_descriptors()})

    if method == "tools/call":
        name = str(params.get("name") or "")
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            return _error(
                request_id,
                _ERR_INVALID_PARAMS,
                f"інструмента «{name}» немає. Доступні: {', '.join(sorted(TOOLS_BY_NAME))}",
            )
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            return _tool_result(request_id, tool.run(db, args))
        except ToolError as exc:
            return _tool_failure(request_id, str(exc))
        except Exception:  # noqa: BLE001 — збій інструмента не валить зʼєднання
            logger.exception("MCP: інструмент %s упав", name)
            return _error(
                request_id,
                _ERR_INTERNAL,
                f"інструмент «{name}» упав — подробиці в лозі CRM (kmill_log із шаблоном «MCP»)",
            )

    return _error(
        request_id,
        _ERR_METHOD_NOT_FOUND,
        f"метод «{method}» не підтримується; є initialize, ping, tools/list, tools/call",
    )


@router.post("/mcp")
def mcp_endpoint(
    request: Request,
    payload: Any = Body(default=None),
    db: Session = Depends(get_db),
):
    if not is_loopback_request(request):
        return _error(
            None,
            _ERR_INVALID_REQUEST,
            "MCP доступний лише з цього компʼютера (127.0.0.1)",
            status=403,
        )
    return handle_rpc(payload, db)


# ── Окремий слухач для запиту з іншої машини ────────────────────────────────


def _remote_db():
    """Своя сесія на запит — як у табло печей (`routers/furnace_board.py::_db`).

    Залежність `get_db` головного застосунку тут не підходить: це інший
    застосунок, і він піднімається не з його життєвого цикла.
    """
    from app.db import SessionLocal

    return SessionLocal()


def _presented_token(request: Request) -> str:
    """Токен із `Authorization: Bearer …` або з `X-Token`.

    Два місця, бо клієнти різні: MCP-клієнт Claude Code шле `Authorization`,
    а ручна перевірка з `curl`/браузера простіше робиться заголовком `X-Token`.
    Порівнює їх `mcp_gateway.token_matches` (там `compare_digest`).
    """
    header = (request.headers.get("authorization") or "").strip()
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return (request.headers.get("x-token") or "").strip()


def create_mcp_app() -> FastAPI:
    """Крихітний окремий застосунок: лише `POST /mcp`, усе інше — 404.

    Той самий прийом, що в табло печей, і з тієї самої причини: головна CRM
    лишається на `127.0.0.1`, а назовні (тунель WireGuard) виходить слухач, у
    якому фізично немає ні сторінок, ні статики, ні сесій — нічого, що могло б
    віддати екран KuubMill у мережу.

    Межа тут — ТОКЕН, а не loopback: сенс слухача саме в тому, щоб пустити
    запит з іншої машини (`app/services/mcp_gateway.py`).
    """
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/mcp")
    def remote_mcp(request: Request, payload: Any = Body(default=None)):
        # Роут звичайний `def` — FastAPI виконує його в threadpool, тож сесія
        # створюється й закривається в ОДНОМУ потоці, а інструменти не блокують
        # event loop (§14 «сесія БД на одному потоці»).
        with _remote_db() as db:
            # Крім токена — ще й перемикач, як у табло печей (`_allowed`).
            # Порт закриває сторож, але між «вимкнув» і «порт зник» проходить
            # до пʼяти секунд, і в ці секунди вимкнений доступ мусить бути
            # вимкненим. Та сама відповідь 401, бо чому саме відмова — не
            # справа того, хто стукає.
            if not gateway_enabled(db) or not token_matches(db, _presented_token(request)):
                # Ні самого токена, ні його частини в тексті: відповідь їде в
                # мережу, і вона не має бути підказкою.
                return _error(
                    None,
                    _ERR_INVALID_REQUEST,
                    "потрібен токен доступу: заголовок Authorization: Bearer … або X-Token",
                    status=401,
                )
            return handle_rpc(payload, db)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    def nothing_else(path: str):
        return PlainTextResponse("Не знайдено", status_code=404)

    return app
