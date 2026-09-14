"""STL-прев'ю: список файлів у теці, віддача одного файлу, відкриття теки.

Головний інструмент звірки на видачі (CLAUDE.md §9.4) — оператор порівнює
форму коронки з лотка з моделлю. `token` тут завжди непрозорий і НІКОЛИ не
сирий шлях: тека щоразу перевиводиться з токена на сервері (див.
app/stl_preview.py), а зіпсований токен дає 404, а не довіру до його вмісту.
"""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.platform_windows import open_folder_in_explorer
from app.routers.deps import (
    TRUSTED_ONLY_DETAIL,
    get_current_user,
    get_db,
    is_trusted_request,
    open_folder_response,
)
from app.stl_preview import list_stl_files, resolve_preview_folder, resolve_stl_file

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/stl-preview/{token}")
def list_stl_preview_files(request: Request, token: str, db: Session = Depends(get_db)):
    """Lists `.stl` filenames for the hover preview popup (app/static/js/stl-preview.js).

    `token` is opaque and never a raw filesystem path — see app/stl_preview.py
    for why. An unresolvable/tampered token degrades to 404, same as any
    other "folder not found" case in this app; it never falls back to
    trusting the token's contents directly.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    folder = resolve_preview_folder(db, token)
    if folder is None:
        raise HTTPException(status_code=404, detail="папку не знайдено")

    return {"files": list_stl_files(folder)}


@router.get("/stl-preview/{token}/{filename:path}")
def get_stl_preview_file(
    request: Request, token: str, filename: str, db: Session = Depends(get_db)
):
    """Streams one `.stl` file's bytes for the hover preview popup.

    `folder` is re-derived from `token` on every call (never cached from the
    list call above) and `filename` is re-validated against that folder by
    `resolve_stl_file` — `.stl` extension only, `/` only between plain
    segments of a subfolder (`Іваненко/crown.stl`, 11.09.26), no `..`, no
    backslash, no link hop, must resolve to an existing regular file inside
    `folder`. `:path` — бо браузер шле `Іваненко%2Fcrown.stl`, а Starlette
    розкодовує `%2F` у `/` ще до маршрутизації: без `:path` такий запит
    не збігся б із маршрутом і дав 404.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    folder = resolve_preview_folder(db, token)
    if folder is None:
        raise HTTPException(status_code=404, detail="папку не знайдено")

    file_path = resolve_stl_file(folder, filename)
    if file_path is None:
        raise HTTPException(status_code=404, detail="файл не знайдено")

    return FileResponse(file_path, media_type="model/stl")


@router.post("/open-folder")
def open_preview_folder(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    """Open a work's resolved folder in Windows Explorer from a preview token.

    A browser can't act on a file:// link from an http page (it's silently
    blocked), so the "Відкрити папку" button in the STL panel and the queue's
    double-click both POST the opaque preview token here instead. Same safety
    envelope as /mail/{id}/open-folder: authenticated, and the token is
    re-resolved server-side to a trusted directory (never a raw path from the
    client — see app/stl_preview.py). Explorer opens only on the server PC;
    a trusted network client gets the path back instead (`open_folder_response`)."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Гейт адреси ДО розбору токена: чужій адресі не кажемо навіть, чи існує тека.
    if not is_trusted_request(request, db):
        raise HTTPException(status_code=403, detail=TRUSTED_ONLY_DETAIL)

    folder = resolve_preview_folder(db, token)
    if folder is None:
        raise HTTPException(status_code=404, detail="папку не знайдено")

    return open_folder_response(
        request, db, folder, opener=open_folder_in_explorer, log_label="stl preview"
    )
