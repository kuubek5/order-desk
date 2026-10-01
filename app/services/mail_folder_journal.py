"""Журнал переносів листа між папками скриньки (власник 29.09.26).

Два листи SmileDent Laba, що прийшли о 09:23, за годину лежали у
«Відфрезеровано», хоча не були прийняті в чергу — роботи під них у CRM не
було. Фільтрів у ukr.net немає, тобто їх хтось переклав. Хто, коли і як —
відповісти не вдалось нічим: перенос через CRM («↦ Перемістити», «⋯»,
прийняття) і перенос прямо в пошті лишали в базі той самий слід
(`mailbox_folder`), а в журнал не писали нічого.

Тепер кожна зміна папки листа — рядок у «Журналі синку» (напрям
`mail_folder`): який лист (відправник, тема, коли прийшов), звідки → куди,
і ЯК — через CRM з іменем оператора чи прямо в пошті (це бачить лише синк,
людини він не знає). Лист, що йде в папку НЕ прийнятим, пишеться зі
статусом `warning` — фільтр «увага» в журналі показує саме ті випадки, коли
робота могла загубитись.

Без міграції: `SyncLog` уже має все потрібне. Запис лише додається в сесію —
коміт робить той, хто міняє папку (один коміт на дію, як і було).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.models import EmailMessage, Order, SyncLog

DIRECTION = "mail_folder"
INBOX = "Вхідні"
GONE = "зник зі Вхідних"

VIA_CRM = "crm"
VIA_ACCEPT = "accept"
VIA_MAILBOX = "mailbox"
VIA_SELF_FIX = "self_fix"


def _describe(email: EmailMessage) -> str:
    who = (email.from_name or email.from_address or "відправник ?").strip()
    subject = (email.subject or "").strip() or "без теми"
    received = email.received_at.strftime("%d.%m %H:%M") if email.received_at else "?"
    return f"лист {email.id} ({who}, «{subject}», прийшов {received})"


def is_unaccepted(email: EmailMessage) -> bool:
    """Лист без роботи: не прийнятий і не привʼязаний до замовлення."""
    return email.status == "нове" and not email.order_id


# Скільки пілот пошти в CRM вважається живим після останнього прийнятого листа.
# Дві доби — щоб вихідні й нічна зміна без прийнять не вимикали попередження.
MAIL_PILOT_ALIVE = timedelta(days=2)


def mail_pilot_active(db: Session) -> bool:
    """Чи ведуть зараз пошту через CRM: за `MAIL_PILOT_ALIVE` прийнято хоч один
    лист (прийняття = робота з пошти; «Повернути в тріаж» роботу видаляє, тож
    скасоване прийняття не рахується).

    Навіщо (власник 01.10.26). Попередження «лист НЕ прийнято через пошту»
    ловить коронку, яку переклали в пошті, але ніде не вписали. Коли ж УСЮ
    пошту ведуть в ukr.net, а роботи вносять у таблицю руками (з 30.09 11:11),
    воно висить на кожному листі — 124 за ніч — і нічого не ловить, лише
    топить справжні проблеми в журналі. CRM рядок таблиці з листом не звʼязує,
    тож відрізнити «вписали руками» від «загубили» тут не можна. Прийняли
    знову лист через CRM — попередження повертаються самі.

    `no_autoflush`: запис журналу лише додається в сесію, а синк пошти тримає
    її поруч із зверненнями до IMAP — змивати незакомічене звідси не можна
    (блокування SQLite на час мережі, CLAUDE.md §14 «Пошта»)."""
    since = utc_now() - MAIL_PILOT_ALIVE
    with db.no_autoflush:
        found = db.scalar(
            select(func.count()).select_from(Order).where(
                Order.source_email_id.is_not(None),
                Order.created_at >= since,
            )
        )
    return bool(found)


def log_folder_move(
    db: Session,
    email: EmailMessage,
    source: str | None,
    target: str | None,
    *,
    via: str,
    user: str | None = None,
) -> None:
    """Записати перенос листа. `source`/`target` — назва папки, None = «Вхідні».

    `via`: VIA_CRM (кнопка/масова дія, `user` — хто), VIA_ACCEPT (прийняття в
    чергу, `user` — хто), VIA_MAILBOX (синк побачив, що лист перемістили прямо
    в пошті — людини тут не знає ніхто, тому й не пишемо), VIA_SELF_FIX (синк
    зняв хибну мітку папки з листа, що лежить у Вхідних — це не перенос людини).
    """
    src = source or INBOX
    dst = target or INBOX
    if src == dst:
        return
    if via == VIA_CRM:
        how = f"через CRM, {user or 'оператор ?'}"
    elif via == VIA_ACCEPT:
        how = f"при прийнятті в чергу, {user or 'оператор ?'}"
    elif via == VIA_SELF_FIX:
        how = "CRM виправила власну хибну мітку: лист весь час лежав у Вхідних"
    else:
        how = "прямо в пошті (не через CRM)"
    # Ризиковий випадок: лист без роботи покидає Вхідні — у папку чи «в нікуди».
    risky = dst != INBOX and is_unaccepted(email)
    # Перенос прямо в пошті, коли пілот CRM-пошти на паузі, — норма, а не
    # ризик (див. `mail_pilot_active`). Рядок у журналі лишається, без «⚠».
    if risky and via == VIA_MAILBOX and not mail_pilot_active(db):
        risky = False
    message = f"{_describe(email)}: «{src}» → «{dst}» · {how}"
    if risky:
        # «Роботи немає» сказати не можна: її могли внести руками через
        # «Додати роботу» — з листом CRM такий рядок не повʼязує.
        message += " · ⚠ лист НЕ прийнято через пошту — перевір, чи робота є в черзі"
    db.add(SyncLog(direction=DIRECTION, status="warning" if risky else "ok", message=message))
