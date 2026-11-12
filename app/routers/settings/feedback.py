"""Звʼязок із розробником: Telegram-бот для скарг і побажань."""

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.routers.deps import get_db, require_admin_or_redirect, templates
from app.settings_store import get_setting, set_setting
from .common import require_settings_admin

router = APIRouter()


# ── Telegram-пуш форми зворотного зв'язку ──────────────────────────────────
# Окремий бот KuubMill (10.09.26: у спільного бота вхідні забирала інша
# програма) — сюди вводять лише його токен і прив'язують chat_id.
# Секрет (токен) зберігається зашифровано, як решта секретів (CLAUDE.md §7);
# порожнє поле токена означає «не міняти» — так само, як для пароля пошти.

@router.get("/settings/feedback", response_class=HTMLResponse)
def get_feedback_settings(request: Request, db: Session = Depends(get_db)):
    user = require_admin_or_redirect(request, db, loopback=False)
    if isinstance(user, RedirectResponse):
        return user

    from app.services import board_models, furnace_board, shop_board, telegram_bot

    flash = request.session.pop("feedback_settings_flash", None)
    # Лише з пам'яті: відкриття сторінки не має ходити в Telegram.
    username = telegram_bot.bot_username(db)
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
            "owner_notify": telegram_bot.owner_notify(db),
            "members": [
                {"m": m, "title": telegram_bot.member_title(m)} for m in telegram_bot.list_members(db)
            ],
            "invites": [
                {"i": i, "link": telegram_bot.invite_link(username, i.code)}
                for i in telegram_bot.active_invites(db)
            ],
            "bot_username": username,
            "board_enabled": furnace_board.board_enabled(db),
            "board_links": furnace_board.board_links(db),
            "shop_links": shop_board.shop_links(db),
            "board_status": furnace_board.status_snapshot(),
            "board_port": furnace_board.BOARD_PORT,
            "sisma_styles": shop_board.SISMA_STYLES,
            "sisma_style": shop_board.read_sisma_style(db),
            "three_d": board_models.read_3d(db),
            "three_d_fields": board_models.THREE_D_FIELDS,
            "power_group": get_setting(db, "power_group") or "",
            "power_board": (get_setting(db, "power_board_enabled") or "") == "1",
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
            "reply_markup": telegram_bot.keyboard("home", telegram_bot.owner_notify(db)),
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


# ── Учасники бота ──────────────────────────────────────────────────────────
# Людина приходить лише за одноразовим посиланням, яке створює адмін.
# Звичайні `def`, не async: «Запросити» може спитати Telegram про ім'я бота
# (getMe), а мережевий виклик на event loop заморозив би весь застосунок.


def _flash(request: Request, kind: str, message: str) -> RedirectResponse:
    request.session["feedback_settings_flash"] = {"kind": kind, "message": message}
    return RedirectResponse("/settings/feedback#bot-members", status_code=303)


@router.post("/settings/feedback/invite")
def create_bot_invite(
    request: Request,
    label: str = Form(""),
    warehouse: str = Form(""),
    db: Session = Depends(get_db),
):
    """`warehouse` — запрошення для складу: людина прийде одразу з роллю
    «лише замовлення дисків» і навіть привітання не принесе їй меню пічок."""
    user = require_settings_admin(request, db)
    from app.services import telegram_bot

    telegram_bot.new_invite(
        db,
        label=label,
        created_by_id=getattr(user, "id", None),
        role=telegram_bot.ROLE_WAREHOUSE if warehouse else "",
    )
    db.commit()
    if telegram_bot.bot_username(db, fetch=True) is None:
        return _flash(
            request, "error",
            "Запрошення створено, але Telegram не назвав ім'я бота — посилання з'явиться, "
            "щойно бот відповість (перевірте токен і зв'язок).",
        )
    return _flash(
        request, "success",
        "Посилання готове — скопіюйте нижче й надішліть людині. Одноразове, діє 24 год.",
    )


