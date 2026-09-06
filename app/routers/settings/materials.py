"""Бібліотека матеріалів і розпізнавання пошти: синоніми, дефолти, спул.

Винятки «не наша робота» тут НЕ живуть — тільки у фільтрах пошти
(`MailFilterRule`), паралельний список колись уже був і його прибрали.
"""

from pathlib import Path
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.config import MAIL_ATTACHMENTS_PATH
from app.mail_spool import prune_spool
from app.material_catalog import (
    MaterialCatalogError,
    add_alias,
    add_material,
    backfill_orders,
    delete_alias,
    ensure_seeded,
    list_materials,
)
from app.services.materials_console import (
    load_colour_rows,
    material_views,
    measure_rules,
    probe_pattern,
    unresolved_breakdown,
)
from app.models import MaterialAlias, Order
from app.routers.deps import get_current_user, login_redirect, get_db, templates
from app.settings_store import (
    get_mail_default_material,
    get_mail_download_all,
    set_mail_default_material,
    set_mail_download_all,
)
from .common import require_settings_admin

router = APIRouter()


@router.get("/settings/materials", response_class=HTMLResponse)
def get_materials_settings(
    request: Request, m: int | None = None, db: Session = Depends(get_db)
):
    """Екран: консоль бібліотеки матеріалів (адмін).

    Двопанельна: зліва матеріали з вимірами, справа — один обраний. `m` тримає
    вибір у URL, тому після додавання/видалення правила адміна повертає туди ж,
    де він працював, а не на початок довгої сторінки.

    Числа рахує app/services/materials_console.py по СПРАВЖНІХ кольорах робіт.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    ensure_seeded(db)
    flash = request.session.pop("materials_flash", None)
    materials = list_materials(db)
    colours = load_colour_rows(db)
    views = material_views(materials, colours)
    unresolved, no_rule_orders, collision_orders = unresolved_breakdown(db, colours)

    selected = next((x for x in materials if x.id == m), None)
    if selected is None and materials:
        selected = materials[0]
    rules = measure_rules(list(selected.aliases), colours) if selected else []

    return templates.TemplateResponse(
        request,
        "settings_materials.html",
        {
            "page_title": "Бібліотека матеріалів",
            "user": user,
            "materials": materials,
            "material_views": views,
            "selected": selected,
            "rules": rules,
            # Число В ШАПЦІ й список під ним МУСЯТЬ рахуватись з одного
            # предиката, інакше екран суперечить сам собі. Перша версія брала
            # unresolved_order_count() (без фільтра архіву) і показувала
            # «24 робіт без матеріалу · 0 написань» — бо розбір дивиться лише на
            # активні роботи. Беремо суму того самого розбору: розійтись нема як.
            "unresolved_count": no_rule_orders + collision_orders,
            "unresolved_items": unresolved[:12],
            "unresolved_total_items": len(unresolved),
            "no_rule_orders": no_rule_orders,
            "collision_orders": collision_orders,
            "show_unresolved": m == -1,
            "flash": flash,
        },
    )


@router.get("/settings/materials/probe", response_class=HTMLResponse)
def probe_material_alias(
    request: Request,
    material_id: int,
    pattern: str = "",
    match_type: str = "contains",
    db: Session = Depends(get_db),
):
    """Живі показання для ще НЕ збереженого правила (HTMX, на введення).

    Читання, нічого не змінює. Відповідає на три питання одразу: скільки робіт
    піймає, на яких написаннях і чи не відбере воно роботи в іншого матеріалу —
    останнє критичне, бо при двох претендентах класифікатор віддає None, тобто
    робота зникає з ОБОХ матеріалів у «не розпізнано».
    """
    require_settings_admin(request, db)
    result = probe_pattern(db, material_id, pattern, match_type)
    return templates.TemplateResponse(
        request, "_matlib_probe.html", {"probe": result}
    )


@router.post("/settings/materials/alias/add")
def add_material_alias(
    request: Request,
    material_id: int = Form(...),
    pattern: str = Form(...),
    match_type: str = Form("contains"),
    db: Session = Depends(get_db),
):
    require_settings_admin(request, db)
    try:
        add_alias(db, material_id, pattern, match_type)
        # Apply the new rule to every order, including already-classified ones.
        changed = backfill_orders(db, only_unresolved=False)
        db.commit()
        request.session["materials_flash"] = {
            "kind": "success",
            "message": f"Правило додано. Перекласифіковано робіт: {changed}.",
        }
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    # Повертаємо НА ТОЙ САМИЙ матеріал: інакше адмін після кожного правила
    # опинявся б на першому в списку й шукав своє місце заново.
    return RedirectResponse(f"/settings/materials?m={material_id}", status_code=303)


@router.post("/settings/materials/alias/{alias_id}/delete")
def remove_material_alias(alias_id: int, request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    alias = db.get(MaterialAlias, alias_id)
    back_to = alias.material_id if alias is not None else None
    delete_alias(db, alias_id)
    # Re-resolve from scratch so orders that only matched the deleted rule are
    # re-evaluated against the remaining rules (may become unresolved again).
    for order in db.scalars(select(Order)).all():
        order.material_id = None
    backfill_orders(db, only_unresolved=False)
    db.commit()
    request.session["materials_flash"] = {"kind": "success", "message": "Правило видалено."}
    target = f"/settings/materials?m={back_to}" if back_to else "/settings/materials"
    return RedirectResponse(target, status_code=303)


@router.post("/settings/materials/add")
def create_material(
    request: Request,
    name: str = Form(...),
    is_production: str = Form("on"),
    db: Session = Depends(get_db),
):
    require_settings_admin(request, db)
    try:
        add_material(db, name, is_production=is_production == "on")
        db.commit()
        request.session["materials_flash"] = {"kind": "success", "message": "Матеріал додано."}
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/reclassify")
def reclassify_materials(request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    for order in db.scalars(select(Order)).all():
        order.material_id = None
    changed = backfill_orders(db, only_unresolved=False)
    db.commit()
    request.session["materials_flash"] = {
        "kind": "success",
        "message": f"Перекласифіковано робіт: {changed}.",
    }
    return RedirectResponse("/settings/materials", status_code=303)


@router.get("/settings/recognition", response_class=HTMLResponse)
def get_recognition_settings(request: Request, db: Session = Depends(get_db)):
    """Screen: mail recognition settings (admin). Holds the default-material
    fallback the triage applies to a signal-less milling letter, and points to
    the two editable dictionaries that live elsewhere — the material library
    (synonyms like «врім'янка» → ПММА) and the mail filters (exception
    categories like 3D-друк / моделювання that route a letter out of the queue)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    flash = request.session.pop("recognition_flash", None)
    return templates.TemplateResponse(
        request,
        "settings_recognition.html",
        {
            "page_title": "Розпізнавання пошти",
            "user": user,
            "materials": list_materials(db),
            "default_material": get_mail_default_material(db),
            "flash": flash,
        },
    )


