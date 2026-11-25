"""Рядок «який диск взяти» в картці листа (власник 12.11.26, варіант A).

Окремий запит, а не частина картки: читання теки дисків і файлів `.blk` не
має гальмувати відкриття листа. Показується лише тим, кому підказку дав
адміністратор (`disc_suggest.enabled_for`); решті — порожньо. Лише читання.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import perf
from app.models import EmailMessage
from app.routers.deps import get_current_user, get_db, templates
from app.services import disc_choice, disc_shadow, disc_suggest, selection_journal
from app.services.mail_conveyor import ConveyorCard, conveyor_summary, material_group_key
from app.settings_store import get_setting

router = APIRouter()


@router.get("/mail/{email_id}/disc-hint", response_class=HTMLResponse)
def get_disc_hint(request: Request, email_id: int, material_color: str = "",
                  db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not disc_suggest.enabled_for(db, user):
        return HTMLResponse("")
    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="лист не знайдено")
    machines: dict[str, str] = {}
    recent: set[str] = set()
    with perf.span("disc:active"):
        active = disc_suggest.active_discs(db, machines=machines, recent=recent)
    attachments = list(email.attachments or [])
    root = get_setting(db, "cam_blanks_path")
    shown_by, rival_by, compare = disc_choice.choosers(db, user.id)
    hint = disc_suggest.suggest(material_color, attachments, root,
                                active=active, machines=machines, recent=recent, chooser=shown_by)
    # Журнал підбору: що саме побачив оператор (при прийнятті не перерахувати).
    entry = selection_journal.remember_shown([email_id], material_color, hint, mode="card")
    if compare:
        disc_shadow.submit(entry, material_color, attachments, root, active=active, recent=recent, chooser=rival_by)
    return templates.TemplateResponse(request, "_mail_disc_hint.html", {"hint": hint})


@router.post("/mail/batch-disc-hint", response_class=HTMLResponse)
def batch_disc_hint(
    request: Request,
    batch_email_id: list[int] = Form([]),
    client_name: list[str] = Form([]),
    material_color: list[str] = Form([]),
    quantity: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    """Один диск на весь мультипрорахунок Конвеєра (власник 22.11.26).

    Мультипрорахунок збирає листи ОДНОГО кольору, тож диск — для матеріалу
    більшості (та сама смуга зведення, `conveyor_summary`), а в розстановку йдуть
    конструкції всіх листів цього матеріалу разом: висота — за найвищою роботою.
    Лист «інший колір» чи без матеріалу — поза розстановкою, лише лічильник.
    Не влазить ніде — «Новий диск», як у картці листа. Лише читання."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not disc_suggest.enabled_for(db, user):
        return HTMLResponse("")

    def _at(values: list[str], i: int) -> str:
        return values[i] if i < len(values) else ""

    cards = [
        ConveyorCard(email_id=eid, client=_at(client_name, i),
                     material=_at(material_color, i), quantity=_at(quantity, i))
        for i, eid in enumerate(batch_email_id)
    ]
    summary = conveyor_summary(cards)
    if not summary.label:
        return HTMLResponse("")
    main_key = material_group_key(summary.label)
    main_ids = [c.email_id for c in cards if material_group_key(c.material) == main_key]
    emails = db.scalars(
        select(EmailMessage).where(EmailMessage.id.in_(main_ids))
        .options(selectinload(EmailMessage.attachments))
    ).all()
    attachments = [a for e in emails for a in (e.attachments or [])]
    machines: dict[str, str] = {}
    recent: set[str] = set()
    with perf.span("disc:active"):
        active = disc_suggest.active_discs(db, machines=machines, recent=recent)
    root = get_setting(db, "cam_blanks_path")
    shown_by, rival_by, compare = disc_choice.choosers(db, user.id)
    hint = disc_suggest.suggest(summary.label, attachments, root,
                                active=active, machines=machines, recent=recent, chooser=shown_by)
    entry = selection_journal.remember_shown(main_ids, summary.label, hint, mode="conveyor",
                                             key=selection_journal.batch_key(main_ids))
    if compare:
        disc_shadow.submit(entry, summary.label, attachments, root, active=active, recent=recent, chooser=rival_by)
    return templates.TemplateResponse(request, "_mail_batch_disc_hint.html", {
        "hint": hint, "letters": len(emails), "skipped": len(cards) - len(main_ids),
    })
