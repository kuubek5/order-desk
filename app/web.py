from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db import SessionLocal
from app.models import Order

app = FastAPI(title="Order Desk")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/", response_class=HTMLResponse)
async def get_queue(request: Request, db: Session = Depends(get_db)):
    orders = db.scalars(select(Order).order_by(Order.id.desc())).all()
    return templates.TemplateResponse(
        request, "queue.html", {"page_title": "Черга робіт", "orders": orders}
    )
