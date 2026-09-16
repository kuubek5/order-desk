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
    add_shortcut,
    backfill_orders,
    delete_alias,
    delete_shortcut,
    ensure_seeded,
    list_materials,
    list_shortcuts,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
    update_shortcut,
)
from app.services.material_suggest import (
    autofill_shortcuts,
    badge_for_name,
    invalidate_cache as invalidate_suggest_cache,
    usage_for_expansion,
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
from app.services.settings_nav import can_edit
from .common import require_settings_edit

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
    if not can_edit(user, "materials"):
        raise HTTPException(status_code=403, detail="розділ доступний лише адміністратору")

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

    # Скорочення — плоский, наскрізний список (не прив'язаний до категорії):
    # `мл → mono`. Бейдж і «вжито» рахуються тут, щоб адмін бачив, якому
    # написанню давати коротше скорочення, і одразу ловив нерозпізнане.
    alias_rows = load_alias_rows(db)
    name_to_id = material_id_by_name(db)
    id_to_name = {mid: name for name, mid in name_to_id.items()}
    shortcut_views = []
    for row in list_shortcuts(db):
        mid = resolve_material_id(row.expansion, alias_rows, name_to_id)
        shortcut_views.append(
            {
                "id": row.id,
                "shortcut": row.shortcut,
                "expansion": row.expansion,
                "used": usage_for_expansion(db, row.expansion),
                "badge": badge_for_name(id_to_name.get(mid)) if mid is not None else None,
                "recognized": mid is not None,
            }
        )

    return templates.TemplateResponse(
        request,
        "settings_materials.html",
        {
            "page_title": "Бібліотека матеріалів",
            "shortcuts": shortcut_views,
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
            # Дефолт пошти живе тут, бо вибирає він саме з цього переліку.
            # Раніше під нього був окремий екран «Розпізнавання пошти» — див.
            # коментар до set_recognition_default_material нижче.
            "default_material": get_mail_default_material(db),
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
    require_settings_edit(request, db, "materials")
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
    require_settings_edit(request, db, "materials")
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
    require_settings_edit(request, db, "materials")
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
    require_settings_edit(request, db, "materials")
    try:
        add_material(db, name, is_production=is_production == "on")
        db.commit()
        request.session["materials_flash"] = {"kind": "success", "message": "Матеріал додано."}
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/shortcut/add")
def create_material_shortcut(
    request: Request,
    shortcut: str = Form(...),
    expansion: str = Form(...),
    db: Session = Depends(get_db),
):
    require_settings_edit(request, db, "materials")
    try:
        add_shortcut(db, shortcut, expansion)
        db.commit()
        # «Вжито» на екрані читається з frecency-кешу — скинути, щоб новий рядок
        # одразу показав своє число, а не чекав TTL.
        invalidate_suggest_cache()
        request.session["materials_flash"] = {"kind": "success", "message": "Скорочення додано."}
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/shortcut/{shortcut_id}/edit")
def edit_material_shortcut(
    shortcut_id: int,
    request: Request,
    shortcut: str = Form(...),
    expansion: str = Form(...),
    db: Session = Depends(get_db),
):
    require_settings_edit(request, db, "materials")
    try:
        update_shortcut(db, shortcut_id, shortcut, expansion)
        db.commit()
        invalidate_suggest_cache()
        request.session["materials_flash"] = {"kind": "success", "message": "Скорочення змінено."}
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/shortcut/{shortcut_id}/delete")
def remove_material_shortcut(shortcut_id: int, request: Request, db: Session = Depends(get_db)):
    require_settings_edit(request, db, "materials")
    delete_shortcut(db, shortcut_id)
    db.commit()
    request.session["materials_flash"] = {"kind": "success", "message": "Скорочення видалено."}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/shortcut/autofill")
def autofill_material_shortcuts(request: Request, db: Session = Depends(get_db)):
    require_settings_edit(request, db, "materials")
    added, skipped = autofill_shortcuts(db)
    db.commit()
    invalidate_suggest_cache()
    msg = f"Заповнено з бази: додано {added}."
    if skipped:
        msg += f" Пропущено {skipped} (колізія скорочення — додайте руками)."
    request.session["materials_flash"] = {"kind": "success", "message": msg}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/reclassify")
def reclassify_materials(request: Request, db: Session = Depends(get_db)):
    require_settings_edit(request, db, "materials")
    for order in db.scalars(select(Order)).all():
        order.material_id = None
    changed = backfill_orders(db, only_unresolved=False)
    db.commit()
    request.session["materials_flash"] = {
        "kind": "success",
        "message": f"Перекласифіковано робіт: {changed}.",
    }
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/mail-spool/prune")
def prune_mail_spool(request: Request, db: Session = Depends(get_db)):
    """Delete the mail-spool folders analyze_spool considers safe (empty ones,
    orphans with no letter row, and rejected letters past the retention
    window). Operator-triggered only — never a background job, see
    app/mail_spool.py."""
    require_settings_edit(request, db, "mail-download")
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
    require_settings_edit(request, db, "mail-download")
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
    back_m: str = Form(""),
    db: Session = Depends(get_db),
):
    """Set (or clear, with an empty value) the material the triage assumes for a
    milling letter with no material signal. Validated against real catalog names
    so a typo can't silently disable the rule.

    Шлях свідомо лишився історичним (`/settings/recognition/...`), хоча екрана
    «Розпізнавання пошти» більше немає: той екран був перехідником (два
    посилання + оцей один селект), і селект переїхав у «Бібліотеку матеріалів».
    Перейменування шляху нічого не дало б, окрім зайвої правки знімка роутів.

    `back_m` — id матеріалу, відкритого в консолі: повертаємо адміна туди, де
    він стояв, як це роблять решта форм бібліотеки.
    """
    require_settings_edit(request, db, "materials")
    back = f"/settings/materials?m={back_m}" if back_m else "/settings/materials"
    clean = (material_name or "").strip()
    valid_names = {m.name for m in list_materials(db)}
    if clean and clean not in valid_names:
        request.session["materials_flash"] = {"kind": "error", "message": "Невідомий матеріал."}
        return RedirectResponse(back, status_code=303)
    set_mail_default_material(db, clean)
    db.commit()
    if clean:
        message = f"Дефолт без сигналу: {clean}."
    else:
        message = "Дефолтний матеріал вимкнено."
    request.session["materials_flash"] = {"kind": "success", "message": message}
    return RedirectResponse(back, status_code=303)
