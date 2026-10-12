"""«Що нового» показується ОДИН раз на оператора й версію (власник 12.10.26).

Бойовий випадок: після одного оновлення (0.21.63 → 0.21.66) картка лізла до
операторів знову й знову — «уже бачив» жило лише в localStorage браузера, а
на робочих місцях цеху воно не доживало до наступного відкриття. Тепер
позначка ще й на акаунті (users.seen_whats_new), а браузер бере вищу з двох.

Що ламається тихо:
- роут приймає позначку, але сторінка її не несе — браузер і далі бачить
  «жодної» і показує картку (тому перевірка — справжнім рендером);
- позначка новіша за запущену версію назавжди сховала б наступні оновлення;
- стара вкладка з попередньою версією опускала б позначку назад;
- дія оператора, не адміна, і з мережевого ПК теж;
- рішення «показати чи ні» в app.js — справжній код у node, не переказ.
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
from pathlib import Path

import pytest

from app.__version__ import VERSION
from app.models import User
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
SEEN_ATTR = re.compile(r'id="whats-new-data" data-version="([^"]*)" data-seen="([^"]*)"')


def _login(app, who=OPERATOR, host: str = "127.0.0.1") -> MiniClient:
    client = MiniClient(app, client_host=host)
    status, _, _ = client.login(*who)
    assert status in (200, 302, 303), status
    return client


def _page_marks(client: MiniClient) -> tuple[str, str]:
    status, _, html = client.get("/account")
    assert status == 200, status
    match = SEEN_ATTR.search(html)
    assert match, "сторінка не несе #whats-new-data з data-seen"
    return match.group(1), match.group(2)


def _stored(session_factory, username: str):
    with session_factory() as db:
        return db.query(User).filter_by(username=username).one().seen_whats_new


def _bump(version: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


# ── Сервер: позначка на акаунті ───────────────────────────────────────────


def test_operator_mark_is_stored_and_comes_back_on_the_page(app_db):  # noqa: F811
    app, session_factory = app_db
    client = _login(app)
    assert _page_marks(client) == (VERSION, ""), "новий акаунт — ще не бачив жодної"

    # Як fetch із браузера: той самий origin, що й сторінка.
    status, _, _ = client.post(
        "/account/whats-new-seen", {"version": VERSION},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert status == 204
    assert _stored(session_factory, OPERATOR[0]) == VERSION
    # Та сама сесія, наступна сторінка — і, головне, новий браузер (нова сесія).
    assert _page_marks(client) == (VERSION, VERSION)
    assert _page_marks(_login(app)) == (VERSION, VERSION), "позначка мусить їхати за акаунтом"
    # Позначка одного оператора не протікає іншому.
    assert _page_marks(_login(app, ADMIN)) == (VERSION, "")


def test_operator_on_a_network_pc_can_mark(app_db):  # noqa: F811
    """«Робота з інших ПК»: оператор за мережевим ПК — не адмінська дія."""
    app, session_factory = app_db
    client = _login(app, host="192.168.1.50")
    status, _, _ = client.post("/account/whats-new-seen", {"version": VERSION})
    assert status == 204
    assert _stored(session_factory, OPERATOR[0]) == VERSION


@pytest.mark.parametrize("bad", ["", "abc", "0.21", "0.21.66.1", "0.21.x", "<b>1.2.3</b>"])
def test_garbage_is_refused(app_db, bad):  # noqa: F811
    app, session_factory = app_db
    client = _login(app)
    status, _, _ = client.post("/account/whats-new-seen", {"version": bad})
    assert status == 422
    assert _stored(session_factory, OPERATOR[0]) is None


def test_version_newer_than_the_running_one_is_refused(app_db):  # noqa: F811
    """Інакше «бачив 99.0.0» назавжди сховало б картку наступних оновлень."""
    app, session_factory = app_db
    client = _login(app)
    for future in (_bump(VERSION), "99.0.0"):
        status, _, _ = client.post("/account/whats-new-seen", {"version": future})
        assert status == 422, future
    assert _stored(session_factory, OPERATOR[0]) is None


def test_mark_only_goes_up(app_db):  # noqa: F811
    """Стара вкладка з попередньою версією не опускає позначку назад."""
    app, session_factory = app_db
    client = _login(app)
    assert client.post("/account/whats-new-seen", {"version": VERSION})[0] == 204
    assert client.post("/account/whats-new-seen", {"version": "0.1.0"})[0] == 204
    assert _stored(session_factory, OPERATOR[0]) == VERSION


def test_anonymous_is_refused(app_db):  # noqa: F811
    app, session_factory = app_db
    status, _, _ = MiniClient(app).post("/account/whats-new-seen", {"version": VERSION})
    assert status == 401
    assert _stored(session_factory, OPERATOR[0]) is None


def test_foreign_origin_is_refused(app_db):  # noqa: F811
    """CSRF-стіна (same_origin_guard) діє й тут: dev-порт не пише в прод."""
    app, session_factory = app_db
    client = _login(app)
    status, _, _ = client.post(
        "/account/whats-new-seen", {"version": VERSION},
        headers={"Origin": "http://127.0.0.1:8002"},
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
    assert "seen_whats_new" in _columns(db_path, "users")


def test_existing_database_gets_the_column_and_operators_stay_unseen(tmp_path, monkeypatch):
    """База з минулим (на 0086): міграція додає колонку, наявні акаунти — NULL,
    тобто рівно як «ще не бачив жодної» до цієї зміни."""
    from app.schema import ensure_schema

    monkeypatch.setenv("DB_PATH", "unused-by-this-test.db")  # ensure_schema його ставить
    db_path = tmp_path / "old.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0086_user_mail_dock"], cwd=ROOT,
        env=dict(os.environ, DB_PATH=str(db_path), PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr[-600:]
    assert "seen_whats_new" not in _columns(db_path, "users")
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

    assert "seen_whats_new" in _columns(db_path, "users")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT seen_whats_new FROM users WHERE username='old_op'").fetchone() == (None,)
    finally:
        conn.close()


# ── Браузер: справжній initWhatsNew з app.js у node ──────────────────────

HARNESS = r"""
const vm = require("vm");
const CASE = __CASE__;
const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, hidden: true, textContent: "", dataset: {}, focus() {}, addEventListener() {},
      classList: { add() {}, remove() {}, contains: () => false },
      querySelector: () => el("card"),
    };
  }
  return els[id];
}
el("whats-new-data").dataset = { version: CASE.current, seen: CASE.account };
el("whats-new-data").textContent = JSON.stringify(CASE.releases);
el("whats-new").hidden = true;
const store = {};
if (CASE.local) store.seenVersion = CASE.local;
const posts = [];
global.window = { KMStore: { get: (k) => (k in store ? store[k] : null), set: (k, v) => { store[k] = String(v); } } };
global.KMStore = global.window.KMStore;
global.fetch = (url, opts) => { posts.push({ url, body: opts.body }); return Promise.resolve({ ok: true }); };
global.document = {
  getElementById: el, documentElement: { dataset: {} }, activeElement: null,
  addEventListener() {}, removeEventListener() {},
};
vm.runInThisContext(__CODE__);
process.stdout.write(JSON.stringify({
  shown: !el("whats-new").hidden,
  ver: el("wnew-ver").textContent,
  step: el("wnew-step").textContent,
  title: el("wnew-title").textContent,
  local: store.seenVersion || "",
  posts,
  flag: global.document.documentElement.dataset.whatsNew || "",
}));
"""

RELEASES = [
    {"version": "0.21.67", "items": [{"title": "Нове 67"}]},
    {"version": "0.21.66", "items": [{"title": "Нове 66 а"}, {"title": "Нове 66 б"}]},
    {"version": "0.21.65", "items": [{"title": "Нове 65"}]},
]


def _init_whats_new_source() -> str:
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index("(function initWhatsNew() {")
    end = text.index("\n})();", start) + len("\n})();")
    return text[start:end]


def _run_js(tmp_path: Path, *, local: str, account: str, current: str = "0.21.67") -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не знайдено — перевірку JS пропущено")
    case = {"current": current, "account": account, "local": local, "releases": RELEASES}
    script = HARNESS.replace("__CASE__", json.dumps(case, ensure_ascii=False)).replace(
        "__CODE__", json.dumps(_init_whats_new_source())
    )
    path = tmp_path / "whats_new_harness.js"
    io.open(path, "w", encoding="utf-8", newline="\n").write(script)
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _posted(out: dict) -> list[str]:
    assert all(p["url"] == "/account/whats-new-seen" for p in out["posts"])
    return [p["body"] for p in out["posts"]]


def test_js_no_mark_anywhere_shows_only_the_current_version_and_marks_both(tmp_path):
    out = _run_js(tmp_path, local="", account="")
    assert out["shown"] and out["flag"] == "1"
    assert (out["ver"], out["title"], out["step"]) == ("0.21.67", "Нове 67", "")
    assert out["local"] == "0.21.67"
    assert _posted(out) == ["version=0.21.67"]


def test_js_account_mark_hides_the_card_in_a_fresh_browser(tmp_path):
    """САМ бойовий випадок: браузер без позначки (дані сайту стерто), акаунт уже бачив."""
    out = _run_js(tmp_path, local="", account="0.21.67")
    assert not out["shown"] and out["flag"] == ""
    assert _posted(out) == []


def test_js_browser_mark_alone_still_hides_and_is_copied_to_the_account(tmp_path):
    """Позначка з часів «лише браузер»: картку не повторюємо, акаунт дописуємо."""
    out = _run_js(tmp_path, local="0.21.67", account="")
    assert not out["shown"]
    assert _posted(out) == ["version=0.21.67"]
    out = _run_js(tmp_path, local="0.21.67", account="0.21.66")
    assert not out["shown"]
    assert _posted(out) == ["version=0.21.67"]


def test_js_after_an_update_shows_everything_newer_than_the_higher_mark(tmp_path):
    # Акаунт бачив 0.21.65 → показати 66 (два пункти) і 67, по одному.
    out = _run_js(tmp_path, local="", account="0.21.65")
    assert out["shown"] and out["step"] == "1 з 3" and out["title"] == "Нове 67"
    assert _posted(out) == ["version=0.21.67"]
    # Браузер бачив вище за акаунт (0.21.66) — пункти 66 вже не повторюються.
    out = _run_js(tmp_path, local="0.21.66", account="0.21.65")
    assert out["shown"] and out["step"] == "" and out["title"] == "Нове 67"
    # Акаунт вище за браузер — так само.
    out = _run_js(tmp_path, local="0.21.65", account="0.21.66")
    assert out["shown"] and out["step"] == ""


def test_js_account_ahead_of_the_running_version_shows_nothing(tmp_path):
    """Відкат на попередню версію: позначка вища за запущену — тиша, без запису."""
    out = _run_js(tmp_path, local="", account="0.21.67", current="0.21.66")
    assert not out["shown"]
    assert _posted(out) == []
