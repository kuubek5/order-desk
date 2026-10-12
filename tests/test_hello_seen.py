"""Ранкове привітання — ОДИН раз на робочий день на оператора (власник 12.10.26).

Бойовий випадок: «Добрий день, … · 45 робіт сьогодні» вилазило над чергою
знову й знову — «уже бачив сьогодні» жило лише в localStorage браузера, а на
робочих місцях цеху воно не доживає до наступного відкриття. Та сама вада, що
й у «Що нового» (tests/test_whats_new_seen.py), той самий лік: позначка ще й
на акаунті (users.seen_hello_day), браузер бере пізнішу з двох.

Що ламається тихо:
- роут приймає позначку, але черга її не несе — браузер і далі бачить «ні» й
  показує привітання (тому перевірка — справжнім рендером черги);
- «бачив завтра» сховало б завтрашнє привітання — майбутній день не пишемо;
  «завтра» — РОБОЧЕ (межа 07:30), не календарне;
- вкладка, відкрита вчора, не опускає сьогоднішню позначку;
- поки на екрані «Що нового», привітання не показується І не позначається —
  інакше день «витрачено» непоміченим;
- рішення в queue.js — справжній код у node, не переказ.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.business_day import business_today
from app.models import User
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

ROOT = Path(__file__).resolve().parent.parent
QUEUE_JS = ROOT / "app" / "static" / "js" / "queue.js"
HELLO_ATTR = re.compile(r'id="mhello" hidden data-day="([^"]*)" data-seen="([^"]*)"')


def _login(app, who=OPERATOR, host: str = "127.0.0.1") -> MiniClient:
    client = MiniClient(app, client_host=host)
    status, _, _ = client.login(*who)
    assert status in (200, 302, 303), status
    return client


def _hello_marks(client: MiniClient) -> tuple[str, str]:
    status, _, html = client.get("/")
    assert status == 200, status
    match = HELLO_ATTR.search(html)
    assert match, "черга не несе #mhello з data-day і data-seen"
    return match.group(1), match.group(2)


def _stored(session_factory, username: str):
    with session_factory() as db:
        return db.query(User).filter_by(username=username).one().seen_hello_day


def _today() -> str:
    return business_today().isoformat()


# ── Сервер: позначка на акаунті ───────────────────────────────────────────


def test_operator_mark_is_stored_and_comes_back_on_the_queue(app_db):  # noqa: F811
    app, session_factory = app_db
    client = _login(app)
    day, seen = _hello_marks(client)
    assert (day, seen) == (_today(), ""), "новий акаунт — ще не бачив"

    # Як fetch із браузера: той самий origin, що й сторінка.
    status, _, _ = client.post(
        "/account/hello-seen", {"day": day}, headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert status == 204
    assert _stored(session_factory, OPERATOR[0]) == day
    assert _hello_marks(client) == (day, day)
    # Новий браузер (нова сесія) — позначка їде за акаунтом.
    assert _hello_marks(_login(app)) == (day, day)
    # Іншому оператору не протікає.
    assert _hello_marks(_login(app, ADMIN)) == (day, "")


def test_operator_on_a_network_pc_can_mark(app_db):  # noqa: F811
    """«Робота з інших ПК»: оператор за мережевим ПК — не адмінська дія."""
    app, session_factory = app_db
    client = _login(app, host="192.168.1.50")
    assert client.post("/account/hello-seen", {"day": _today()})[0] == 204
    assert _stored(session_factory, OPERATOR[0]) == _today()


@pytest.mark.parametrize(
    "bad", ["", "abc", "12.10.26", "20261012", "2026-13-01", "2026-02-30", "2026-1-5", "<b>2026-10-12</b>"]
)
def test_garbage_is_refused(app_db, bad):  # noqa: F811
    app, session_factory = app_db
    client = _login(app)
    assert client.post("/account/hello-seen", {"day": bad})[0] == 422
    assert _stored(session_factory, OPERATOR[0]) is None


def test_future_business_day_is_refused(app_db):  # noqa: F811
    """«Бачив завтра» сховало б завтрашнє привітання."""
    app, session_factory = app_db
    client = _login(app)
    tomorrow = (business_today() + timedelta(days=1)).isoformat()
    assert client.post("/account/hello-seen", {"day": tomorrow})[0] == 422
    assert _stored(session_factory, OPERATOR[0]) is None


def test_mark_only_goes_up(app_db):  # noqa: F811
    """Вкладка, відкрита вчора, сьогоднішню позначку не опускає."""
    app, session_factory = app_db
    client = _login(app)
    yesterday = (business_today() - timedelta(days=1)).isoformat()
    assert client.post("/account/hello-seen", {"day": _today()})[0] == 204
    assert client.post("/account/hello-seen", {"day": yesterday})[0] == 204
    assert _stored(session_factory, OPERATOR[0]) == _today()


def test_back_fill_from_an_older_day_moves_the_mark_forward(app_db):  # noqa: F811
    app, session_factory = app_db
    client = _login(app)
    yesterday = (business_today() - timedelta(days=1)).isoformat()
    assert client.post("/account/hello-seen", {"day": yesterday})[0] == 204
    assert _stored(session_factory, OPERATOR[0]) == yesterday
    assert client.post("/account/hello-seen", {"day": _today()})[0] == 204
    assert _stored(session_factory, OPERATOR[0]) == _today()


def test_anonymous_is_refused(app_db):  # noqa: F811
    app, session_factory = app_db
    assert MiniClient(app).post("/account/hello-seen", {"day": _today()})[0] == 401
    assert _stored(session_factory, OPERATOR[0]) is None


def test_foreign_origin_is_refused(app_db):  # noqa: F811
    """CSRF-стіна (same_origin_guard): dev-порт не пише в прод."""
    app, session_factory = app_db
    client = _login(app)
    status, _, _ = client.post(
        "/account/hello-seen", {"day": _today()}, headers={"Origin": "http://127.0.0.1:8002"},
    )
    assert status == 403
    assert _stored(session_factory, OPERATOR[0]) is None


# ── Схема: колонка є і на чистій базі, і на старій ───────────────────────


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    finally:
        conn.close()


def test_fresh_database_gets_the_column_in_a_clean_interpreter(tmp_path):
    """Як стартує лаунчер: `ensure_schema` ДО імпорту моделей (див. §14 DR)."""
    db_path = tmp_path / "fresh.db"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from app.schema import ensure_schema\n"
        "assert not any(m.startswith('app.models') for m in sys.modules)\n"
        f"ensure_schema(Path({str(db_path)!r}), Path({str(tmp_path / 'backups')!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT,
        env=dict(os.environ, DB_PATH=str(db_path), PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr[-600:]
    assert "seen_hello_day" in _columns(db_path, "users")


def test_existing_database_gets_the_column_and_operators_stay_unseen(tmp_path, monkeypatch):
    """База на 0087 (прод після 0.21.67): колонка доїжджає, наявні акаунти — NULL."""
    from app.schema import ensure_schema

    monkeypatch.setenv("DB_PATH", "unused-by-this-test.db")  # ensure_schema його ставить
    db_path = tmp_path / "old.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0087_user_seen_whats_new"], cwd=ROOT,
        env=dict(os.environ, DB_PATH=str(db_path), PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr[-600:]
    assert "seen_hello_day" not in _columns(db_path, "users")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active) "
            "VALUES ('old_op', 'x', 'оператор', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    ensure_schema(db_path, tmp_path / "backups")

    assert "seen_hello_day" in _columns(db_path, "users")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT seen_hello_day FROM users WHERE username='old_op'").fetchone() == (None,)
    finally:
        conn.close()


# ── Браузер: справжній initMorningHello з queue.js у node ────────────────

HARNESS = r"""
const vm = require("vm");
const CASE = __CASE__;
const docListeners = {};
const box = {
  hidden: true, dataset: { day: CASE.day, seen: CASE.account },
  classList: { add() {}, remove() {}, contains: () => false },
  addEventListener() {}, querySelectorAll: () => [],
};
const store = {};
if (CASE.local) store.helloDay = CASE.local;
const posts = [];
global.window = {
  KMStore: { get: (k) => (k in store ? store[k] : null), set: (k, v) => { store[k] = String(v); } },
  matchMedia: () => ({ matches: true }),
};
global.KMStore = global.window.KMStore;
global.fetch = (url, opts) => { posts.push({ url, body: opts.body }); return Promise.resolve({ ok: true }); };
global.setTimeout = () => 0;
global.requestAnimationFrame = () => 0;
global.document = {
  hidden: !!CASE.hidden,
  getElementById: (id) => (id === "mhello" ? box : null),
  documentElement: { dataset: CASE.whatsNew ? { whatsNew: "1" } : {} },
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  removeEventListener: (t, fn) => { docListeners[t] = (docListeners[t] || []).filter((f) => f !== fn); },
};
vm.runInThisContext(__CODE__);
const before = { shown: !box.hidden, local: store.helloDay || "", posts: posts.length };
if (CASE.reveal) {
  document.hidden = false;
  (docListeners.visibilitychange || []).slice().forEach((fn) => fn());
}
process.stdout.write(JSON.stringify({
  before,
  shown: !box.hidden,
  local: store.helloDay || "",
  posts,
}));
"""

DAY, PREV = "2026-10-12", "2026-10-11"


def _hello_source() -> str:
    text = QUEUE_JS.read_text(encoding="utf-8")
    start = text.index("(function initMorningHello() {")
    show = text.index("function showHello(", start)
    end = text.index("\n}\n", show) + len("\n}\n")
    return text[start:end]


def _run_js(tmp_path: Path, *, local: str, account: str, whats_new: bool = False,
            hidden: bool = False, reveal: bool = False) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не знайдено — перевірку JS пропущено")
    case = {"day": DAY, "local": local, "account": account, "whatsNew": whats_new,
            "hidden": hidden, "reveal": reveal}
    script = HARNESS.replace("__CASE__", json.dumps(case)).replace(
        "__CODE__", json.dumps(_hello_source())
    )
    path = tmp_path / "hello_harness.js"
    io.open(path, "w", encoding="utf-8", newline="\n").write(script)
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _posted(out: dict) -> list[str]:
    assert all(p["url"] == "/account/hello-seen" for p in out["posts"])
    return [p["body"] for p in out["posts"]]


def test_js_nothing_seen_shows_and_marks_both(tmp_path):
    out = _run_js(tmp_path, local="", account="")
    assert out["shown"] and out["local"] == DAY
    assert _posted(out) == [f"day={DAY}"]


def test_js_account_already_saw_today_in_a_fresh_browser_shows_nothing(tmp_path):
    """САМ бойовий випадок: браузер без позначки (дані сайту стерто), акаунт уже бачив."""
    out = _run_js(tmp_path, local="", account=DAY)
    assert not out["shown"]
    assert _posted(out) == []


def test_js_browser_saw_today_but_account_not_back_fills(tmp_path):
    for account in ("", PREV):
        out = _run_js(tmp_path, local=DAY, account=account)
        assert not out["shown"], account
        assert _posted(out) == [f"day={DAY}"], account


def test_js_yesterday_marks_show_again_today(tmp_path):
    """Новий робочий день — нове привітання; пізніша позначка з двох — вчорашня."""
    out = _run_js(tmp_path, local=PREV, account=PREV)
    assert out["shown"] and out["local"] == DAY
    assert _posted(out) == [f"day={DAY}"]
    out = _run_js(tmp_path, local="", account=PREV)
    assert out["shown"]


def test_js_whats_new_on_screen_neither_shows_nor_marks(tmp_path):
    out = _run_js(tmp_path, local="", account="", whats_new=True)
    assert not out["shown"] and out["local"] == ""
    assert _posted(out) == []


def test_js_background_tab_marks_only_when_revealed(tmp_path):
    """Чергу відкрили у фоновій вкладці: позначка — лише коли привітання справді видно."""
    out = _run_js(tmp_path, local="", account="", hidden=True, reveal=True)
    assert out["before"] == {"shown": False, "local": "", "posts": 0}
    assert out["shown"] and out["local"] == DAY
    assert _posted(out) == [f"day={DAY}"]
