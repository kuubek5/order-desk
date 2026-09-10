"""Розділ «Виробіток»: місячний облік виготовлених одиниць для нарахування ЗП.

Окремий екран (не «Статистика»): таблиця днів × колонок, CRM рахує сама,
оператор може виправити будь-яку клітинку. Доменна логіка (правила підрахунку,
гроші, знімок авто) — в app/services/vyrobitok.py; тут лише HTTP: збір періоду,
ПІН-гейт розділу й точкові збереження клітинок/налаштувань.

ПІН-гейт: один код на розділ. Правильний код відкриває розділ на обмежений час
(`_PIN_TTL_SECONDS`, зараз година), а не «до кінця сесії»: спільний цеховий ПК
не має лишатись відчиненим після того, як людина відійшла. Кнопка «Замкнути»
на сторінці скидає дозвіл негайно — наступний вхід знову просить код. Термін
дії живе в підписаному session-cookie як мітка часу закінчення (підробити не
можна). Код лежить у налаштуваннях (`vyrobitok_pin`, зашифровано); поки не
заданий — розділ відкритий будь-якому оператору, що ввійшов.
"""

import secrets
import time
from datetime import date

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import sync_control
from app.business_day import business_today
from app.auth import verify_password
from app.routers.section_gate import blocked_response
from app.routers.deps import get_current_user, login_redirect, get_db, templates
from app.services.attempt_limit import block_message, pin_limiter
from app.sheet_sync_service import SheetSyncError, sync_google_sheets
from app.sheets import tab_name_for
from app.services.vyrobitok import (
    HUE,
    MATERIAL_COLS,
    OPAK_PEOPLE,
    clear_day_overrides,
    compute_month,
    resync_guard,
    save_month_settings,
    set_cell,
    unfreeze_day,
)
from app.settings_store import get_setting

router = APIRouter()

# Мітка часу (epoch-секунди) закінчення дозволу в session. Мітка, а не «True»:
# дозвіл сам протухає, щоб відкритий на спільному ПК розділ не лишався
# відчиненим після того, як людина відійшла.
_PIN_SESSION_KEY = "vyrobitok_pin_until"
# Скільки триває дозвіл після правильного коду. Година — за проханням власника.
_PIN_TTL_SECONDS = 3600


def _pin_matches(entered: str, stored: str) -> bool:
    """Чи збігається введений код зі збереженим.

    Нові коди лежать ХЕШЕМ. Старі — відкритим текстом (під шифруванням
    налаштувань), і їх треба приймати далі: інакше оновлення замкнуло б розділ
    для того, хто код і задавав. Відкритий шлях лишається з `compare_digest`,
    щоб час порівняння не залежав від того, скільки перших цифр збіглося.
    """
    if not entered or not stored:
        return False
    if stored.startswith("$"):        # формат хеша passlib/bcrypt
        return verify_password(entered, stored)
    return secrets.compare_digest(entered, stored)


def _pin_unlocked(request: Request) -> bool:
    """Чи чинний ще дозвіл на розділ у цій сесії (введений код не протух)."""
    until = request.session.get(_PIN_SESSION_KEY)
    return isinstance(until, (int, float)) and time.time() < until


def _pin_required(request: Request, db: Session) -> bool:
    """Чи треба показати ПІН-екран замість табеля."""
    pin = get_setting(db, "vyrobitok_pin")
    if not pin:
        return False
    return not _pin_unlocked(request)


def _clamp_period(year: int | None, month: int | None) -> tuple[int, int]:
    today = business_today()
    y = year if year is not None else today.year
    m = month if month is not None else today.month
    if m < 1 or m > 12:
        m = today.month
    # Розумна межа років — таблиця не існувала до 2024 і навряд переживе 2100.
    if y < 2024 or y > 2100:
        y = today.year
    return y, m


def _grid_context(db: Session, user, year: int, month: int, *, persist: bool = True) -> dict:
    grid = compute_month(db, year, month, persist=persist)
    return {
        "user": user,
        "grid": grid,
        "material_cols": MATERIAL_COLS,
        "opak_people": OPAK_PEOPLE,
        "hue": HUE,
    }


