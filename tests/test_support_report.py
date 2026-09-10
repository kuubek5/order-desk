"""«Звіт для розробника»: що в ньому мусить бути і чого там бути НЕ СМІЄ.

Власник працює сам і звертається до розробника раз на тиждень. Звіт існує, щоб
цей тиждень не пішов на «а покажи ще»: усе потрібне — одним файлом.

Головний тест тут — про секрети. Файл їде в месенджер, тобто на чужий сервер;
пароль пошти або ключ Google, який туди поїхав, вважається розкритим. Тому
перевірка не «здається, не видно», а буквальна: підставляємо впізнавані
секрети й вимагаємо, щоб жодного з них у тексті не було.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MachineLinkEvent, Order, SyncLog, User
from app.services import machine_link
from app.services import support_report
from app.settings_store import set_setting


# Значення, яких у звіті бути не може. Навмисно впізнавані: якщо таке
# просочиться, тест назве саме його, а не «щось не так».
SECRET_PASSWORD = "SUPER-SECRET-MAIL-PASSWORD-9137"
SECRET_JSON = '{"private_key":"-----BEGIN PRIVATE KEY-----AAAABBBB"}'
SECRET_LICENSE = "LICENSE-KEY-DO-NOT-LEAK-4242"
SHEET_ID = "1IIEkBnPoDcxgo3-41IdbJu6FZXNawYX9UNdoekFDPbs"
MAIL_LOGIN = "phantom.lab@ukr.net"


def _db() -> Session:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _filled(db: Session) -> Session:
    set_setting(db, "imap_password", SECRET_PASSWORD)
    set_setting(db, "google_service_account_json", SECRET_JSON)
    set_setting(db, "license_key", SECRET_LICENSE)
    set_setting(db, "google_sheet_id", SHEET_ID)
    set_setting(db, "imap_login", MAIL_LOGIN)
    db.add(User(username="roma", password_hash="x", full_name="Рома", role="адмін"))
    db.add(
        Order(
            source="lab", work_order_no="30150", status="нове",
            created_at=datetime.now(),
        )
    )
    db.add(
        SyncLog(
            direction="sheet→crm", status="ok", sheet_tab="10.09.26",
            message="прочитано 96 рядків",
            occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    db.add(
        MachineLinkEvent(
            host="192.168.1.50-8765", name="350i",
            detected_at=datetime.now() - timedelta(minutes=30),
            ended_at=datetime.now() - timedelta(minutes=28),
            error="Знімок не вдався: ПК 192.168.1.50 працює, але на порту 8765 ніхто не слухає",
            cause=machine_link.CAUSE_AGENT_DOWN,
        )
    )
    db.commit()
    return db


# ── Секрети ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "secret", [SECRET_PASSWORD, SECRET_JSON, SECRET_LICENSE]
)
def test_no_secret_ever_reaches_the_report(secret):
    """Файл їде в месенджер. Секрет, що туди поїхав, вважається розкритим."""
    db = _filled(_db())
    text = support_report.build_report(db)
    assert secret not in text
    # І не шматками: половина приватного ключа так само непридатна для файлу.
    assert secret[:16] not in text


def test_secret_fields_are_reported_as_present_not_hidden():
    """«Не показувати» — не те саме, що «мовчати»: розробнику треба знати, що
    пароль ЗАДАНО, інакше «пошта не працює» і «пошта не налаштована» зіллються."""
    db = _filled(_db())
    text = support_report.build_report(db)
    assert "Пароль пошти" in text
    assert "задано" in text


def test_an_empty_secret_says_so():
    db = _db()
    text = support_report.build_report(db)
    assert "не задано" in text


def test_identifiers_are_masked_but_recognisable():
    """Sheet ID і логін пошти треба ВПІЗНАТИ («так, та сама таблиця»), а не
    прочитати. Тому кінці видно, середину — ні."""
    db = _filled(_db())
    text = support_report.build_report(db)
    assert SHEET_ID not in text
    assert SHEET_ID[:4] in text and SHEET_ID[-4:] in text
    assert MAIL_LOGIN not in text
    assert MAIL_LOGIN[:4] in text


# ── Вміст ───────────────────────────────────────────────────────────────────


def test_report_has_every_section_a_diagnosis_needs():
    db = _filled(_db())
    text = support_report.build_report(db)
    for title in (
        "ЗАСТОСУНОК", "САМОПЕРЕВІРКА", "НАЛАШТУВАННЯ", "ДОСТУП ДО РОЗДІЛІВ",
        "БАЗА ДАНИХ", "ЖУРНАЛ СИНКУ", "ЗВ'ЯЗОК З ВЕРСТАТАМИ", "ПОМИЛКИ В ЛОЗІ",
        "ХВІСТ ЛОГА",
    ):
        assert title in text, f"у звіті немає розділу «{title}»"


def test_report_counts_every_table():
    """Кількість рядків у КОЖНІЙ таблиці — те, з чого видно «дані зникли»."""
    db = _filled(_db())
    text = support_report.build_report(db)
    for table in ("orders", "users", "sync_logs", "machine_link_events"):
        assert table in text


def test_sync_journal_shows_kyiv_time_not_utc():
    """`SyncLog.occurred_at` — UTC. Показаний як є, він бреше на три години, і
    саме на цьому вже спалився один розбір (CLAUDE.md §14)."""
    db = _db()
    utc = datetime(2026, 9, 10, 6, 0, 0)      # 09:00 у Києві влітку
    db.add(SyncLog(direction="sheet→crm", status="ok", message="проба", occurred_at=utc))
    db.commit()

    text = support_report.build_report(db)

    assert "09:00" in text
    assert "10.09 06:00" not in text


def test_machine_outage_comes_with_its_plain_language_cause():
    db = _filled(_db())
    text = support_report.build_report(db)
    assert "350i" in text
    assert "Агент на ПК верстата не працює" in text


def test_selfcheck_results_are_included_when_given():
    db = _db()
    results = [
        _Result(
            key="sheets", name="Доступ до Google Таблиці", ok=False, warn=False,
            detail="не налаштовано — ID або JSON-ключ порожні", ms=12,
        )
    ]
    text = support_report.build_report(db, selfcheck_results=results)
    assert "Доступ до Google Таблиці" in text
    assert "ЗБІЙ" in text
    assert "0 з 1" in text


def test_report_says_plainly_when_selfcheck_was_not_run():
    """Мовчання читалось би як «перевірено й усе добре»."""
    db = _db()
    text = support_report.build_report(db)
    assert "не запускалась" in text


def test_missing_log_file_is_named_not_hidden(monkeypatch, tmp_path):
    """Якщо лога немає, звіт мусить сказати ДЕ його шукали: у dev-запуску його
    справді немає, і мовчазний порожній розділ виглядав би як «помилок нема»."""
    monkeypatch.setattr(support_report, "_log_path", lambda: tmp_path / "kuubmill.log")
    db = _db()
    text = support_report.build_report(db)
    assert "не знайдено" in text
    assert "kuubmill.log" in text


def test_report_survives_an_unreadable_secret(monkeypatch):
    """Зміна ключа шифрування → InvalidToken. Звіт мусить це НАЗВАТИ й
    дозбирати решту, а не впасти саме тоді, коли він найпотрібніший."""
    db = _filled(_db())

    def boom(_db, key):
        raise ValueError("InvalidToken")

    monkeypatch.setattr(support_report, "get_setting", boom)
    text = support_report.build_report(db)
    assert "не читається" in text
    assert "ХВІСТ ЛОГА" in text          # решта розділів усе одно зібралась


def test_filename_carries_the_date():
    name = support_report.report_filename(datetime(2026, 9, 10, 14, 5))
    assert name == "kuubmill-zvit_10.09.26_14-05.txt"


class _Result:
    """Мінімальний двійник `selfcheck.Result` — щоб не тягти сюди мережеві проби."""

    def __init__(self, key, name, ok, warn, detail, ms):
        self.key, self.name, self.ok = key, name, ok
        self.warn, self.detail, self.ms = warn, detail, ms
