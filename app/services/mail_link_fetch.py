"""Файли за посиланням у листі (Google Диск, ukr.net) — одне скачування для
кнопки листа і для фонового проходу.

Кнопка «Скачати за посиланням» (`/mail/{id}/fetch-link`) і фоновий воркер
`kuubmill-mail-links` (власник 09.11.26: «клієнти, які стоять на
автоскачуванні, і там посилання на Google Диск — теж автоматично у фоні»)
ходять через `fetch_link`: той самий лок на (лист, посилання), та сама тека
спулу, та сама позначка «оброблено». Дві копії цього шляху вже раз розійшлись
(тека `<uid>` замість `<uidvalidity>_<uid>`, хендоф 25.09.26).

Фон бере ЛИШЕ:
  * листи довірених відправників (`is_auto_sender`) — перемикач «качати файли
    всіх» посилань не стосується: це сервер ходить за адресою з листа;
  * у «Вхідних», ще «нове», з уже скачаними вкладеннями;
  * що прийшли ПІСЛЯ першого запуску функції (`mail_link_autofetch_since`):
    старі листи не чіпаються;
  * з адміновим вимикачем `mail_link_autofetch` (дефолт увімкнено).
Білий список сервісів, ручні перенаправлення і межа розміру — ті самі, що в
кнопки (`app/link_attachments.py`).

Невдача не крутиться вічно: відмова сервісу (немає доступу, ліміт Google)
вдруге не пробується, тимчасова (обрив, 5xx) — ще двічі з паузою. Пам'ять
спроб у процесі: перезапуск дає ще одну серію, і це межа зверху.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.archive_extract import is_archive
from app.business_day import business_now
from app.link_attachments import (
    LinkAttachment,
    LinkDownloadError,
    TransientLinkError,
    download_link,
    undownloaded_links,
)
from app.mail_inbox import in_inbox
from app.mail_reader import extract_archive_attachments
from app.mail_spool import spool_folder_name
from app.models import Attachment, EmailMessage
from app.sender_memory import is_auto_sender
from app.settings_store import get_mail_attachments_path, get_setting, set_setting

logger = logging.getLogger(__name__)

AUTO_KEY = "mail_link_autofetch"
SINCE_KEY = "mail_link_autofetch_since"

# Тимчасова невдача: скільки серій спроб і яка пауза між ними. Одна серія —
# це вже до трьох спроб усередині `download_link` (бюджет 150 с).
TRANSIENT_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=15)

# Хости, за якими SQL відсіює листи до розбору тексту (той самий білий
# список, що в `link_attachments`, лише грубіше — точне рішення за ним).
_HOST_HINTS = ("drive.google", "docs.google", "ukr.net")


# ── Позначка «посилання оброблено» і лок ─────────────────────────────────────


def mark_link_handled(db: Session, email_id: int, ref: str) -> None:
    """Додати посилання до `handled_link_refs` одним атомарним UPDATE.

    Список відсортований і без дублів (як і раніше писався з Python), але
    читається й пишеться всередині SQLite — паралельні запити не перетирають
    позначки одне одного. Комітить викликач."""
    db.execute(
        text(
            "UPDATE email_messages SET handled_link_refs = ("
            " SELECT json_group_array(value) FROM ("
            "  SELECT value FROM json_each(COALESCE(email_messages.handled_link_refs, '[]'))"
            "  UNION SELECT :ref ORDER BY 1))"
            " WHERE id = :id"
        ),
        {"ref": ref, "id": email_id},
    )


_locks_guard = threading.Lock()
_locks: dict[tuple[int, str], threading.Lock] = {}


def link_fetch_lock(email_id: int, ref: str) -> threading.Lock:
    """Один лок на (лист, посилання): два запити на ТЕ САМЕ посилання (клік по
    рядку, поки «Скачати за посиланням» його вже качає; друга вкладка, другий
    оператор або фоновий прохід) ідуть по черзі, і другий бачить файл першого
    серед вкладень — «такий файл уже є», а не двійник «(2)». Різні посилання
    одного листа качаються паралельно, як і раніше."""
    key = (email_id, ref)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


# ── Одне посилання ───────────────────────────────────────────────────────────


@dataclass
class LinkFetchResult:
    status: str  # "done" | "skip" | "error"
    message: str | None = None
    result_name: str | None = None
    transient: bool = False  # помилка мережі/сервісу, яку варто повторити


def link_ref(link: LinkAttachment) -> str:
    return link.file_id or link.url


def fetch_link(db: Session, email: EmailMessage, link: LinkAttachment) -> LinkFetchResult:
    """Скачати одне посилання листа в теку спулу, записати вкладення й позначку
    «оброблено». Комітить сам; файл, чий запис не закомітився, прибирає."""
    ref = link_ref(link)
    with link_fetch_lock(email.id, ref):
        # Під локом — СВІЖИЙ склад вкладень: запит на те саме посилання, що
        # чекав на лок, мусить побачити файл попереднього й відповісти «уже є».
        db.refresh(email)
        existing = frozenset(a.filename for a in email.attachments)
        result = LinkFetchResult(status="error")
        path: Path | None = None
        try:
            # Та сама тека спулу, що й у вкладень листа (`<uidvalidity>_<uid>`,
            # mail_spool.spool_folder_name). Колись тут стояв голий `email.uid`, і
            # файли за посиланням лягали в ІНШУ теку, ніж вкладення того самого листа.
            spool_dir = Path(get_mail_attachments_path(db)) / spool_folder_name(email.uid, email.uid_validity)
            path = download_link(link, spool_dir, existing_names=existing)
        except LinkDownloadError as exc:
            result = LinkFetchResult(
                status="error", message=str(exc), transient=isinstance(exc, TransientLinkError)
            )
            # Відмова без сліду в лозі — саме так «скачати все» роками
            # губило файли непомітно: з лога не видно, що і чому не дійшло.
            logger.warning(
                "Лист %s: файл за посиланням (%s) не скачано — %s",
                email.id, urlsplit(link.url).hostname or "?", result.message,
            )
        except Exception:  # noqa: BLE001 — одне погане посилання не валить решту
            logger.exception("Link download failed for email %s: %s", email.id, link.url)
            host = urlsplit(link.url).hostname or "сервер файлів"
            result = LinkFetchResult(
                status="error",
                message=f"немає з'єднання з {host} (інтернет / проксі?)",
                transient=True,
            )
        else:
            if path is None:
                result = LinkFetchResult(status="skip")
            else:
                db.add(
                    Attachment(
                        email_message_id=email.id,
                        filename=path.name,
                        saved_path=str(path),
                        size_bytes=path.stat().st_size,
                    )
                )
                email.attachments_status = "ready"
                result = LinkFetchResult(status="done", result_name=path.name)
        if result.status in ("done", "skip"):
            # Посилання оброблено — лічильник «ще N за посиланням» падає.
            # АТОМАРНО, одним UPDATE: «Скачати за посиланням» шле запит на КОЖНЕ
            # посилання паралельно, і читання-зміна-запис у Python губив чужі
            # позначки (бойовий лист 25.09.26: 9 файлів скачано, «оброблених» 2).
            mark_link_handled(db, email.id, ref)
        try:
            db.commit()
        except Exception as commit_error:  # noqa: BLE001 — файл уже на диску
            # Файл скачано ДО коміту. Невдалий коміт відкочує рядок `Attachment`,
            # і в спулі лишається файл, якого в базі немає: у тріажі його не
            # видно, але «Відкрити папку» показує зайву коронку поруч зі
            # справжніми (ревʼю 07.09.26, LOW).
            db.rollback()
            logger.exception("Link download commit failed for email %s", email.id)
            if result.status == "done" and path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.error("Orphan link file left in spool: %s", path)
            result = LinkFetchResult(
                status="error", message=f"не вдалося зберегти запис про файл: {commit_error}"
            )
    return result


def unpack_if_archive(db: Session, email: EmailMessage, result_name: str | None) -> dict | None:
    """Свіжоскачаний архів (клієнт запакував STL у .zip/.rar) — розпакувати.
    Повертає тост для оператора або None. Невдача не валить викликача."""
    if not result_name or not is_archive(result_name):
        return None
    db.refresh(email)
    try:
        extracted, extract_errors = extract_archive_attachments(db, email)
        if extracted or extract_errors:
            db.commit()
        if extracted:
            return {"message": f"Розпаковано {extracted} файл(ів) з архіву", "kind": "success"}
        if extract_errors:
            return {"message": "Архів: " + extract_errors[0], "kind": "error"}
    except Exception:  # noqa: BLE001 — розпакування не валить панель і фон
        logger.exception("Archive extract failed for email %s", email.id)
        db.rollback()
    return None


# ── Фоновий прохід ───────────────────────────────────────────────────────────


def autofetch_enabled(db: Session) -> bool:
    """Вимикач адміна. ВЛАСНЕ слово для кожного стану ("1"/"0"): порожнє
    значення означає «ще не чіпали» = дефолт увімкнено (§14 «Порожнє поле»)."""
    return (get_setting(db, AUTO_KEY) or "1") != "0"


def set_autofetch_enabled(db: Session, enabled: bool) -> None:
    set_setting(db, AUTO_KEY, "1" if enabled else "0")


def autofetch_since(db: Session, now: datetime | None = None) -> datetime:
    """З якого моменту листи беруться у фон. Перший виклик записує «зараз»:
    листи, що лежали до встановлення, фон не чіпає (обіцянка власнику)."""
    raw = get_setting(db, SINCE_KEY)
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("Пошта: невідомий %s=%r — рахую від зараз", SINCE_KEY, raw)
    stamp = (now or business_now()).replace(microsecond=0)
    set_setting(db, SINCE_KEY, stamp.isoformat())
    db.commit()
    return stamp


@dataclass
class _Attempt:
    count: int
    next_try: datetime | None  # None = більше не пробувати


_attempts: dict[tuple[int, str], _Attempt] = {}
_attempts_guard = threading.Lock()


def _reset_attempts_for_tests() -> None:
    with _attempts_guard:
        _attempts.clear()


def _due(key: tuple[int, str], now: datetime) -> bool:
    with _attempts_guard:
        attempt = _attempts.get(key)
    if attempt is None:
        return True
    return attempt.next_try is not None and now >= attempt.next_try


def _note_failure(key: tuple[int, str], result: LinkFetchResult, now: datetime) -> None:
    with _attempts_guard:
        count = (_attempts[key].count if key in _attempts else 0) + 1
        retry = result.transient and count < TRANSIENT_ATTEMPTS
        _attempts[key] = _Attempt(count=count, next_try=now + RETRY_AFTER if retry else None)


def pending_letters(db: Session, since: datetime) -> list[EmailMessage]:
    """Листи-кандидати фону без перевірки відправника (вона — у Python)."""
    return list(
        db.scalars(
            select(EmailMessage)
            .where(
                in_inbox,
                EmailMessage.status == "нове",
                EmailMessage.attachments_status == "ready",
                EmailMessage.received_at >= since,
                or_(*(EmailMessage.body_text.contains(h) for h in _HOST_HINTS)),
            )
            .order_by(EmailMessage.received_at)
        )
    )


def fetch_pending_once(
    db: Session,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    blocked: Callable[[], str | None] = lambda: None,
) -> int:
    """Один фоновий прохід. Повертає, скільки файлів скачано.

    `blocked` — гейт покинутого фетчу пошти (`zombie_fetch_blocks_files`):
    поки зомбі качає в ті самі теки, не чіпаємо нічого, як і ручні кнопки."""
    if not autofetch_enabled(db):
        return 0
    now = now or business_now()
    since = autofetch_since(db, now)
    done = 0
    for email in pending_letters(db, since):
        if should_stop():
            break
        if not is_auto_sender(db, email):
            continue
        for link in undownloaded_links(email):
            if should_stop():
                return done
            key = (email.id, link_ref(link))
            if not _due(key, now):
                continue
            reason = blocked()
            if reason:
                logger.info("Пошта: фонове скачування за посиланням чекає — %s", reason)
                return done
            result = fetch_link(db, email, link)
            if result.status == "done":
                done += 1
                logger.info(
                    "Пошта: лист %s — файл за посиланням (%s) скачано у фоні: %s",
                    email.id, urlsplit(link.url).hostname or "?", result.result_name,
                )
                unpack_if_archive(db, email, result.result_name)
            elif result.status == "error":
                _note_failure(key, result, now)
    return done
