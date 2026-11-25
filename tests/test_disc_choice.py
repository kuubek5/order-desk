"""Вивчений порядок дисків: читач журналу Sum3D, модель, картка, мовчазна пара."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import decisions_store as store
from app.models import ActionLog, Base, Order, SelectionRecord, User
from app.services import decision_rules as rules
from app.services import disc_choice as dc
from app.services import disc_history as dh
from app.services import disc_shadow, disc_suggest, disc_watch, selection_journal
from tests.test_disc_suggest import _disc, _stl, shop  # noqa: F401 — фікстура
from tests.test_sum3d_disc import _square

T0 = datetime(2026, 9, 1, 8, 0, 0)


def block(at, project, machine, blank, items=("Petrenko_11_crown_cad.stl",), thickness=14.0):
    lines = ["-" * 51, f"date             : {at:%Y-%m-%d}", f"time             : {at:%H:%M:%S}",
             f"project          : D:\\CAM-work\\{project}.cam", f"machine          : {machine}",
             "material         : Zirconium", f"thickness        : {thickness:.3f}",
             f"blank            : {blank}", "lot              : ", "shrink factor    : 1.25000"]
    lines += [f"item {i:03d}         : TS01 - {name}" for i, name in enumerate(items, 1)]
    return "\r\n".join([*lines, "calculation time : 00:01:02", ""])


def job(at, blank, machine="350i", items=2):
    return dh.Job(at=at, project=f"{at:%Y-%m-%d_%H-%M-%S}", machine=machine, blank=blank.lower(),
                  thickness=14.0, shrink=1.25, items=items)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    dc.forget_cache()
    disc_watch.forget()
    selection_journal.forget_shown()
    monkeypatch.setattr(disc_shadow, "INLINE", True)
    disc_suggest._fit_cache.clear()
    yield
    dc.forget_cache()
    selection_journal.forget_shown()


@pytest.fixture
def journal(tmp_path):
    store.configure(tmp_path / "decisions.db")
    yield
    store.shutdown()


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# ── Читач ──────────────────────────────────────────────────────────────────


def test_reader_takes_the_disc_and_never_the_patient(tmp_path):
    path = tmp_path / "jobs_list.txt"
    path.write_bytes((
        block(T0, "2026-09-01_08-00-00", "350i_Boris", "zr14_14-Monolith-A2-x47",
              ("Petrenko_11.stl", "Шевченко_12.stl"))
        + block(T0 + timedelta(hours=1), "Lege", "250i", r"C:\sum\DENTAL\zr16_16-Monolith-A3-x5.blk")
    ).encode("cp1251"))
    first, second = dh.read_tail(str(path), dh.LEARN_BYTES)
    assert (first.blank, first.machine, first.items, first.sum3d) == (
        "zr14_14-monolith-a2-x47", "350i_Boris", 2, "08-00-00")
    assert first.thickness == 14.0 and first.shrink == 1.25
    assert (second.blank, second.project) == ("zr16_16-monolith-a3-x5", "")
    assert "petrenko" not in repr([first, second]).lower()


def test_truncated_head_and_broken_dates_make_no_job(tmp_path):
    path = tmp_path / "jobs_list.txt"
    whole = block(T0, "2026-09-01_08-00-00", "m", "zr14_14-Monolith-A2-x1")
    path.write_bytes(whole.encode("cp1251") * 3)
    assert len(dh.read_tail(str(path), len(whole) + 40)) == 1
    broken = whole.replace("2026-09-01\r", "2026-13-01\r").encode("cp1251")
    assert list(dh.parse(broken.splitlines())) == []


def test_recalculated_project_is_one_choice_on_its_last_disc():
    moved = [job(T0, "a-x1"), job(T0 + timedelta(hours=1), "a-x1")]
    again = dh.Job(T0 + timedelta(hours=2), moved[0].project, "m", "a-x2", 14, 1.25, 1)
    assert [c.at for c in dh.choices(moved[:1] + [dh.Job(T0 + timedelta(minutes=5), moved[0].project, "m",
                                                         "a-x1", 14, 1.25, 1)])] == [T0]
    assert [c.blank for c in dh.choices([*moved, again])] == ["a-x1", "a-x2"]


def test_live_shelf_rereads_only_when_the_journal_grew(tmp_path, monkeypatch):
    path = tmp_path / "jobs_list.txt"
    path.write_bytes(block(T0, "2026-09-01_08-00-00", "m", "zr14_14-Monolith-A2-x1").encode())
    assert set(dh.live_shelf([str(path)]).discs) == {"zr14_14-monolith-a2-x1"}
    monkeypatch.setattr(dh, "_TTL_SECONDS", 0)
    reads = []
    real = dh.read_all
    monkeypatch.setattr(dh, "read_all", lambda *a: reads.append(1) or real(*a))
    dh.live_shelf([str(path)])
    assert reads == []
    with open(path, "ab") as f:
        f.write(block(T0 + timedelta(days=1), "2026-09-02_08-00-00", "m", "zr14_14-Monolith-A2-x2").encode())
    assert len(dh.live_shelf([str(path)]).discs) == 2 and reads == [1]


def test_missing_journal_is_an_empty_history_not_an_error(tmp_path):
    assert dh.read_all([str(tmp_path / "нема.txt")]) == []
    assert dh.live_shelf([]) is None


# ── Навчання ───────────────────────────────────────────────────────────────


def shop_history(rule, *, n=400, heights=(14,), seed=3):
    """Цех, де на кожну висоту лежить кілька живих дисків, а `rule` вибирає один."""
    rnd = random.Random(seed)
    jobs, live, serial = [], {h: [] for h in heights}, 0
    at = T0
    for _ in range(n):
        at += timedelta(hours=rnd.choice([2, 3, 5, 9]))
        h = rnd.choice(heights)
        while len(live[h]) < 4:
            serial += 1
            live[h].append({"name": f"zr{h}_{h}-monolith-a2-x{serial}", "last": at, "jobs": 0})
        pick = rule(live[h], rnd)
        pick["last"], pick["jobs"] = at, pick["jobs"] + 1
        jobs.append(job(at, pick["name"]))
        if pick["jobs"] >= 8:
            live[h].remove(pick)
    return jobs


def _first(found, name):
    return found["exam"]["диск_тієї_самої_висоти"][dc.ALL][name]["перший_%"]


def test_learns_that_operators_keep_cutting_the_last_disc(db):
    jobs = shop_history(lambda live, rnd: max(live, key=lambda d: d["last"]) if rnd.random() < 0.9 else rnd.choice(live))
    found = dc.mine(db, jobs=jobs, now=jobs[-1].at)
    assert _first(found, "вивчений порядок") >= _first(found, "найповніший")
    assert _first(found, "останній, з якого різали") > 75
    assert found["logic"][0]["лінія"] == "Monolith"


def test_learns_the_opposite_habit_too(db):
    """Не «останній» зашито в код, а звичка: цех, що добиває найповніший."""
    jobs = shop_history(lambda live, rnd: max(live, key=lambda d: (d["jobs"], d["name"]))
                        if rnd.random() < 0.85 else rnd.choice(live))
    found = dc.mine(db, jobs=jobs, now=jobs[-1].at)
    assert _first(found, "вивчений порядок") > 70
    assert _first(found, "вивчений порядок") > _first(found, "найпорожніший") + 40


def test_random_shop_gives_no_false_confidence(db):
    jobs = shop_history(lambda live, rnd: rnd.choice(live))
    found = dc.mine(db, jobs=jobs, now=jobs[-1].at)
    assert _first(found, "вивчений порядок") < 60


def test_height_habit_comes_from_works_with_known_thickness(db):
    rnd = random.Random(1)
    jobs, at = [], T0
    for i in range(160):
        at += timedelta(hours=3, seconds=7)
        # Робота 12,2 мм: до 14 лишається 1,8 — цех бере 16.
        h = 16 if rnd.random() < 0.85 else 14
        jobs.append(job(at, f"zr{h}_{h}-monolith-a2-x{i % 3}{h}"))
        db.add(Order(source="sheet_client", sheet_tab="01.09.26", client_name="К", material_color="mono a2", quantity="1",
                     sum3d_id=jobs[-1].sum3d, stl_height_mm=12.2 / 1.25, created_at=at))
    db.commit()
    found = dc.mine(db, jobs=jobs, now=at)
    assert found["з_товщиною"] == 160
    table = found["model"]["heights"]
    assert dc._height_share(table, "Monolith", "тісно", 1) > 0.7 > dc._height_share(table, "Monolith", "тісно", 0)
    both = found["exam"]["диск_і_висота_разом"]
    assert both[dc.LEARNED]["виборів"] > 20


def test_thickness_of_another_day_or_material_is_not_borrowed(db):
    jobs = [job(T0, "zr14_14-monolith-a2-x1")]
    known = dc.Known({"08-00-00": [(T0 + timedelta(days=30), 12.0, "Цирконій"), (T0, 9.0, "ПММА")]}, {})
    event = dc.events_of(jobs, known, since=T0 - timedelta(days=1))[0]
    assert event.thickness is None and event.chosen == -1


def test_different_colour_line_or_hipc_never_compete():
    jobs = [job(T0 + timedelta(hours=i), name) for i, name in enumerate([
        "zr14_14-monolith-a2-x1", "zr14_14-monolith-a3-x2", "zr14_14-emotions-a2-x3",
        "pmma14_14-a2-x4", "pmma14_14-hipc-a2-x5", "zr14_14-monolith-a2-x6"])]
    last = dc.events_of(jobs, dc.Known(), since=T0)[-1]
    assert last.names == ["zr14_14-monolith-a2-x1"] and last.chosen == -1


def test_exam_never_scores_ties_in_our_favour():
    import numpy as np

    assert dc._rank(np.zeros(4), 1) == 3
    assert dc._rank(np.array([1.0, 2.0]), 1) == 0


# ── Картка ─────────────────────────────────────────────────────────────────


MODEL = {"weights": {dc.ALL: [5.0, 0, 0, 0, -1.0, 0]}, "heights": {}}


def _two_discs(tmp_path):
    root = tmp_path / "blanks"
    disc_suggest._scan_cache.clear()
    _disc(root, "zr", 14, "zr14_14-Monolith-A2-x1", [_square(x, 0, 8) for x in (-25, 0, 25)])
    _disc(root, "zr", 14, "zr14_14-Monolith-A2-x2", [_square(0, 0, 4)])
    stl = tmp_path / "m"
    stl.mkdir()
    path = _stl(stl / "c_11-crown_cad.stl", 8, 8, 8.0)
    from types import SimpleNamespace
    return root, [SimpleNamespace(filename=path.name, saved_path=str(path))]


def test_without_a_model_the_card_is_exactly_as_before(tmp_path):
    root, files = _two_discs(tmp_path)
    hint = disc_suggest.suggest("mono a2", files, str(root))
    assert hint.by == "rule" and hint.best.name.endswith("x1")  # правило: найповніший
    assert {p["d"] for p in hint.pool} == {"zr14_14-Monolith-A2-x1", "zr14_14-Monolith-A2-x2"}


def test_learned_order_puts_the_last_cut_disc_first(tmp_path):
    root, files = _two_discs(tmp_path)
    shelf = dh.shelf_of([job(T0, "zr14_14-Monolith-A2-x1"), job(T0 + timedelta(days=1), "zr14_14-Monolith-A2-x2")])
    chooser = dc.make_chooser(MODEL, shelf, now=T0 + timedelta(days=2))
    hint = disc_suggest.suggest("mono a2", files, str(root), chooser=chooser)
    assert hint.by == "learned" and hint.best.name.endswith("x2") and hint.best.fit
    assert [p.name[-2:] for p in hint.alts] == ["x1"]


def test_learned_order_never_offers_a_disc_lower_than_the_work(tmp_path):
    root, files = _two_discs(tmp_path)
    _disc(root, "zr", 8, "zr8_8-Monolith-A2-x9", [_square(0, 0, 4)])
    disc_suggest._scan_cache.clear()
    chooser = dc.make_chooser(MODEL, dh.shelf_of([job(T0, "zr8_8-Monolith-A2-x9")]), now=T0)
    hint = disc_suggest.suggest("mono a2", files, str(root), chooser=chooser)
    assert all(p.height == 14 for p in [hint.best, *hint.alts])


def test_status_gives_the_card_rules_until_the_admin_says_on(db, journal, monkeypatch):
    marker = object()
    monkeypatch.setattr(dc, "current", lambda _db, _op=None: marker)
    assert dc.choosers(db) == (None, marker, True)
    rules.save_status(db, dc.RULE, "on", user_id=1)
    assert dc.choosers(db) == (marker, None, True)
    rules.save_status(db, dc.RULE, "off", user_id=1)
    assert dc.choosers(db) == (None, None, False)
    rules.save_status(db, dc.RULE, "paused", user_id=None)
    assert dc.choosers(db) == (None, marker, True)


def test_broken_model_leaves_the_rules(db, journal, monkeypatch):
    monkeypatch.setattr(dc, "current", lambda _db, _op=None: 1 / 0)
    assert dc.choosers(db) == (None, None, False)


def test_current_needs_both_the_model_and_the_journal(db, journal, monkeypatch, tmp_path):
    assert dc.current(db) is None
    dc.save({"model": MODEL})
    monkeypatch.setattr(dh, "files", lambda _db: [])
    assert dc.current(db) is None
    dc.forget_cache()
    path = tmp_path / "jobs_list.txt"
    path.write_bytes(block(T0, "2026-09-01_08-00-00", "m", "zr14_14-Monolith-A2-x1").encode())
    monkeypatch.setattr(dh, "files", lambda _db: [str(path)])
    assert dc.current(db) is not None


# ── Мовчазна пара ──────────────────────────────────────────────────────────


def _record(db, i, actual, rule, learned, *, by="rule", batch=None, at=None):
    shown, rival = (rule, learned) if by == "rule" else (learned, rule)
    hint = {"best": {"disc": shown[0]} if shown else None, "alts": [{"disc": n} for n in shown[1:]], "by": by,
            "rival": {"by": "learned" if by == "rule" else "rule", "discs": rival}}
    db.add(SelectionRecord(email_id=i, order_id=i, chosen_material="mono a2", actual_disc=actual, batch_key=batch,
                           mode="conveyor" if batch else "card", hint_json=json.dumps(hint),
                           created_at=at or datetime(2026, 11, 20)))
    db.commit()


NOW = datetime(2026, 11, 25)
X1, X2 = "zr14_14-Monolith-A2-x1", "zr14_14-Monolith-A2-x2"


def test_rivalry_counts_both_sides_once_per_batch(db):
    _record(db, 1, "14-monolith-a2-x2", [X1, X2], [X2, X1])
    _record(db, 2, "14-monolith-a2-x2", [X2], [X1], by="learned")
    _record(db, 3, "14-monolith-a2-x1", [X1], [X2], batch="c1")
    _record(db, 4, "14-monolith-a2-x1", [X1], [X2], batch="c1")
    tally = selection_journal.rivalry(db, 30, now=NOW)
    assert tally["рішень"] == 3
    assert tally["правила"] == {"перший": 2, "у_трьох": 3}
    assert tally["вивчений"] == {"перший": 1, "у_трьох": 1}


def test_records_without_a_rival_are_not_evidence(db):
    db.add(SelectionRecord(email_id=1, chosen_material="m", actual_disc="x", created_at=NOW,
                           hint_json=json.dumps({"best": None, "alts": []})))
    db.commit()
    assert selection_journal.rivalry(db, 30, now=NOW)["рішень"] == 0


def test_learned_order_pauses_itself_when_it_loses(db, journal):
    for i in range(25):
        _record(db, i, "14-monolith-a2-x1", [X1], [X2], by="learned")
    assert dc.review(db, now=NOW) is None  # не ввімкнено — вимикати нічого
    rules.save_status(db, dc.RULE, "on", user_id=1)
    assert dc.review(db, now=NOW).startswith("пауза")
    assert dc.status(db) == "paused" and "25" in rules.load(db)[dc.KEY]["note"]


def test_learned_order_stays_when_it_wins(db, journal):
    for i in range(25):
        _record(db, i, "14-monolith-a2-x2", [X1], [X2], by="learned")
    rules.save_status(db, dc.RULE, "on", user_id=1)
    assert dc.review(db, now=NOW) is None and dc.status(db) == "on"


def test_shadow_lands_in_what_the_operator_saw(tmp_path):
    root, files = _two_discs(tmp_path)
    shown = disc_suggest.suggest("mono a2", files, str(root))
    entry = selection_journal.remember_shown([7], "mono a2", shown)
    shelf = dh.shelf_of([job(T0, X1), job(T0 + timedelta(days=1), X2)])
    disc_shadow.submit(entry, "mono a2", files, str(root), active={}, recent=set(),
                       chooser=dc.make_chooser(MODEL, shelf, now=T0 + timedelta(days=2)))
    hint = selection_journal.shown_for(7)["hint"]
    assert hint["by"] == "rule" and hint["rival"]["by"] == "learned"
    assert hint["rival"]["discs"][0] == X2 and hint["best"]["disc"] == X1


def test_failed_shadow_is_silent_and_frees_its_place(tmp_path, monkeypatch):
    monkeypatch.setattr(disc_suggest, "suggest", lambda *a, **k: 1 / 0)
    entry = {"hint": {}}
    for _ in range(disc_shadow.MAX_WAITING + 2):
        disc_shadow.submit(entry, "mono a2", [], None, active={}, recent=set(), chooser=None)
    assert entry == {"hint": {}} and disc_shadow._waiting == 0


def test_without_height_evidence_the_lowest_covering_disc_wins():
    import numpy as np

    x = np.zeros((3, len(dc.FEATURES)))
    values = dc.score(MODEL, "Monolith", 10.0, [14, 16, 20], [20, 14, 16], x)
    assert list(np.argsort(-values)) == [1, 2, 0]


# ── Людина, звичка, впевненість ────────────────────────────────────────────


def test_each_operator_gets_their_own_habit(db):
    db.add_all([User(id=1, username="a", password_hash="x"), User(id=2, username="b", password_hash="x")])
    rnd = random.Random(7)

    def rule(live, _rnd):
        who["now"] = rnd.choice([1, 2])
        if rnd.random() < 0.1:
            return rnd.choice(live)
        return (max if who["now"] == 1 else min)(live, key=lambda d: (d["last"], d["name"]))

    who, owners = {}, []
    jobs, live, serial, at = [], [], 0, T0
    for i in range(500):
        at += timedelta(hours=3, seconds=i)
        while len(live) < 4:
            serial += 1
            live.append({"name": f"zr14_14-monolith-a2-x{serial}", "last": at, "jobs": 0})
        pick = rule(live, rnd)
        pick["last"], pick["jobs"] = at, pick["jobs"] + 1
        if pick["jobs"] >= 12:
            live.remove(pick)
        jobs.append(job(at, pick["name"]))
        owners.append(who["now"])
        order = Order(source="sheet_client", sheet_tab="01.09.26", client_name="К", material_color="mono a2",
                      quantity="1", sum3d_id=jobs[-1].sum3d, created_at=at)
        db.add(order)
        db.flush()
        db.add(ActionLog(order_id=order.id, operator_id=who["now"], action_type="sum3d", created_at=at))
    db.commit()
    found = dc.mine(db, jobs=jobs, now=at)
    assert found["з_оператором"] == 500
    mine = {row["оператор"]: row["звички_%"] for row in found["logic"]}
    assert mine[1]["останній, з якого різали"] > 70 > 30 > mine[2]["останній, з якого різали"]
    both = {k: v["перший_%"] for k, v in found["exam"]["за_оператором"].items()}
    assert min(both.values()) > 60  # спільна звичка дала б ~50


def test_unknown_operator_falls_back_to_the_line_not_to_zero():
    model = {"weights": {dc.ALL: [1.0] * dc.K, "Monolith": [2.0] * dc.K, "Monolith|1": [3.0] * dc.K}}
    assert dc._weights(model, "Monolith", 1)[0] == 3.0
    assert dc._weights(model, "Monolith", 99)[0] == 2.0
    assert dc._weights(model, "Emotions", None)[0] == 1.0


def test_old_choices_weigh_less_than_new_ones(db):
    """Цех змінив звичку: модель іде за новою."""
    def mostly(pick):
        return lambda live, rnd: pick(live, key=lambda d: (d["last"], d["name"])) if rnd.random() < 0.8 else rnd.choice(live)

    old = shop_history(mostly(min), n=250)
    shift = old[-1].at + timedelta(days=200) - T0
    new = [dh.Job(j.at + shift, f"{j.at + shift:%Y-%m-%d_%H-%M-%S}", j.machine, j.blank.replace("-x", "-x9"),
                  j.thickness, j.shrink, j.items)
           for j in shop_history(mostly(max), n=250)]
    weights = dc._train(dc.events_of(old + new, dc.Known(), since=T0), new[-1].at)["weights"]["Monolith"]
    assert weights[4] < 0  # що давніше різали, то рідше беруть


def test_simple_habit_replaces_weights_only_when_proven():
    exam = dc._Exam()
    exam.same["Monolith"][dc.LEARNED] = dc._Tally(n=100, first=50)
    exam.same["Monolith"]["найновіший"] = dc._Tally(n=100, first=80)
    exam.pairs["Monolith|найновіший"].update(звичка=35, ваги=5)
    assert exam.better_habits() == {"Monolith": "найновіший"}
    exam.pairs["Monolith|найновіший"] = dc.Counter(звичка=6, ваги=5)
    assert exam.better_habits() == {}


def test_confidence_is_the_exam_hit_rate_or_silence(tmp_path):
    root, files = _two_discs(tmp_path)
    shelf = dh.shelf_of([job(T0, X1), job(T0 + timedelta(days=1), X2)])
    sure = {**MODEL, "bins": {"4": [72, 80]}}
    hint = disc_suggest.suggest("mono a2", files, str(root), chooser=dc.make_chooser(sure, shelf, now=T0 + timedelta(days=2)))
    assert hint.confidence == 0.9 and "різали останнім" in hint.best.habit
    few = {**MODEL, "bins": {"4": [5, 6]}}
    hint = disc_suggest.suggest("mono a2", files, str(root), chooser=dc.make_chooser(few, shelf, now=T0 + timedelta(days=2)))
    assert hint.confidence is None


def test_untouched_new_disc_is_not_the_last_cut_one(tmp_path):
    root, files = _two_discs(tmp_path)
    _disc(root, "zr", 14, "zr14_14-Monolith-A2-x3", [])  # новий файл, ще не різаний
    disc_suggest._scan_cache.clear()
    shelf = dh.shelf_of([job(T0, X1), job(T0 + timedelta(days=1), X2)])
    hint = disc_suggest.suggest("mono a2", files, str(root), chooser=dc.make_chooser(MODEL, shelf, now=T0 + timedelta(days=2)))
    assert [p.name for p in [hint.best, *hint.alts]][:2] == [X2, X1]


def test_fullest_disc_must_have_room():
    uses = {n: dh.DiscUse(T0, T0) for n in "abc"}
    row = dc.features(uses, set(), T0, free={"a": 0.05, "b": 0.3, "c": 0.9}, need=0.1)
    assert [row[n][dc._FULLEST] for n in "abc"] == [0.0, 1.0, 0.0]


# ── «Автоматично» ──────────────────────────────────────────────────────────


def test_auto_turns_on_only_when_proven_and_off_when_losing(db, journal):
    rules.save_status(db, dc.RULE, "auto", user_id=1)
    for i in range(20):
        _record(db, i, "14-monolith-a2-x2", [X1], [X2])
    assert dc.review(db, now=NOW) is None and not dc.is_on(db)  # 20 рішень — замало
    for i in range(20, 35):
        _record(db, i, "14-monolith-a2-x2", [X1], [X2])
    assert dc.review(db, now=NOW).startswith("увімкнено сам") and dc.is_on(db)
    for i in range(100, 140):
        _record(db, i, "14-monolith-a2-x1", [X1], [X2], by="learned")
    assert dc.review(db, now=NOW).startswith("вимкнено сам") and not dc.is_on(db)


def test_verdict_needs_a_gap_bigger_than_chance():
    tally = {"рішень": 60, "лише_вивчений": 12, "лише_правила": 9}
    assert dc.verdict(tally, was_on=False)[0] is False
    assert dc.verdict(tally, was_on=True)[0] is True
    assert dc.verdict({**tally, "лише_вивчений": 25}, was_on=False)[0] is True


def test_auto_is_only_for_discs(db):
    assert dc.STATUS_AUTO in rules.STATUSES
    rules.save_status(db, {"key": "repeat:1", "kind": "repeat", "params": {"types": "crown+crown"}}, "auto", user_id=1)
    assert rules.apply(db, client_name="", system_material="", filenames=["a_11-crown_cad.stl", "b_11-crown_cad.stl"]).extra_units == 0


# ── Чому промахнулись ──────────────────────────────────────────────────────


def _miss(db, i, actual, why_not, best=X1):
    db.add(SelectionRecord(email_id=i, order_id=i, chosen_material="mono a2", actual_disc=actual, created_at=NOW,
                           hint_json=json.dumps({"best": {"disc": best}, "alts": [], "why_not": why_not})))
    db.commit()


def test_every_miss_has_a_reason(db):
    _miss(db, 1, "14-monolith-a2-x1", {})
    _miss(db, 2, "14-monolith-a2-x2", {X2: "розстановка"})
    _miss(db, 3, "14-monolith-a2-x9", {X2: "висота"})
    found = selection_journal.misses(db, 30, now=NOW)
    assert found["причини"] == {
        "перший у картці": 1, "CRM: не влазить — а цех поклав": 1,
        "не було серед дисків (новий, інший матеріал чи колір)": 1}


def test_old_records_without_reasons_are_not_guessed(db):
    db.add(SelectionRecord(email_id=1, chosen_material="m", actual_disc="x", created_at=NOW,
                           hint_json=json.dumps({"best": None, "alts": []})))
    db.commit()
    assert selection_journal.misses(db, 30, now=NOW)["рішень"] == 0


def test_every_reason_the_hint_gives_has_words(tmp_path):
    assert set(disc_suggest.REASON_CODES.values()) <= set(selection_journal.MISS_WORDS)
    root, files = _two_discs(tmp_path)
    _disc(root, "zr", 25, "zr25_25-Monolith-A2-x7", [_square(0, 0, 4)])
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x6", [_square(0, 0, 4)])
    _disc(root, "zr", 14, "zr14_14-Monolith-A2-x8", [_square(0, 0, 44, n=48)])
    disc_suggest._scan_cache.clear()
    hint = disc_suggest.suggest("mono a2", files, str(root))
    assert hint.why_not["zr25_25-Monolith-A2-x7"] == "висота"
    assert hint.why_not["zr14_14-Monolith-A2-x8"] in ("площа", "вирізаний", "розстановка")
    assert X1 not in hint.why_not


# ── Живі вибори ────────────────────────────────────────────────────────────


@pytest.fixture
def watched(tmp_path, db, journal, monkeypatch):
    from app.settings_store import set_setting

    root = tmp_path / "blanks"
    disc_suggest._scan_cache.clear()
    _disc(root, "zr", 14, X1, [_square(x, 0, 8) for x in (-25, 0, 25)])
    _disc(root, "zr", 14, X2, [_square(0, 0, 4)])
    path = tmp_path / "jobs_list.txt"
    set_setting(db, "cam_blanks_path", str(root))
    monkeypatch.setattr(dh, "files", lambda _db: [str(path)])
    now = datetime.now().replace(microsecond=0)
    import os
    old = (now - timedelta(days=2)).timestamp()
    for name in (X1, X2):
        os.utime(root / "zr" / "14" / f"{name}.blk", (old, old))

    def add(at, blank, items=1, project=None):
        with open(path, "ab") as f:
            f.write(block(at, project or f"{at:%Y-%m-%d_%H-%M-%S}", "350i", blank, ["x.stl"] * items).encode())

    add(now - timedelta(days=2), X1)
    add(now - timedelta(days=1), X2)
    assert disc_watch.observe(db, now=now) == 0  # перший прохід лише запам'ятовує
    return root, now, add


def _rows():
    return store.query("SELECT project, blank, body, rank FROM disc_choices ORDER BY at")


def test_new_project_is_recorded_with_everything_it_was_chosen_from(watched, db):
    root, now, add = watched
    at = now - timedelta(minutes=1)
    add(at, X2, items=3)
    assert disc_watch.observe(db, now=now) == 1
    row = _rows()[0]
    body = row["body"] if isinstance(row["body"], dict) else json.loads(row["body"])
    free = {c[0]: c[2] for c in body["c"]}
    assert row["blank"] == X2.lower() and set(free) == {X1.lower(), X2.lower()}
    assert 0 < free[X1.lower()] < free[X2.lower()] < 1 and body["units"] == 3
    event = disc_watch.events(dc.Known(), since=now - timedelta(days=1))[0]
    assert event.live and event.names[event.chosen] == X2.lower()
    assert disc_watch.observe(db, now=now) == 0 and len(_rows()) == 1
    assert "x.stl" not in json.dumps(body)


def test_room_before_comes_from_the_remembered_state_not_the_rewritten_file(watched, db):
    root, now, add = watched
    add(now - timedelta(minutes=2), X1)
    disc_watch.observe(db, now=now)  # стан обох дисків запам'ятовано
    (root / "zr" / "14" / f"{X2}.blk").unlink()
    _disc(root, "zr", 14, X2, [_square(0, 0, 4), _square(20, 20, 8)])  # Sum3D поклала роботу
    disc_suggest._scan_cache.clear()
    later = now + timedelta(minutes=5)
    add(later, X2)
    assert disc_watch.observe(db, now=later) == 1
    body = [r for r in _rows() if r["blank"] == X2.lower()][0]["body"]
    body = body if isinstance(body, dict) else json.loads(body)
    assert body["used"] and body["used"] > 0


def test_rewritten_disc_with_unknown_past_has_no_invented_room(watched, db):
    import os

    root, now, add = watched
    os.utime(root / "zr" / "14" / f"{X2}.blk", (now.timestamp(), now.timestamp()))
    add(now, X2)
    disc_watch.observe(db, now=now)
    body = _rows()[0]["body"]
    body = body if isinstance(body, dict) else json.loads(body)
    assert {c[0]: c[2] for c in body["c"]}[X2.lower()] is None and body["used"] is None


def test_two_sum3d_writing_out_of_order_lose_nothing(watched, db):
    _root, now, add = watched
    add(now - timedelta(minutes=1), X2)
    disc_watch.observe(db, now=now)
    add(now - timedelta(minutes=30), X1)  # довший розрахунок дописав блок пізніше
    assert disc_watch.observe(db, now=now) == 1
    assert {r["blank"] for r in _rows()} == {X1.lower(), X2.lower()}


def test_long_silence_is_skipped_not_invented(watched, db):
    _root, now, add = watched
    add(now - timedelta(hours=13), X1)
    assert disc_watch.observe(db, now=now) == 0 and _rows() == []


def test_disc_with_no_room_is_not_a_choice():
    body = {"c": [["a", 14, 0.05, T0.isoformat(), T0.isoformat(), 5, 2], ["b", 14, 0.6, T0.isoformat(), T0.isoformat(), 1, 1],
                  ["c", 14, None, T0.isoformat(), T0.isoformat(), 1, 1]],
            "used": 0.1, "units": 1, "height": 14, "mounted": [], "group": ["Цирконій", "monolith", ["a2"], None, False]}
    event = disc_watch._event(T0, "b", body, None, None)
    assert list(event.feasible) == [False, True, True]


def test_live_choices_replace_the_same_project_from_the_journal(watched, db):
    _root, now, add = watched
    add(now - timedelta(minutes=1), X2)
    disc_watch.observe(db, now=now)
    jobs = dh.read_all(dh.files(db))
    found = dc.mine(db, jobs=jobs, now=now)
    assert found["живих_виборів"] == 1 and found["проєктів"] == len(jobs)


def test_curve_counts_what_the_model_of_that_day_said(watched, db):
    _root, now, add = watched
    dc.save({"at": now.isoformat(), "model": MODEL})
    add(now - timedelta(minutes=1), X2)
    disc_watch.observe(db, now=now)
    assert [w["перший_%"] for w in disc_watch.curve(now=now)] == [100]


def test_model_point_is_kept_once_a_day(journal):
    exam = {"диск_тієї_самої_висоти": {dc.ALL: {dc.LEARNED: {"виборів": 5, "перший_%": 60.0}}}}
    for at in ("2026-11-24T04:00:00", "2026-11-24T09:00:00", "2026-11-25T04:00:00"):
        dc.save({"at": at, "model": MODEL, "exam": exam})
    assert [c["at"] for c in dc.load()["curve"]] == ["2026-11-24T09:00:00", "2026-11-25T04:00:00"]


# ── kmill_machine_history ──────────────────────────────────────────────────


def test_machine_history_reads_by_the_key_with_the_port(db):
    from app.models import Machine, MachineReading
    from app.services import shop_history
    from app.services.machines import DEFAULT_PORT

    db.add_all([Machine(name="Агент", host="192.168.1.81", port=8765, created_at=T0),
                Machine(name="VNC", host="192.168.1.85", port=DEFAULT_PORT, created_at=T0)])
    now = datetime.now()
    for host in ("192.168.1.81-8765", "192.168.1.85"):
        for m, pct in ((30, 10), (20, 100)):
            db.add(MachineReading(host=host, iso_name="14-monolith-a2-x1_2026-11-25_07-00-00.iso", percent=pct,
                                  captured_at=now - timedelta(minutes=m)))
    db.commit()
    found = {r["верстат"]: r for r in shop_history.machine_history(db, 7)["верстати"]}
    assert found["Агент"]["показів"] == found["VNC"]["показів"] == 2
    assert found["Агент"]["програм"] == found["VNC"]["програм"] == 1
