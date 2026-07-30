from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app.auth import verify_password
from app.config import SESSION_SECRET_KEY
from app.db import SessionLocal
from app.models import Order, StatusEvent, SyncLog, User
from app.sheet_writer import write_order_fields
from app.sheets import get_worksheet_by_name, open_spreadsheet
from app.statuses import STATUSES

app = FastAPI(title="Order Desk")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return db.get(User, user_id)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Невірний логін або пароль"}
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def get_queue(request: Request, period: str = "today", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Validate period parameter
    if period not in ("today", "yesterday", "tomorrow", "earlier"):
        period = "today"

    # Fetch all orders
    all_orders = db.scalars(select(Order).order_by(Order.id.desc())).all()

    # Define date boundaries
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    # Function to parse sheet_tab (format: %d.%m.%y, e.g., "27.07.26")
    def parse_sheet_tab(sheet_tab: str | None) -> date | None:
        if not sheet_tab:
            return None
        try:
            from datetime import datetime
            return datetime.strptime(sheet_tab, "%d.%m.%y").date()
        except (ValueError, TypeError):
            return None

    # Categorize orders into buckets
    buckets = {"today": [], "yesterday": [], "tomorrow": [], "earlier": []}

    for order in all_orders:
        order_date = parse_sheet_tab(order.sheet_tab)
        if order_date is None:
            # Route None/unparseable to today (safe default for email-sourced orders)
            buckets["today"].append(order)
        elif order_date == today:
            buckets["today"].append(order)
        elif order_date == yesterday:
            buckets["yesterday"].append(order)
        elif order_date == tomorrow:
            buckets["tomorrow"].append(order)
        else:
            buckets["earlier"].append(order)

    # Get the filtered list for the current period
    orders = buckets[period]

    # Count for all buckets
    counts = {k: len(v) for k, v in buckets.items()}

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "page_title": "Черга робіт",
            "orders": orders,
            "user": user,
            "statuses": STATUSES,
            "period": period,
            "counts": counts,
        },
    )


@app.post("/orders/{order_id}/sum3d-id", response_class=HTMLResponse)
async def set_sum3d_id(
    request: Request,
    order_id: int,
    sum3d_id: str = Form(...),
    db: Session = Depends(get_db),
):
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

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

    return templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES}
    )


@app.post("/orders/{order_id}/status", response_class=HTMLResponse)
async def set_status(
    request: Request,
    order_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="невідомий статус")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    order.status = status
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=status, actor=user.username)
    )
    db.commit()
    db.refresh(order)

    return templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES}
    )


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def get_order_detail(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "order": order,
            "user": user,
        },
    )
