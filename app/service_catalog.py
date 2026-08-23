"""Bridge between the pure service classifier (app/service_classifier.py) and
the DB table (ServiceKeyword).

The migration seeds the table for real installs; ensure_seeded() covers the
create_all path (tests, first boot) idempotently. load_service_rows() returns
the rules as classifier ServiceKeywordRow objects for guess_service_type.
Mirrors app/material_catalog.py — keep the two in step when either changes.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.service_classifier import (
    SEED_SERVICE_KEYWORDS,
    ServiceKeywordRow,
    normalize_service,
)
from app.models import ServiceKeyword

VALID_MATCH_TYPES = ("contains", "token")
VALID_SERVICE_TYPES = ("3d_print",)


class ServiceCatalogError(ValueError):
    """User-facing, safe validation error for service-keyword edits."""


def ensure_seeded(session: Session) -> None:
    """Seed the keyword table from the classifier taxonomy if it's empty.
    Idempotent: a no-op once any keyword exists, so it's safe on every boot and
    in tests that build the schema via create_all (the migration seed skips
    those)."""
    if session.execute(select(ServiceKeyword.id).limit(1)).first() is not None:
        return
    for service_type, keywords in SEED_SERVICE_KEYWORDS.items():
        for pattern, match_type in keywords:
            session.add(
                ServiceKeyword(
                    pattern=pattern,
                    match_type=match_type,
                    service_type=service_type,
                    confirmed=True,
                )
            )
    session.flush()


def load_service_rows(session: Session) -> list[ServiceKeywordRow]:
    """All keyword rules as classifier ServiceKeywordRow objects. Load once per
    sync, not per message."""
    rows = session.execute(
        select(ServiceKeyword.pattern, ServiceKeyword.match_type, ServiceKeyword.service_type)
    ).all()
    return [ServiceKeywordRow(pattern=p, match_type=mt, service_type=st) for p, mt, st in rows]


def list_keywords(session: Session) -> list[ServiceKeyword]:
    """Keywords in a stable display order for the settings screen."""
    return list(
        session.execute(
            select(ServiceKeyword).order_by(ServiceKeyword.service_type, ServiceKeyword.pattern)
        ).scalars()
    )


def add_keyword(
    session: Session,
    pattern: str,
    match_type: str,
    service_type: str = "3d_print",
) -> ServiceKeyword:
    """Add one keyword rule after validating and normalizing it. Raises
    ServiceCatalogError on bad input or a duplicate."""
    if match_type not in VALID_MATCH_TYPES:
        raise ServiceCatalogError("Невідомий тип зіставлення.")
    if service_type not in VALID_SERVICE_TYPES:
        raise ServiceCatalogError("Невідомий тип послуги.")
    normalized = normalize_service(pattern)
    if not normalized:
        raise ServiceCatalogError("Порожній шаблон.")
    if len(normalized) > 200:
        raise ServiceCatalogError("Шаблон задовгий.")
    exists = session.scalar(
        select(ServiceKeyword.id).where(
            ServiceKeyword.pattern == normalized,
            ServiceKeyword.match_type == match_type,
            ServiceKeyword.service_type == service_type,
        )
    )
    if exists is not None:
        raise ServiceCatalogError(f"Правило «{normalized}» вже існує.")
    keyword = ServiceKeyword(
        pattern=normalized, match_type=match_type, service_type=service_type, confirmed=True
    )
    session.add(keyword)
    session.flush()
    return keyword


def delete_keyword(session: Session, keyword_id: int) -> None:
    keyword = session.get(ServiceKeyword, keyword_id)
    if keyword is not None:
        session.delete(keyword)
        session.flush()
