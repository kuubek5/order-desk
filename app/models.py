from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(50), default="оператор")
    # The 1-2 letter initial the operator writes in the sheet's "Прорахував"
    # column (М/Х) to mark who calculated a work in Sum3D (Р=Рома, К=Костя,
    # СТ=Стас…). Filled by the portal automatically when this operator enters a
    # Sum3D ID, so the sheet keeps its existing human convention. Unique across
    # operators (enforced in the settings route); NULL until an admin assigns it.
    sheet_initial: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    # Візуальні налаштування оператора (кабінет /account): тема інтерфейсу
    # ("" = бірюзовий канон, "forge" = Amber Forge) і стиль іконок
    # ("" = канон, "thin"/"duo"/"bold"/"neon"). Рендеряться сервер-сайд
    # атрибутами на <html> у base.html — тому підуть за оператором на будь-який
    # браузер цього ПК, а не житимуть у localStorage конкретного профілю.
    ui_theme: Mapped[str] = mapped_column(String(20), default="", server_default="")
    ui_icon_style: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # Решта візуального набору з галереї «графічний фонд»: трактування кнопок,
    # індикатор очікування і форма чіпів. Порожній рядок скрізь = канон, тобто
    # рівно те, що бачить оператор, який нічого не міняв.
    ui_button_style: Mapped[str] = mapped_column(String(20), default="", server_default="")
    ui_loader_style: Mapped[str] = mapped_column(String(20), default="", server_default="")
    ui_chip_style: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # Вигляд списку листів на екрані тріажу (шестерня над списком). Усе в
    # пікселях, 0 = «як було»: вертикальний відступ рядка, ширина панелі
    # списку і крок, яким оператор ці два значення підкручує. Тримається тут,
    # а не в localStorage, з тієї ж причини, що й решта візуального набору:
    # налаштування їде за оператором на будь-який браузер цього ПК і
    # повертається при наступному вході. Застосовується сервер-сайд на
    # <main class="mailv2">, тому список не мигає канонним виглядом.
    mail_row_pad: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    mail_list_width: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    mail_ui_step: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Те саме для черги. Щільність і вигляд колонки «Матеріал / Колір» жили в
    # localStorage — тобто гинули при зміні браузера й не їхали за оператором.
    # Переїхали сюди ЄДИНИМ джерелом: два місця для одного значення рано чи
    # пізно розійшлись би. Ширини стовпців свідомо лишились локальними — вони
    # прив'язані до конкретного монітора, а не до людини.
    queue_density: Mapped[str] = mapped_column(String(20), default="", server_default="")
    queue_row_pad: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    queue_mat_style: Mapped[str] = mapped_column(String(20), default="", server_default="")
    queue_ui_step: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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
    # A technician corrected this row in the sheet after we imported it. The
    # corrected row looks identical on screen, so without this the operator can
    # mill the version they read minutes ago — scrap that costs money. NULL =
    # nothing to flag; a timestamp plus a short human list of what changed
    # ("колір, шлях") drives the queue's change badge. Cleared only when the
    # operator dismisses it, never on a timer. `updated_at` cannot stand in:
    # it also moves on our own Sum3D/status write-backs.
    sheet_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    sheet_changed_fields: Mapped[Optional[str]] = mapped_column(
        String(400), nullable=True
    )
    # Archive marker. NULL = active (lives in the working queue while its
    # sheet-tab date is within the retention window). Set to a timestamp when
    # the order should leave the working space but be KEPT for the archive: it
    # vanished from Google (a whole tab or a single row removed — the lab
    # deletes old tabs to free space, which must never lose our copy) OR it was
    # explicitly archived. The order is never hard-deleted; archived rows stay
    # searchable on the Archive screen. Ageing out of the window is derived from
    # the tab date, so it needs no write here — only early removal sets this.
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True, index=True
    )
    # The triage letter this order was accepted from (source == "email" only).
    # One letter can spawn SEVERAL orders — a client sends multiple colours in
    # one email and the operator accepts them in batches (partial accept), each
    # batch its own order. NULL for sheet-sourced orders.
    source_email_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("email_messages.id"), nullable=True, index=True
    )
    # Auto-accepted (no operator review) — future auto-list feature stamps this
    # so the history/badge can say so. NULL/False = accepted through the wizard.
    auto_accepted: Mapped[bool] = mapped_column(default=False)

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


