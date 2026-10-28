"""Сторожі «далі не гірше» (28.10.26) — три вади, що вже ламали цех.

Код застосунку тут не міняється: тест лише рахує й звіряє зі знімком
tests/code_guards.txt. Нове місце — червоний; прибрали місце — теж червоний,
щоб знімок не відставав (тоді просто оновити його).

1. **Копії умови «яке джерело роботи»** (`order.source == "email"`,
   `Order.source in (...)`). 30.09.26 пошті дали рядок у таблиці й оновили
   одну умову, а сім копій мовчки пропускали пошту — ранкова видача стала
   (CLAUDE.md §14, «Чи має робота рядок у таблиці»). Перш ніж додати нову
   копію, пошукай готовий предикат: `sheet_writeback.order_writes_to_sheet`,
   `order_has_client_fill_row`, `queue.is_hold_order`, `queue_filters`.
   Новий предикат — свідомо оновити знімок.
2. **Тихий широкий `except`** — `except Exception` / `BaseException` / голий
   `except:`, який не пише в лог і не кидає далі. Так кнопки «Перевірити»
   дев'ять днів віддавали 500 без сліду (CLAUDE.md §14, «Зниклий шаблон»).
   Новий такий блок: лог через `log.warning`/`log_throttle`, або вузький тип
   винятку, або — якщо мовчання справді задумане — оновити знімок.
3. **Підміна в тесті, що нічого не підміняє** — `monkeypatch.setattr(модуль,
   "X")`, де модуль X лише імпортує й сам не вживає (код переїхав). Тест
   зелений і порожній; один такий
   пустив IMAP-тест у справжню мережу (CLAUDE.md §14, «Архітектура»).
   Тут допуску немає: підміняти там, де X визначено або вживається.

Оновити знімок (з кореня репо), переконавшись, що зміна навмисна:
    .venv/Scripts/python.exe -m tests.test_code_guards
"""

from __future__ import annotations

import ast
import warnings
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
TESTS = ROOT / "tests"
BASELINE = TESTS / "code_guards.txt"

LOG_CALLS = {"exception", "error", "warning", "warn", "info", "debug", "critical", "log"}


def _py_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    # Чужі «\d» у докстрінгах дають SyntaxWarning на кожен прогін — не наше.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ── 1. Умова джерела роботи ─────────────────────────────────────────────────


def count_source_compares(tree: ast.AST) -> int:
    """`<щось>.source ==/!=/in/not in ...` — коментарі й локальна змінна
    `source` (параметр фільтра) не рахуються."""
    total = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "source"
            and any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops)
        ):
            total += 1
    return total


# ── 2. Тихий широкий except ─────────────────────────────────────────────────


def _is_broad(exc_type: ast.expr | None) -> bool:
    if exc_type is None:
        return True
    if isinstance(exc_type, ast.Name):
        return exc_type.id in ("Exception", "BaseException")
    if isinstance(exc_type, ast.Tuple):
        return any(_is_broad(e) for e in exc_type.elts)
    return False


def _speaks(body: list[ast.stmt]) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                low = name.lower()
                if name in LOG_CALLS or "log" in low or "warn" in low:
                    return True
    return False


def count_silent_excepts(tree: ast.AST) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and _is_broad(node.type) and not _speaks(node.body)
    )


# ── Знімок ──────────────────────────────────────────────────────────────────

KINDS = {"source": count_source_compares, "except": count_silent_excepts}


def current_counts() -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {kind: Counter() for kind in KINDS}
    for path in _py_files(APP):
        tree = _parse(path)
        rel = path.relative_to(ROOT).as_posix()
        for kind, counter in KINDS.items():
            n = counter(tree)
            if n:
                out[kind][rel] = n
    return out


def read_baseline() -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {kind: Counter() for kind in KINDS}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind, rel, n = line.split()
        out[kind][rel] = int(n)
    return out


def write_baseline() -> None:
    counts = current_counts()
    lines = ["# Знімок сторожів tests/test_code_guards.py: вид, файл, кількість."]
    for kind in KINDS:
        lines += [f"{kind} {rel} {n}" for rel, n in sorted(counts[kind].items())]
    BASELINE.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


HINTS = {
    "source": "нова копія умови джерела роботи — візьми готовий предикат (див. докстрінг)",
    "except": "новий тихий `except Exception` — додай лог або звузь тип винятку",
}


def _diff(kind: str) -> tuple[list[str], list[str]]:
    now, was = current_counts()[kind], read_baseline()[kind]
    worse = [f"{rel}: {was.get(rel, 0)} → {n}" for rel, n in sorted(now.items()) if n > was.get(rel, 0)]
    better = [f"{rel}: {n} → {now.get(rel, 0)}" for rel, n in sorted(was.items()) if now.get(rel, 0) < n]
    return worse, better


def _check(kind: str) -> None:
    worse, better = _diff(kind)
    assert not worse, f"{HINTS[kind]}:\n  " + "\n  ".join(worse)
    assert not better, (
        "стало менше — добре; оновити знімок: .venv/Scripts/python.exe -m tests.test_code_guards\n  "
        + "\n  ".join(better)
    )


