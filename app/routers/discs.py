"""Екран «Нові диски»: узяті з архіву диски й замовлення на склад.

Замінив розділ налаштувань «Заготовки» (бриф NEW_DISCS_BRIEF.md, 10.09.26).
Ліворуч — незамовлені диски по змінах, праворуч — кошик замовлення, унизу —
історія замовлень і всі створені диски. Шлях до теки CAM, «Перечитати» й
«Перевірити читання» — у нижній смузі.

Права — як були в розділі: оператор робить усе нарівні з адміном (замовлення
складу — щоденна робота того, хто за верстатом). Адмін може зачинити екран
для не-адмінів гейтом розділів, як і будь-який інший.

Домен — `app/services/cam_blanks.py` (тека, формат рядка, зміни) і
`app/services/disc_orders.py` (замовлення, скасування, Telegram); тут — HTTP.

Фрагменти, які свапає екран:
  * `#dz-work`   — ліва колонка + кошик (після замовлення, скасування,
                   перечитування; власний GET на подію `dz-work-refresh`);
  * `#dz-live`   — жива частина кошика (підсумок, групи, прев'ю, кнопки) —
                   на кожну зміну галочок, поле дописування НЕ свапається;
  * `#dz-orders` — вкладка «Історія замовлень»;
  * `#dz-all`    — вкладка «Усі створені диски» (графік + список).
Після кожної зміни замовлень відповідь шле HX-Trigger `dz-changed`, і обидві
вкладки перечитуються самі, зберігаючи свої фільтри.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_current_user, get_db, login_redirect, templates
from app.routers.section_gate import admin_banner, blocked_response
from app.services import disc_orders, telegram_bot
from app.services.cam_blanks import (
    bare_name,
    disc_needs_look,
    disc_position,
    last_scan,
    order_groups,
    order_positions,
    order_text,
    pending_blanks,
    pileup_note,
    probe_blanks,
    shift_groups,
    sync_blanks,
)
from app.settings_store import get_setting, set_setting

router = APIRouter()

SECTION = "discs"
PATH_KEY = "cam_blanks_path"


def _ids(raw: str) -> list[int]:
    """`"12,13,40"` → `[12, 13, 40]`; сміття мовчки відкидається."""
    return [int(part) for part in (raw or "").split(",") if part.strip().isdigit()]


def _user_or_none(request: Request, db: Session):
    return get_current_user(request, db)


def _path(db: Session) -> str:
    return (get_setting(db, PATH_KEY) or "").strip()


# ── Контекст ────────────────────────────────────────────────────────────────


def _scopes(pending, groups) -> list[dict]:
    """Кнопки обсягу в кошику: «Усе незамовлене» → денні → нічні (порядок
    задав власник 10.09.26)."""
    if not pending:
        return []
    oldest = min(row.first_seen_at for row in pending)
    scopes = [{
        "key": "all",
        "label": "Усе незамовлене",
        "sub": f"з {oldest:%d.%m %H:%M}",
        "ids": [row.id for row in pending],
    }]
    for kind in ("day", "night"):
        for group in groups:
            if group.shift.kind != kind:
                continue
            scopes.append({
                "key": group.shift.key,
                "label": group.shift.title,
                "sub": group.shift.when,
                "ids": [row.id for row in group.rows],
            })
    return scopes


def _live_context(db: Session, *, pending, selected, note: str, now: datetime) -> dict:
    """Жива частина кошика: що вийде з позначеного + дописаного."""
    extra = (note or "").strip()[: disc_orders.NOTE_LIMIT]
    text = order_text(selected, extra)
    blocker = telegram_bot.warehouse_blocker(db)
    return {
        "live_groups": order_groups(selected),
        "live_count": len(selected),
        "live_positions": order_positions(selected),
        "live_note": extra,
        "live_left": len(pending) - len(selected),
        "live_text": text,
        "live_preview": disc_orders.telegram_text(text, now),
        "live_now": now,
        "send_blocker": blocker,
    }


def _work_context(
    db: Session,
    *,
    on: set[int],
    note: str = "",
    notice: Optional[str] = None,
    error: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now()
    pending = pending_blanks(db)
    groups = shift_groups(pending, now=now)
    # Обране — лише те, що людина позначила (власник 11.09.26: галочки за
    # замовчуванням зняті). Новий диск, що зʼявився під час роботи, теж
    # приходить без галочки — тому памʼятаємо ОБРАНЕ, а не зняте.
    selected = [row for row in pending if row.id in on]
    warehouse = telegram_bot.warehouse_members(db)
    latest = disc_orders.latest_active(db)
    ctx = {
        "pending": pending,
        "shift_groups": groups,
        "on_ids": on,
        "scopes": _scopes(pending, groups),
        "note": note,
        "fresh": disc_orders.fresh_order(db, now=now),
        "recent_notes": disc_orders.recent_notes(db),
        "burs": disc_orders.burs_rows(),
        "warehouse_names": [telegram_bot.member_title(member) for member in warehouse],
        "pileup": pileup_note(pending),
        "notice": notice,
        "error": error,
        "has_path": bool(_path(db)),
        "last_order_at": latest.created_at if latest else None,
        "disc_pos": disc_position,
        "needs_look": disc_needs_look,
        "bare": bare_name,
        # Фрагмент (не сторінка) — тягне за собою out-of-band оновлення шапки.
        "oob": True,
    }
    ctx.update(_live_context(db, pending=pending, selected=selected, note=note, now=now))
    return ctx


def _page_context(request: Request, db: Session, user) -> dict:
    now = datetime.now()
    ctx = _work_context(db, on=set(), now=now)
    ctx.update(_orders_context(db))
    ctx.update(_all_context(db))
    ctx.update(_footer_context(db))
    ctx.update({
        "request": request,
        "user": user,
        "topbar_active": "discs",
        "section_banner": admin_banner(db, user, SECTION),
        # Повна сторінка — жодних out-of-band шматків: шапка вже на місці.
        "oob": False,
    })
    return ctx


def _orders_context(db: Session, limit: int = disc_orders.HISTORY_PAGE) -> dict:
    return {
        "orders": disc_orders.history(db, limit=limit),
        "orders_total": disc_orders.history_total(db),
        "orders_limit": limit,
        "oob": True,
    }


def _all_context(
    db: Session, *, mat: str = "all", st: str = "all", q: str = "", day: str = "", shown: int = 0
) -> dict:
    mat = mat or "all"
    st = st if st in ("all", "wait", "done") else "all"
    q = (q or "")[:80]
    day = day or ""
    shown = max(disc_orders.DAYS_STEP, min(int(shown or 0), 400))
    return {
        "journal": disc_orders.journal(db, mat=mat, st=st, q=q, day=day, shown=shown),
        "jf": {"mat": mat, "st": st, "q": q, "day": day, "shown": shown},
        "oob": True,
    }


def _footer_context(db: Session) -> dict:
    scan = last_scan()
    path = _path(db)
    return {
        "blanks_path": path,
        # Крапка зелена лише з підтвердження: цей процес щойно бачив теку
        # саме за цим шляхом.
        "scan": scan if scan.at is not None and scan.root == path else None,
    }


def _render(request: Request, name: str, ctx: dict, *, triggers: Optional[dict] = None) -> HTMLResponse:
    response = templates.TemplateResponse(request, name, ctx)
    if triggers:
        response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


def _toast(message: str, kind: str = "success", undo: Optional[str] = None) -> dict:
    toast = {"message": message, "kind": kind}
    if undo:
        toast["undoUrl"] = undo
    return toast


def _discs_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} диск"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} диски"
    return f"{n} дисків"


def _gate(request: Request, db: Session):
    """Користувач або готова відповідь (логін / блокатор розділу)."""
    user = _user_or_none(request, db)
    if user is None:
        return None, login_redirect(request)
    blocked = blocked_response(request, db, user, SECTION)
    if blocked is not None:
        return None, blocked
    return user, None


# ── Сторінка й фрагменти ────────────────────────────────────────────────────


@router.get("/discs", response_class=HTMLResponse)
def get_discs(request: Request, db: Session = Depends(get_db)):
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    ctx = _page_context(request, db, user)
    ctx["toast_flash"] = request.session.pop("toast_flash", None)
    return templates.TemplateResponse(request, "discs.html", ctx)


@router.get("/discs/work", response_class=HTMLResponse)
def get_discs_work(request: Request, on: str = "", note: str = "", db: Session = Depends(get_db)):
    """Ліва колонка + кошик — на подію `dz-work-refresh` (скасування з тоста).
    `on` — позначені диски, `note` — недописане: оновлення не має їх губити."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    return _render(request, "_discs_work.html", _work_context(db, on=set(_ids(on)), note=note))


