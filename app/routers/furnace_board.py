"""Окремий веб-застосунок табло цеху (порт 8010, мережа цеху).

Живе ПОЗА головним застосунком навмисно (див. `app/services/furnace_board.py`):
тут фізично немає інших маршрутів, ні сесій, ні форм — лише дві сторінки за
посиланням із секретом і кілька файлів оформлення. Будь-яка інша адреса — 404.

Сторінок дві, бо аудиторії різні: `/t/<token>` — печі для логістів,
`/t/<token>/shop` — верстати, принтер і печі на телевізор цеху. Друга не
замінює першу: логісти відкривають свою адресу й нічого нового не вчать.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.routers.deps import templates
from app.runtime import resource_path
from app.services import furnace_board, shop_board

# Лише ці файли оформлення віддає табло — не вся тека /static.
_CSS = {"fonts.css", "tokens.css", "theme-forge.css"}
_IMAGES = {
    "furnace-crowns.jpg", "furnace-bg-open.jpg", "furnace-bg-closed.jpg",
    # Дефолти моделей — коли свого фото верстата ще не завантажили.
    "machine-portrait-350i.jpg", "machine-portrait-350i-loader.jpg",
    "machine-portrait-250i.jpg", "machine-portrait-250i-dry.jpg",
}
_JS = {"shop_board_slm.js"}


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _not_found() -> Response:
    return PlainTextResponse("Не знайдено", status_code=404)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def create_board_app() -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    static_root = Path(resource_path("app/static"))
    app.mount("/static/fonts", StaticFiles(directory=str(static_root / "fonts")), name="board-fonts")

    def _allowed(db, token: str) -> bool:
        return furnace_board.board_enabled(db) and furnace_board.token_matches(db, token)

    @app.get("/t/{token}", response_class=HTMLResponse)
    def board_page(request: Request, token: str):
        with _db() as db:
            if not _allowed(db, token):
                return _not_found()
            view = furnace_board.board_view(db)
        return _no_store(templates.TemplateResponse(request, "furnace_board.html", {
            "view": view, "token": token, "refresh": furnace_board.BOARD_REFRESH_SECONDS,
        }))

    @app.get("/t/{token}/cards", response_class=HTMLResponse)
    def board_cards(request: Request, token: str):
        with _db() as db:
            if not _allowed(db, token):
                return _not_found()
            view = furnace_board.board_view(db)
        return _no_store(templates.TemplateResponse(request, "_furnace_board_cards.html", {"view": view}))

    @app.get("/t/{token}/shop", response_class=HTMLResponse)
    def shop_page(request: Request, token: str):
        with _db() as db:
            if not _allowed(db, token):
                return _not_found()
            view = shop_board.shop_view(db, token=token)
        return _no_store(templates.TemplateResponse(request, "shop_board.html", {
            "view": view, "token": token, "refresh": furnace_board.BOARD_REFRESH_SECONDS,
        }))

    @app.get("/t/{token}/shop/cards", response_class=HTMLResponse)
    def shop_cards(request: Request, token: str):
        with _db() as db:
            if not _allowed(db, token):
                return _not_found()
            view = shop_board.shop_view(db, token=token)
        return _no_store(templates.TemplateResponse(request, "_shop_board_cards.html", {
            "view": view, "token": token,
        }))

    @app.get("/t/{token}/portrait/{machine_id}.jpg")
    def shop_portrait(token: str, machine_id: int):
        """Фото верстата з Налаштувань. Своя адреса, бо маршрутів головного
        застосунку тут немає; `machine_id` — ціле, тож вийти за теку неможливо."""
        from app.machine_portraits import portrait_path

        with _db() as db:
            if not _allowed(db, token):
                return _not_found()
        path = portrait_path(machine_id)
        if not path.exists():
            return _not_found()
        return _no_store(FileResponse(path, media_type="image/jpeg"))

    @app.get("/static/css/{name}")
    def board_css(name: str):
        if name not in _CSS:
            return _not_found()
        return FileResponse(static_root / "css" / name, media_type="text/css")

    @app.get("/static/img/{name}")
    def board_image(name: str):
        if name not in _IMAGES:
            return _not_found()
        return FileResponse(static_root / "img" / name, media_type="image/jpeg")

    @app.get("/static/js/{name}")
    def board_js(name: str):
        if name not in _JS:
            return _not_found()
        return FileResponse(static_root / "js" / name, media_type="application/javascript")

    return app