def test_no_new_copies_of_the_source_condition():
    _check("source")


def test_no_new_silent_broad_excepts():
    _check("except")


# ── 3. Підміна, що нічого не підміняє ───────────────────────────────────────


class _Module:
    def __init__(self, tree: ast.Module):
        self.defined: set[str] = set()
        self.imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.imported.add((alias.asname or alias.name).split(".")[0])
        for node in tree.body:
            self._collect_defs(node)
        self.used = {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

    def _collect_defs(self, node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                self.defined |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            # Модульні визначення під if/try (запасні імпорти, платформа).
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    self._collect_defs(child)
            for handler in getattr(node, "handlers", []):
                for child in handler.body:
                    self._collect_defs(child)

    def problem(self, name: str) -> str | None:
        if name in self.defined:
            return None
        if name in self.imported:
            return None if name in self.used else "лише імпортується, у модулі не вживається"
        return "у модулі такого імені немає"


_modules: dict[str, _Module | None] = {}


def _module(dotted: str) -> _Module | None:
    if dotted not in _modules:
        base = ROOT.joinpath(*dotted.split("."))
        path = base.with_suffix(".py")
        if not path.exists():
            path = base / "__init__.py"
        _modules[dotted] = _Module(_parse(path)) if path.exists() else None
    return _modules[dotted]


def _app_aliases(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app") and alias.asname:
                    out[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "app":
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if _module(dotted) is not None:
                    out[alias.asname or alias.name] = dotted
    return out



def find_noop_patches(tree: ast.Module, where: str) -> list[str]:
    aliases = _app_aliases(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setattr"):
            continue
        args = node.args
        if (
            len(args) >= 2
            and isinstance(args[0], ast.Name)
            and args[0].id in aliases
            and isinstance(args[1], ast.Constant)
            and isinstance(args[1].value, str)
        ):
            dotted, name = aliases[args[0].id], args[1].value
        elif args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str) and args[0].value.startswith("app."):
            dotted, _, name = args[0].value.rpartition(".")
        else:
            continue
        module = _module(dotted)
        if module is None:
            continue
        # Імені немає взагалі: без raising=False monkeypatch упаде сам, а з
        # ним це свідоме ДОДАВАННЯ атрибута (conftest: `mail_auto_backfill.
        # recorded`), не підміна — тут не судимо.
        if name in module.imported and module.problem(name):
            found.append(f"{where}:{node.lineno} {dotted}.{name} — {module.problem(name)}")
    return found


def test_no_patch_that_patches_nothing():
    found: list[str] = []
    for path in _py_files(TESTS):
        found += find_noop_patches(_parse(path), path.relative_to(ROOT).as_posix())
    assert not found, (
        "підміна нічого не змінює — підміняй там, де ім'я визначено або вживається:\n  "
        + "\n  ".join(found)
    )


# ── Самоперевірка: детектори не мертві ──────────────────────────────────────


def test_detectors_catch_what_they_should():
    code = ast.parse(
        "if order.source == 'email': pass\n"
        "q = select(Order).where(Order.source != 'lab')\n"
        "if source == 'lab': pass\n"  # локальна змінна — не рахується
        "try:\n    x()\nexcept Exception:\n    pass\n"
        "try:\n    x()\nexcept (ValueError, Exception):\n    return None\n"
        "try:\n    x()\nexcept:\n    y = 1\n"
        "try:\n    x()\nexcept Exception:\n    log.warning('x')\n"
        "try:\n    x()\nexcept Exception:\n    raise\n"
        "try:\n    x()\nexcept ValueError:\n    pass\n"
    )
    assert count_source_compares(code) == 2
    assert count_silent_excepts(code) == 3


def test_noop_patch_detector(monkeypatch):
    # Справжній модуль: ім'я, яке web визначає сам, і свідоме додавання.
    web = _module("app.web")
    assert web is not None and "app" in web.defined
    probe = ast.parse(
        "import app.web as web\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(web, 'app', None)\n"
        "    monkeypatch.setattr(web, 'no_such_name_xyz', None, raising=False)\n"
    )
    assert find_noop_patches(probe, "probe.py") == []

    # Код переїхав: модуль лише імпортує ім'я — обидві форми підміни ловляться.
    moved = _Module(ast.parse("from app.services.x import fetch\n__all__ = ['fetch']\n"))
    used = _Module(ast.parse("from app.services.x import fetch\ndef go():\n    return fetch()\n"))
    monkeypatch.setitem(_modules, "app.moved", moved)
    monkeypatch.setitem(_modules, "app.used", used)
    probe = ast.parse(
        "from app import moved, used\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(moved, 'fetch', None)\n"
        "    monkeypatch.setattr('app.moved.fetch', None)\n"
        "    monkeypatch.setattr(used, 'fetch', None)\n"
    )
    assert find_noop_patches(probe, "probe.py") == [
        "probe.py:3 app.moved.fetch — лише імпортується, у модулі не вживається",
        "probe.py:4 app.moved.fetch — лише імпортується, у модулі не вживається",
    ]


if __name__ == "__main__":
    write_baseline()
    print(f"Знімок оновлено: {BASELINE.relative_to(ROOT)}")
