"""Форма зворотного зв'язку: приймання звернень і адмін-стрічка «Вхідні».

POST /feedback доступний кожному операторові (маячок є на кожному екрані);
стрічка /feedback/inbox і дії над зверненнями — лише адмін, у ряд із рештою
керування. Віддача скріншотів — за сесією, шлях перевіряється наново.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.__version__ import VERSION
from app.feedback_images import (
    FeedbackImageError,
    media_type_for,
    resolve_image_file,
    save_image,
)
from app.models import Feedback, FeedbackImage, User
from app.routers.deps import get_current_user, login_redirect, get_db, templates, toast_response
from app.services import feedback as feedback_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_user(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="потрібен вхід")
    return user


def _require_admin(request: Request, db: Session) -> User:
    user = _require_user(request, db)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    return user


def _attach_images(
    db: Session, feedback: Feedback, uploads: list[UploadFile]
) -> list[str]:
    """Прикріпити скріншоти; повертає список проблем (провал одного НЕ валить
    саме звернення — воно вже в базі)."""
    problems: list[str] = []
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        try:
            save_image(
                db,
                feedback,
                stream=upload.file,
                filename=upload.filename,
            )
        except FeedbackImageError as exc:
            problems.append(str(exc))
        except Exception:  # noqa: BLE001
            logger.warning("feedback: збій збереження скріншота", exc_info=True)
            problems.append("не вдалось зберегти скріншот")
    return problems


@router.post("/feedback")
def submit_feedback(
    request: Request,
    kind: str = Form(...),
    text: str = Form(""),
    severity: str = Form(""),
    screen: str = Form(""),
    images: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    try:
        feedback = feedback_service.create_feedback(
            db,
            kind=kind,
            text=text,
            severity=severity,
            screen=screen,
            app_version=VERSION,
            author=user,
        )
    except feedback_service.FeedbackError as exc:
        return toast_response(str(exc), kind="error")

    db.commit()
    problems = _attach_images(db, feedback, images or [])
    db.commit()

    # Пуш у Telegram — окремий крок; збій не чіпає вже збережене звернення.
    try:
        feedback_service.try_push(db, feedback)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("feedback: збій Telegram-пуша", exc_info=True)
        db.rollback()

    message = "Дякуємо — надіслано."
    if problems:
        message = "Надіслано (скріншот не додано: " + "; ".join(problems) + ")"
    return toast_response(
        message,
        kind="success" if not problems else "error",
        triggers={"refresh-feedback-badge": True},
    )


@router.get("/feedback/inbox", response_class=HTMLResponse)
def feedback_inbox(
    request: Request,
    status: str = "",
    partial: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    status_filter = status if status in feedback_service.STATUSES else None
    items = feedback_service.list_feedback(db, status=status_filter)
    context = {
        "request": request,
        "user": user,
        "items": items,
        "status_filter": status_filter or "all",
        "open_count": feedback_service.open_count(db),
        "topbar_active": "feedback",
    }
    if partial == "list":
        return templates.TemplateResponse(request, "_feedback_inbox_list.html", context)
    return templates.TemplateResponse(request, "feedback_inbox.html", context)


@router.post("/feedback/{feedback_id}/seen")
def mark_seen(request: Request, feedback_id: int, db: Session = Depends(get_db)):
    _require_admin(request, db)
    feedback = db.get(Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="звернення не знайдено")
    feedback_service.mark_seen(db, feedback)
    db.commit()
    return toast_response(
        "Позначено прочитаним.",
        triggers={"refresh-feedback-badge": True, "refresh-feedback-inbox": True},
    )


@router.post("/feedback/{feedback_id}/resolve")
def resolve(request: Request, feedback_id: int, db: Session = Depends(get_db)):
    _require_admin(request, db)
    feedback = db.get(Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="звернення не знайдено")
    feedback_service.mark_resolved(db, feedback)
    db.commit()
    return toast_response(
        "Закрито.",
        triggers={"refresh-feedback-badge": True, "refresh-feedback-inbox": True},
    )


@router.post("/feedback/{feedback_id}/reopen")
def reopen(request: Request, feedback_id: int, db: Session = Depends(get_db)):
    _require_admin(request, db)
    feedback = db.get(Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="звернення не знайдено")
    feedback_service.reopen(db, feedback)
    db.commit()
    return toast_response(
        "Відкрито знову.",
        triggers={"refresh-feedback-badge": True, "refresh-feedback-inbox": True},
    )


@router.get("/feedback/images/{image_id}")
def get_feedback_image(request: Request, image_id: int, db: Session = Depends(get_db)):
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="потрібен вхід")
    image = db.get(FeedbackImage, image_id)
    path = resolve_image_file(image) if image is not None else None
    media_type = media_type_for(path) if path is not None else None
    if path is None or media_type is None:
        raise HTTPException(status_code=404, detail="файл не знайдено")
    return FileResponse(
        path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"}
    )
