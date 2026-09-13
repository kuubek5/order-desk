"""Обрив зв'язку лишає слід у Журналі синку.

Доти фоновий мережевий збій не потрапляв у журнал узагалі: рядок персистився
лише для РУЧНОГО синку, а фоновий тік лишав тільки `logger.warning` і помилку
в пульсі. Пульс зеленіє на першому ж успіху — тож після повернення зв'язку
сліду не лишалось ЖОДНОГО, і питання «що не доїхало вночі» відповіді не мало.

Тут перевіряється головне: початок обриву видно одразу, повторні тіки журнал
не топлять, а КІНЕЦЬ вікна записується окремим рядком із тривалістю.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import SyncLog
from app.services import outage_journal


@pytest.fixture(autouse=True)
def _clean_state():
    """Стан живе в пам'яті ПРОЦЕСУ — між тестами він протікає, і глушник
    «раз на годину» тихо гасив би записи наступного тесту."""
    outage_journal.reset()
    yield
    outage_journal.reset()


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def _rows(db):
    return db.scalars(select(SyncLog).order_by(SyncLog.id)).all()


def test_first_failure_is_written_immediately():
    """Обрив має бути видно одразу, а не через годину."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        outage_journal.note_failure(db, kind="sheet", message="немає з'єднання")
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0].status == "error"
        assert rows[0].direction == "sheet_to_db"
        assert "Зв'язок втрачено" in rows[0].message
        assert "немає з'єднання" in rows[0].message


def test_repeated_ticks_do_not_flood_the_journal(db):
    """Тік ходить раз на хвилину, обрив триває годинами — за ніч це сотні
    однакових рядків, і серед них потонуло б усе інше."""
    for _ in range(50):
        outage_journal.note_failure(db, kind="sheet", message="немає з'єднання")
    assert len(_rows(db)) == 1


def test_recovery_closes_the_window(db):
    """Рядок про відновлення — головний: без нього журнал показував би початок
    аварії без кінця, і чи вона ще триває, зрозуміти було б неможливо."""
    outage_journal.note_failure(db, kind="sheet", message="немає з'єднання")
    outage_journal.note_recovery(db, kind="sheet")
    rows = _rows(db)
    assert len(rows) == 2
    assert rows[1].status == "ok"
    assert "Зв'язок відновлено" in rows[1].message
    assert "невдалих спроб: 1" in rows[1].message


def test_recovery_counts_every_attempt_even_the_muted_ones(db):
    """Глушник ховає РЯДКИ, але не спроби: підсумок мусить назвати всі."""
    for _ in range(12):
        outage_journal.note_failure(db, kind="sheet", message="немає з'єднання")
    outage_journal.note_recovery(db, kind="sheet")
    assert "невдалих спроб: 12" in _rows(db)[-1].message


def test_success_without_an_outage_writes_nothing(db):
    """Успіх — норма, і засмічувати ним журнал не можна."""
    outage_journal.note_recovery(db, kind="sheet")
    outage_journal.note_recovery(db, kind="sheet")
    assert _rows(db) == []


def test_sheet_and_mail_do_not_mute_each_other(db):
    """Один глушник на двох означав би, що обрив пошти ховає обрив таблиці."""
    outage_journal.note_failure(db, kind="sheet", message="таблиця")
    outage_journal.note_failure(db, kind="mail", message="пошта")
    rows = _rows(db)
    assert {r.direction for r in rows} == {"sheet_to_db", "mail_to_db"}


def test_a_new_outage_after_recovery_speaks_up_again(db):
    """Другий обрив — нова подія, а не продовження старої: глушник має бути
    скинутий, інакше про нього мовчали б цілу годину."""
    outage_journal.note_failure(db, kind="sheet", message="перший")
    outage_journal.note_recovery(db, kind="sheet")
    outage_journal.note_failure(db, kind="sheet", message="другий")
    rows = _rows(db)
    assert len(rows) == 3
    assert "другий" in rows[2].message


def test_duration_reads_as_a_length_not_a_timestamp(db):
    outage_journal.note_failure(db, kind="sheet", message="немає з'єднання")
    # Відсунути початок обриву назад, не чекаючи наживо.
    with outage_journal._lock:
        started, attempts = outage_journal._outages["sheet"]
        outage_journal._outages["sheet"] = (started - timedelta(minutes=125), attempts)
    outage_journal.note_recovery(db, kind="sheet")
    assert "2 год 5 хв" in _rows(db)[-1].message


def test_unknown_source_is_ignored_rather_than_logged_raw(db):
    """Напрямок мусить збігатися з DIRECTION_LABELS журналу, інакше запис
    показався б сирим ключем."""
    outage_journal.note_failure(db, kind="печі", message="x")
    outage_journal.note_recovery(db, kind="печі")
    assert _rows(db) == []


def test_directions_match_the_journal_labels():
    """Сторож пари: нове джерело без рядка в журналі показалось би ключем."""
    from app.routers.sync_journal import DIRECTION_LABELS

    for direction in outage_journal._DIRECTIONS.values():
        assert direction in DIRECTION_LABELS


def test_both_ticks_are_wired():
    """Без виклику в тіку модуль був би мертвим кодом."""
    from pathlib import Path

    web = Path("app/web.py").read_text(encoding="utf-8")
    assert 'outage_journal.note_failure(db, kind="sheet"' in web
    assert 'outage_journal.note_recovery(db, kind="sheet")' in web
    assert 'outage_journal.note_failure(db, kind="mail"' in web
    assert 'outage_journal.note_recovery(db, kind="mail")' in web


def test_a_broken_journal_never_breaks_the_sync():
    """Журнал — страховка. Впасти на ньому означало б зламати сам синк.

    Сесію підміняємо такою, що ГАРАНТОВАНО кидає: закрита сесія SQLAlchemy
    відкривається знову сама, тож тест на ній нічого б не доводив."""

    class _Broken:
        """Падає на ВСЬОМУ, зокрема на відкаті.

        Заглушка, чий `rollback()` працює, доводить лише себе: саме такої я
        спершу й написав, і вона пропустила справжню ваду — обробник помилки
        кликав відкат без захисту, тож на мертвій сесії виняток вилітав із
        самої страховки."""

        def __init__(self):
            self.rollback_tried = False

        def add(self, _obj):
            raise RuntimeError("база недоступна")

        def commit(self):
            raise RuntimeError("база недоступна")

        def rollback(self):
            self.rollback_tried = True
            raise RuntimeError("відкат теж недоступний")

    broken = _Broken()
    outage_journal.note_failure(broken, kind="sheet", message="немає з'єднання")
    outage_journal.note_recovery(broken, kind="sheet")
    assert broken.rollback_tried   # шлях аварії справді пройдено


def test_a_tick_without_a_session_does_not_explode():
    """Тік викликають і з `db=None` — журнал не має цього помічати."""
    outage_journal.note_failure(None, kind="sheet", message="немає з'єднання")
    outage_journal.note_recovery(None, kind="sheet")
