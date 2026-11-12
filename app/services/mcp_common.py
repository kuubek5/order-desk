"""Спільне для інструментів MCP: опис інструмента, помилка з наступним
кроком, читання числових аргументів. Винесено з `mcp_tools.py` (12.11.26),
щоб групи інструментів жили в окремих файлах і не імпортували одна одну.
Лише читання — як і всі інструменти (див. докстрінг `mcp_tools`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session


class ToolError(Exception):
    """Помилка, з якої видно, що робити далі.

    Правило з `mcp-builder`: «не зміг» без наступного кроку змушує того, хто
    питає, гадати. Тому текст завжди називає або допустимі значення, або
    наступну дію.
    """


def _int_arg(args: dict, name: str, *, default: int, low: int, high: int) -> int:
    raw = args.get(name, default)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"`{name}` мусить бути числом {low}…{high}, а не «{raw}»") from None
    if not low <= value <= high:
        raise ToolError(f"`{name}` поза межами {low}…{high}: {value}")
    return value


def _days_limit_schema(days_default: int, days_max: int, extra: Optional[dict] = None) -> dict:
    props: dict[str, Any] = {
        "days": {"type": "integer", "minimum": 1, "maximum": days_max,
                 "description": f"За скільки днів, типово {days_default}."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                  "description": "Скільки прикладів у списках."},
    }
    props.update(extra or {})
    return {"type": "object", "properties": props, "additionalProperties": False}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[Session, dict], dict[str, Any]]


_DAY_PROP = {
    "type": "string",
    "description": "today / yesterday / tomorrow або дата (2026-09-11, 11.09.26). "
    "Робочий день вкладки: у вихідні «today» = пʼятниця.",
}
