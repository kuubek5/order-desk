from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(50), default="оператор")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    sheet_tab: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    row_number: Mapped[Optional[int]] = mapped_column(nullable=True)
    work_order_no: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    job_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    quantity: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    material_color: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    kind: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    due_time: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    technician_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    cam_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sum3d_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    calculated_raw: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    milled_raw: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    last_milled_date: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    mill_count: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    client_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # Resolved material category (app/material_classifier.py maps the free-text
    # material_color onto the Material catalog). NULL = unresolved, surfaced for
    # an operator to classify; never guessed.
    material_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("materials.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(100), default="нове")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )

    status_events: Mapped[list["StatusEvent"]] = relationship(
        "StatusEvent", back_populates="order", cascade="all, delete-orphan"
    )
    comments: Mapped[list["Comment"]] = relationship(
        "Comment", back_populates="order", cascade="all, delete-orphan"
    )
    rework_records: Mapped[list["ReworkRecord"]] = relationship(
        "ReworkRecord", back_populates="order", cascade="all, delete-orphan"
    )
    material: Mapped[Optional["Material"]] = relationship("Material")

    @property
    def active_rework(self) -> Optional["ReworkRecord"]:
        """The order's current rework, or None. Reworks are sheet-sourced and
        upserted one-per-order (see app/sync.py::_sync_rework), so the latest by
        created_at is the live one. Drives the queue rework badge and routes the
        Sum3D-ID write to the redo column instead of the main one."""
        if not self.rework_records:
            return None
        return max(self.rework_records, key=lambda r: r.created_at or datetime.min)


class StatusEvent(Base):
    __tablename__ = "status_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    operator_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(100))
    actor: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )

    order: Mapped["Order"] = relationship("Order", back_populates="status_events")
    operator: Mapped[Optional["User"]] = relationship("User")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    source: Mapped[str] = mapped_column(String(50))
    author: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    order: Mapped["Order"] = relationship("Order", back_populates="comments")


class ReworkRecord(Base):
    __tablename__ = "rework_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    occurrence: Mapped[Optional[int]] = mapped_column(nullable=True)
    blame: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    blame_quantity: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    redo_quantity: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    cam_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sum3d_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    calculated_raw: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    milled_raw: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )

    order: Mapped["Order"] = relationship("Order", back_populates="rework_records")


class ClientNameAlias(Base):
    __tablename__ = "client_name_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    sheet_name: Mapped[str] = mapped_column(String(200), index=True)
    export_folder_name: Mapped[str] = mapped_column(String(200))
    confirmed: Mapped[bool] = mapped_column(default=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )


class Material(Base):
    """A material category in the catalog (Цирконій / ПММА / СЛМ / Титан, plus
    a non-production "Не матеріал" bucket). Categories are stable; the raw
    spellings that resolve to them live in MaterialAlias. is_production=False
    keeps stage/part rows out of material production statistics."""

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_production: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=100)

    aliases: Mapped[list["MaterialAlias"]] = relationship(
        "MaterialAlias", back_populates="material", cascade="all, delete-orphan"
    )


class MaterialAlias(Base):
    """One raw-text rule mapping the free-text colour column onto a Material.

    match_type: "token" (exact whitespace token — for numeric colour codes and
    `ti`, which must not match as a substring) or "contains" (substring). New
    rows accumulate as operators classify previously-unresolved colours, so the
    classifier asks less over time — same idea as ClientNameAlias for handout."""

    __tablename__ = "material_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), index=True)
    pattern: Mapped[str] = mapped_column(String(200), index=True)
    match_type: Mapped[str] = mapped_column(String(20), default="contains")
    confirmed: Mapped[bool] = mapped_column(default=True)

    material: Mapped["Material"] = relationship("Material", back_populates="aliases")


class Client(Base):
    """A client profile the operator maintains directly (contact info, notes).

    Deliberately NOT linked to Order via a foreign key. Order.client_name
    stays free text, populated by three independent write paths (sheet
    import, mail-order accept, sheet-side manual entry) that already work in
    production. A Client's orders are found at read time by fuzzy-matching
    canonical_name against Order.client_name (see app/client_profile.py),
    not by a stored client_id — see that module's docstring for the reasoning.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(50))
    sheet_tab: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50))
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    from_address: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    client_name_guess: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    material_color_guess: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    kind_guess: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    quantity_guess: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Best-effort hint from app.mail_parser.guess_service_type: "3d_print" if
    # the message looks like it's about 3D printing (a service this lab does
    # NOT offer) rather than milling, else None. Purely advisory for the
    # triage screen — never used to hide a message (CLAUDE.md screen 2).
    service_type_guess: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="нове")
    # Two-phase IMAP fetch (app.mail_reader.fetch_new_emails): "pending" means
    # the row exists (headers only, phase 1) but body/attachments have not
    # been downloaded yet; "ready" means phase 2 finished (successfully, even
    # if the message genuinely has zero attachments). Never a third "failed"
    # state — a failed phase-2 attempt is simply left/reset to "pending" so
    # the next sync run (2 min later, or manual) retries automatically.
    attachments_status: Mapped[str] = mapped_column(String(20), default="pending")
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now())

    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", back_populates="email_message", cascade="all, delete-orphan"
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_message_id: Mapped[int] = mapped_column(ForeignKey("email_messages.id"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    saved_path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now())

    email_message: Mapped["EmailMessage"] = relationship("EmailMessage", back_populates="attachments")
