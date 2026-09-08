"""Заготовки: тека дисків CAM і замовлення для комірниці.

Коли оператор створює новий диск у CAM, той кладе файл у
`<корінь>/<матеріал>/<висота>/*.blk`. Створення диска майже завжди означає
«взяв новий з архіву», тож замовлення для комірниці вже існує на диску —
раніше його складали обходом шухляд і писали у Viber (~10 хв щодня).

Розділ ЧИТАЄ теку. Видалення файлів (окремий блок) буде свідомим і
підтвердженим: це запис у теку, якою володіє CAM.
"""

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import CamBlank
from app.routers.deps import get_current_user, get_db, templates
from app.services.cam_blanks import (
    mark_ordered,
    pileup_note,
    probe_blanks,
    order_text,
    pending_blanks,
    sync_blanks,
)
from app.services.settings_nav import can_edit
from app.services.settings_status import build_slab
from app.settings_store import get_setting, set_setting
from .common import require_settings_edit

router = APIRouter()

SECTION = "blanks"


def blanks_context(db: Session, *, error: str | None = None) -> dict:
    """Усе, що потрібно партіалу розділу — і плиті, і списку.

    Тека тут НЕ читається: похід у файлову систему з потоку запиту робить те
    саме, від чого страждала видача. Показуємо те, що вже знає база; свіжість
    дає фоновий воркер або кнопка «Перечитати».
    """
    path = (get_setting(db, "cam_blanks_path") or "").strip()
    pending = pending_blanks(db) if path else []
    present = None
    mismatched = 0
    mismatched_rows: list[CamBlank] = []
    if path:
        present = db.scalar(
            select(func.count()).select_from(CamBlank).where(CamBlank.gone_at.is_(None))
        ) or 0
        # Не просто число: лічильник, під яким не видно ЯКИЙ саме диск лежить
        # не там, змусив би оператора шукати його руками по всій теці.
        # Помилку розкладання показуємо незалежно від того, чи її вже
        # замовили — вона лишається на полиці, поки її не переклали.
        mismatched_rows = list(
            db.scalars(
                select(CamBlank)
                .where(CamBlank.gone_at.is_(None), CamBlank.height_mismatch.is_(True))
                .order_by(CamBlank.material_dir, CamBlank.height_dir, CamBlank.file_name)
            ).all()
        )
        mismatched = len(mismatched_rows)
    return {
        "blanks_path": path,
        "blanks_pending": pending,
        "blanks_text": order_text(pending),
        "blanks_present": present,
        "blanks_mismatched": mismatched,
        "blanks_mismatched_rows": mismatched_rows,
        "blanks_error": error,
        # Проба заповнюється лише своїм роутом; на звичайному рендері її нема.
        "blanks_probe": None,
        "blanks_note": None,
        # Підказка «схоже, забули замовити» — рахується з самого списку.
        "blanks_pileup": pileup_note(pending),
    }


def _body(request: Request, db: Session, *, error: str | None = None, probe=None, note: str | None = None) -> HTMLResponse:
    user = get_current_user(request, db)
    ctx = blanks_context(db, error=error)
    ctx["blanks_probe"] = probe
    ctx["blanks_note"] = note
    return templates.TemplateResponse(
        request,
        "_settings_blanks_body.html",
        {"user": user, "can_edit": can_edit, **ctx},
    )


@router.post("/settings/blanks/path")
async def save_blanks_path(
    request: Request,
    cam_blanks_path: str = Form(""),
    db: Session = Depends(get_db),
):
    """Зберегти шлях до теки. Порожнє значення вимикає стеження — саме тому
    ключ є у CLEARABLE_SETTING_KEYS: помилковий шлях має зніматись з екрана."""
    require_settings_edit(request, db, SECTION)
    set_setting(db, "cam_blanks_path", cam_blanks_path.strip())
    db.commit()
    if request.headers.get("HX-Request") == "true":
        return _body(request, db)
    return RedirectResponse("/settings#blanks", status_code=303)


@router.post("/settings/blanks/rescan")
def rescan_blanks(request: Request, db: Session = Depends(get_db)):
    """Перечитати теку зараз. Звичайний `def` — це похід у файлову систему,
    і на event loop його пускати не можна (те саме правило, що для запису в
    таблицю): FastAPI віддасть його у threadpool."""
    # Той самий гейт, що й у решти роутів розділу: права описані ОДИН раз у
    # реєстрі меню, і кнопку та POST перевіряє те саме `can_edit`. Свій
    # ручний чек тут розійшовся б із рештою при першій же зміні ролей.
    require_settings_edit(request, db, SECTION)
    path = (get_setting(db, "cam_blanks_path") or "").strip()
    if not path:
        return _body(request, db, error="Спершу задайте шлях до теки заготовок.")
    try:
        result = sync_blanks(db, path)
    except OSError as exc:
        return _body(request, db, error=f"Теку не вдалось прочитати: {exc}")
    # Перший прохід — база відліку. Без цього повідомлення оператор натисне
    # кнопку, побачить порожній список і вирішить, що не працює.
    note = None
    if result.baseline:
        note = (
            f"Перше читання: {result.baseline} наявних дисків узято за точку "
            "відліку — вони вже були в теці, тож замовляти їх не треба. "
            "У список потраплятиме лише те, що зʼявиться далі."
        )
    return _body(request, db, note=note)


@router.post("/settings/blanks/ordered")
def blanks_ordered(request: Request, db: Session = Depends(get_db)):
    """Позначити все як замовлене — з цієї миті починається нове вікно.

    Саме позначка, а не календар: комірниця йде о 18:00, далі диски бере
    нічна зміна, а у вихідні комірниці немає взагалі. Межа робочої доби
    (07:30) відрізала б рівно те, що взяли вночі.
    """
    require_settings_edit(request, db, SECTION)
    mark_ordered(db)
    return _body(request, db)


@router.post("/settings/blanks/probe")
def probe_blanks_folder(request: Request, db: Session = Depends(get_db)):
    """Подивитись на теку, НІЧОГО не записавши.

    Потрібно рівно для одного: перед вмиканням стеження на робочому ПК
    переконатись, що розбір назв влучає в реальні файли — і мати звіт, який
    можна скопіювати й переслати. Проба не створює й не міняє жодного рядка,
    тож помилковий шлях нічого не псує.

    Звичайний `def`: це похід у файлову систему, і на event loop його пускати
    не можна.
    """
    require_settings_edit(request, db, SECTION)
    path = (get_setting(db, "cam_blanks_path") or "").strip()
    if not path:
        return _body(request, db, error="Спершу задайте шлях до теки заготовок.")
    return _body(request, db, probe=probe_blanks(path))
