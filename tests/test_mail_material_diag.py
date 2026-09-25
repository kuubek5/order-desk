"""kmill_mail_material: чому картка листа підставила (чи ні) матеріал (25.09.26)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import EmailMessage, Order


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_explains_owner_case_step_by_step():
    from app.business_day import utc_now
    from app.material_catalog import backfill_orders
    from app.services.material_suggest import invalidate_cache
    from app.services.mcp_tools import tool_mail_material

    db = _db()
    for text, n in {"mono bl2": 6, "emo bl2": 6, "emo a3": 6}.items():
        for _ in range(n):
            db.add(Order(source="lab", status="нове", material_color=text,
                         created_at=utc_now() - timedelta(days=1)))
    db.flush()
    backfill_orders(db, only_unresolved=False)
    email = EmailMessage(uid="1", from_address="d@x", subject="майстерня . пац сидор",
                         body_text="циркон блич 2 емоутион мульти или какой у вас там обычно.",
                         status="нове", attachments_status="ready",
                         material_color_guess="Цирконій")
    db.add(email)
    db.commit()
    invalidate_cache()
    try:
        out = tool_mail_material(db, {"email_id": email.id})
    finally:
        invalidate_cache()
    assert out["відтінки"] == ["bl2"]
    assert any(t["канон"] == "emo" for t in out["слова_тексту"])
    assert out["поле_картки"] == "emo bl2"
    # Текст листа цілком не віддаємо — лише слова, що зіставились.
    assert "сидор" not in str(out)
