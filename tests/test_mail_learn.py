"""Навчання на прийнятих листах (`app/services/mail_learn.py`, 18.11.26):
що картка підставила б проти того, що лишив оператор, і чого бракувало."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import Attachment, EmailMessage, Order
from app.services import mail_learn, material_suggest
from app.services.mcp_tools import TOOLS_BY_NAME

NOW = datetime(2026, 11, 18, 12, 0)


@pytest.fixture(autouse=True)
def _fresh_material_cache():
    material_suggest.invalidate_cache()
    yield
    material_suggest.invalidate_cache()


@pytest.fixture
def db(db_session: Session) -> Session:
    # Як цех пише в таблиці — з цього будується канон слів.
    for text in ["kappa"] * 6 + ["pmma a3"] * 8 + ["mono a2"] * 8 + ["emo a2"] * 6:
        db_session.add(Order(source="lab", material_color=text, quantity="1", created_at=NOW))
    db_session.commit()
    return db_session


def _letter(db, *, subject="", body="", guess=None, files=("crown.stl",), operator="mono a2",
            client="Клініка Усмішка", quantity="1", when=NOW - timedelta(hours=2)) -> Order:
    email = EmailMessage(uid=f"u{db.query(EmailMessage).count()}", status="прийнято", subject=subject,
                         body_text=body, material_color_guess=guess, attachments_status="ready")
    db.add(email)
    db.flush()
    order = Order(source="email", source_email_id=email.id, client_name=client,
                  material_color=operator, quantity=quantity, created_at=when)
    db.add(order)
    db.flush()
    for name in files:
        db.add(Attachment(email_message_id=email.id, order_id=order.id, filename=name, saved_path=name))
    db.commit()
    return order


def _row(out, order):
    return next(r for r in out["приклади"] if r["робота"] == order.id)


def test_matching_letter_is_counted_not_listed(db):
    _letter(db, subject="pmma a3", guess="pmma a3", operator="pmma a3")
    out = mail_learn.report(db, 7, now=NOW)
    assert out["листів_звірено"] == 1 and out["точність_%"] == 100.0
    assert out["підсумок"] == {"збіг": 1} and out["приклади"] == []


def test_nothing_in_letter_but_client_habit_would_guess(db):
    for day in (10, 20, 30):
        db.add(Order(source="sheet_client", client_name="Клініка Усмішка", material_color="mono a2",
                     quantity="1", created_at=NOW - timedelta(days=day)))
    db.commit()
    order = _letter(db, subject="робота", body="Доброго дня", operator="mono a2")
    out = mail_learn.report(db, 7, now=NOW)
    row = _row(out, order)
    assert row["висновок"] == mail_learn.NO_MATERIAL
    assert row["чого_бракувало"] == "у листі немає ні матеріалу, ні кольору"
    assert row["звичне_клієнта"] == "вгадало б"
    assert out["звичне_клієнта_на_помилках"] == {"вгадало б": 1}
    assert out["оператор_писав_коли_не_впізнано"] == {"mono": 1}


def test_shade_only_in_file_names(db):
    order = _letter(db, subject="моноліт", guess="моноліт", files=("Petrenko_A2_crown.stl",),
                    operator="mono a2", client="Інший")
    row = _row(mail_learn.report(db, 7, now=NOW), order)
    assert row["висновок"] == mail_learn.NO_SHADE
    assert row["відтінок_оператора"] == "лише в назвах файлів"
    assert row["чого_бракувало"] == "відтінок лише в назвах файлів"
    assert row["звичне_клієнта"] == "звичного немає"


def test_operator_chose_another_material(db):
    order = _letter(db, subject="емо а2", guess="емо а2", operator="mono a2", client="Третій")
    out = mail_learn.report(db, 7, now=NOW)
    row = _row(out, order)
    assert row["висновок"] == mail_learn.OTHER_MATERIAL and row["система"] == "emo a2"
    assert out["інший_матеріал_пари"] == {"emo → mono": 1}


def _katana_canon(db):
    for text in ("katana a1", "stml a1", "utml a1"):
        for _ in range(6):
            db.add(Order(source="lab", material_color=text, quantity="1", created_at=NOW))
    db.commit()


def test_plain_katana_in_letter_is_the_stml_the_operator_wrote(db):
    # Прод 26.11.26: «замовлено Katana STML A1 — у листі Katana A1» червоною
    # карткою сторожа. Власник: «Це один і той же» — просто katana = диск STML
    # (`disc_suggest.lines_conflict`, 23.11.26).
    _katana_canon(db)
    order = _letter(db, subject="katana a1", guess="katana a1", operator="stml a1", client="Четвертий")
    row = _row(mail_learn.report(db, 7, now=NOW), order)
    assert row["висновок"] == mail_learn.OTHER_SPELLING


def test_stml_against_utml_is_still_another_material(db):
    _katana_canon(db)
    order = _letter(db, subject="utml a1", guess="utml a1", operator="stml a1", client="П'ятий")
    row = _row(mail_learn.report(db, 7, now=NOW), order)
    assert row["висновок"] == mail_learn.OTHER_MATERIAL


def test_splint_letter_matches_kappa(db):
    _letter(db, subject="сплінт РММА прозорий", guess="ПММА",
            files=("2026-11-17_00006-001-37-36-35-34-33-32-31-41-42-43-44-45-46-47-bitesplint_cad.stl",),
            operator="kappa", quantity="14")
    out = mail_learn.report(db, 7, now=NOW)
    assert out["підсумок"] == {"збіг": 1}
    assert out["кількість_з_назв_файлів"] == {"збіг": 1}


def test_letter_split_into_several_works_is_left_out(db):
    first = _letter(db, subject="pmma a3", guess="pmma a3", operator="pmma a3")
    db.add(Order(source="email", source_email_id=first.source_email_id, client_name="x",
                 material_color="mono a2", quantity="1", created_at=NOW))
    db.commit()
    out = mail_learn.report(db, 7, now=NOW)
    assert out["листів_звірено"] == 0 and out["лист_на_кілька_робіт"] == 1


def test_old_letters_are_outside_the_window(db):
    _letter(db, subject="pmma a3", guess="pmma a3", operator="pmma a3", when=NOW - timedelta(days=9))
    assert mail_learn.report(db, 7, now=NOW)["листів_звірено"] == 0


def test_report_carries_no_letter_text_names_or_file_names(db):
    order = _letter(db, subject="пацієнт Шевченко Тарас", body="Доброго дня, пацієнт Шевченко Тарас, моноліт",
                    files=("Шевченко_Тарас_A2_crown.stl",), operator="mono a2", client="Клініка Дента-Люкс")
    out = mail_learn.report(db, 7, now=NOW)
    assert _row(out, order)["висновок"] != mail_learn.MATCH
    dump = json.dumps(out, ensure_ascii=False)
    for secret in ("Шевченко", "Тарас", "Дента-Люкс", "Доброго дня", ".stl"):
        assert secret not in dump


def test_tool_is_registered_and_isolated(db):
    tool = TOOLS_BY_NAME["kmill_mail_learn"]
    assert tool.isolate is not None and tool.isolate({}) is True
    assert tool.run(db, {"days": 3, "limit": 5})["днів"] == 3
