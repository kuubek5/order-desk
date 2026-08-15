"""The "ready" filter for the queue screen (CLAUDE.md section 9, screen 1) —
independent of the existing `period` filter (today/yesterday/tomorrow/earlier)
in app/web.py::get_queue, combined via a separate `ready` query parameter.

"Ready" means a folder was actually resolved on disk by
`attach_job_code_folder_uris()` or `attach_export_folder_uris()`. Kept DB-free
so callers can attach those transient attributes in one batched pass before
filtering.
"""

from app.models import EmailMessage, Order

READY_FILTERS = ("all", "not_ready", "can_take", "in_work")
SOURCE_FILTERS = ("all", "lab", "client")
HANDOUT_SOURCE_FILTERS = ("all", "email")

# "Клієнти" groups every email-client work: auto-parsed IMAP orders ("email")
# and clients typed straight into the sheet without a наряд ("sheet_client").
# "Лабораторія" is the internal work source ("lab").
CLIENT_SOURCES = ("email", "sheet_client")

# Visual-only filter for the mail triage screen (CLAUDE.md screen 2):
# "milling" hides nothing from the database, it just narrows what's shown in
# the current view. "all" (the default) always shows every pending email —
# every email stays reachable at /mail/{id} regardless of this filter, and no
# email is ever excluded from GET /mail's underlying query, only from this
# in-memory list. See app.mail_parser.guess_service_type for how the guess is
# produced.
SERVICE_TYPE_FILTERS = ("all", "milling", "other")


def filter_by_source(orders: list[Order], source: str) -> list[Order]:
    """Filter the queue by origin: "lab" (internal works) or "client" (all
    email clients — IMAP + sheet-entered). Unknown values show every source."""
    if source == "lab":
        return [order for order in orders if order.source == "lab"]
    if source == "client":
        return [order for order in orders if order.source in CLIENT_SOURCES]
    return list(orders)


def count_by_source(orders: list[Order]) -> dict[str, int]:
    """Counts for source chips, scoped to the already selected period."""
    return {
        "all": len(orders),
        "lab": sum(1 for order in orders if order.source == "lab"),
        "client": sum(1 for order in orders if order.source in CLIENT_SOURCES),
    }


def group_has_email_order(orders: list[Order]) -> bool:
    """Whether a handout (screen 4) client group contains at least one
    email-sourced order — used to power the "тільки з пошти" filter."""
    return any(order.source == "email" for order in orders)


def filter_client_groups_by_source(client_groups: list[dict], source: str) -> list[dict]:
    """Filters handout screen client groups (each a dict with an "orders"
    key, as built by get_handout in app/web.py) down to those with at least
    one order from the given source.

    Only "email" narrows the list; "all" and any unrecognized value show
    every group, so a bad/stale query param degrades to no filtering rather
    than an error page (mirrors filter_by_source/filter_by_readiness above).
    """
    if source == "email":
        return [group for group in client_groups if group_has_email_order(group["orders"])]
    return list(client_groups)


def count_client_groups_by_source(client_groups: list[dict]) -> dict[str, int]:
    """Counts for the handout screen's source filter tabs."""
    email_count = sum(1 for group in client_groups if group_has_email_order(group["orders"]))
    return {"all": len(client_groups), "email": email_count}


def filter_emails_by_service_type(
    emails: list[EmailMessage], service: str
) -> list[EmailMessage]:
    """Visually narrows the mail triage list by guess_service_type's guess.

    "milling" keeps only emails with no 3D-print signal (service_type_guess
    is None) — i.e. the default assumption for this mailbox. "other" keeps
    only emails flagged "3d_print". Any other value (including "all" and any
    stale/unknown query param) shows everything, same degrade-to-safe pattern
    as filter_by_source/filter_by_readiness above — this is a display filter
    only, never a way to make a message unreachable.
    """
    if service == "milling":
        return [email for email in emails if email.service_type_guess is None]
    if service == "other":
        return [email for email in emails if email.service_type_guess == "3d_print"]
    return list(emails)


def count_by_service_type(emails: list[EmailMessage]) -> dict[str, int]:
    """Counts for the mail triage screen's service-type filter chips."""
    other_count = sum(1 for email in emails if email.service_type_guess == "3d_print")
    return {
        "all": len(emails),
        "milling": len(emails) - other_count,
        "other": other_count,
    }


def _has_path(order: Order) -> bool:
    """Whether the work is "handed off and ready to take".

    For a lab work that means the technician wrote the working-directory path
    (Order.job_code). Client works (email / sheet-entered) have no technician
    path — the client already sent the files, so they are ready by nature.
    Treating them as path-present keeps them out of "Не готово" and lets the
    readiness split fall on their Sum3D like everything else (no Sum3D → «Можна
    брати», has Sum3D → «В роботі»)."""
    if order.source in CLIENT_SOURCES:
        return True
    return bool(order.job_code and order.job_code.strip())


def _has_sum3d(order: Order) -> bool:
    """Operator has taken the job into work: they calculated it in Sum3D and
    wrote the project ID back. Mirrors what the queue row shows (see
    _order_row.html): for a rework the live ID is the rework's own
    (active_rework.sum3d_id, sheet column W), otherwise the base run's
    (Order.sum3d_id, column L). This keeps the filter consistent with the
    editable ID the operator actually sees in the row — a rework whose redo
    hasn't been calculated yet reads as "можна набрати", not "в роботі"."""
    rework = order.active_rework
    value = rework.sum3d_id if rework else order.sum3d_id
    return bool(value and value.strip())


def is_order_ready(order: Order) -> bool:
    """Kept as the "lab handed it off" predicate (path filled), regardless of
    whether the operator has taken it yet. Used by callers that only care that
    the technician is done."""
    return _has_path(order)


def order_can_take(order: Order) -> bool:
    """«Можна набрати»: technician dropped the path but no Sum3D ID yet — the
    operator can pick it up and calculate it."""
    return _has_path(order) and not _has_sum3d(order)


def order_in_work(order: Order) -> bool:
    """«В роботі»: path is filled AND the operator already set a Sum3D ID, so
    the job is being calculated/milled, not waiting to be picked up."""
    return _has_path(order) and _has_sum3d(order)


def filter_by_readiness(orders: list[Order], ready: str) -> list[Order]:
    """Filter orders by lifecycle readiness:

      * "not_ready" — technician has not dropped the path yet (job_code empty)
      * "can_take"  — path filled, no Sum3D ID → operator can pick it up
      * "in_work"   — path filled AND Sum3D ID set → operator already took it

    An unrecognized `ready` value behaves like "all" (no filtering) rather
    than raising, so a bad/stale query param degrades to showing everything
    instead of an error page.
    """
    if ready == "not_ready":
        return [order for order in orders if not _has_path(order)]
    if ready == "can_take":
        return [order for order in orders if order_can_take(order)]
    if ready == "in_work":
        return [order for order in orders if order_in_work(order)]
    return list(orders)


def count_by_readiness(orders: list[Order]) -> dict[str, int]:
    """Counts `orders` per readiness bucket, for the filter chips' badges."""
    can_take = sum(1 for order in orders if order_can_take(order))
    in_work = sum(1 for order in orders if order_in_work(order))
    not_ready = sum(1 for order in orders if not _has_path(order))
    return {
        "all": len(orders),
        "not_ready": not_ready,
        "can_take": can_take,
        "in_work": in_work,
    }