class ActionLog(Base):
    """One row per state-CHANGING operator action — the shared backbone for both
    "Скасувати" (undo the last action) and the laconic action journal.

    ``field``/``old_value``/``new_value`` let undo restore the previous state
    (write the old value back to DB + sheet); ``note`` is the pre-rendered
    one-line human summary the journal shows ("Sum3D → 12-01-45"). ``undone_at``
    is stamped when the action is reverted, so it shows as undone and can't be
    undone twice. Read-only actions (open folder, copy path) are NOT logged —
    only things that changed data. order_id is nullable so an action on an
    already-deleted order still keeps its journal line."""
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id"), index=True, nullable=True
    )
    operator_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    action_type: Mapped[str] = mapped_column(String(50), index=True)
    field: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Локальний час, БЕЗ server_default=func.now(): на SQLite func.now() пише
    # UTC, і /journal показував кожну дію на 3 години в минулому. Це екран, за
    # яким розбирають брак і чия це провина, тож час тут і є змістом — та сама
    # причина, з якої від server_default відмовились у ShiftNote.
    # Наявні рядки лишаються в UTC: разова міграція нижче їх не чіпає свідомо,
    # бо зсув невідомий для машин в інших зонах.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=datetime.now, index=True
    )
    undone_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    order: Mapped[Optional["Order"]] = relationship("Order")
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
    # NULL = no operator has opened this letter's triage card yet → the row is
    # highlighted as "unread by me" in the "Нові з пошти" list. Stamped the
    # first time any operator opens the detail panel (GET /mail/{id}). Shared,
    # not per-user (max two operators — a single seen flag is enough).
    seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)
    # JSON list of download-link refs (file_id/url) already pulled from the body
    # (or found already on disk). Lets «Файли + STL» count only links STILL to
    # download, so the "ще N за посиланням" warning clears once all are fetched.
    handled_link_refs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Mail filtering (screen 2's «Відфільтровані» tab): a matched MailFilterRule
    # stamps its category + id here. The letter is NEVER deleted or hidden for
    # good — status stays "нове", it just moves to the filtered tab and one
    # click (unfilter) brings it back. NULL = not filtered, shown in the queue.
    filter_category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    filter_rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("mail_filter_rules.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now())

    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", back_populates="email_message", cascade="all, delete-orphan"
    )


class MailFilterRule(Base):
    """One triage-filter rule: keyword-in-text or sender-substring → category.

    Matching letters are routed to the «Відфільтровані» tab instead of the main
    triage list — categorised, never deleted (CLAUDE.md screen 2: a silently
    missing letter is unacceptable). `enabled=False` rules don't match; a
    disabled SENDER rule doubles as "operator declined the suggestion for this
    sender" so the suggest-banner doesn't nag again (see web.py reject flow).
    """

    __tablename__ = "mail_filter_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "keyword" (substring of subject+body, case-insensitive) or
    # "sender" (substring of from_address, case-insensitive — an address or a
    # whole domain like "@buh.example.com").
    kind: Mapped[str] = mapped_column(String(20))
    pattern: Mapped[str] = mapped_column(String(300))
    # Free-text category shown as the badge/reason: "3D-друк", "бухгалтерія",
    # "спам", … — no fixed enum, the admin names their own buckets.
    category: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(default=True)
    # How many letters this rule has filtered — visibility into what a rule
    # actually does, and the evidence for keeping/removing it.
    hits: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class MailFilterCategory(Base):
    """Editable list of filter categories (the buckets on «Відфільтровані»).

    Seeded with the four defaults (3D-друк, бухгалтерія, спам, інше) by
    migration 0010; admins add/rename/delete their own on the settings screen.
    Renaming cascades into existing rules and stamped letters (web.py); deleting
    is refused while any rule still uses the name — stamped letters keep the old
    string as history, they never block.
    """

    __tablename__ = "mail_filter_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class ClientSenderMemory(Base):
    """«Памʼять відправника»: what the operator did the LAST time a letter came
    from this sender — which client name they typed and which export folder the
    files landed in. Deterministic recurring-client identification for the
    accept wizard (the sender address is the one signal a client repeats
    reliably; names/subjects drift). See app/sender_memory.py.

    sender_key = lower-cased from_address, plus "|<original sender>" when the
    letter was forwarded (one forwarder relays many clients — the quoted From:
    in the body disambiguates). Upserted on every accept, so the memory keeps
    following the operator's latest correction.
    """

    __tablename__ = "client_sender_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_key: Mapped[str] = mapped_column(String(400), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(200))
    export_folder: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    orders_count: Mapped[int] = mapped_column(default=1)
    # Trusted sender: their letters are auto-accepted on arrival (files moved to
    # export, order created, no operator step) — but ONLY when the guardrails
    # pass (single confident material, no files-behind-link). Operator toggles
    # this per sender on the «Авто-прийняття» screen. Default False.
    auto_accept: Mapped[bool] = mapped_column(default=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_message_id: Mapped[int] = mapped_column(ForeignKey("email_messages.id"), index=True)
    # Which order (partial-accept batch) moved this file into export. NULL = the
    # file is still unclaimed (in the mail spool), i.e. the letter has more to
    # accept. Set on accept, cleared on restore.
    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id"), nullable=True, index=True
    )
    # Auto-moved into export ahead of acceptance (trusted-sender auto-download).
    # True = the file already lives in export (not the spool), so the manual
    # accept must NOT move it again — only stamp order_id. Cleared on restore.
    staged_to_export: Mapped[bool] = mapped_column(default=False)
    filename: Mapped[str] = mapped_column(String(300))
    saved_path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), server_default=func.now())

    email_message: Mapped["EmailMessage"] = relationship("EmailMessage", back_populates="attachments")