@router.post("/discs/basket", response_class=HTMLResponse)
def post_discs_basket(
    request: Request, ids: str = Form(""), note: str = Form(""), db: Session = Depends(get_db)
):
    """Жива частина кошика під поточні галочки. Нічого не записує.

    Текст складає сервер, а не браузер: у буфер, у Telegram і в історію йде
    рядок з одного джерела — і прев'ю не може розійтися з тим, що піде."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    wanted = set(_ids(ids))
    pending = pending_blanks(db)
    selected = [row for row in pending if row.id in wanted]
    return _render(
        request,
        "_discs_live.html",
        _live_context(db, pending=pending, selected=selected, note=note, now=datetime.now()),
    )


@router.post("/discs/order", response_class=HTMLResponse)
def post_discs_order(
    request: Request,
    ids: str = Form(""),
    on: str = Form(""),
    note: str = Form(""),
    via: str = Form(disc_orders.VIA_MANUAL),
    db: Session = Depends(get_db),
):
    """«Надіслати на склад» (via=telegram) або «Замовлено без відправки».

    Один клік без підтверджень; «Скасувати» — одразу в тості й під кнопками.
    `ids` екран збирає з галочок У МИТЬ КЛІКУ (htmx:configRequest). Порожнє
    поле — «нічого не позначено», а не «усе» (CLAUDE.md §14). `on` —
    позначені: якщо замовлення не вийшло, екран мусить лишитись як був."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    keep = set(_ids(on))
    if via == disc_orders.VIA_TELEGRAM:
        blocker = telegram_bot.warehouse_blocker(db)
        if blocker:
            return _render(
                request, "_discs_work.html",
                _work_context(db, on=keep, note=note, error=blocker),
                triggers={"toast": _toast(blocker, "error")},
            )
    now = datetime.now()
    order = disc_orders.create_order(
        db, ids=_ids(ids), note=note, via=via, user_id=user.id, now=now
    )
    if order is None:
        db.rollback()
        if _ids(ids):
            # Сторінка застаріла: ці диски вже замовив інший оператор.
            message = "Позначені диски вже замовлені — список оновлено."
        else:
            message = "Нічого не позначено — ні дисків, ні дописаного."
        return _render(
            request, "_discs_work.html",
            _work_context(db, on=keep, note=note, error=message),
            triggers={"toast": _toast(message, "warning")},
        )
    db.commit()
    what = " + ".join(
        part for part in (
            _discs_word(order.disc_count) if order.disc_count else "",
            "дописане" if order.note else "",
        ) if part
    )
    if order.via == disc_orders.VIA_TELEGRAM:
        telegram_bot.wake_outbound()
        # Чесно: Telegram ще не прийняв — «надіслано HH:MM» зʼявиться під
        # кнопками, щойно прийме (рядок #dz-last сам себе опитує).
        message = f"Надсилаю на склад ({what}) — позначено замовленим."
    else:
        message = f"Позначено замовленим ({what}), без відправки."
    return _render(
        request, "_discs_work.html",
        _work_context(db, on=set(), now=now),
        triggers={
            "toast": _toast(message, undo=f"/discs/orders/{order.id}/cancel"),
            "dz-changed": True,
        },
    )


