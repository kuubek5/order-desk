"""Triage mail filtering: route non-milling mail out of the main list.

Rules (MailFilterRule) are admin-managed on the settings screen:
  kind="keyword" — case-insensitive substring of subject+body
  kind="sender"  — case-insensitive substring of from_address (address/domain)

A match stamps the letter's filter_category/filter_rule_id; the letter keeps
status "нове" and moves to the triage screen's «Відфільтровані» tab. Nothing is
ever deleted or hidden for good (CLAUDE.md screen 2: a letter silently missing
from triage is unacceptable) — unfiltering just clears the stamp.

First matching rule wins, keyword rules before sender rules, older rules first
within each kind — deterministic and explainable ("filtered by rule X").
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage, MailFilterRule


def _rule_matches(rule: MailFilterRule, email: EmailMessage) -> bool:
    pattern = (rule.pattern or "").strip().lower()
    if not pattern:
        return False
    if rule.kind == "sender":
        return pattern in (email.from_address or "").lower()
    if rule.kind == "keyword":
        haystack = f"{email.subject or ''}\n{email.body_text or ''}".lower()
        return pattern in haystack
    return False


def _enabled_rules(session: Session) -> list[MailFilterRule]:
    rules = session.scalars(
        select(MailFilterRule)
        .where(MailFilterRule.enabled.is_(True))
        .order_by(MailFilterRule.id.asc())
    ).all()
    # keyword rules first — a content match is a stronger, more specific signal
    # than a sender match, and the order must not depend on creation history.
    return sorted(rules, key=lambda r: (r.kind != "keyword", r.id))


def apply_filters_to_email(session: Session, email: EmailMessage) -> bool:
    """Stamp `email` with the first matching enabled rule. Returns True when a
    rule matched. Does not commit — the caller owns the transaction. Skips
    letters that are already stamped (an operator's unfilter must not be
    re-overridden by the next sync — only NEW letters get auto-filtered)."""
    if email.filter_category is not None:
        return False
    for rule in _enabled_rules(session):
        if _rule_matches(rule, email):
            email.filter_category = rule.category
            email.filter_rule_id = rule.id
            rule.hits = (rule.hits or 0) + 1
            return True
    return False


def apply_rule_retroactively(session: Session, rule: MailFilterRule) -> int:
    """Run one (enabled) rule over the letters currently sitting UNFILTERED in
    triage (status "нове") — so a freshly created rule immediately tidies the
    list instead of only affecting future syncs. Returns how many letters it
    stamped. Does not commit."""
    if not rule.enabled:
        return 0
    stamped = 0
    pending = session.scalars(
        select(EmailMessage).where(
            EmailMessage.status == "нове",
            EmailMessage.filter_category.is_(None),
        )
    ).all()
    for email in pending:
        if _rule_matches(rule, email):
            email.filter_category = rule.category
            email.filter_rule_id = rule.id
            stamped += 1
    if stamped:
        rule.hits = (rule.hits or 0) + stamped
    return stamped
