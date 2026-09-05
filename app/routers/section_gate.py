"""HTTP-бік гейта розділів: віддати сторінку-блокатор не-адміну.

Використання в роуті розділу (після перевірки входу):

    blocked = blocked_response(request, db, user, "stats")
    if blocked is not None:
        return blocked
    ...
    context["section_banner"] = admin_banner(db, user, "stats")

Стан і тексти — app/services/section_gate.py.
"""

from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import templates
from app.services.section_gate import SECTIONS, VARIANTS, admin_banner, blocked_for

__all__ = ["blocked_response", "admin_banner"]


def blocked_response(request: Request, db: Session, user, section: str) -> Response | None:
    variant = blocked_for(db, user, section)
    if variant is None:
        return None
    meta = SECTIONS[section]
    return templates.TemplateResponse(
        request,
        "section_blocked.html",
        {
            "user": user,
            "section": section,
            "section_title": meta["title"],
            "variant": variant,
            "copy": VARIANTS[variant],
        },
    )
