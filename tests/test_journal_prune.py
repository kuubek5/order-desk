"""Журнали справді чистяться — обіцянка `SELF_PRUNING` тепер має виконавця.

`sync_logs` і `action_log` роками були оголошені в
`health_snapshot.SELF_PRUNING` як такі, що чистяться за розкладом, не маючи
прибиральника ЖОДНОГО (аудит 08.09.26). Ріст без межі — півбіди; гірше друге:
`SELF_PRUNING` означає «зменшення тут нормальне», тож справжня втрата даних у
цих таблицях не підняла б тривоги після оновлення. Обіцянка без виконавця
зробила запобіжник глухим саме там, де він мав дивитись уважно.
"""

from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import utc_now
from app.db import Base
from app.models import ActionLog, SyncLog
from app.services.journal_prune import (
    ACTION_LOG_RETENTION_DAYS,
    SYNC_LOG_RETENTION_DAYS,
    prune_journals,
)


def _session() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def test_old_sync_log_rows_are_removed():
    db = _session()
    old = utc_now() - timedelta(days=SYNC_LOG_RETENTION_DAYS + 5)
    fresh = utc_now() - timedelta(days=1)
    db.add(SyncLog(direction="db_to_sheet", status="ok", message="давнє", occurred_at=old))
    db.add(SyncLog(direction="db_to_sheet", status="ok", message="свіже", occurred_at=fresh))
    db.commit()

    removed = prune_journals(db)
    db.commit()

    assert removed["sync_logs"] == 1
    left = [r.message for r in db.scalars(select(SyncLog))]
    assert left == ["свіже"]


def test_fresh_rows_survive():
    """Прибиральник не має чіпати те, заради чого журнал і існує."""
    db = _session()
    for days in (0, 1, 10, 60):
        db.add(SyncLog(
            direction="db_to_sheet", status="ok", message=f"-{days}д",
            occurred_at=utc_now() - timedelta(days=days),
        ))
    db.commit()

    removed = prune_journals(db)
    db.commit()

    assert removed["sync_logs"] == 0
    assert len(db.scalars(select(SyncLog)).all()) == 4


def test_action_log_keeps_a_longer_window():
    """На журналі дій «хто що зробив», а брак впливає на гроші — тому рік."""
    db = _session()
    db.add(ActionLog(
        action_type="status", note="торішнє",
        created_at=datetime.now() - timedelta(days=ACTION_LOG_RETENTION_DAYS + 10),
    ))
    db.add(ActionLog(
        action_type="status", note="піврічне",
        created_at=datetime.now() - timedelta(days=180),
    ))
    db.commit()

    removed = prune_journals(db)
    db.commit()

    assert removed["action_log"] == 1
    assert [r.note for r in db.scalars(select(ActionLog))] == ["піврічне"]


def test_the_two_journals_use_their_own_clocks():
    """`SyncLog.occurred_at` пише за Гринвічем (`server_default=func.now()` на
    SQLite), а `ActionLog.created_at` — локально. Рахувати межу однаково було б
    просто неправильно, і перший, хто зменшить вікно до годин, отримав би тихий
    зсув на три години."""
    db = _session()
    # Рядок, що потрапляє у вікно ЛИШЕ при правильній шкалі: старший за межу на
    # годину, тобто зсув у три години перекинув би його на інший бік.
    db.add(SyncLog(
        direction="db_to_sheet", status="ok", message="межовий",
        occurred_at=utc_now() - timedelta(days=SYNC_LOG_RETENTION_DAYS, hours=1),
    ))
    db.commit()

    removed = prune_journals(db)
    db.commit()

    assert removed["sync_logs"] == 1, (
        "межовий рядок не прибрано — схоже, межу порахували в чужій шкалі часу"
    )


def test_declared_self_pruning_tables_actually_have_a_pruner():
    """Сторож проти повторення самої помилки: оголосити таблицю такою, що
    чиститься сама, і не написати прибиральника."""
    import inspect

    from app.services import journal_prune
    from app.services.health_snapshot import SELF_PRUNING

    source = inspect.getsource(journal_prune)
    covered = {"sync_logs", "action_log"}
    # Решту чистять інші модулі — перевіряємо, що кожна оголошена таблиця має
    # СВОГО виконавця десь у коді.
    others = {
        "furnace_readings": "app/services/furnace.py::prune_readings",
        "shift_note_images": "app/shift_images.py::prune_shift_images",
    }
    for table in SELF_PRUNING:
        assert table in covered or table in others, (
            f"таблиця {table} оголошена SELF_PRUNING, але прибиральника немає — "
            "це не лише ріст без межі, а й сліпа зона звірки після оновлення"
        )
    for table in covered:
        assert table in source
