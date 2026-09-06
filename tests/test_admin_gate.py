"""Сторож адмінського гейта (аудит 05.09.26, крок 2.7).

Перевірок «це адмін» було чотири незалежні копії, і вони встигли розійтись:
`/diag/*` пускав адміна по мережі, а пароль печі — ні. Тепер правило одне
(`deps.require_admin`), а цей тест не дає новому роуту в `/settings` чи `/diag`
тихо зʼявитись без гейта: список винятків нижче доводиться поповнювати руками,
і саме це змушує подумати, чи виняток справді потрібен.

Перевірка статична (по AST), а не через запити: роутів 50+, і кожен має свій
набір форм-параметрів — підняти їх усі коштувало б дорожче, ніж дає тест.
"""

import ast
import io
from pathlib import Path

import pytest

from app.routers import deps

ROOT = Path(__file__).resolve().parents[1]
# Налаштування живуть у пакеті з тематичних модулів (аудит 05.09.26, крок 2.9).
# Список збираємо глобом, а не руками: інакше новий модуль пакета проскочив би
# повз сторожа мовчки — саме те, від чого цей тест і поставлено.
GATED_MODULES = sorted(
    path.relative_to(ROOT).as_posix()
    for path in (ROOT / "app" / "routers" / "settings").glob("*.py")
) + ["app/routers/diag.py"]

# Роути, які СВІДОМО доступні не лише адміну. Кожен — з причиною.
OPERATOR_ALLOWED = {
    # Сторінка налаштувань і збереження: частина полів operator-editable
    # (OPERATOR_EDITABLE_KEYS) — шляхи до тек оператор міняє сам, коли ПК
    # інший. Секрети в цьому ж роуті гейтяться окремо, всередині.
    "GET /settings",
    "POST /settings",
    # Живий пробник шляху: read-probe для всіх, а write-probe (створити й
    # видалити файл у вказаній теці) — лише адміну, всередині
    # check_path_status(write_probe=...). Див. аудит, безпека M-1.
    "POST /settings/check-path",
    # Сповіщення — налаштування ОПЕРАТОРА під себе, не машини.
    "GET /api/notify-state",
    "POST /settings/notifications",
    # Клієнтські числа профайлера шле сама сторінка будь-якого оператора:
    # саме його затримки й цікаві. Гейт тут — «увійшов», не «адмін».
    "POST /diag/perf/client",
}

GATE_MARKERS = ("require_settings_admin", "require_admin", 'role != "адмін"')


def _routes_without_gate(module: str) -> list[str]:
    source = io.open(ROOT / module, encoding="utf-8").read()
    tree = ast.parse(source)
    missing: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        paths = [
            (decorator.func.attr.upper(), decorator.args[0].value)
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post", "put", "delete", "patch"}
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
        ]
        if not paths:
            continue
        body = ast.get_source_segment(source, node) or ""
        if any(marker in body for marker in GATE_MARKERS):
            continue
        missing.extend(f"{method} {path}" for method, path in paths)
    return missing


@pytest.mark.parametrize("module", GATED_MODULES)
def test_every_settings_and_diag_route_is_gated(module):
    ungated = set(_routes_without_gate(module)) - OPERATOR_ALLOWED
    assert not ungated, (
        f"{module}: роути без адмінського гейта: {sorted(ungated)}.\n"
        "Якщо роут справді має бути доступний оператору — додай його в "
        "OPERATOR_ALLOWED з причиною; якщо ні — постав require_settings_admin."
    )


def test_the_allowlist_has_no_stale_entries():
    """Виняток, який більше нікому не потрібен, мовчки послаблює сторожа."""
    live = set()
    for module in GATED_MODULES:
        live.update(_routes_without_gate(module))
    stale = OPERATOR_ALLOWED - live
    assert not stale, f"У списку винятків лишились неіснуючі роути: {sorted(stale)}"


class TestRequireAdmin:
    """Сам гейт: три відмови й один прохід."""

    def _request(self, user_id, host="127.0.0.1"):
        from types import SimpleNamespace

        return SimpleNamespace(
            session={} if user_id is None else {"user_id": user_id},
            client=SimpleNamespace(host=host),
        )

    def _db_with(self, role):
        from types import SimpleNamespace

        user = SimpleNamespace(id=1, role=role, is_active=True)
        return SimpleNamespace(get=lambda model, uid: user if uid == 1 else None)

    def test_anonymous_gets_401(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setattr(deps, "get_current_user", lambda request, db: None)
        with pytest.raises(HTTPException) as exc:
            deps.require_admin(self._request(None), None)
        assert exc.value.status_code == 401

    def test_operator_gets_403(self, monkeypatch):
        from fastapi import HTTPException
        from types import SimpleNamespace

        monkeypatch.setattr(
            deps, "get_current_user",
            lambda request, db: SimpleNamespace(role="оператор"),
        )
        with pytest.raises(HTTPException) as exc:
            deps.require_admin(self._request(1), None)
        assert exc.value.status_code == 403
        assert "адміністратора" in exc.value.detail

    def test_admin_over_the_network_is_refused_when_loopback_required(self, monkeypatch):
        from fastapi import HTTPException
        from types import SimpleNamespace

        monkeypatch.setattr(
            deps, "get_current_user",
            lambda request, db: SimpleNamespace(role="адмін"),
        )
        with pytest.raises(HTTPException) as exc:
            deps.require_admin(self._request(1, host="192.168.88.20"), None)
        assert exc.value.status_code == 403
        assert "цьому комп" in exc.value.detail

    def test_admin_over_the_network_passes_when_loopback_is_waived(self, monkeypatch):
        from types import SimpleNamespace

        admin = SimpleNamespace(role="адмін")
        monkeypatch.setattr(deps, "get_current_user", lambda request, db: admin)
        assert deps.require_admin(
            self._request(1, host="192.168.88.20"), None, loopback=False
        ) is admin

    def test_admin_on_this_machine_passes(self, monkeypatch):
        from types import SimpleNamespace

        admin = SimpleNamespace(role="адмін")
        monkeypatch.setattr(deps, "get_current_user", lambda request, db: admin)
        assert deps.require_admin(self._request(1), None) is admin
