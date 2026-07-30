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
