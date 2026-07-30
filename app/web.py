from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db import SessionLocal
from app.models import Order, SyncLog
from app.sheet_writer import write_order_fields
from app.sheets import get_worksheet_by_name, open_spreadsheet

app = FastAPI(title="Order Desk")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    return templates.TemplateResponse(
        request, "login.html", {"error": "Автентифікація ще не підключена"}
    )


@app.get("/", response_class=HTMLResponse)
async def get_queue(request: Request, db: Session = Depends(get_db)):
    orders = db.scalars(select(Order).order_by(Order.id.desc())).all()
    return templates.TemplateResponse(
        request, "queue.html", {"page_title": "Черга робіт", "orders": orders}
    )


@app.post("/orders/{order_id}/sum3d-id", response_class=HTMLResponse)
async def set_sum3d_id(
    request: Request,
    order_id: int,
    sum3d_id: str = Form(...),
    db: Session = Depends(get_db),
):
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    order.sum3d_id = sum3d_id.strip() or None
    db.commit()
    db.refresh(order)

    try:
        worksheet = get_worksheet_by_name(open_spreadsheet(), order.sheet_tab)
        if worksheet is None:
            raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
        write_order_fields(worksheet, order, {"sum3d_id"})
        db.add(SyncLog(direction="db_to_sheet", sheet_tab=order.sheet_tab, status="ok", message=f"order {order.id}: sum3d_id"))
    except Exception as exc:
        db.add(SyncLog(direction="db_to_sheet", sheet_tab=order.sheet_tab, status="error", message=str(exc)))
    db.commit()

    return templates.TemplateResponse(request, "_order_row.html", {"order": order})