@router.post("/discs/orders/{order_id}/cancel")
def post_discs_cancel(
    request: Request,
    order_id: int,
    on: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Скасувати БУДЬ-ЯКЕ замовлення (рішення власника 10.09.26).

    Із кнопки на екрані (ціль `#dz-work`) — віддаємо перемальовану робочу
    зону. Із тоста («Скасувати» там шле POST без цілі) — 204 і подію
    `dz-work-refresh`, на яку зона перечитається сама."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    result = disc_orders.cancel_order(db, order_id, user_id=user.id)
    if result is None:
        db.rollback()
        message = "Це замовлення вже скасоване — список оновлено."
        kind = "info"
    else:
        db.commit()
        if result.edits:
            telegram_bot.wake_outbound()
        order = result.order
        parts = [f"Замовлення {order.created_at:%d.%m %H:%M} скасовано"]
        if result.returned:
            parts.append(f"{_discs_word(result.returned)} знову в списку")
        message = " — ".join(parts)
        if result.edits:
            message += ". У Telegram повідомлення складу буде закреслено."
        kind = "success"
    triggers = {"toast": _toast(message, kind), "dz-changed": True}
    if request.headers.get("HX-Target") == "dz-work":
        return _render(
            request, "_discs_work.html",
            _work_context(db, on=set(_ids(on)), note=note),
            triggers=triggers,
        )
    triggers["dz-work-refresh"] = True
    response = Response(status_code=204)
    response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


@router.get("/discs/orders", response_class=HTMLResponse)
def get_discs_orders(request: Request, limit: int = disc_orders.HISTORY_PAGE, db: Session = Depends(get_db)):
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    limit = max(disc_orders.HISTORY_PAGE, min(limit, 2000))
    return _render(request, "_discs_orders.html", _orders_context(db, limit))


@router.get("/discs/all", response_class=HTMLResponse)
def get_discs_all(
    request: Request,
    mat: str = "all",
    st: str = "all",
    q: str = "",
    day: str = "",
    shown: int = 0,
    db: Session = Depends(get_db),
):
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    return _render(
        request, "_discs_all.html",
        _all_context(db, mat=mat, st=st, q=q, day=day, shown=shown),
    )


@router.get("/discs/last", response_class=HTMLResponse)
def get_discs_last(request: Request, db: Session = Depends(get_db)):
    """Рядок «Надіслано на склад HH:MM» під кнопками. Поки повідомлення в
    дорозі, він сам себе опитує — «надіслано» зʼявляється, коли Telegram
    прийняв, а не коли натиснули кнопку."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    return _render(request, "_discs_last.html", {"fresh": disc_orders.fresh_order(db)})


# ── Тека ────────────────────────────────────────────────────────────────────


@router.post("/discs/rescan", response_class=HTMLResponse)
def post_discs_rescan(
    request: Request, on: str = Form(""), note: str = Form(""), db: Session = Depends(get_db)
):
    """Перечитати теку зараз. Звичайний `def` — похід у файлову систему на
    event loop пускати не можна: FastAPI віддасть його у threadpool."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    path = _path(db)
    keep = set(_ids(on))
    if not path:
        message = "Шлях до теки CAM не задано — натисніть «Шлях…» унизу."
        return _render(
            request, "_discs_work.html", _work_context(db, on=keep, note=note, error=message),
            triggers={"toast": _toast(message, "warning"), "dz-footer": True},
        )
    try:
        result = sync_blanks(db, path)
    except OSError as exc:
        message = f"Теку не вдалось прочитати: {exc}"
        return _render(
            request, "_discs_work.html", _work_context(db, on=keep, note=note, error=message),
            triggers={"toast": _toast(message, "error"), "dz-footer": True},
        )
    notice = None
    if result.missing:
        message = f"Теки «{path}» немає або вона недоступна — нічого не змінено."
        return _render(
            request, "_discs_work.html", _work_context(db, on=keep, note=note, error=message),
            triggers={"toast": _toast(message, "error"), "dz-footer": True},
        )
    if result.baseline:
        # Без цього оператор побачить порожній список і вирішить, що не працює.
        notice = (
            f"Перше читання: {result.baseline} наявних дисків узято за точку відліку — "
            "вони вже були в теці, тож замовляти їх не треба. У список потраплятиме "
            "лише те, що зʼявиться далі."
        )
        message = "Теку прочитано вперше — точку відліку поставлено."
    elif result.appeared:
        message = f"Теку перечитано: нових {_discs_word(result.appeared)}."
    else:
        message = "Теку перечитано — нових дисків немає."
    return _render(
        request, "_discs_work.html", _work_context(db, on=keep, note=note, notice=notice),
        triggers={"toast": _toast(message, "success"), "dz-changed": True, "dz-footer": True},
    )


@router.post("/discs/probe", response_class=HTMLResponse)
def post_discs_probe(request: Request, db: Session = Depends(get_db)):
    """Подивитись на теку, НІЧОГО не записавши, — звіт, який можна
    скопіювати й переслати. Звичайний `def`: це похід у файлову систему."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    path = _path(db)
    probe = probe_blanks(path) if path else None
    return _render(request, "_discs_probe.html", {"probe": probe, "blanks_path": path})


@router.get("/discs/footer", response_class=HTMLResponse)
def get_discs_footer(request: Request, db: Session = Depends(get_db)):
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    return _render(request, "_discs_footer.html", _footer_context(db))


@router.post("/discs/path")
def post_discs_path(request: Request, cam_blanks_path: str = Form(""), db: Session = Depends(get_db)):
    """Зберегти шлях до теки. Порожнє значення вимикає стеження (ключ у
    CLEARABLE_SETTING_KEYS: помилковий шлях має зніматись з екрана)."""
    user, stop = _gate(request, db)
    if stop is not None:
        return stop
    path = cam_blanks_path.strip()
    set_setting(db, PATH_KEY, path)
    db.commit()
    request.session["toast_flash"] = {
        "message": (
            "Шлях збережено. Теку прочитає фоновий прохід за кілька хвилин — або натисніть «Перечитати»."
            if path else "Шлях прибрано — стеження за текою вимкнено."
        ),
        "kind": "success",
    }
    return RedirectResponse("/discs", status_code=303)
