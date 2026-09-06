"""Палітра команд (Ctrl+K) і J/K у тріажі — аудит 05.09.26, крок 3.3.

Дві половини фічі перевіряються по-різному, бо ламаються по-різному.

**Сервер.** Прямі виклики роутів (той самий підхід, що в tests/test_search.py):
головне, що тут може тихо поїхати — гейт за роллю. Адмінський пункт, показаний
оператору, дає 403 після Enter, і застосунок читається як зламаний.

**Клієнт.** Однолітерна гаряча клавіша ламається рівно одним способом: «j» у
полі вводу перемикає рядок замість того, щоб надрукуватись. Це не питання
стилю коду, тому й перевірка не текстова — справжні `mail.js` і `palette.js`
виконуються в node зі скелетом DOM, і тест натискає клавішу.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import Order, User
from app.routers import palette as palette_mod

ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = ROOT / "app" / "static" / "js"


def _user(db: Session, role: str) -> User:
    user = User(username=role, password_hash="unused", full_name=role, role=role)
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None):
    return SimpleNamespace(session={} if user_id is None else {"user_id": user_id})


def _body(response) -> dict:
    return json.loads(response.body)


# ── перелік екранів ──────────────────────────────────────────────────────


def test_commands_require_a_session(db_session):
    response = palette_mod.palette_commands(request=_request(None), db=db_session)
    assert response.status_code == 401
    assert _body(response)["items"] == []


def test_operator_does_not_see_admin_screens(db_session):
    """Пункт у палітрі — це обіцянка, що Enter кудись відкриє. Адмінський екран
    в оператора має не з'являтись узагалі, а не давати 403 після натискання."""
    operator = _user(db_session, "оператор")
    hrefs = {c["href"] for c in _body(palette_mod.palette_commands(_request(operator.id), db_session))["items"]}
    assert "/journal/sync" not in hrefs
    assert "/feedback/inbox" not in hrefs
    # Робочі екрани при цьому на місці — гейт не має зрізати зайвого.
    assert {"/", "/mail", "/handout", "/shift", "/settings"} <= hrefs


def test_admin_sees_every_screen(db_session):
    admin = _user(db_session, "адмін")
    hrefs = {c["href"] for c in _body(palette_mod.palette_commands(_request(admin.id), db_session))["items"]}
    assert hrefs == {c["href"] for c in palette_mod.COMMANDS}


def test_commands_never_leak_the_admin_flag(db_session):
    """`admin` — серверна деталь. Якщо вона доїде до браузера, наступна правка
    неминуче спокуситься ховати пункт стилями, а це вже не гейт."""
    admin = _user(db_session, "адмін")
    for item in _body(palette_mod.palette_commands(_request(admin.id), db_session))["items"]:
        assert "admin" not in item


def test_every_command_points_at_a_real_route():
    """Опечатка в href дає 404 з палітри — а це саме той екран, по якому
    оператор судить, чи застосунок узагалі працює."""
    import app.web as web

    def walk(routes):
        # FastAPI не розкладає include_router у плоский список — той самий
        # обхід, що в tests/test_route_inventory.py.
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)
                continue
            path = getattr(route, "path", None)
            if path:
                yield path

    known = set(walk(web.app.routes))
    for command in palette_mod.COMMANDS:
        assert command["href"] in known, f"{command['label']}: немає роуту {command['href']}"


# ── швидкий пошук робіт ──────────────────────────────────────────────────


def _order(db: Session, **kwargs) -> Order:
    order = Order(source="lab", status="нове", **kwargs)
    db.add(order)
    db.commit()
    return order


def test_search_finds_by_work_order_and_sum3d(db_session):
    user = _user(db_session, "оператор")
    order = _order(db_session, work_order_no="24122", sum3d_id="12-01-45", client_name="Іванов")

    by_order = _body(palette_mod.palette_search(_request(user.id), q="2412", db=db_session))["items"]
    assert [i["href"] for i in by_order] == [f"/orders/{order.id}"]
    assert by_order[0]["label"] == "24122"
    assert "Іванов" in by_order[0]["sub"]

    by_sum3d = _body(palette_mod.palette_search(_request(user.id), q="01-45", db=db_session))["items"]
    assert [i["href"] for i in by_sum3d] == [f"/orders/{order.id}"]


def test_search_ignores_a_query_too_short_to_mean_anything(db_session):
    user = _user(db_session, "оператор")
    _order(db_session, work_order_no="24122")
    assert _body(palette_mod.palette_search(_request(user.id), q="2", db=db_session))["items"] == []


def test_search_treats_underscore_as_a_letter(db_session):
    """`_` у LIKE означає «будь-який символ». Без екранування запит «12_01»
    знаходив би і «12-01», і «12x01» — тихо неправильний результат, який
    оператор не має шансу помітити."""
    user = _user(db_session, "оператор")
    _order(db_session, sum3d_id="12-01-45")
    hit = _order(db_session, sum3d_id="12_01_45")
    found = _body(palette_mod.palette_search(_request(user.id), q="12_01", db=db_session))["items"]
    assert [i["href"] for i in found] == [f"/orders/{hit.id}"]


