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

    missing: list[str] = []
    if not (getattr(email, "material_color_guess", None) or "").strip():
        missing.append("матеріал")

    return {"state": "ready" if not missing else "incomplete", "missing": missing}
