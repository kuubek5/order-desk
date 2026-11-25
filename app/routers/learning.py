"""Екрани журналу рішень: «Навчання» (адмін) і «Чому так» у паспорті роботи.

Доменна частина — `app/services/decision_*.py`. Тут лише HTTP.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import decisions_store
from app.models import Order, User
from app.routers.deps import get_current_user, get_db, login_redirect, require_admin, require_admin_or_redirect, templates
from app.services import decision_learn, decision_rules, decision_story, decisions, disc_choice, disc_watch, selection_journal

logger = logging.getLogger(__name__)

router = APIRouter()

_mining = threading.Lock()

FORGET = "forget"


def _context(db: Session, error: str = "") -> dict:
    entries = decision_rules.load(db)
    found = decision_learn.load_found()
    used = decision_learn.usage(days=60, since={k: e.get("at") or "" for k, e in entries.items()})
    return {
        "found": found,
        "candidates": [c for c in found.get("candidates", []) if c["key"] not in entries],
        "rules": [
            {"key": key, "used": used.get(key) or {"залишили": 0, "виправили": 0}, **entry}
            for key, entry in sorted(entries.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)
            if entry.get("rule") and key != disc_choice.KEY
        ],
        "disc": disc_choice.load(),
        "disc_key": disc_choice.KEY,
        "disc_status": disc_choice.status(db),
        "disc_auto": disc_choice.auto_state(),
        "disc_live": disc_watch.state(),
        "disc_curve": disc_watch.curve(),
        "disc_misses": selection_journal.misses(db, 30),
        "people": {u.id: (u.full_name or u.username) for u in db.scalars(select(User))},
        "disc_note": (entries.get(disc_choice.KEY) or {}).get("note") or "",
        "disc_rivals": selection_journal.rivalry(db, 30),
        "accuracy": decision_learn.accuracy(14),
        "journal": decision_learn.journal_state(),
        "mining": _mining.locked(),
        "learn_error": error,
    }


@router.get("/learning", response_class=HTMLResponse)
def get_learning(request: Request, db: Session = Depends(get_db)):
    user = require_admin_or_redirect(request, db, loopback=False)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request, "learning.html", {"user": user, "topbar_active": "learning", **_context(db)}
    )


@router.get("/learning/body", response_class=HTMLResponse)
def get_learning_body(request: Request, db: Session = Depends(get_db)):
    """Фрагмент — поки йде перерахунок, екран питає його сам."""
    user = require_admin(request, db, loopback=False)
    return templates.TemplateResponse(request, "_learning_body.html", {"user": user, **_context(db)})


@router.post("/learning/rule", response_class=HTMLResponse)
def post_learning_rule(
    request: Request, key: str = Form(...), status: str = Form(...), db: Session = Depends(get_db)
):
    user = require_admin(request, db, loopback=False)
    if status == decision_rules.STATUS_AUTO and key != disc_choice.KEY:
        raise HTTPException(status_code=400, detail="«Автоматично» є лише для порядку дисків")
    if status == FORGET:
        decision_rules.forget(db, key)
    else:
        entry = decision_rules.load(db).get(key)
        rule = disc_choice.RULE if key == disc_choice.KEY else (entry or {}).get("rule") or next(
            (c for c in decision_learn.load_found().get("candidates", []) if c["key"] == key), None
        )
        if rule is None:
            raise HTTPException(status_code=404, detail="такого правила вже немає")
        try:
            decision_rules.save_status(db, rule, status, user_id=user.id)
        except ValueError:
            raise HTTPException(status_code=400, detail="невідомий стан правила") from None
    db.commit()
    decisions_store.put(
        decisions.STEP_RULE, decisions.ACTOR_OPERATOR, user_id=user.id, rule=key, chosen=status
    )
    return templates.TemplateResponse(request, "_learning_body.html", {"user": user, **_context(db)})


def mine_now(days: int = decision_learn.DAYS) -> bool:
    """Перерахувати кандидатів ОКРЕМИМ процесом. False — вже рахується."""
    from app import mcp_isolated
    from app.services.mcp_tools import TOOLS_BY_NAME

    if not _mining.acquire(blocking=False):
        return False
    try:
        # Журнали Sum3D — десятки мегабайт: теж окремим процесом. Збій тут не має
        # забрати кандидатів із листів.
        try:
            disc_choice.save(mcp_isolated.run(TOOLS_BY_NAME["kmill_disc_choice"], {}, timeout=900))
        except Exception:
            logger.exception("Навчання: диски не пораховано")
        found = mcp_isolated.run(TOOLS_BY_NAME["kmill_decision_learn"], {"days": days}, timeout=900)
        decision_learn.save_found(found)
        logger.info("Навчання: листів %s, кандидатів %s", found.get("letters"), len(found.get("candidates", [])))
        return True
    finally:
        _mining.release()


def _mine_quietly() -> None:
    try:
        mine_now()
    except Exception:
        logger.exception("Навчання: перерахунок не вдався")


@router.post("/learning/recount", response_class=HTMLResponse)
def post_learning_recount(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db, loopback=False)
    context = _context(db)
    if not _mining.locked():
        threading.Thread(target=_mine_quietly, name="kuubmill-learn-mine", daemon=True).start()
    context["mining"] = True
    return templates.TemplateResponse(request, "_learning_body.html", {"user": user, **context})


@router.get("/orders/{order_id}/why", response_class=HTMLResponse)
def get_order_why(request: Request, order_id: int, db: Session = Depends(get_db)):
    if get_current_user(request, db) is None:
        return login_redirect(request)
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return templates.TemplateResponse(
        request, "_order_why.html", {"lines": decision_story.story(db, order)}
    )