@router.post("/settings/mail-spool/prune")
def prune_mail_spool(request: Request, db: Session = Depends(get_db)):
    """Delete the mail-spool folders analyze_spool considers safe (empty ones,
    orphans with no letter row, and rejected letters past the retention
    window). Operator-triggered only — never a background job, see
    app/mail_spool.py."""
    require_settings_admin(request, db)
    removed, freed = prune_spool(db, Path(MAIL_ATTACHMENTS_PATH))
    mb = round(freed / (1024 * 1024), 1)
    request.session["settings_flash"] = {
        "kind": "success",
        "message": (
            f"Прибрано папок: {removed}, звільнено {mb} МБ."
            if removed
            else "Нічого прибирати — спул чистий."
        ),
    }
    return RedirectResponse("/settings#mail-download", status_code=303)


@router.post("/settings/mail-download/toggle")
def toggle_mail_download_all(
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Flip the "auto-download every incoming letter's attachments" toggle
    (admin). Off (default) keeps the selective whitelist behaviour; on pulls
    files from every sender into the spool as mail arrives. `return_to=mail`
    lands back on the triage screen (the toggle is mirrored in its header),
    otherwise on the settings section."""
    require_settings_admin(request, db)
    new_value = not get_mail_download_all(db)
    set_mail_download_all(db, new_value)
    db.commit()
    message = (
        "Авто-скачування всіх вкладень увімкнено."
        if new_value
        else "Авто-скачування вимкнено — качаються лише довірені відправники."
    )
    if return_to == "mail":
        request.session["toast_flash"] = {"kind": "success", "message": message}
        return RedirectResponse("/mail", status_code=303)
    request.session["settings_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings#mail-download", status_code=303)


@router.post("/settings/recognition/default-material")
def set_recognition_default_material(
    request: Request,
    material_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Set (or clear, with an empty value) the material the triage assumes for a
    milling letter with no material signal. Validated against real catalog names
    so a typo can't silently disable the rule."""
    require_settings_admin(request, db)
    clean = (material_name or "").strip()
    valid_names = {m.name for m in list_materials(db)}
    if clean and clean not in valid_names:
        request.session["recognition_flash"] = {"kind": "error", "message": "Невідомий матеріал."}
        return RedirectResponse("/settings/recognition", status_code=303)
    set_mail_default_material(db, clean)
    db.commit()
    if clean:
        message = f"Дефолт без сигналу: {clean}."
    else:
        message = "Дефолтний матеріал вимкнено."
    request.session["recognition_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings/recognition", status_code=303)
