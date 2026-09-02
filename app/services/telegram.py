"""Telegram-пуш звернень зворотного зв'язку.

Бот уже є — VARTAAIR; сюди приходить лише його токен (у налаштуваннях,
зашифровано) і chat_id приватного чату. Виходить наверх через
new_legacy_session (app/sheets.py): у цеху HTTPS іде крізь TLS-проксі з
legacy renegotiation, і звичайна сесія requests його рве — та сама причина, що
у скачуванні за посиланням і перевірці оновлень.

Цей модуль НІКОЛИ не є джерелом правди: звернення вже лежить у базі, коли ми
сюди заходимо. Помилка тут повертається рядком у виклик, а не піднімається —
роут її ковтає, ставить telegram_error і йде далі. Дошле фоновий ретрай.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.feedback_images import resolve_image_file
from app.models import Feedback
from app.settings_store import get_setting

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = (10, 30)
# Ліміт підпису до фото в Telegram — 1024 символи; звичайного повідомлення —
# 4096. Довший опис ідемо окремим sendMessage, а фото — без підпису.
_CAPTION_LIMIT = 1024

_KIND_LABEL = {"bug": "🐞 Баг", "idea": "💡 Ідея", "question": "❓ Питання"}
_SEVERITY_LABEL = {
    "minor": "дрібниця",
    "annoying": "заважає",
    "blocking": "блокує",
}


def get_bot_token(db: Session) -> str | None:
    value = (get_setting(db, "telegram_bot_token") or "").strip()
    return value or None


def get_chat_id(db: Session) -> str | None:
    value = (get_setting(db, "telegram_chat_id") or "").strip()
    return value or None


def push_enabled(db: Session) -> bool:
    """Чи має форма слати пуш. Вимикач + наявність токена й chat_id.

    Запис у базу від цього НЕ залежить — він завжди відбувається; це лише про
    те, чи намагатись відправити в Telegram."""
    if (get_setting(db, "feedback_telegram_enabled") or "") != "1":
        return False
    return bool(get_bot_token(db)) and bool(get_chat_id(db))


def _new_session():
    # Імпорт локальний: у деяких оточеннях (тести без мережі) app.sheets тягне
    # важкі залежності, а сюди ми заходимо лише коли пуш справді ввімкнено.
    from app.sheets import new_legacy_session

    return new_legacy_session()


def _build_caption(feedback: Feedback) -> str:
    head = _KIND_LABEL.get(feedback.kind, feedback.kind)
    if feedback.severity:
        head += f" · {_SEVERITY_LABEL.get(feedback.severity, feedback.severity)}"
    lines = [head, ""]
    lines.append(feedback.text or "—")
    lines.append("")
    meta = []
    if feedback.screen:
        meta.append(f"екран: {feedback.screen}")
    if feedback.author is not None:
        meta.append(f"від: {feedback.author.full_name or feedback.author.username}")
    if feedback.app_version:
        meta.append(f"версія: {feedback.app_version}")
    meta.append(f"#KM-{feedback.id}")
    lines.append(" · ".join(meta))
    return "\n".join(lines)


def send_feedback(db: Session, feedback: Feedback) -> tuple[bool, str | None]:
    """Спробувати доставити звернення в Telegram. Повертає (успіх, помилка).

    Нічого не комітить і не піднімає винятків назовні: всі мережеві й API-збої
    згортаються в рядок помилки. Викликач вирішує, що з ним робити (лічильник
    спроб, telegram_error, ретрай)."""
    token = get_bot_token(db)
    chat_id = get_chat_id(db)
    if not token or not chat_id:
        return False, "не задано токен або chat_id"

    caption = _build_caption(feedback)
    paths: list[Path] = []
    for image in feedback.images:
        resolved = resolve_image_file(image)
        if resolved is not None:
            paths.append(resolved)

    try:
        session = _new_session()
    except Exception as exc:  # noqa: BLE001 — будь-який збій = не відправлено
        logger.warning("telegram: не вдалось відкрити сесію", exc_info=True)
        return False, f"сесія: {exc}"

    try:
        if not paths:
            return _send_message(session, token, chat_id, caption)

        # Є скріншоти. Якщо опис влазить у підпис — перше фото з підписом; ні —
        # окреме повідомлення з описом, а фото без підпису.
        first_caption = caption if len(caption) <= _CAPTION_LIMIT else None
        if first_caption is None:
            ok, err = _send_message(session, token, chat_id, caption)
            if not ok:
                return ok, err
        for idx, path in enumerate(paths):
            cap = first_caption if idx == 0 else None
            ok, err = _send_photo(session, token, chat_id, path, cap)
            if not ok:
                return ok, err
        return True, None
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _send_message(session, token, chat_id, text) -> tuple[bool, str | None]:
    url = _API.format(token=token, method="sendMessage")
    try:
        resp = session.post(
            url,
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"мережа: {exc}"
    return _check(resp)


def _send_photo(session, token, chat_id, path: Path, caption) -> tuple[bool, str | None]:
    url = _API.format(token=token, method="sendPhoto")
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    try:
        with open(path, "rb") as fh:
            resp = session.post(
                url, data=data, files={"photo": fh}, timeout=_TIMEOUT
            )
    except OSError as exc:
        return False, f"файл: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"мережа: {exc}"
    return _check(resp)


def _check(resp) -> tuple[bool, str | None]:
    if resp.status_code == 200:
        return True, None
    # Telegram кладе причину в JSON description; беремо її, а не сирий HTML.
    try:
        detail = resp.json().get("description", "")
    except Exception:  # noqa: BLE001
        detail = ""
    return False, f"HTTP {resp.status_code} {detail}".strip()


def discover_chat_id(db: Session) -> tuple[str | None, str | None]:
    """Знайти chat_id останнього приватного чату, що написав боту (getUpdates).

    Бот не може написати першим, поки користувач не натисне /start. Оператор
    пише боту, тисне тут «Прив'язати чат» — і ми беремо chat_id з останнього
    оновлення. Повертає (chat_id, помилка)."""
    token = get_bot_token(db)
    if not token:
        return None, "спершу збережіть токен бота"
    url = _API.format(token=token, method="getUpdates")
    try:
        session = _new_session()
    except Exception as exc:  # noqa: BLE001
        return None, f"сесія: {exc}"
    try:
        resp = session.get(url, timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return None, f"мережа: {exc}"
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    try:
        updates = resp.json().get("result", [])
    except Exception:  # noqa: BLE001
        return None, "невірна відповідь Telegram"

    # Останнє оновлення з приватним чатом.
    for update in reversed(updates):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is not None:
            return str(chat_id), None
    return None, "не бачу повідомлень боту — напишіть боту /start і спробуйте ще раз"
