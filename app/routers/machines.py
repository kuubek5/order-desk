"""Екран «Верстати» — живі кадри екранів RemiCORE, тільки перегляд.

Фаза 1: кадр раз на кілька секунд, без розпізнавання чисел. Це вже закриває
головний сценарій — «глянути, що на верстаті, не відкриваючи RustDesk».
Фаза 2 (OCR відсотка/часу/програми) додасться поверх цих самих кадрів.

Керування верстатом відсутнє свідомо й повністю: знімок іде через
app/furnace_vnc.py, який фізично не вміє слати ввід (перевірено стендом).
Доменна логіка — app/services/machines.py; тут лише HTTP.
"""

import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.machine_portraits import portrait_path
from app.services import machine_link
from app.settings_store import get_machine_calibration_path
from app.routers.deps import get_current_user, login_redirect, get_db, is_loopback_request, templates
from app.services.machines import (
    POLL_INTERVAL_SECONDS,
    calibration_status,
    calibration_zip_bytes,
    configured_targets,
    day_timeline,
    machine_side_context,
    sisma_context,
    poll_all,
    resolve_frame,
    snapshot,
)

router = APIRouter()


def _context(request: Request, db: Session, user) -> dict:
    return {
        "request": request,
        "user": user,
        "topbar_active": "machines",
        "cards": snapshot(db),
        "poll_seconds": int(POLL_INTERVAL_SECONDS),
        # Банер калібрування: показується, лише доки шрифт підпису неповний.
        "calibration": calibration_status(get_machine_calibration_path(db)),
    }


@router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    return templates.TemplateResponse(request, "machines.html", _context(request, db, user))


# Оголошено ВИЩЕ за адреси з `{key}` — те саме правило, що з паролем печей:
# FastAPI бере перший збіг, і літерал, який стоїть нижче за шаблон, ризикує
# бути зʼїденим як ідентифікатор.
#
# Вікна: доба (комірка = година) і 7/30 днів (комірка = день). Одна смуга на
# верстат, вирівняні по часу — і питання «рвалось у всіх одразу чи в одного»
# читається оком, без жодного запиту. Одночасно = мережа чи живлення,
# поодинці = той конкретний ПК; без цього розрізнення шукають не там.
_DIAG_WINDOWS = {1: 24, 7: 7, 30: 30}


@router.get("/machines/diag", response_class=HTMLResponse)
def machines_diag(request: Request, days: int = 1, db: Session = Depends(get_db)):
    """Журнал обривів зв'язку: смуга «коли», підсумок «хто» і розбір «чому»."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    days = days if days in _DIAG_WINDOWS else 1
    cells = _DIAG_WINDOWS[days]
    # Час у журналі локальний (як у показаннях обладнання) — межі рахуємо тим
    # самим годинником, інакше комірки зʼїхали б на три години.
    now = datetime.now()
    if days == 1:
        # Рівно по годинах: смуга доби, у якій «03:00» це справді третя година.
        end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        step = timedelta(hours=1)
    else:
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        step = timedelta(days=1)
    edges = [end - step * (cells - i) for i in range(cells + 1)]

    views = machine_link.load_outages(db, since=edges[0])
    summaries = machine_link.summarize(views)
    # Верстати, що НЕ рвались, теж мають бути в таблиці: порожня смуга — це
    # відповідь («цей тримає»), а відсутній рядок читався б як «його нема».
    known = {s.host for s in summaries}
    for target in configured_targets(db):
        if target.key not in known:
            summaries.append(
                machine_link.MachineSummary(
                    host=target.key, name=target.name, deep=target.diagnose_link
                )
            )
    machine_link.bucket_grid(views, summaries, edges=edges)

    return templates.TemplateResponse(
        request,
        "machines_diag.html",
        {
            "request": request,
            "user": user,
            "topbar_active": "machines",
            "days": days,
            "edges": edges,
            "step_hours": days == 1,
            "views": views,
            "summaries": summaries,
            "probe_words": machine_link.PROBE_WORDS,
        },
    )


@router.get("/machines/side", response_class=HTMLResponse)
def machines_side(request: Request, db: Session = Depends(get_db)):
    """Секція «Верстати» в бічній панелі черги — власний 30-секундний годинник.

    Читає ЛИШЕ стан у пам'яті процесу: до верстата з потоку запиту не ходимо,
    бо мовчазний ПК тримав би чергу двадцять секунд. Обгортку віддаємо ЗАВЖДИ
    (навіть без верстатів), інакше елемент зник би з DOM разом зі своїм поллом.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(
        request, "_machine_side.html", machine_side_context(db)
    )


@router.get("/machines/strip", response_class=HTMLResponse)
def machines_strip(request: Request, db: Session = Depends(get_db)):
    """Стрічка «назва + %» над чергою — власний 10-секундний годинник.

    Той самий контракт, що в /machines/side: лише пам'ять процесу, обгортка
    завжди, контекст з machine_side_context — два входи не розійдуться.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(
        request, "_machine_strip.html", machine_side_context(db)
    )


@router.get("/machines/sisma", response_class=HTMLResponse)
def machines_sisma(request: Request, db: Session = Depends(get_db)):
    """Віджет SLM-принтера над чергою — власний 15-секундний годинник.

    Оголошено ВИЩЕ `/machines/{key}/frame.png`: параметричний роут з'їв би
    «sisma» як ключ верстата (та сама пастка, що з zip і банером).

    Той самий контракт, що в /machines/strip: лише пам'ять процесу, обгортка
    віддається завжди — інакше фрагмент зник би з DOM разом зі своїм поллом.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(request, "_sisma_widget.html", sisma_context(db))