@router.post("/settings/feedback/invite/{invite_id}/revoke")
def revoke_bot_invite(request: Request, invite_id: int, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    from datetime import datetime

    from app.models import TelegramInvite

    invite = db.get(TelegramInvite, invite_id)
    if invite is not None and invite.used_at is None:
        invite.revoked_at = datetime.now()
        db.commit()
    return _flash(request, "success", "Запрошення скасовано — посилання більше не діє.")


@router.post("/settings/feedback/member/{member_id}/remove")
def remove_bot_member(request: Request, member_id: int, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    from app.models import TelegramMember
    from app.services import telegram_bot

    member = db.get(TelegramMember, member_id)
    if member is None:
        return _flash(request, "error", "Такого учасника вже немає.")
    title = telegram_bot.member_title(member)
    db.delete(member)
    db.commit()
    return _flash(request, "success", f"{title}: доступ до бота закрито.")


@router.post("/settings/feedback/member/{member_id}/notify")
def toggle_bot_member_notify(request: Request, member_id: int, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    from app.models import TelegramMember
    from app.services import telegram_bot

    member = db.get(TelegramMember, member_id)
    if member is None:
        return _flash(request, "error", "Такого учасника вже немає.")
    member.notify = not member.notify
    db.commit()
    state = "увімкнено" if member.notify else "вимкнено"
    return _flash(request, "success", f"{telegram_bot.member_title(member)}: сповіщення {state}.")


# ── Табло пічок для логістів (порт 8010, мережа цеху) ─────────────────────


def _board_flash(request: Request, kind: str, message: str) -> RedirectResponse:
    request.session["feedback_settings_flash"] = {"kind": kind, "message": message}
    return RedirectResponse("/settings/feedback#furnace-board", status_code=303)


@router.post("/settings/feedback/board/toggle")
def toggle_furnace_board(request: Request, db: Session = Depends(get_db)):
    """Увімкнути/вимкнути табло. Вимкнене не слухає порт узагалі; сторож
    табло (web.py) відкриває чи закриває його за кілька секунд."""
    require_settings_admin(request, db)
    from app.services import furnace_board

    on = not furnace_board.board_enabled(db)
    if on and not furnace_board.board_token(db):
        furnace_board.regenerate_token(db)
    set_setting(db, furnace_board.BOARD_ENABLED_KEY, "1" if on else "")
    db.commit()
    if on:
        return _board_flash(
            request, "success",
            f"Табло вмикається на порту {furnace_board.BOARD_PORT} — посилання нижче. Якщо Windows "
            "спитає про доступ до мережі, дозвольте «Приватні мережі».",
        )
    return _board_flash(request, "success", "Табло вимкнено — порт закрито, посилання не працює.")


@router.post("/settings/feedback/board/regenerate")
def regenerate_furnace_board_link(request: Request, db: Session = Depends(get_db)):
    """Нове посилання; старе одразу перестає працювати (напр. якщо його
    переслали не тим людям)."""
    require_settings_admin(request, db)
    from app.services import furnace_board

    furnace_board.regenerate_token(db)
    db.commit()
    return _board_flash(request, "success", "Нове посилання готове — старе більше не відкривається.")


@router.post("/settings/feedback/board/power")
def save_power_schedule(request: Request, group: str = Form(""), board: str = Form(""),
                        db: Session = Depends(get_db)):
    """Графік відключень світла: номер черги й смуга на табло (власник 11.11.26).

    Форма шле обидва поля одним «Зберегти», тож порожній прапорець тут — це
    справді «вимкнути смугу», а порожня черга — «прибрати віджет звідусіль».
    Адреса не питається й не зберігається: графік іде по черзі."""
    require_settings_admin(request, db)
    from app.services import power_schedule

    clean = power_schedule.clean_group(group)
    if group.strip() and not clean:
        return _board_flash(request, "error", "Номер черги має вигляд 4.1 — як на сайті ДТЕК.")
    on_board = board == "1"
    set_setting(db, power_schedule.GROUP_KEY, clean)
    set_setting(db, power_schedule.BOARD_KEY, "1" if on_board else "")
    db.commit()
    power_schedule.configure(clean, on_board)
    if not clean:
        return _board_flash(request, "success", "Чергу прибрано — віджета «Світло» більше немає ніде.")
    return _board_flash(
        request, "success",
        f"Черга {clean} збережена. Графік зʼявиться за кілька секунд; смуга на табло цеху "
        + ("увімкнена." if on_board else "вимкнена."),
    )


@router.post("/settings/feedback/board/sisma-style")
def save_sisma_style(request: Request, style: str = Form(...), db: Session = Depends(get_db)):
    """Що малює плитка SISMA на табло цеху (власник 12.10.26). Плитку, футер
    і правило «рух лише під час друку» стиль не міняє — лише картинку в
    полотні. Зберігається власне слово стилю, ніколи не порожнє."""
    require_settings_admin(request, db)
    from app.services import shop_board

    names = {key: name for key, name, _ in shop_board.SISMA_STYLES}
    if style not in names:
        return _board_flash(request, "error", "Такого вигляду SISMA немає.")
    set_setting(db, shop_board.SISMA_STYLE_KEY, style)
    db.commit()
    return _board_flash(
        request, "success",
        f"SISMA на табло: «{names[style]}». Телевізор підхопить з наступним оновленням (до 30 с).",
    )


PREVIEW_MODEL_BASE = "/settings/feedback/board/3d/model/"


@router.get("/settings/feedback/board/3d/preview", response_class=HTMLResponse)
def board_3d_preview(request: Request, db: Session = Depends(get_db)):
    """Справжнє табло цеху для прев'ю 3D у Налаштуваннях (власник 12.11.26).

    Той самий шаблон і ті самі дані, що на телевізорі, лише без живого
    оновлення; моделі — через адмінський маршрут нижче. Жоден верстат не
    фрезерує — першій плитці підставляємо найбільшу підготовлену роботу,
    щоб було що налаштовувати. Форма шле значення прямо в
    `KM3D.setConfig` цієї сторінки (той самий origin)."""
    require_settings_admin(request, db)
    from app.services import board_models, shop_board

    view = shop_board.shop_view(db, token="preview")
    for m in view.machines:
        for model in m.models:
            model["url"] = model["url"].replace("/t/preview/model/", PREVIEW_MODEL_BASE)
        if m.portrait.startswith("/t/preview/portrait/"):
            m.portrait = m.portrait.replace("/t/preview/portrait/", "/machines/portrait/")
    if not any(m.models and m.tone == "run" for m in view.machines):
        demo = board_models.preview_models(PREVIEW_MODEL_BASE)
        if demo:
            if not view.machines:
                # Верстатів не налаштовано (новий ПК, dev): сітка як на ТВ,
                # інакше одна плитка на весь екран і пропорції не ті.
                for i in range(8):
                    view.machines.append(shop_board.ShopMachine(
                        key=f"preview-{i}", name=f"Верстат {i + 1}", state="idle", tone="calm", word="стоїть"))
            target = view.machines[0]
            target.state, target.tone, target.word, target.note = "run", "run", "фрезерує", ""
            target.percent = target.percent or 40
            target.action, target.works_are_last = "", False
            if not target.works:
                target.works = [shop_board.ShopWork(name="Прев'ю", material="mono a2 · 3")]
            if not target.has_prog:
                target.prog_head, target.prog_tail = "Monolith-a2-x12_", "09-47-12"
            target.models = demo
    return templates.TemplateResponse(request, "shop_board.html", {
        "view": view, "token": "preview", "refresh": 30, "build": "", "preview": True,
    })


@router.get("/settings/feedback/board/3d/model/{key}.json")
def board_3d_preview_model(request: Request, key: str, db: Session = Depends(get_db)):
    """Модель для прев'ю — той самий кеш, що табло на 8010 віддає за секретом."""
    require_settings_admin(request, db)
    from fastapi.responses import FileResponse, PlainTextResponse

    from app.services import board_models

    path = board_models.cached_file(key)
    if path is None:
        return PlainTextResponse("Не знайдено", status_code=404)
    return FileResponse(path, media_type="application/json", headers={"Content-Encoding": "gzip"})


@router.post("/settings/feedback/board/3d")
async def save_board_3d(request: Request, db: Session = Depends(get_db)):
    """3D на плитках табло цеху: перемикачі й розміри (власник 12.11.26).
    Форма шле УСІ поля разом, тож відсутня галочка = вимкнено. «Типові» —
    кнопка `reset`. Телевізор бере значення з живого шматка за ≤30 с."""
    require_settings_admin(request, db)
    import json

    from app.services import board_models

    form = await request.form()
    if form.get("reset"):
        values = board_models.clean_3d({})
    else:
        raw: dict = {}
        for f in board_models.THREE_D_FIELDS:
            raw[f.key] = bool(form.get(f.key)) if f.is_switch else form.get(f.key)
        values = board_models.clean_3d(raw)
    set_setting(db, board_models.THREE_D_KEY, json.dumps(values, ensure_ascii=False))
    db.commit()
    tail = "Телевізор підхопить з наступним оновленням (до 30 с)."
    if form.get("reset"):
        return _board_flash(request, "success", "3D на табло: повернуто типові. " + tail)
    if not values["on"]:
        return _board_flash(request, "success", "3D на табло вимкнено, моделі у фоні не готуються. " + tail)
    return _board_flash(request, "success", "3D на табло збережено. " + tail)


@router.post("/settings/feedback/member/{member_id}/warehouse")
def toggle_bot_member_warehouse(request: Request, member_id: int, db: Session = Depends(get_db)):
    """Зробити учасника складом (лише замовлення дисків з екрана «Нові
    диски») або повернути звичайним учасником."""
    require_settings_admin(request, db)
    from app.models import TelegramMember
    from app.services import telegram_bot

    member = db.get(TelegramMember, member_id)
    if member is None:
        return _flash(request, "error", "Такого учасника вже немає.")
    to_warehouse = member.role != telegram_bot.ROLE_WAREHOUSE
    member.role = telegram_bot.ROLE_WAREHOUSE if to_warehouse else ""
    db.commit()
    title = telegram_bot.member_title(member)
    if to_warehouse:
        return _flash(
            request, "success",
            f"{title}: тепер склад — отримує лише замовлення дисків, без меню й сповіщень печей.",
        )
    return _flash(request, "success", f"{title}: знову звичайний учасник — меню пічок і Sisma.")
