"""Журнал обривів зв'язку з верстатами і розбір їхніх причин.

Скарга власника 09.09.26: «періодично обривається зв'язок віджета зі станком».
Відповісти на це було нічим — стан верстата живе в памʼяті процесу і зникає на
рестарті, а показання (`machine_readings`) пишуться ощадно: перша невдача
негайно, далі однакові раз на 15 хвилин, тож обрив на дві хвилини не лишав
ЖОДНОГО сліду.

Головний тест тут — перший. Перевірка досяжності («постукати в 445/135/3389,
щоб відрізнити мертвий ПК від закритого порту») була написана 08.09.26 і не
спрацювала в цеху ЖОДНОГО разу: умова її запуску стояла на порівнянні тексту
помилки за РІВНІСТЮ, а обидва шляхи опитування віддають текст із префіксом
«Знімок не вдався: ». Тести лишались зелені, бо годували `poll_target` голим
повідомленням — тобто перевіряли не те, що відбувається в цеху. Тому тест
нижче навмисно годує ПРОДАКШЕН-ФОРМУ тексту.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MachineLinkEvent
from app.services import machine_link
from app.services import machines as service


def make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _wait_for_probes(timeout=3.0):
    import threading

    for thread in list(threading.enumerate()):
        if thread.name.startswith("reach-probe-"):
            thread.join(timeout)
            assert not thread.is_alive(), "перевірка досяжності не завершилась"


@pytest.fixture
def target():
    return service.MachineTarget(
        name="350i", host="192.168.1.50", port=8765, agent_token="t", machine_id=1
    )


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    service.reset_state_for_tests()
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    yield
    service.reset_state_for_tests()


def _fail(db, target, error, times=None):
    """Погнати верстат у обрив продакшен-формою тексту."""
    times = times or service.PROBLEM_AFTER_FAILURES
    state = None
    for _ in range(times):
        state = service.poll_target(db, target, None, error=error)
    return state


# ── Регресія, заради якої все це й писалось ─────────────────────────────────


def test_probe_runs_on_the_wrapped_text_the_shop_actually_sees(monkeypatch, target):
    """Текст із цеху приходить із префіксом «Знімок не вдався: ».

    Саме на ньому стара умова (порівняння за рівністю) не спрацьовувала, і
    стук у порти не робився жодного разу за весь час життя фічі.
    """
    probes = []
    monkeypatch.setattr(
        service, "host_answers_at_all", lambda host, *a, **k: probes.append(host) or True
    )
    wrapped = machine_link.WRAP_PREFIX + service.msg_host_silent(target.host, target.port)

    _fail(None, target, wrapped)
    _wait_for_probes()

    assert probes == [target.host], "стук у порти не відбувся на реальному тексті"


def test_classify_reads_both_the_bare_and_the_wrapped_text(target):
    bare = service.msg_agent_not_listening(target.host, target.port)
    wrapped = machine_link.WRAP_PREFIX + bare
    for text in (bare, wrapped):
        assert (
            machine_link.classify(text, target.host, target.port)
            == machine_link.CAUSE_AGENT_DOWN
        )


@pytest.mark.parametrize(
    "builder, expected",
    [
        (lambda t: service.msg_agent_not_listening(t.host, t.port), machine_link.CAUSE_AGENT_DOWN),
        (lambda t: service.msg_host_silent(t.host, t.port), machine_link.CAUSE_PORT_SILENT),
        (lambda t: service.msg_host_no_answer(t.host), machine_link.CAUSE_NETWORK),
        (lambda t: "агент відхилив токен (403) — звір токен", machine_link.CAUSE_TOKEN),
        (lambda t: f"агент {t.host}:{t.port} не віддав кадр за 8 с", machine_link.CAUSE_SLOW),
    ],
)
def test_every_known_failure_gets_its_own_class(target, builder, expected):
    text = machine_link.WRAP_PREFIX + builder(target)
    assert machine_link.classify(text, target.host, target.port) == expected


def test_a_dead_agent_is_still_not_probed_without_deep_mode(monkeypatch, target):
    """ПК відповів відмовою — він свідомо живий, платити стуком нема за що."""
    probes = []
    monkeypatch.setattr(service, "host_answers_at_all", lambda host, *a, **k: probes.append(host))
    wrapped = machine_link.WRAP_PREFIX + service.msg_agent_not_listening(target.host, target.port)

    _fail(None, target, wrapped)
    _wait_for_probes()

    assert probes == []


def test_deep_mode_probes_even_a_dead_agent(monkeypatch, target):
    """Галочка на верстаті = «збери повний доказ», навіть коли й так ясно."""
    deep = service.MachineTarget(
        name=target.name, host=target.host, port=target.port,
        agent_token="t", machine_id=1, diagnose_link=True,
    )
    rows = [{"port": 445, "result": machine_link.PROBE_REFUSED, "ms": 12}]
    monkeypatch.setattr(machine_link, "probe_ports", lambda *a, **k: rows)
    wrapped = machine_link.WRAP_PREFIX + service.msg_agent_not_listening(deep.host, deep.port)

    state = _fail(None, deep, wrapped)
    _wait_for_probes()

    assert state.reach_probe == rows


# ── Рядок у журналі ─────────────────────────────────────────────────────────


def test_an_outage_writes_one_row_no_matter_how_long_it_lasts(target):
    """Мертвий за ніч верстат має дати ОДИН рядок, а не тисячі."""
    db = make_session()
    wrapped = machine_link.WRAP_PREFIX + service.msg_agent_not_listening(target.host, target.port)

    _fail(db, target, wrapped, times=service.PROBLEM_AFTER_FAILURES + 40)

    rows = db.scalars(select(MachineLinkEvent)).all()
    assert len(rows) == 1
    assert rows[0].cause == machine_link.CAUSE_AGENT_DOWN
    assert rows[0].ended_at is None, "обрив ще триває — кінця бути не може"
    assert rows[0].host == target.key


def test_recovery_closes_the_row_and_counts_the_failed_polls(target):
    db = make_session()
    wrapped = machine_link.WRAP_PREFIX + service.msg_host_silent(target.host, target.port)
    from PIL import Image

    _fail(db, target, wrapped, times=5)
    service.poll_target(db, target, None, frame=Image.new("RGB", (40, 30)))

    row = db.scalars(select(MachineLinkEvent)).one()
    assert row.ended_at is not None
    assert row.failed_polls == 5


def test_a_short_outage_is_recorded_even_though_readings_would_miss_it(target):
    """Рівно та діра, заради якої таблиця й зʼявилась.

    `machine_readings` пише повторні помилки раз на 15 хвилин, тож обрив, що
    вклався між двома такими рядками, там не видно взагалі.
    """
    db = make_session()
    wrapped = machine_link.WRAP_PREFIX + service.msg_host_no_answer(target.host)
    from PIL import Image

    _fail(db, target, wrapped)
    service.poll_target(db, target, None, frame=Image.new("RGB", (40, 30)))

    assert db.scalars(select(MachineLinkEvent)).one().ended_at is not None


def test_the_probe_verdict_reaches_the_row_on_the_next_tick(monkeypatch, target):
    """Стук іде окремим потоком і закінчується вже після запису рядка.

    Своєї сесії БД той потік не відкриває (правило «сесія на одному потоці»),
    тож вирок мусить дописати наступний тік опитування.
    """
    db = make_session()
    monkeypatch.setattr(service, "host_answers_at_all", lambda host, *a, **k: True)
    wrapped = machine_link.WRAP_PREFIX + service.msg_host_silent(target.host, target.port)

    _fail(db, target, wrapped)
    _wait_for_probes()
    service.poll_target(db, target, None, error=wrapped)   # наступний тік

    row = db.scalars(select(MachineLinkEvent)).one()
    assert row.probe_verdict and "мовчить саме порт" in row.probe_verdict
    assert row.probe_late is False


def test_a_late_verdict_is_kept_but_marked(monkeypatch, target):
    """Вирок, що приїхав після відновлення, на плитку не йде — але в журналі
    лишається з міткою: для розбору «а що там було» це все одно доказ.

    Стук навмисно тримаємо, поки зв'язок не повернеться: саме цей порядок і
    губив вирок, бо на момент його готовності рядок обриву вже закрито.
    """
    db = make_session()
    from PIL import Image
    import threading

    recovered = threading.Event()

    def slow(host, *a, **k):
        recovered.wait(3.0)
        return True

    monkeypatch.setattr(service, "host_answers_at_all", slow)
    wrapped = machine_link.WRAP_PREFIX + service.msg_host_silent(target.host, target.port)
    _fail(db, target, wrapped)
    # Зв'язок повернувся ДО того, як стук устиг покласти вирок.
    service.poll_target(db, target, None, frame=Image.new("RGB", (40, 30)))
    recovered.set()
    _wait_for_probes()
    service.poll_target(db, target, None, frame=Image.new("RGB", (40, 30)))

    state = service._states[target.key]
    assert state.reach_note is None, "вирок не має чіплятись на живий верстат"
    row = db.scalars(select(MachineLinkEvent)).one()
    assert row.probe_late is True, "спізнілий вирок мусить лишитись у журналі"


def test_error_trail_is_deep_mode_only(target):
    """Слід змін тексту — дороге поле, і воно вмикається галочкою."""
    db = make_session()
    from PIL import Image

    deep = service.MachineTarget(
        name=target.name, host=target.host, port=target.port,
        agent_token="t", machine_id=1, diagnose_link=True,
    )
    first = machine_link.WRAP_PREFIX + service.msg_host_silent(deep.host, deep.port)
    second = machine_link.WRAP_PREFIX + service.msg_host_no_answer(deep.host)
    _fail(db, deep, first)
    service.poll_target(db, deep, None, error=second)
    service.poll_target(db, deep, None, frame=Image.new("RGB", (40, 30)))

    row = db.scalars(select(MachineLinkEvent)).one()
    trail = machine_link.load_probe(row.error_trail)
    assert len(trail) == 2, "мають лишитись обидві РІЗНІ помилки, не всі тіки"

    service.reset_state_for_tests()
    db2 = make_session()
    _fail(db2, target, first)
    service.poll_target(db2, target, None, frame=Image.new("RGB", (40, 30)))
    assert db2.scalars(select(MachineLinkEvent)).one().error_trail is None


def test_polling_survives_a_broken_journal(monkeypatch, target):
    """Журнал діагностики не має права завалити опитування верстатів."""
    db = make_session()

    def boom(*a, **k):
        raise RuntimeError("база впала")

    monkeypatch.setattr(db, "commit", boom)
    wrapped = machine_link.WRAP_PREFIX + service.msg_agent_not_listening(target.host, target.port)

    state = _fail(db, target, wrapped)          # не має кинути
    assert state.fail_streak >= service.PROBLEM_AFTER_FAILURES


def test_without_a_session_nothing_is_written_and_nothing_breaks(target):
    """poll_target законно кличуть і без БД (разовий знімок, тести)."""
    wrapped = machine_link.WRAP_PREFIX + service.msg_host_silent(target.host, target.port)
    state = _fail(None, target, wrapped)
    assert state.link_event_id is None


def test_prune_drops_old_outages_only():
    db = make_session()
    now = datetime(2026, 9, 9, 12, 0)
    old = now - timedelta(days=200)
    for at in (old, now):
        db.add(MachineLinkEvent(host="h-1", name="v", detected_at=at, error="", cause="other"))
    db.commit()

    removed = service.prune_machine_link_events(db, now=now)

    assert removed == 1
    assert db.scalars(select(MachineLinkEvent)).one().detected_at == now


# ── Пояснення ───────────────────────────────────────────────────────────────


def test_refused_says_the_pc_is_alive_and_names_the_service():
    ex = machine_link.explain(
        cause=machine_link.CAUSE_AGENT_DOWN, error="", host="10.0.0.9", port=8765
    )
    assert "ВІДМОВОЮ" in " ".join(ex.proof)
    assert any("kmill-agent" in step for step in ex.actions)


def test_silence_everywhere_names_both_explanations_not_one():
    """Мовчання всіх портів НЕ доводить, що ПК вимкнено: брандмауер у профілі
    «Загальнодоступна» глушить їх так само. Вигаданий однозначний вирок гірший
    за чесно названі два пояснення (§14, «хибне число гірше за жодне»)."""
    rows = [
        {"port": 445, "result": machine_link.PROBE_SILENT, "ms": 1000},
        {"port": 135, "result": machine_link.PROBE_SILENT, "ms": 1000},
    ]
    ex = machine_link.explain(
        cause=machine_link.CAUSE_PORT_SILENT,
        error="",
        host="10.0.0.9",
        port=8765,
        probe_json=machine_link.dump_probe(rows),
    )
    doubt = " ".join(ex.doubt)
    assert "НЕ доводить" in doubt and "Загальнодоступна" in doubt


def test_an_answering_port_moves_the_blame_to_the_firewall():
    rows = [{"port": 445, "result": machine_link.PROBE_REFUSED, "ms": 14}]
    ex = machine_link.explain(
        cause=machine_link.CAUSE_PORT_SILENT,
        error="",
        host="10.0.0.9",
        port=8765,
        probe_json=machine_link.dump_probe(rows),
    )
    assert any("брандмауер" in step for step in ex.actions)


def test_no_probe_at_all_admits_it_and_offers_the_checkbox():
    ex = machine_link.explain(
        cause=machine_link.CAUSE_PORT_SILENT, error="", host="10.0.0.9", port=8765
    )
    doubt = " ".join(ex.doubt)
    assert "невідомо" in doubt and "Детальний журнал" in doubt


def test_plain_mode_verdict_is_the_answer_not_a_contradiction():
    """10.09.26, 150i і 250i: звичайний режим стукав і записав вирок «ПК
    озивається», а розбір, не знайшовши покрокових рядків, писав поруч «стук
    не робився, чи живий ПК — невідомо» і губив пораду про профіль мережі."""
    host, port = "192.168.1.82", 8765
    verdict = service._note_from_answer(True, host, port)
    ex = machine_link.explain(
        cause=machine_link.CAUSE_PORT_SILENT, error="", host=host, port=port,
        probe_verdict=verdict,
    )
    assert "невідомо" not in " ".join(ex.doubt)
    assert any("профіль мережі" in step for step in ex.actions)
    assert verdict not in ex.proof, "той самий факт удруге іншими словами"


def test_plain_mode_silent_verdict_names_both_explanations():
    host, port = "192.168.1.82", 8765
    ex = machine_link.explain(
        cause=machine_link.CAUSE_PORT_SILENT, error="", host=host, port=port,
        probe_verdict=service._note_from_answer(False, host, port),
    )
    assert "НЕ доводить" in " ".join(ex.doubt)


def test_a_late_plain_verdict_is_not_taken_as_proof():
    """Стук, що завершився після відновлення, описує вже живий ПК."""
    host, port = "192.168.1.82", 8765
    ex = machine_link.explain(
        cause=machine_link.CAUSE_PORT_SILENT, error="", host=host, port=port,
        probe_verdict=service._note_from_answer(True, host, port), probe_late=True,
    )
    assert not any("профіль мережі" in step for step in ex.actions)
    assert "невідомо" in " ".join(ex.doubt)


def test_an_unknown_verdict_text_is_not_guessed():
    assert machine_link.answered_from_verdict("щось зовсім інше", "10.0.0.9", 8765) is None
    assert machine_link.answered_from_verdict(None, "10.0.0.9", 8765) is None


def test_outage_length_counts_from_the_last_answer_not_from_detection():
    """Обрив визнаємо на третій невдачі, через 15-25 с після того, як верстат
    замовк. Ці секунди — теж обрив (10.09.26: 08:52:28 → виявлено 08:52:51)."""
    last_ok = datetime(2026, 9, 10, 8, 52, 28)
    event = MachineLinkEvent(
        id=1, host="192.168.1.82-8765", name="150i-Olejka",
        started_at=last_ok, detected_at=last_ok + timedelta(seconds=23),
        ended_at=last_ok + timedelta(seconds=23 + 137),
        error="", cause=machine_link.CAUSE_PORT_SILENT, failed_polls=16,
    )
    assert machine_link.view_of(event).seconds == 160

    event.started_at = None
    assert machine_link.view_of(event).seconds == 137


def test_refused_probe_counts_as_an_answer():
    """Відмова — така сама відповідь, як згода: її шле сам ПК."""
    rows = [
        {"port": 445, "result": machine_link.PROBE_SILENT, "ms": 1000},
        {"port": 135, "result": machine_link.PROBE_REFUSED, "ms": 9},
    ]
    assert machine_link.answered_from_probe(rows) is True
    assert machine_link.answered_from_probe([]) is None


def test_a_corrupt_probe_row_does_not_break_the_screen():
    assert machine_link.load_probe("{не json") == []
    assert machine_link.load_probe(None) == []


def test_a_restart_mid_outage_does_not_invent_a_second_outage(target):
    """Стан живе в памʼяті процесу, тож рестарт посеред обриву стирає знання,
    що рядок уже відкрито. У цеху застосунок перезапускається на авто-оновленні,
    а верстат може лежати вихідні — і один обрив давав ШІСТЬ рядків (живий
    прогін 09.09.26). Завищеною при цьому ставала рівно та цифра, заради якої
    екран і відкривають."""
    db = make_session()
    wrapped = machine_link.WRAP_PREFIX + service.msg_agent_not_listening(target.host, target.port)

    _fail(db, target, wrapped)
    first = db.scalars(select(MachineLinkEvent)).one().id
    service.reset_state_for_tests()          # той самий рестарт
    _fail(db, target, wrapped)

    rows = db.scalars(select(MachineLinkEvent)).all()
    assert len(rows) == 1, "рестарт посеред обриву не має заводити другий рядок"
    assert rows[0].id == first


def test_an_ancient_open_row_is_not_adopted(target):
    """Стеля потрібна, щоб не приписати верстату місячний простій за час, коли
    застосунок узагалі не працював і нічого не спостерігав."""
    db = make_session()
    stale = datetime.now() - timedelta(days=service.MACHINE_LINK_ADOPT_DAYS + 3)
    db.add(
        MachineLinkEvent(
            host=target.key, name=target.name, detected_at=stale,
            error="", cause=machine_link.CAUSE_AGENT_DOWN,
        )
    )
    db.commit()
    wrapped = machine_link.WRAP_PREFIX + service.msg_agent_not_listening(target.host, target.port)

    _fail(db, target, wrapped)

    assert len(db.scalars(select(MachineLinkEvent)).all()) == 2


def test_adopting_refreshes_the_error_text(target):
    """За час обриву поломка могла змінитись («мовчить» → «немає в мережі»),
    і актуальний текст корисніший за перший."""
    db = make_session()
    first = machine_link.WRAP_PREFIX + service.msg_host_silent(target.host, target.port)
    second = machine_link.WRAP_PREFIX + service.msg_host_no_answer(target.host)

    _fail(db, target, first)
    service.reset_state_for_tests()
    _fail(db, target, second)

    row = db.scalars(select(MachineLinkEvent)).one()
    assert row.cause == machine_link.CAUSE_NETWORK


def test_deep_probe_of_a_dead_agent_records_evidence_without_a_wrong_verdict(
    monkeypatch, target
):
    """У детальному режимі стукаємо й тоді, коли ПК уже відповів відмовою.
    Проби — доказ, але підпис «мовчить саме порт, винен брандмауер» там
    неправда: агент помер, порт ні до чого (живий прогін 09.09.26)."""
    db = make_session()
    deep = service.MachineTarget(
        name=target.name, host=target.host, port=target.port,
        agent_token="t", machine_id=1, diagnose_link=True,
    )
    rows = [{"port": 445, "result": machine_link.PROBE_OPEN, "ms": 1}]
    monkeypatch.setattr(machine_link, "probe_ports", lambda *a, **k: rows)
    wrapped = machine_link.WRAP_PREFIX + service.msg_agent_not_listening(deep.host, deep.port)

    _fail(db, deep, wrapped)
    _wait_for_probes()
    service.poll_target(db, deep, None, error=wrapped)

    row = db.scalars(select(MachineLinkEvent)).one()
    assert machine_link.load_probe(row.probe_json) == rows
    assert row.probe_verdict is None, "чужий вирок не має чіплятись до цієї поломки"