@router.get("/machines/cards", response_class=HTMLResponse)
def machines_cards(request: Request, db: Session = Depends(get_db)):
    """Фрагмент для полла HTMX — повний свап безпечний: на екрані немає ні
    полів вводу, ні дій над рядком (та сама причина, що на пічках)."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(request, "_machine_cards.html", _context(request, db, user))


# Мінімальна пауза між ручними знімками. Кнопка «Оновити зараз» одразу йде в
# мережу до КОЖНОГО верстата; без паузи затиснутий Enter на ній перетворював
# застосунок на генератор запитів до цехових ПК, а кожен зайвий обхід ще й
# відбирає потоки в планового опитування (ревʼю 07.09.26, LOW).
MANUAL_REFRESH_COOLDOWN_SECONDS = 3.0
_last_manual_refresh = 0.0
_manual_refresh_lock = threading.Lock()


@router.post("/machines/refresh", response_class=HTMLResponse)
def machines_refresh(request: Request, db: Session = Depends(get_db)):
    """Зняти кадри просто зараз. Це ЧИТАННЯ, а не дія над верстатом."""
    global _last_manual_refresh
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    now = time.monotonic()
    with _manual_refresh_lock:
        too_soon = now - _last_manual_refresh < MANUAL_REFRESH_COOLDOWN_SECONDS
        if not too_soon:
            _last_manual_refresh = now
    # У паузі просто віддаємо поточні картки: екран однаково оновлюється, а
    # верстати не отримують другого обходу за секунду. Пояснювати оператору
    # нема чого — для нього це той самий свіжий стан.
    if not too_soon:
        poll_all(db)
    return templates.TemplateResponse(request, "_machine_cards.html", _context(request, db, user))


@router.get("/machines/calibration/banner", response_class=HTMLResponse)
def machines_calibration_banner(request: Request, db: Session = Depends(get_db)):
    """Свіжий банер калібрування — власний полл, бо він поза `#machine-cards`.

    Оголошено ВИЩЕ `/machines/{key}/frame.png` з тієї ж причини, що й zip:
    параметричний роут з'їв би «calibration» як ключ верстата.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        # Не помилка, а порожнє місце: фрагмент службовий, і оператор його
        # ніде не запитує — але й 403 у полл-фрагменті був би шумом у консолі.
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "_machine_calibration_banner.html",
        {
            "request": request,
            "user": user,
            "calibration": calibration_status(get_machine_calibration_path(db)),
        },
    )


@router.get("/machines/calibration.zip")
def machines_calibration_zip(request: Request, db: Session = Depends(get_db)):
    """Скачати всі зібрані калібрувальні кадри одним zip.

    Оголошено ВИЩЕ за `/machines/{key}/frame.png`: FastAPI приміряє роути в
    порядку оголошення, і параметричний з'їв би «calibration» як ключ (та сама
    пастка, що з паролем печі — є тест-сторож).

    Адмін + лише з цього ПК: це обслуговуюча дія над локальним диском, як і
    решта дій рівня машини (відкрити теку, оновлення)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    data = calibration_zip_bytes(get_machine_calibration_path(db))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="calibration_frames.zip"'},
    )


@router.get("/machines/portrait/{machine_id}.jpg")
def machine_portrait(request: Request, machine_id: int, db: Session = Depends(get_db)):
    """Фото верстата, завантажене в Налаштуваннях. Шлях будується з числа,
    а не з рядка запиту; немає файлу — 404, картка тоді бере дефолт моделі."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    path = portrait_path(machine_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="фото немає")
    # У URL є mtime (?v=), тому кешувати можна довго: нове фото = новий URL.
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/machines/{key}/history", response_class=HTMLResponse)
def machine_history(request: Request, key: str, db: Session = Depends(get_db)):
    """Стрічка «працює/стоїть» верстата за останню добу.

    Читає ЛИШЕ пам'ять процесу (як /machines/side): показання верстатів у базу
    не пишуться взагалі, тож історія тут — від старту застосунку, і шаблон
    каже це прямо. До верстата з потоку запиту не ходимо.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    target = next((t for t in configured_targets(db) if t.key == key), None)
    if target is None:
        raise HTTPException(status_code=404, detail="невідомий верстат")
    return templates.TemplateResponse(
        request,
        "_machine_history.html",
        {
            "request": request,
            "machine_name": target.name,
            "timeline": day_timeline(target.key),
        },
    )


@router.get("/machines/{key}/frame.png")
def machine_frame(request: Request, key: str, db: Session = Depends(get_db)):
    """Останній кадр екрана верстата.

    `key` НЕ підставляється у шлях: resolve_frame звіряє його зі станом
    процесу, і шлях будується з відомого верстата. Невідомий ключ — 404,
    а не спроба відкрити те, що написали в адресному рядку.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    path = resolve_frame(key)
    if path is None:
        raise HTTPException(status_code=404, detail="кадру ще немає")
    # Кадр перезаписується під тим самим іменем — без no-store браузер
    # показував би вчорашню картинку.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})
