"""Звʼязок із розробником: Telegram-бот для скарг і побажань."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.routers.deps import get_db, require_admin_or_redirect, templates
from app.settings_store import get_setting, set_setting
from .common import require_settings_admin

router = APIRouter()


# ── Telegram-пуш форми зворотного зв'язку ──────────────────────────────────
# Бот уже є (VARTAAIR) — сюди вводять лише його токен і прив'язують chat_id.
# Секрет (токен) зберігається зашифровано, як решта секретів (CLAUDE.md §7);
# порожнє поле токена означає «не міняти» — так само, як для пароля пошти.

@router.get("/settings/feedback", response_class=HTMLResponse)
def get_feedback_settings(request: Request, db: Session = Depends(get_db)):
    user = require_admin_or_redirect(request, db, loopback=False)
    if isinstance(user, RedirectResponse):
        return user

    from app.services import telegram_bot

    flash = request.session.pop("feedback_settings_flash", None)
    return templates.TemplateResponse(
        request,
        "settings_feedback.html",
        {
            "page_title": "Зворотний зв'язок",
            "user": user,
            # Токен не віддаємо в контекст — лише ознаку «збережено».
            "token_saved": bool((get_setting(db, "telegram_bot_token") or "").strip()),
            "chat_id": get_setting(db, "telegram_chat_id") or "",
            "push_enabled": (get_setting(db, "feedback_telegram_enabled") or "") == "1",
            "bot_enabled": (get_setting(db, "telegram_bot_enabled") or "") == "1",
            "bot_status": telegram_bot.status_snapshot(),
            "bot_outbox": telegram_bot.outbox_summary(db),
            "flash": flash,
        },
    )


@router.post("/settings/feedback")
async def save_feedback_settings(request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    form = await request.form()
    token = (form.get("telegram_bot_token") or "").strip()
    chat_id = (form.get("telegram_chat_id") or "").strip()
    enabled = "1" if form.get("feedback_telegram_enabled") else ""
    bot_enabled = "1" if form.get("telegram_bot_enabled") else ""

    # Порожній токен = не міняти (він рендериться порожнім навмисно, як пароль).
    if token:
        set_setting(db, "telegram_bot_token", token)
    set_setting(db, "telegram_chat_id", chat_id)
    set_setting(db, "feedback_telegram_enabled", enabled)
    set_setting(db, "telegram_bot_enabled", bot_enabled)
    db.commit()

    request.session["feedback_settings_flash"] = {
        "kind": "success",
        "message": "Збережено.",
    }
    return RedirectResponse("/settings/feedback", status_code=303)


@router.post("/settings/feedback/bind")
def bind_feedback_chat(request: Request, db: Session = Depends(get_db)):
    """Спіймати chat_id останнього, хто написав боту (getUpdates), і зберегти.

    Бот не може написати першим — оператор пише боту /start, тисне цю кнопку."""
    require_settings_admin(request, db)
    from app.services.telegram import discover_chat_id

    chat_id, error = discover_chat_id(db)
    if chat_id is None:
        request.session["feedback_settings_flash"] = {
            "kind": "error",
            "message": error or "не вдалось знайти чат",
        }
    else:
        set_setting(db, "telegram_chat_id", chat_id)
        db.commit()
        request.session["feedback_settings_flash"] = {
            "kind": "success",
            "message": f"Прив'язано чат {chat_id}.",
        }
    return RedirectResponse("/settings/feedback", status_code=303)


@router.post("/settings/feedback/test")
def test_feedback_push(request: Request, db: Session = Depends(get_db)):
    """Надіслати тестове повідомлення в Telegram — перевірити токен і chat_id.

    Коли бот увімкнено, тестом іде саме головне меню з кнопками: це і
    перевірка зв'язку, і перша точка входу — далі в чаті лише тиснути."""
    require_settings_admin(request, db)
    from app.services import telegram_bot
    from app.services.telegram import KMILL_PREFIX, api_call, get_bot_token, get_chat_id, _new_session

    token = get_bot_token(db)
    chat_id = get_chat_id(db)
    if not token or not chat_id:
        request.session["feedback_settings_flash"] = {
            "kind": "error",
            "message": "Спершу збережіть токен і прив'яжіть чат.",
        }
        return RedirectResponse("/settings/feedback", status_code=303)

    if telegram_bot.bot_enabled(db):
        payload = {
            "chat_id": chat_id,
            "text": telegram_bot.render(db, "home"),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": telegram_bot.keyboard("home"),
        }
    else:
        payload = {"chat_id": chat_id, "text": f"{KMILL_PREFIX}: тестове повідомлення ✓"}
    try:
        session = _new_session()
        try:
            result = api_call(session, token, "sendMessage", payload)
            ok, err = result.ok, result.error
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc).replace(token, "***")

    request.session["feedback_settings_flash"] = {
        "kind": "success" if ok else "error",
        "message": "Надіслано — перевірте Telegram." if ok else f"Не вдалось: {err}",
    }
    return RedirectResponse("/settings/feedback", status_code=303)
