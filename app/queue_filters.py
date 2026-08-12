"""The "ready" filter for the queue screen (CLAUDE.md section 9, screen 1) —
independent of the existing `period` filter (today/yesterday/tomorrow/earlier)
in app/web.py::get_queue, combined via a separate `ready` query parameter.

"Ready" means a folder was actually resolved on disk by
`attach_job_code_folder_uris()` or `attach_export_folder_uris()`. Kept DB-free
so callers can attach those transient attributes in one batched pass before
filtering.
"""

from app.models import EmailMessage, Order

READY_FILTERS = ("all", "ready", "not_ready")
SOURCE_FILTERS = ("all", "lab", "email")
HANDOUT_SOURCE_FILTERS = ("all", "email")

# Visual-only filter for the mail triage screen (CLAUDE.md screen 2):
# "milling" hides nothing from the database, it just narrows what's shown in
# the current view. "all" (the default) always shows every pending email —
# every email stays reachable at /mail/{id} regardless of this filter, and no
# email is ever excluded from GET /mail's underlying query, only from this
# in-memory list. See app.mail_parser.guess_service_type for how the guess is
# produced.
SERVICE_TYPE_FILTERS = ("all", "milling", "other")


def filter_by_source(orders: list[Order], source: str) -> list[Order]:
    """Filter the queue by origin; unknown values safely show all sources."""
    if source in ("lab", "email"):
        return [order for order in orders if order.source == source]
    return list(orders)


def count_by_source(orders: list[Order]) -> dict[str, int]:
    """Counts for source chips, scoped to the already selected period."""
    return {
        "all": len(orders),
        "lab": sum(1 for order in orders if order.source == "lab"),
        "email": sum(1 for order in orders if order.source == "email"),
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


def is_order_ready(order: Order) -> bool:
    """Whether the technician has finished this job and the operator can take it
    into work now. The marker is a filled working-directory path — the sheet's
    "Номер роботи" (Order.job_code): the lab writes it once the model is done
    and the files are on the server, so a non-empty job_code means "ready to
    take". (This is the operator-readiness signal, distinct from the on-disk
    export folder used for morning handout.)"""
    return bool(order.job_code and order.job_code.strip())


def filter_by_readiness(orders: list[Order], ready: str) -> list[Order]:
    """Filter orders by an actually resolved on-disk folder.

    An unrecognized `ready` value behaves like "all" (no filtering) rather
    than raising, so a bad/stale query param degrades to showing everything
    instead of an error page.
    """
    if ready == "ready":
        return [order for order in orders if is_order_ready(order)]
    if ready == "not_ready":
        return [order for order in orders if not is_order_ready(order)]
    return list(orders)


def count_by_readiness(orders: list[Order]) -> dict[str, int]:
    """Counts `orders` per readiness bucket, for the filter chips' badges."""
    ready_count = sum(1 for order in orders if is_order_ready(order))
    return {
        "all": len(orders),
        "ready": ready_count,
        "not_ready": len(orders) - ready_count,
    }