def test_search_requires_a_session(db_session):
    response = palette_mod.palette_search(request=_request(None), q="24122", db=db_session)
    assert response.status_code == 401


# ── J/K у тріажі: справжній JS у node ────────────────────────────────────

# Скелет DOM рівно на те, що чіпають наші скрипти під час завантаження й на
# keydown. Повний jsdom сюди тягнути ні до чого: перевіряємо ОДНЕ рішення —
# чи долітає літера до рядка списку, коли фокус у полі вводу.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const listeners = {};
function on(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }

function makeRow(i) {
  return {
    tagName: "DIV", i, focused: false, clicked: false, isContentEditable: false,
    classList: { contains: (c) => c === "active" && rowsActive === i },
    focus() { this.focused = true; }, click() { this.clicked = true; },
    scrollIntoView() {}, closest: () => null, getAttribute: () => null,
  };
}
const rows = [makeRow(0), makeRow(1), makeRow(2)];
let rowsActive = ACTIVE_INDEX;

global.window = { setTimeout, clearTimeout };
global.document = {
  addEventListener: on,
  body: { addEventListener: on, appendChild() {} },
  createElement: () => ({ style: {}, classList: { add() {}, remove() {}, contains: () => false },
                          setAttribute() {}, querySelector: () => null, addEventListener() {} }),
  querySelector: (sel) => {
    if (sel === ".mailrow.active") return rowsActive < 0 ? null : rows[rowsActive];
    return null;
  },
  querySelectorAll: (sel) => (sel === ".mailrow" ? rows : []),
  getElementById: () => null,
  activeElement: null,
};
global.Node = { TEXT_NODE: 3 };
global.KMStore = { get: () => null, set() {}, remove() {} };

for (const file of FILES) {
  vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
}

const target = TARGET;
const event = {
  key: KEY, code: CODE, target,
  ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
  prevented: false, preventDefault() { this.prevented = true; },
};
(listeners.keydown || []).forEach((fn) => fn(event));

console.log(JSON.stringify({
  prevented: event.prevented,
  clicked: rows.map((r) => r.clicked),
  focused: rows.map((r) => r.focused),
  isTypingSaysYes: window.KMKeys.isTyping(target),
}));
"""

TARGETS = {
    "textarea": '{ tagName: "TEXTAREA", isContentEditable: false, closest: () => null, getAttribute: () => null }',
    "input": '{ tagName: "INPUT", isContentEditable: false, closest: () => null, getAttribute: () => null }',
    "contenteditable": '{ tagName: "DIV", isContentEditable: true, closest: () => null, getAttribute: () => null }',
    "role-textbox": '{ tagName: "DIV", isContentEditable: false, closest: () => null, getAttribute: (n) => (n === "role" ? "textbox" : null) }',
    "page": '{ tagName: "BODY", isContentEditable: false, closest: () => null, getAttribute: () => null }',
}


def _run(tmp_path: Path, target: str, key: str = "j", code: str = "KeyJ", active: int = 0) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не знайдено — перевірку JS пропущено")
    files = [str(STATIC_JS / "palette.js"), str(STATIC_JS / "mail.js")]
    script = (
        HARNESS.replace("FILES", json.dumps(files))
        .replace("TARGET", TARGETS[target])
        .replace("ACTIVE_INDEX", str(active))
        .replace("KEY", json.dumps(key))
        .replace("CODE", json.dumps(code))
    )
    path = tmp_path / "harness.js"
    io.open(path, "w", encoding="utf-8", newline="\n").write(script)
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("target", ["textarea", "input", "contenteditable", "role-textbox"])
def test_j_stays_a_letter_while_the_operator_is_typing(tmp_path, target):
    """ГОЛОВНИЙ тест фічі. «j» у коментарі, в імені клієнта чи в будь-якому
    полі мусить надрукуватись, а не перемкнути лист. Перевіряємо і те, що
    подію не з'їли (preventDefault), і те, що жоден рядок не смикнувся."""
    out = _run(tmp_path, target)
    assert out["isTypingSaysYes"] is True
    assert out["prevented"] is False
    assert out["clicked"] == [False, False, False]
    assert out["focused"] == [False, False, False]


def test_j_moves_to_the_next_letter_outside_a_field(tmp_path):
    out = _run(tmp_path, "page", key="j", code="KeyJ", active=0)
    assert out["prevented"] is True
    assert out["clicked"] == [False, True, False]
    assert out["focused"] == [False, True, False]


def test_k_moves_back(tmp_path):
    out = _run(tmp_path, "page", key="k", code="KeyK", active=2)
    assert out["clicked"] == [False, True, False]


def test_ukrainian_layout_works_by_key_position(tmp_path):
    """Фізична J в українській розкладці дає «о». Дивимось на event.code —
    інакше фіча мовчки не працює саме в тій розкладці, у якій тут працюють."""
    out = _run(tmp_path, "page", key="о", code="KeyJ", active=0)
    assert out["clicked"] == [False, True, False]


def test_j_without_a_selected_letter_starts_from_the_top(tmp_path):
    out = _run(tmp_path, "page", key="j", code="KeyJ", active=-1)
    assert out["clicked"] == [True, False, False]
