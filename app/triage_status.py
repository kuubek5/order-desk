"""Field-completeness of a triage letter — the queue's «готово / перевірити»
badge (replacing the old «розпізнано», which only meant "not 3D-print" and read
as if every field had been parsed).

Single source of truth for what "ready to accept" means, so changing the
required-field set is a one-line edit here, not a template hunt. Used as a Jinja
global (registered in app/web.py) by _mail_triage_list.html and the detail panel.
"""

from __future__ import annotations


def triage_readiness(email) -> dict:
    """Return {"state": ..., "missing": [labels]} for one triage letter.

    state:
      "3d"         — a 3D-print hint (email.service_type_guess). The lab mills,
                     it doesn't print, so this isn't ours; shown until an
                     operator turns it into a filter rule (see MailFilterRule).
                     Highest precedence.
      "incomplete" — a required field wasn't recognised; `missing` names it so
                     the badge can say «перевірити: матеріал».
      "ready"      — everything needed to accept in one click is present.

    Required field = material only. The client is effectively always known (the
    sender address stands in when no name was parsed) and quantity defaults to 1
    on accept, so neither blocks readiness. Change the checks here to change what
    «готово» means across the whole UI.
    """
    if getattr(email, "service_type_guess", None) == "3d_print":
        return {"state": "3d", "missing": []}

    # Файли на ДИСКУ, а не рядки в базі. Панель уже цю правду знає, а список —
    # ні, і бейдж «ГОТОВО» стояв на листі, чиї файли хтось видалив: рядки в
    # базі є, приймати нема чого. Оператор веде очима саме по списку, тож
    # приймав роботу без жодного STL. Готовність без файлів неможлива за
    # означенням, тому цей стан перебиває решту.
    if _has_rows_but_no_files(email):
        return {"state": "no_files", "missing": ["файли"]}

    missing: list[str] = []
    if not (getattr(email, "material_color_guess", None) or "").strip():
        missing.append("матеріал")

    return {"state": "ready" if not missing else "incomplete", "missing": missing}


def _has_rows_but_no_files(email) -> bool:
    """Вкладення в базі є, а на диску їх немає (теку прибрали після скачування).

    Прийняте в чергу вкладення не рахуємо: його файл переїхав у export, і
    відсутність у спулі — норма, а не втрата.
    """
    from pathlib import Path as _Path

    attachments = [
        a for a in getattr(email, "attachments", []) or []
        if getattr(a, "order_id", None) is None
    ]
    if not attachments:
        return False
    return not any(_Path(a.saved_path).exists() for a in attachments)


def files_on_disk(email) -> int:
    """Скільки вкладень листа реально лежить на диску.

    Список тріажу писав `email.attachments|length` — тобто РЯДКИ бази. Панель
    від цієї ж брехні вже полагодили, а список, за яким оператор веде очима,
    лишався. Одна функція на обидва місця, щоб вони не розійшлись знову.

    Прийняте в чергу вкладення рахується наявним: його файл переїхав у export,
    і відсутність у спулі — норма.
    """
    from pathlib import Path as _Path

    total = 0
    for attachment in getattr(email, "attachments", []) or []:
        if getattr(attachment, "order_id", None) is not None:
            total += 1
        elif _Path(attachment.saved_path).exists():
            total += 1
    return total
