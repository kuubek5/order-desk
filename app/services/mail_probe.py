"""Чи прийшов новий лист — одним дешевим запитом між повними синками пошти.

Навіщо. Повний синк пошти йде раз на `MAIL_SYNC_INTERVAL_SECONDS` (2 хв): він
читає заголовки за 30 днів, дзеркалить папки, качає вкладення — ганяти його
частіше не варто. Через це лист, що прийшов одразу після синку, з'являвся в
CRM лише за ~2 хв, тоді як ukr.net у браузері показує його за секунди (скарга
власника 03.11.26). Тут між синками раз на `MAIL_PROBE_INTERVAL_SECONDS`
питаємо в сервера лише `STATUS INBOX (UIDNEXT UIDVALIDITY)` — номер, який
отримає НАСТУПНИЙ лист. Змінився — у Вхідні щось прийшло, і повний синк
запускається одразу, не чекаючи свого часу.

UIDNEXT росте, лише коли у Вхідні щось ПОТРАПЛЯЄ: новий лист або лист,
повернутий туди з папки (і тоді синк теж потрібен). Прийняття (переніс із
Вхідних), видалення чи позначка «прочитано» його не рухають, тож синк не
смикається від власних дій оператора.

З'єднання ОДНЕ й живе між пробами. Логін на кожну пробу — це понад чотири
тисячі входів на добу, і поштовий сервер може прийняти таке за підбір пароля.
Будь-яка помилка — з'єднання закривається, проба каже «не знаю» (False), а
наступна відкриває нове. Розклад повних синків від проби не залежить: якщо
вона мовчить чи падає, пошта приходить, як і раніше, раз на 2 хв.
"""

from __future__ import annotations

import imaplib
import logging
from typing import Any, Callable, Optional

from imap_tools import MailBox
from imap_tools.errors import ImapToolsError

from app import log_throttle
from app.mail_reader import IMAP_HOST, IMAP_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# Як часто питати сервер між повними синками. Лист з'являється в CRM у
# середньому за ~половину цього (плюс сам синк), а не за ~хвилину, як було.
MAIL_PROBE_INTERVAL_SECONDS = 20

_ERROR_LOG_KEY = "mail_probe.error"


def _connect(login: str, password: str) -> Any:
    return MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password)


class InboxProbe:
    """Стежить за UIDNEXT Вхідних на одному постійному з'єднанні."""

    def __init__(self, connect: Callable[[str, str], Any] = _connect) -> None:
        self._connect = connect
        self._mailbox: Any = None
        self._login: Optional[str] = None
        self._last: Optional[tuple[int, int]] = None

    def changed(self, login: str, password: str) -> bool:
        """True — у Вхідних з'явився лист від попередньої проби.

        Перша проба (і перша після зміни логіна) лише запам'ятовує стан:
        порівнювати ще нема з чим. False — і коли «не змінилось», і коли
        «не знаю» (помилка): тоді лист прийде звичайним синком."""
        if login != self._login:
            self.close()
            self._login = login
            self._last = None
        try:
            if self._mailbox is None:
                self._mailbox = self._connect(login, password)
            status = self._mailbox.folder.status("INBOX", ["UIDNEXT", "UIDVALIDITY"])
            now = (int(status["UIDVALIDITY"]), int(status["UIDNEXT"]))
        except Exception as exc:  # noqa: BLE001 — проба не має права валити синк
            skipped = log_throttle.due(_ERROR_LOG_KEY)
            if skipped is not None:
                logger.warning(
                    "Пошта: швидка перевірка нових листів не вдалась (%s: %s)%s — "
                    "листи прийдуть звичайним синком",
                    type(exc).__name__, exc,
                    f" (ще {skipped} разів відтоді)" if skipped else "",
                )
            self.close()
            return False
        log_throttle.clear(_ERROR_LOG_KEY)
        previous, self._last = self._last, now
        return previous is not None and now != previous

    def close(self) -> None:
        mailbox, self._mailbox = self._mailbox, None
        if mailbox is None:
            return
        try:
            mailbox.logout()
        except (OSError, imaplib.IMAP4.error, ImapToolsError):
            # З'єднання й так уже мертве (обрив, таймаут) — закривати нема чого.
            pass