@router.get("/vyrobitok", response_class=HTMLResponse)
def get_vyrobitok(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    # Розділ може бути зачинений адміністратором (Налаштування → Доступ до
    # розділів): не-адмін бачить екран-блокатор, адмін — сам розділ.
    blocked = blocked_response(request, db, user, "vyrobitok")
    if blocked is not None:
        return blocked

    if _pin_required(request, db):
        return templates.TemplateResponse(
            request, "vyrobitok.html", {"user": user, "pin_required": True}
        )

    y, m = _clamp_period(year, month)
    context = _grid_context(db, user, y, m)
    context["pin_required"] = False
    # Кнопку «Замкнути» показуємо лише коли розділ реально під кодом.
    context["pin_protected"] = bool(get_setting(db, "vyrobitok_pin"))
    return templates.TemplateResponse(request, "vyrobitok.html", context)


@router.post("/vyrobitok/pin", response_class=HTMLResponse)
def post_vyrobitok_pin(
    request: Request, pin: str = Form(""), db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    # Ключ ліміту — сесія браузера: розділ ховає зарплатні цифри від самих
    # операторів, тож рахуємо спроби того, хто сидить за цим ПК, а не IP
    # (він у всіх один — 127.0.0.1).
    limiter_key = f"vyrobitok:{request.session.get('user_id')}"
    wait = pin_limiter.retry_after(limiter_key)
    if wait:
        return templates.TemplateResponse(
            request,
            "vyrobitok.html",
            {"user": user, "pin_required": True, "pin_error": block_message(wait)},
            status_code=429,
        )

    expected = get_setting(db, "vyrobitok_pin")
    if expected and _pin_matches(pin.strip(), expected.strip()):
        pin_limiter.reset(limiter_key)
        request.session[_PIN_SESSION_KEY] = time.time() + _PIN_TTL_SECONDS
        y, m = _clamp_period(None, None)
        context = _grid_context(db, user, y, m)
        context["pin_required"] = False
        context["pin_protected"] = True
        return templates.TemplateResponse(request, "vyrobitok.html", context)

    blocked = pin_limiter.register_failure(limiter_key)
    return templates.TemplateResponse(
        request,
        "vyrobitok.html",
        {
            "user": user,
            "pin_required": True,
            "pin_error": block_message(blocked) if blocked else "Невірний код",
        },
        status_code=400,
    )


@router.post("/vyrobitok/day-sync", response_class=HTMLResponse)
def post_vyrobitok_day_sync(
    request: Request, day: str = Form(...), db: Session = Depends(get_db)
):
    """Перечитати вкладку ОДНОГО дня й перерахувати його начисто.

    Звичайний (`def`) роут, а не `async`: синк ходить у мережу й блокує потік —
    на event loop його пускати не можна (CLAUDE.md §14).

    Це та сама ручна синхронізація, лише звужена до однієї вкладки
    (`include_tabs`), тож усі перевірки й запобіжники ті самі: структура
    вкладки, кольори заливки, запис СЛМ, поріг масової архівації. Дія свідома,
    точкова й дешева (одне читання, ~3 с на теплому кеші) — тому вона ЄДИНА,
    що знімає заморозку дня: якщо оператор знає, що таблиця ціла, а знімок ні,
    він каже це прямо. Фоновий і повний синк заморожений день не чіпають.

    Двостороння: якщо рядок із тієї вкладки в таблиці вже видалили, робота піде
    в архів так само, як при звичайному ручному синку того дня.

    «Начисто» означає і скидання ручних правок авто-колонок дня
    (`clear_day_overrides`): правка стоїть ПОВЕРХ auto_value, тож без цього
    свіже число лишалось би невидимим і кнопка виглядала б мовчазною.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if _pin_required(request, db):
        return RedirectResponse("/vyrobitok", status_code=303)

    try:
        d = date.fromisoformat(day)
    except ValueError:
        return HTMLResponse("невірна дата", status_code=422)

    # Синк призупинений (перемикач у /sync або трей) — той самий гейт, що й
    # ручний "/sheets/sync" (app/routers/queue.py): пауза зупиняє ЙОГО читання
    # й запис, точковий день-синк не має бути дірою повз неї (F6, аудит
    # 06.09.26). Не розморожуємо день і не читаємо таблицю — просто кажемо чому.
    if sync_control.is_paused():
        context = _grid_context(db, user, d.year, d.month)
        context["day_sync_error"] = (
            "Синхронізацію призупинено. Зніміть паузу, щоб синхронізувати."
        )
        return templates.TemplateResponse(
            request, "_vyrobitok_body.html", context, status_code=409,
        )

    # Розморожуємо ДО синку: інакше запис СЛМ упреться в заморозку й тихо
    # пропустить число, заради якого синк і запускали.
    error: str | None = None
    cleared = 0
    with resync_guard(d):
        unfreeze_day(db, d)
        try:
            sync_google_sheets(db, trigger="manual", include_tabs={tab_name_for(d)})
        except SheetSyncError as exc:
            error = str(exc)
        else:
            # Правки поверх авто-колонок стираємо ЛИШЕ після успішного читання:
            # інакше свіже число сховалось би під старим ручним і кнопка
            # виглядала б мовчазною (рішення власника 09.09.26), а на впалому
            # синку оператор втратив би і правку, і перерахунок — обидва числа
            # разом. Ручні колонки не чіпаємо ніколи: там авто немає.
            cleared = clear_day_overrides(db, d)

    context = _grid_context(db, user, d.year, d.month)
    context["day_sync_error"] = error
    if error is None and cleared:
        context["day_sync_note"] = (
            f"День {d.strftime('%d.%m')} перераховано начисто; "
            f"ручних правок скинуто: {cleared}."
        )
    return templates.TemplateResponse(request, "_vyrobitok_body.html", context)


@router.post("/vyrobitok/lock")
def post_vyrobitok_lock(request: Request, db: Session = Depends(get_db)):
    """Замкнути розділ негайно: скинути дозвіл, наступний вхід знову просить код.
    Для спільного ПК — щоб людина, відходячи, не лишала табель відкритим."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    request.session.pop(_PIN_SESSION_KEY, None)
    return RedirectResponse("/vyrobitok", status_code=303)


def _parse_cell_value(raw: str) -> int | None:
    """Порожньо → None (повернути авто). Інакше ціле ≥ 0; сміття → None."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        n = int(text)
    except ValueError:
        return None
    return max(0, n)


@router.post("/vyrobitok/cell", response_class=HTMLResponse)
def post_vyrobitok_cell(
    request: Request,
    day: str = Form(...),
    col_key: str = Form(...),
    value: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    try:
        d = date.fromisoformat(day)
    except ValueError:
        return HTMLResponse("невірна дата", status_code=422)

    try:
        set_cell(db, d, col_key, _parse_cell_value(value))
    except ValueError:
        return HTMLResponse("невідома колонка", status_code=422)

    # Перерахунок і повний перемал тіла: підсумки й гроші залежать від цієї
    # клітинки, а change спрацьовує на blur — фокусу в клітинці вже немає, тож
    # свап тіла нічого не рве.
    context = _grid_context(db, user, d.year, d.month)
    return templates.TemplateResponse(request, "_vyrobitok_body.html", context)


@router.post("/vyrobitok/settings", response_class=HTMLResponse)
def post_vyrobitok_settings(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    kurs: str = Form(""),
    people_count: int = Form(5),
    rate_override: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    y, m = _clamp_period(year, month)
    save_month_settings(
        db, y, m, kurs=kurs, people_count=people_count, rate_override=rate_override
    )
    context = _grid_context(db, user, y, m)
    return templates.TemplateResponse(request, "_vyrobitok_body.html", context)