class ShiftNote(Base):
    """Одна записка передачі зміни (екран «Зміна»).

    Нічний оператор іде о ~05:00, наступний приходить о ~08:00 — три години,
    коли в цеху нікого. Печі, стан верстатів і «цю не запускай» зараз
    передаються СМС-ками; ця таблиця — їхня заміна.

    Час тут — зміст, а не метадані («піч №2 відкрити о 9:00»), тому жодне поле
    НЕ має server_default=func.now(): на SQLite func.now() пише UTC, і живий
    наслідок цього вже видно на ActionLog.created_at (/journal показує зсув
    3 години). Усі мітки ставить сервісний шар через datetime.now(). Прецедент
    обов'язкової недефолтної дати — ClientSenderMemory.last_seen_at.
    """

    __tablename__ = "shift_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "info" — до відома (зникає з дошки після прийняття);
    # "action" — потребує дії (лишається, доки хтось не закриє).
    kind: Mapped[str] = mapped_column(String(20), index=True)
    text: Mapped[str] = mapped_column(Text)
    author_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True)
    # Автор змінив текст. Редагування СКИДАЄ прийняття (див. сервіс): інакше
    # хтось «прийняв» один текст, а на дошці висить інший.
    edited_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    # Прочитано. Одне на записку, не персональний стан: перший, хто натиснув,
    # закриває для всіх.
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    acknowledged_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # Виконано — лише для kind="action".
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    resolved_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )

    # Три FK на users в одній таблиці — foreign_keys обов'язковий на КОЖНОМУ.
    # Без нього SQLAlchemy не виведе умову з'єднання й упаде
    # AmbiguousForeignKeysError на конфігурації маперів, тобто на імпорті
    # app.models — уся збірка червона ще до першого тесту. Зразок ActionLog
    # (relationship("User") без уточнень) тут скопіювати НЕ можна.
    author: Mapped[Optional["User"]] = relationship("User", foreign_keys=[author_id])
    acknowledged_by: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[acknowledged_by_id]
    )
    resolved_by: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[resolved_by_id]
    )
    images: Mapped[list["ShiftNoteImage"]] = relationship(
        "ShiftNoteImage",
        back_populates="note",
        cascade="all, delete-orphan",
        order_by="ShiftNoteImage.id",
    )


class ShiftNoteImage(Base):
    """Скріншот, вставлений у записку зміни (Ctrl+V, файл або drag'n'drop).

    Байти лежать під SHIFT_IMAGES_PATH за розкладкою <YYYY-MM>/<note_id>/<NN><ext>:
    ім'я на диску наше, з клієнтського імені жоден байт у шлях не потрапляє
    (Windows-скріншот у буфері завжди приходить як image.png — усі вставки мали
    б однакове ім'я). filename — санітизований оригінал ЛИШЕ для показу.
    """

    __tablename__ = "shift_note_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[int] = mapped_column(
        ForeignKey("shift_notes.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(300))
    saved_path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[Optional[int]] = mapped_column(nullable=True)
    width: Mapped[Optional[int]] = mapped_column(nullable=True)
    height: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True)
    # Файл прибрано автоприбиранням за 6 місяців — рядок лишається, щоб текст
    # записки й слід «тут був скріншот» жили далі. Бекап не несе байтів файлів,
    # тому шаблон малює відсутній файл ТАК САМО, як прибраний: один
    # деградований стан, не два.
    pruned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    note: Mapped["ShiftNote"] = relationship("ShiftNote", back_populates="images")


