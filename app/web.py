from datetime import date, datetime, timedelta
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
from app.client_matcher import match_client_name
from app.config import SESSION_SECRET_KEY
from app.db import Base, SessionLocal, engine
from app.export_scanner import scan_export_folder
from app.models import ClientNameAlias, EmailMessage, Order, StatusEvent, SyncLog, User
from app.settings_store import SETTING_FIELDS, get_all_settings, get_export_folder_path, set_setting
from app.sheet_writer import write_order_fields
from app.sheets import get_worksheet_by_name, open_spreadsheet
from app.statuses import STATUSES


def _parse_sheet_tab(sheet_tab: str | None) -> date | None:
    if not sheet_tab:
        return None
    try:
        return datetime.strptime(sheet_tab, "%d.%m.%y").date()
    except ValueError:
        return None


Base.metadata.create_all(engine)

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

    # Categorize orders into buckets
    buckets = {"today": [], "yesterday": [], "tomorrow": [], "earlier": []}

    for order in all_orders:
        order_date = _parse_sheet_tab(order.sheet_tab)
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
        worksheet = get_worksheet_by_name(open_spreadsheet(db=db), order.sheet_tab)
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


@app.get("/handout", response_class=HTMLResponse)
async def get_handout(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    today = date.today()
    candidates = db.scalars(
        select(Order).where(Order.client_name.is_not(None), Order.status != "видано")
    ).all()

    groups: dict[str, list[Order]] = {}
    for order in candidates:
        order_date = _parse_sheet_tab(order.sheet_tab)
        if order_date is not None and order_date >= today:
            continue
        groups.setdefault(order.client_name, []).append(order)

    entries = scan_export_folder(Path(get_export_folder_path(db)))
    folder_names = sorted({e.client_folder_name for e in entries})
    aliases = {
        a.sheet_name: a.export_folder_name
        for a in db.scalars(select(ClientNameAlias).where(ClientNameAlias.confirmed.is_(True))).all()
    }

    client_groups = []
    for client_name, group_orders in groups.items():
        match = match_client_name(client_name, folder_names, aliases)
        export_entries = (
            [e for e in entries if e.client_folder_name == match.matched_folder_name]
            if match.matched_folder_name
            else []
        )
        all_found = all(o.status in ("знайдено при видачі", "видано") for o in group_orders)
        client_groups.append(
            {
                "client_name": client_name,
                "orders": group_orders,
                "match": match,
                "export_entries": export_entries,
                "all_found": all_found,
            }
        )

    return templates.TemplateResponse(
        request,
        "handout.html",
        {"page_title": "Ранкова видача", "user": user, "client_groups": client_groups},
    )


@app.post("/orders/{order_id}/mark-found")
async def mark_found(request: Request, order_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    order.status = "знайдено при видачі"
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username)
    )
    db.commit()

    return RedirectResponse("/handout", status_code=303)


@app.post("/handout/confirm-alias")
async def confirm_alias(
    request: Request,
    sheet_name: str = Form(...),
    export_folder_name: str = Form(...),
    db: Session = Depends(get_db),
):
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    existing = db.scalar(select(ClientNameAlias).where(ClientNameAlias.sheet_name == sheet_name))
    if existing is not None:
        existing.export_folder_name = export_folder_name
        existing.confirmed = True
        existing.confirmed_at = datetime.now()
    else:
        db.add(
            ClientNameAlias(
                sheet_name=sheet_name,
                export_folder_name=export_folder_name,
                confirmed=True,
                confirmed_at=datetime.now(),
            )
        )
    db.commit()

    return RedirectResponse("/handout", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def get_settings(
    request: Request, saved: str | None = None, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    values = get_all_settings(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "fields": SETTING_FIELDS,
            "values": values,
            "user": user,
            "saved": saved is not None,
        },
    )


@app.post("/settings", response_class=HTMLResponse)
async def post_settings(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    form = await request.form()
    for field in SETTING_FIELDS:
        value = form.get(field.key, "").strip()
        if value:
            set_setting(db, field.key, value)
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/mail", response_class=HTMLResponse)
async def get_mail(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    emails = db.scalars(
        select(EmailMessage)
        .where(EmailMessage.status == "нове")
        .order_by(
            EmailMessage.received_at.desc().nullslast(),
            EmailMessage.created_at.desc()
        )
    ).all()

    return templates.TemplateResponse(
        request,
        "mail_triage.html",
        {"page_title": "Нові з пошти", "emails": emails, "user": user},
    )


@app.get("/mail/{email_id}", response_class=HTMLResponse)
async def get_mail_detail(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    return templates.TemplateResponse(
        request,
        "mail_detail.html",
        {"email": email, "user": user},
    )


@app.post("/mail/{email_id}/accept", response_class=HTMLResponse)
async def accept_email(
    request: Request,
    email_id: int,
    client_name: str = Form(...),
    material_color: str = Form(""),
    kind: str = Form(""),
    quantity: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    new_order = Order(
        source="email",
        sheet_tab=None,
        row_number=None,
        client_name=client_name.strip() or None,
        material_color=material_color.strip() or None,
        kind=kind.strip() or None,
        quantity=quantity.strip() or None,
        status="нове",
    )
    db.add(new_order)
    db.flush()

    email.order_id = new_order.id
    email.status = "прийнято"
    db.add(
        StatusEvent(order_id=new_order.id, operator_id=user.id, status="нове", actor=user.username)
    )
    db.commit()

    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/{email_id}/reject")
async def reject_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    email.status = "відхилено"
    db.commit()

    return RedirectResponse("/mail", status_code=303)
