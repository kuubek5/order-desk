"""Екран «Статистика» (STATS_BRIEF.md, етап 1): вкладки Огляд і Надходження,
решта — видимі заглушки наступних етапів; «Техніки» — лише адміну.

Роут лише читає адресу й віддає контекст із `app/services/stats_view.py`;
агрегати — SQL у `app/services/stats_data.py`. HTMX-запит (вкладка, період,
фільтр) отримує фрагмент `#st-root`, звичайний — цілу сторінку: адреса
лишається такою, що нею можна поділитись. Рахує гроші не CRM — її справа
чесно віддати цифри (CLAUDE.md §5).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.business_day import business_today
from app.routers.deps import get_current_user, get_db, login_redirect, templates
from app.routers.section_gate import admin_banner, blocked_response
from app.services import stats_export, stats_view
from app.services.section_gate import is_admin

router = APIRouter()

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _gate(request: Request, db: Session):
    """(user, None) або (None, відповідь-перешкода)."""
    user = get_current_user(request, db)
    if user is None:
        return None, login_redirect(request)
    # Розділ закрито «на калібрування» (стан у налаштуваннях): не-адмін бачить
    # блокатор, адмін — статистику з банером «Відкрити для всіх».
    blocked = blocked_response(request, db, user, "stats")
    if blocked is not None:
        return None, blocked
    return user, None


def _params(request: Request, user) -> stats_view.Params:
    return stats_view.parse_params(request.query_params, business_today(), is_admin(user))


def page_context(db: Session, p: stats_view.Params, admin: bool) -> dict:
    ctx: dict = {"p": p, "chrome": stats_view.chrome(db, p, admin)}
    if p.tab == "overview":
        ctx["ov"] = stats_view.overview(db, p)
        if ctx["ov"]["selected_day"] is not None:
            ctx["side"] = stats_view.side_list(db, p, stats_view.ListRequest(day=ctx["ov"]["selected_day"]))
            ctx["side_day"] = ctx["ov"]["selected_day"].isoformat()
    elif p.tab == "intake":
        ctx["it"] = stats_view.intake(db, p)
    else:
        ctx["stage"] = next(t for t in stats_view.TABS if t[0] == p.tab)
        ctx["stage_text"] = stats_view.STAGE_TEXT.get(p.tab, "")
    return ctx


@router.get("/stats", response_class=HTMLResponse)
def get_stats(request: Request, db: Session = Depends(get_db)):
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    admin = is_admin(user)
    p = _params(request, user)
    ctx = page_context(db, p, admin)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_stats_root.html", ctx)
    ctx.update(
        {
            "page_title": "Статистика",
            "user": user,
            "section_banner": admin_banner(db, user, "stats"),
        }
    )
    return templates.TemplateResponse(request, "stats.html", ctx)


@router.get("/stats/list", response_class=HTMLResponse)
def get_stats_list(request: Request, db: Session = Depends(get_db)):
    """Бічна панель: роботи дня / клітинки карти / без кількості / браку."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    p = _params(request, user)
    req = stats_view.ListRequest.parse(request.query_params)
    side = stats_view.side_list(db, p, req)
    return templates.TemplateResponse(request, "_stats_side.html", {"side": side, "p": p})


def _download(payload: bytes, filename: str) -> Response:
    return Response(
        content=payload,
        media_type=_XLSX,
        headers={"Content-Disposition": f"attachment; filename=\"stats.xlsx\"; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/stats/export")
def get_stats_export(request: Request, db: Session = Depends(get_db)):
    """«У Excel» — поточна вкладка (Огляд або Надходження)."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    p = _params(request, user)
    stamp = f"{p.period.start:%d.%m}-{p.period.end:%d.%m.%y}"
    if p.tab == "intake":
        return _download(stats_export.intake_book(db, p), f"Статистика · Надходження · {stamp}.xlsx")
    return _download(stats_export.overview_book(db, p), f"Статистика · Огляд · {stamp}.xlsx")


@router.get("/stats/list/export")
def get_stats_list_export(request: Request, db: Session = Depends(get_db)):
    """«Список у Excel» — те саме, що в бічній панелі."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    p = _params(request, user)
    req = stats_view.ListRequest.parse(request.query_params)
    return _download(stats_export.list_book(db, p, req), "Статистика · список робіт.xlsx")