class OrderFocus(Base):
    """Особиста мітка «беру зараз» на роботі — робочий набір оператора.

    Навіщо: взявши кілька нарядів у роботу, оператор має не загубити, КУДИ
    вписувати Sum3D ID. На папері це робиться маркером; тут — мітка плюс
    фільтр «лише мої».

    Чому окрема таблиця, а не колонка в orders: мітка ПЕРСОНАЛЬНА. Одну роботу
    можуть тримати в наборі двоє (у зміні максимум двоє), і чужа мітка не має
    затирати мою — тому оператор входить у ключ.

    Мітка знімається сама, щойно вписано Sum3D: причина її існування зникла.
    """

    __tablename__ = "order_focus"
    __table_args__ = (
        # Подвійний клік (або дві вкладки) інакше дадуть два рядки — і
        # лічильник у бейджі почне брехати.
        UniqueConstraint("order_id", "user_id", name="uq_order_focus_order_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Час локальний, без server_default: на SQLite func.now() пише UTC (див.
    # ShiftNote). Тут це не зміст, але й розходження двох часових баз у сусідніх
    # таблицях нікому не потрібне.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))

    order: Mapped["Order"] = relationship("Order")
    user: Mapped["User"] = relationship("User")


class FurnaceReading(Base):
    """Один знімок табло печі: що показував екран у цю секунду.

    Пишеться НЕ на кожен кадр. Кадр знімається кожні кілька секунд (щоб
    картинка на екрані була живою), а рядок з'являється лише коли щось
    змінилось або минула хвилина — інакше одна піч давала б ~17 тис. рядків на
    добу заради даних, які не змінювались.

    Порожні temp_c / remaining_seconds — нормальний стан, а не збій: у цьому
    проєкті хибне число гірше за жодне (див. app/furnace_ocr.py). Тому поруч
    лежать raw_* — рівно те, що прочиталось із пікселів, разом зі знаками «?»
    на невпізнаних символах. За ними видно, ЧОМУ поле порожнє, і за ними ж
    донавчаються еталони цифр.

    Час локальний і без server_default=func.now(): на SQLite func.now() пише
    UTC, а тут час — це відповідь на питання «коли пекти закінчить», яку
    оператор звіряє з годинником на стіні (та сама причина, що в ShiftNote).
    """

    __tablename__ = "furnace_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Адреса печі, а не її назва: назву в налаштуваннях можуть перейменувати,
    # і історія показань не має від цього розсипатись.
    host: Mapped[str] = mapped_column(String(60), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True)
    # RUN / WAIT / "?" — останнє означає, що сигнали розійшлись між собою.
    status: Mapped[str] = mapped_column(String(10))
    temp_c: Mapped[Optional[int]] = mapped_column(nullable=True)
    remaining_seconds: Mapped[Optional[int]] = mapped_column(nullable=True)
    elapsed_seconds: Mapped[Optional[int]] = mapped_column(nullable=True)
    command: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    raw_temp: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    raw_remaining: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Кадр узагалі не знявся (піч вимкнена, мережа, пароль). Тоді решта полів
    # порожня, а рядок лишається слідом, що ми пробували і що саме сказала піч.
    error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class Furnace(Base):
    """Пічка спікання: назва, адреса екрана, чи стежимо за нею.

    Раніше перелік жив одним текстовим полем у налаштуваннях («Назва=адреса»
    по рядку). Це трималось, поки пічка була одна; з трьома потрібні окремі
    поля, вимикач і власний пароль — тобто рядки таблиці, а не рядки тексту.

    `enabled` — не те саме, що видалити. Пічку виводять із мережі на ремонт і
    повертають; вимкнена лишається в переліку зі своїми налаштуваннями, але не
    опитується й не показується у віджеті.

    `password_encrypted` — необов'язковий власний пароль. Порожній означає
    «спільний з налаштувань», і так буде майже завжди: пароль заводський, один
    на модель. Але дві моделі в цеху вже є, тому місце під різні паролі краще
    мати одразу, ніж переробляти таблицю потім.
    """

    __tablename__ = "furnaces"
    __table_args__ = (
        # Дві пічки на одній адресі й порту — це та сама пічка, заведена двічі.
        # Без цього вона опитувалась би двома потоками й писала подвійну історію.
        UniqueConstraint("host", "port", name="uq_furnace_host_port"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    host: Mapped[str] = mapped_column(String(60))
    port: Mapped[int] = mapped_column(default=5900)
    enabled: Mapped[bool] = mapped_column(default=True)
    password_encrypted: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # Порядок у переліку й у віджеті задає оператор: пічки в цеху стоять у
    # відомому йому порядку, і сортування за id чи назвою його ламає.
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
