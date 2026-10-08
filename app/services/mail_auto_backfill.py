"""Ввімкнули «Авто» — докачати листи цього відправника, що ВЖЕ чекають (08.10.26).

Рішення «качати вкладення чи ні» синк приймає один раз — у фазі 2, коли лист
щойно прийшов (`mail_reader._apply_attachments`). Лист, що прийшов ДО галочки
«Авто», лишався «skipped» назавжди, і оператор мусив тиснути «Скачати» на
кожному (власник 08.10.26: «декільком клієнтам поставив авто, пройшов час, а
воно не скачалось» — у лозі ночі серія ручних «Скачати»).

Тут — лише ці листи: у «Вхідних» (`mail_inbox.in_inbox`), ще не прийняті,
файли «skipped», відправник — САМЕ той, кому щойно ввімкнули «Авто» (той самий
збіг, що в `sender_memory.is_auto_sender`: повний ключ або гола адреса).
Скачування — та сама функція й той самий лок, що й ручне «Скачати»
(`download_attachments_now`, `attachment_lock`): двійників «(2)» не буде, і
лист, який оператор саме качає руками, фон пропустить.

У фоні: IMAP-вхід на кожен лист коштує секунди, а перемикач має відповісти
одразу. Один потік — входи в скриньку по черзі.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mail_inbox import in_inbox
from app.models import EmailMessage
from app.sender_memory import sender_key_for

logger = logging.getLogger(__name__)

# Стеля одного докачування: «Вхідні» тримають 30 днів, і відправник із
# десятками старих листів не має займати скриньку на пів години. Решту оператор
# докачає руками, як і раніше.
MAX_LETTERS = 30

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail-auto-backfill")


def _submit(fn, *args) -> Future | None:
    """Точка подачі в пул — тести підміняють її виконанням на місці."""
    return _pool.submit(fn, *args)


def _sender_matches(email: EmailMessage, keys: set[str]) -> bool:
    key = sender_key_for(email)
    base = (email.from_address or "").strip().lower()
    return bool((key and key in keys) or (base and base in keys))


def waiting_letters(db: Session, keys: set[str]) -> list[int]:
    """Листи відправника з `keys`, що чекають файлів у «Вхідних». Найновіші
    перші — їх оператор рахуватиме найближчим часом."""
    keys = {k.strip().lower() for k in keys if k and k.strip()}
    if not keys:
        return []
    rows = db.scalars(
        select(EmailMessage)
        .where(in_inbox, EmailMessage.status == "нове", EmailMessage.attachments_status == "skipped")
        .order_by(EmailMessage.id.desc())
    ).all()
    return [e.id for e in rows if _sender_matches(e, keys)][:MAX_LETTERS]


def schedule_backfill(db: Session, keys: set[str]) -> int:
    """Поставити докачування в чергу фону. Повертає, скільки листів поставлено
    (для підпису перемикача). Задача відкриває ВЛАСНУ сесію на ту саму базу."""
    ids = waiting_letters(db, keys)
    if ids:
        _submit(_backfill, db.get_bind(), ids)
    return len(ids)


def _backfill(bind, email_ids: list[int]) -> None:
    from app.archive_extract import is_archive
    from app.mail_reader import attachment_lock, download_attachments_now, extract_archive_attachments
    from app.mail_sync_service import zombie_fetch_blocks_files
    from app.settings_store import get_mail_attachments_path

    done = 0
    for email_id in email_ids:
        busy = zombie_fetch_blocks_files()
        if busy:
            logger.warning("Авто-докачування зупинено: %s (лишилось %d листів)", busy,
                           len(email_ids) - done)
            return
        try:
            with attachment_lock(email_id):
                with Session(bind=bind, autoflush=False, expire_on_commit=False) as bg:
                    email = bg.get(EmailMessage, email_id)
                    # Під локом — свіжий стан: оператор міг скачати чи прийняти лист.
                    if email is None or email.attachments_status != "skipped" or email.status != "нове":
                        continue
                    try:
                        saved = download_attachments_now(bg, email, Path(get_mail_attachments_path(bg)))
                        bg.commit()
                    except Exception:  # noqa: BLE001 — один лист не зупиняє решту
                        bg.rollback()
                        logger.exception("Авто-докачування: лист %s не скачано", email_id)
                        continue
                    if any(is_archive(a.filename) for a in email.attachments):
                        try:
                            extracted, errors = extract_archive_attachments(bg, email)
                            if extracted or errors:
                                bg.commit()
                        except Exception:  # noqa: BLE001 — архів лишиться, кнопка є
                            bg.rollback()
                            logger.exception("Авто-докачування: розпакування листа %s", email_id)
                    done += 1
                    logger.info("Авто-докачування: лист %s — %d файл.", email_id, saved)
        except Exception:  # noqa: BLE001 — фон не має права мовчки померти
            logger.exception("Авто-докачування: лист %s", email_id)
    if done:
        logger.info("Авто-докачування: скачано %d з %d листів", done, len(email_ids))
