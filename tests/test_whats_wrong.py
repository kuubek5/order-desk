"""«Що не так»: чи справді поломка перекладається на мову, якою можна діяти.

Власник працює сам. Екран існує рівно для того, щоб він РОЗІБРАВСЯ БЕЗ
розробника — тож тести перевіряють не «щось показалось», а що в картці є три
речі: що сталось, що через це не працює, і що робити руками.

Другий предмет охорони — щоб екран лишався читабельним, коли поломка
повторюється щохвилини. Саме на цьому вже спіткнувся звіт: 19 з 25 рядків
журналу синку були буквально однакові.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MachineLinkEvent, SyncLog
from app.services import machine_link, whats_wrong


TABS_MESSAGE = (
    "вкладки за сьогодні (10.09.26) немає серед датованих; у таблиці є: "
    "['08.09.26', '09.09.26']"
)


def _db() -> Session:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _utc(minutes_ago: int = 5) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)


def _sync(db: Session, direction: str, status: str, message: str, minutes_ago: int = 5):
    db.add(
        SyncLog(
            direction=direction, status=status, message=message,
            occurred_at=_utc(minutes_ago),
        )
    )
    db.commit()


def _find(problems, needle: str):
    return next((p for p in problems if needle.lower() in p.title.lower()), None)


# ── Кожна поломка мусить мати наслідок і кроки ──────────────────────────────


@pytest.mark.parametrize(
    "direction, status, message, needle",
    [
        ("sheet_to_db", "skipped", TABS_MESSAGE, "вкладки"),
        ("sheet_to_db", "error", "Не вдалося синхронізувати Google Таблицю.", "доступу"),
        ("db_to_sheet", "error", "order 134: вкладку '11.08.26' не знайдено", "записати"),
        ("mail_to_db", "error", "Не вдалося синхронізувати пошту.", "пошта"),
    ],
)
def test_every_known_failure_says_what_it_means_and_what_to_do(
    direction, status, message, needle
):
    db = _db()
    _sync(db, direction, status, message)

    found = _find(whats_wrong.collect(db), needle)

    assert found is not None, f"поломку «{needle}» не впізнано"
    assert found.means and len(found.means) > 40, "немає наслідку — «що через це не працює»"
    assert found.actions, "немає кроків руками"
    assert found.raw == message, "сирий текст мусить лишитись для розробника"


def test_the_missing_tab_names_the_tabs_the_app_does_see():
    """Це доказ, що доступ до таблиці цілий і справа саме в назві вкладки."""
    db = _db()
    _sync(db, "sheet_to_db", "skipped", TABS_MESSAGE)

    found = _find(whats_wrong.collect(db), "вкладки")

    assert "08.09.26" in found.means and "09.09.26" in found.means
    assert found.level == whats_wrong.LEVEL_STOP


def test_an_unknown_failure_is_shown_not_swallowed():
    """Незнайому поломку показуємо як є: мовчати про неї гірше, ніж показати
    сирий текст, якого власник не зрозуміє, — принаймні він її ПОБАЧИТЬ."""
    db = _db()
    _sync(db, "sheet_to_db", "error", "щось геть нове й дивне")

    problems = whats_wrong.collect(db)

    assert problems, "невідома поломка зникла"
    assert any("щось геть нове" in p.raw for p in problems)


def test_successful_sync_is_not_a_problem():
    db = _db()
    _sync(db, "sheet_to_db", "ok", "прочитано 96 рядків")
    assert whats_wrong.collect(db) == []


# ── Читабельність ───────────────────────────────────────────────────────────


def test_a_failure_repeating_every_minute_stays_one_card():
    """Сорок однакових карток — це екран, на якому не видно ДРУГОЇ проблеми."""
    db = _db()
    for i in range(40):
        _sync(db, "sheet_to_db", "skipped", TABS_MESSAGE, minutes_ago=i + 1)

    problems = whats_wrong.collect(db)

    assert len(problems) == 1
    assert problems[0].times == 40


def test_different_failures_stay_separate():
    db = _db()
    _sync(db, "sheet_to_db", "skipped", TABS_MESSAGE, minutes_ago=2)
    _sync(db, "mail_to_db", "error", "Не вдалося синхронізувати пошту.", minutes_ago=3)

    assert len(whats_wrong.collect(db)) == 2


def test_the_most_urgent_comes_first():
    """Екран відкривають з питанням «що робити», і «стоп» мусить бути в першому
    рядку — навіть якщо він старший за свіже попередження."""
    db = _db()
    _sync(db, "mail_to_db", "error", "Не вдалося синхронізувати пошту.", minutes_ago=1)
    _sync(db, "sheet_to_db", "skipped", TABS_MESSAGE, minutes_ago=200)

    problems = whats_wrong.collect(db)

    assert problems[0].level == whats_wrong.LEVEL_STOP


def test_the_window_actually_cuts_off_old_trouble():
    db = _db()
    _sync(db, "mail_to_db", "error", "Не вдалося синхронізувати пошту.", minutes_ago=60 * 40)

    assert whats_wrong.collect(db, hours=24) == []
    assert whats_wrong.collect(db, hours=168) != []


# ── Верстати ────────────────────────────────────────────────────────────────


def test_a_live_outage_says_the_machine_is_invisible_right_now():
    db = _db()
    db.add(
        MachineLinkEvent(
            host="192.168.1.50-8765", name="350i",
            detected_at=datetime.now() - timedelta(minutes=10),
            error="ПК 192.168.1.50 працює, але на порту 8765 ніхто не слухає",
            cause=machine_link.CAUSE_AGENT_DOWN,
        )
    )
    db.commit()

    found = _find(whats_wrong.collect(db), "350i")

    assert found is not None
    assert "ЗАРАЗ" in found.means
    assert any("kmill-agent" in step for step in found.actions)
    assert found.level == whats_wrong.LEVEL_PROBLEM


def test_a_healed_outage_is_only_a_warning():
    """Верстат повернувся — це вже не «зараз не працює», а «подивись, чи не
    повторюється». Інакше рейка світилась би через учорашню подію."""
    db = _db()
    started = datetime.now() - timedelta(minutes=50)
    db.add(
        MachineLinkEvent(
            host="192.168.1.50-8765", name="350i",
            detected_at=started, ended_at=started + timedelta(minutes=3),
            error="ПК мовчить", cause=machine_link.CAUSE_PORT_SILENT,
        )
    )
    db.commit()

    found = _find(whats_wrong.collect(db), "350i")

    assert found.level == whats_wrong.LEVEL_WARN


# ── Мовчання ────────────────────────────────────────────────────────────────


def test_a_silent_sync_is_a_problem_even_though_nothing_failed():
    """Найпідступніший випадок: у журналі НЕМАЄ нічого, і саме тому цього не
    видно, поки хтось не помітить, що дані старі."""
    db = _db()
    _sync(db, "sheet_to_db", "ok", "прочитано 96 рядків", minutes_ago=120)

    found = _find(whats_wrong.collect(db), "мовчить")

    assert found is not None
    assert found.level == whats_wrong.LEVEL_PROBLEM
    assert "паузу" in " ".join(found.actions)


def test_a_fresh_sync_is_not_reported_as_silent():
    db = _db()
    _sync(db, "sheet_to_db", "ok", "прочитано 96 рядків", minutes_ago=2)
    assert _find(whats_wrong.collect(db), "мовчить") is None


def test_an_app_that_never_synced_is_not_accused_of_silence():
    """Щойно встановлений застосунок ще не синхронізувався жодного разу — це
    не поломка, і лякати нею на першому запуску не можна."""
    assert _find(whats_wrong.collect(_db()), "мовчить") is None


# ── Лічильник для рейки ─────────────────────────────────────────────────────


def test_the_rail_counter_ignores_warnings():
    """Рейка мусить світитись, коли щось справді не працює. Якщо туди
    потрапляють попередження, на неї перестають дивитись."""
    db = _db()
    started = datetime.now() - timedelta(minutes=50)
    db.add(
        MachineLinkEvent(
            host="h-1", name="350i", detected_at=started,
            ended_at=started + timedelta(minutes=2),
            error="ПК мовчить", cause=machine_link.CAUSE_PORT_SILENT,
        )
    )
    db.commit()

    assert whats_wrong.collect(db)          # попередження на екрані видно
    assert whats_wrong.count(db) == 0       # а рейка мовчить


def test_the_rail_counter_counts_real_trouble():
    db = _db()
    _sync(db, "sheet_to_db", "skipped", TABS_MESSAGE)
    assert whats_wrong.count(db) == 1


# ── Стійкість ───────────────────────────────────────────────────────────────


def test_collect_survives_a_broken_source(monkeypatch):
    """Один зламаний постачальник не сміє забрати з екрана решту проблем —
    саме тоді, коли на екран і дивляться."""
    db = _db()
    _sync(db, "sheet_to_db", "skipped", TABS_MESSAGE)

    def boom(*a, **k):
        raise RuntimeError("джерело впало")

    monkeypatch.setattr(whats_wrong, "_disk_problem", boom)
    problems = whats_wrong.collect(db)

    assert _find(problems, "вкладки") is not None, "решта проблем зникла разом зі збоєм"
    broken = _find(problems, "діагностики не спрацювала")
    assert broken is not None, "про власний збій діагностика мусить сказати, а не мовчати"
    assert "джерело впало" in broken.raw


def test_silence_is_measured_in_hours_not_raw_minutes():
    """«1178 хв» технічно правда, але скільки це — доводиться рахувати в голові
    саме тоді, коли не до того."""
    db = _db()
    _sync(db, "sheet_to_db", "ok", "прочитано 96 рядків", minutes_ago=1178)

    found = _find(whats_wrong.collect(db, hours=48), "мовчить")

    assert "год" in found.title
    assert "1178" not in found.title


def test_machine_headline_keeps_its_capitals():
    """`.lower()` на чужому заголовку перетворював «ПК» на «пк»."""
    db = _db()
    db.add(
        MachineLinkEvent(
            host="h-1", name="350i", detected_at=datetime.now() - timedelta(minutes=5),
            error="ПК мовчить", cause=machine_link.CAUSE_AGENT_DOWN,
        )
    )
    db.commit()

    found = _find(whats_wrong.collect(db), "350i")

    assert "ПК" in found.title


# ── Мовчання журналу ≠ мовчання синхронізації ───────────────────────────────


def test_sync_silence_keeps_quiet_while_the_fast_lane_is_reading(monkeypatch):
    """Журнал — аудит, і швидка смуга (`_sheet_hot_tick`, кожні ~15 с) рядка в
    нього свідомо не пише. Тому мовчання журналу саме по собі нічого не
    доводить, і поки пульс каже «щойно читали успішно», тривоги бути не має.

    Бойовий випадок 15.09.26: екран показував «Синхронізація мовчить 2 год
    40 хв» тієї ж хвилини, коли в лозі стояло `Hot-tab sync ...: updated 1`,
    а черга наповнювалась. Хибна тривога на екрані тривог знецінює весь екран.
    """
    from app import sync_heartbeat

    db = _db()
    _sync(db, "sheet_to_db", "ok", "trigger background", minutes_ago=160)

    # Пульс живий: останній УСПІХ щойно. Годинник пульсу локальний, не UTC.
    monkeypatch.setitem(
        sync_heartbeat.heartbeats,
        "sheet",
        sync_heartbeat.SyncHeartbeat(
            last_attempt_at=datetime.now(),
            last_success_at=datetime.now() - timedelta(minutes=1),
            status="ok",
        ),
    )
    assert _find(whats_wrong.collect(db), "мовчить") is None


def test_sync_silence_still_fires_when_the_fast_lane_is_dead_too(monkeypatch):
    """Зворотний бік: пульс теж давно без успіху — тоді мовчання журналу
    справжнє, і тривога мусить бути."""
    from app import sync_heartbeat

    db = _db()
    _sync(db, "sheet_to_db", "ok", "trigger background", minutes_ago=160)
    monkeypatch.setitem(
        sync_heartbeat.heartbeats,
        "sheet",
        sync_heartbeat.SyncHeartbeat(
            last_attempt_at=datetime.now(),
            last_success_at=datetime.now() - timedelta(minutes=160),
            status="error",
        ),
    )
    found = _find(whats_wrong.collect(db), "мовчить")
    assert found is not None
    assert found.actions


def test_sync_silence_falls_back_to_the_journal_after_a_restart(monkeypatch):
    """Пульс живе лише в памʼяті процесу й рестарт не переживає. Поки успіху
    ще не було, єдине джерело — журнал, і поводимось як раніше."""
    from app import sync_heartbeat

    db = _db()
    _sync(db, "sheet_to_db", "ok", "trigger background", minutes_ago=160)
    monkeypatch.setitem(
        sync_heartbeat.heartbeats,
        "sheet",
        sync_heartbeat.SyncHeartbeat(),
    )
    assert _find(whats_wrong.collect(db), "мовчить") is not None


# ── Глушник відомої причини ────────────────────────────────────────────────
#
# Навіщо взагалі: у 350i Loader несправна мережева карта (власник, 17.09.26),
# вона рве звʼязок щодня — і значок «Що не так» світиться постійно з причини,
# яку вже розібрали. Значок, що горить завжди, за тиждень перестають читати, і
# справжня аварія тоне в ньому. Той самий аргумент, що в `_sync_silence`:
# хибна тривога на екрані тривог гірша за відсутню.


def _two_sync_problems(db: Session):
    """Дві РІЗНІ проблеми, щоб було видно: глушиться саме одна."""
    _sync(db, "sheet_to_db", "skipped", TABS_MESSAGE)
    _sync(db, "mail_to_db", "error", "Не вдалося синхронізувати пошту.")
    problems = whats_wrong.collect(db)
    assert len(problems) >= 2
    return problems


def test_muted_problem_leaves_the_list_and_the_badge():
    db = _db()
    problems = _two_sync_problems(db)
    target = problems[0]
    before = whats_wrong.count(db)

    whats_wrong.mute(db, key=target.key, title=target.title, days=7)
    db.commit()

    keys = [p.key for p in whats_wrong.collect(db)]
    assert target.key not in keys, "заглушена причина лишилась у переліку"
    assert len(keys) == len(problems) - 1, "заглушили більше, ніж просили"
    assert whats_wrong.count(db) < before, "значок у рейці не помітив глушника"


def test_muted_problem_is_still_visible_in_its_own_strip():
    """Ховати зовсім не можна: людина мусить бачити, ЩО мовчить і до якого числа."""
    db = _db()
    target = _two_sync_problems(db)[0]
    whats_wrong.mute(db, key=target.key, title=target.title, days=3, note="міняємо карту")
    db.commit()

    muted = whats_wrong.muted_problems(db)

    assert [p.key for p in muted] == [target.key]
    row = whats_wrong.active_mutes(db)[target.key]
    assert row.note == "міняємо карту"
    assert row.title == target.title, "заголовок мусить лишитись, коли подія випаде з вікна"


def test_a_mute_expires_by_itself():
    """Вічний глушник — той самий сліпий екран, тільки тихий."""
    db = _db()
    target = _two_sync_problems(db)[0]
    past = datetime.now() - timedelta(days=1)
    whats_wrong.mute(db, key=target.key, title=target.title, days=1, now=past - timedelta(days=1))
    db.commit()

    assert target.key in [p.key for p in whats_wrong.collect(db)], "протухлий глушник ще тримає"
    assert whats_wrong.active_mutes(db) == {}


def test_a_mute_cannot_outlive_the_ceiling():
    db = _db()
    target = _two_sync_problems(db)[0]
    now = datetime.now()

    row = whats_wrong.mute(db, key=target.key, title=target.title, days=365, now=now)
    db.commit()

    assert row.until <= now + timedelta(days=whats_wrong.MUTE_MAX_DAYS)


def test_unmute_brings_the_problem_back():
    db = _db()
    target = _two_sync_problems(db)[0]
    whats_wrong.mute(db, key=target.key, title=target.title, days=7)
    db.commit()

    assert whats_wrong.unmute(db, key=target.key) is True
    db.commit()

    assert target.key in [p.key for p in whats_wrong.collect(db)]


def test_muting_one_cause_leaves_the_other_causes_of_the_same_machine():
    """Глушиться пара «пристрій + причина», не пристрій: інша поломка того
    самого верстата мусить пройти й засвітитись."""
    db = _db()
    host = "192.168.1.85"
    for cause, error in (
        ("silent", "ПК 192.168.1.85 мовчить на порту 8765"),
        ("no_answer", "агент 192.168.1.85:8765 не віддав кадр за 8 с"),
    ):
        db.add(
            MachineLinkEvent(
                host=host, name="350i Loader", cause=cause, error=error,
                detected_at=datetime.now() - timedelta(minutes=10),
                ended_at=datetime.now() - timedelta(minutes=5),
                failed_polls=3,
            )
        )
    db.commit()
    problems = [p for p in whats_wrong.collect(db) if p.key.startswith("machine_")]
    assert len(problems) == 2, "дві різні причини мають дати дві картки"

    whats_wrong.mute(db, key=problems[0].key, title=problems[0].title, days=7)
    db.commit()

    left = [p.key for p in whats_wrong.collect(db) if p.key.startswith("machine_")]
    assert left == [problems[1].key]
