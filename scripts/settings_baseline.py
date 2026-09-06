"""Знімок «що бачить користувач у налаштуваннях» — базова лінія редизайну меню.

Навіщо: під час переїзду меню в реєстр (`app/services/settings_nav.py`) легко
загубити пункт, форму або поле й не помітити — рейка все одно виглядає повною.
Скрипт піднімає СПРАВЖНІЙ застосунок на тимчасовій базі, логіниться адміном і
оператором, обходить сторінки налаштувань і виписує все, що можна натиснути:
посилання рейки, `data-sec` секцій, `action`/`hx-*` форм, `name`/`id` полів і
тон плити стану кожного розділу.

    python scripts/settings_baseline.py            # → design/settings-redesign/baseline_*.json
    python scripts/settings_baseline.py --out /tmp/after
    diff baseline_адмін.json /tmp/after/baseline_адмін.json

Порожній diff = «нічого не пропало». Фонові воркери НЕ стартують: TestClient
використовується без `with`, тобто без lifespan — жодної мережі, жодного IMAP.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ADMIN = ("baseline_admin", "b@seline-Adm1n", "адмін")
OPERATOR = ("baseline_operator", "b@seline-Op1r", "оператор")

# Сторінки, які обходимо для кожної ролі. Список фіксований НАВМИСНО: якщо
# брати його з реєстру, зникнення пункту з реєстру зникне і зі знімка — тобто
# перевірка перестане ловити рівно те, заради чого існує.
PAGES = (
    "/settings",
    "/account",
    "/journal",
    "/journal/sync",
    "/settings/materials",
    "/settings/feedback",
    "/diag/perf",
    "/license",
)

ACTION_ATTRS = ("action", "formaction", "hx-post", "hx-get", "hx-delete", "hx-put")


class Snapshot(HTMLParser):
    """Витягує з HTML усе клікабельне. Стек тегів потрібен, щоб зрозуміти,
    чи посилання лежить у рейці налаштувань і в якій секції стоїть плита."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rail_hrefs: list[str] = []
        self.sections: list[str] = []
        self.actions: set[str] = set()
        self.names: set[str] = set()
        self.ids: set[str] = set()
        self.slabs: dict[str, dict[str, str]] = {}
        self._stack: list[dict] = []
        self._pill: dict | None = None

    # ── допоміжне ───────────────────────────────────────────────
    def _in_rail(self) -> bool:
        return any(frame["rail"] for frame in self._stack)

    def _current_sec(self) -> str | None:
        for frame in reversed(self._stack):
            if frame["sec"]:
                return frame["sec"]
        return None

    def handle_starttag(self, tag, attrs):
        attr = {k: (v or "") for k, v in attrs}
        classes = attr.get("class", "").split()
        sec = attr.get("data-sec")
        frame = {
            "tag": tag,
            "rail": "rail-settings" in classes,
            "sec": sec if sec and "scon-sec" in classes else None,
        }

        if attr.get("href") and self._in_rail():
            self.rail_hrefs.append(attr["href"])
        if sec and "scon-sec" in classes:
            self.sections.append(sec)
        for key in ACTION_ATTRS:
            if attr.get(key):
                self.actions.add(f"{key}={attr[key]}")
        if attr.get("name"):
            self.names.add(attr["name"])
        if attr.get("id"):
            self.ids.add(attr["id"])

        tone = next((c for c in classes if c.startswith("stand-state-")), None)
        if tone is not None:
            current = self._current_sec()
            if current:
                self._pill = {"sec": current, "tone": tone, "text": ""}

        # void-елементи не кладемо в стек — вони не мають закривального тега
        if tag not in ("br", "hr", "img", "input", "meta", "link", "path", "circle",
                       "rect", "ellipse", "line", "polygon", "polyline", "use", "source"):
            self._stack.append(frame)

    def handle_endtag(self, tag):
        if self._pill is not None:
            pill = self._pill
            self.slabs[pill["sec"]] = {"tone": pill["tone"], "text": pill["text"].strip()}
            self._pill = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._pill is not None:
            self._pill["text"] += data

    def result(self) -> dict:
        return {
            "rail_hrefs": self.rail_hrefs,
            "sections": self.sections,
            "actions": sorted(self.actions),
            "names": sorted(self.names),
            "ids": sorted(self.ids),
            "slabs": self.slabs,
        }


def _prepare_env(tmp: Path) -> None:
    from cryptography.fernet import Fernet

    os.environ["DB_PATH"] = str(tmp / "baseline.db")
    os.environ.setdefault("DB_ENCRYPTION_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("SESSION_SECRET_KEY", "baseline-secret")
    os.environ["KUUBMILL_SCHEMA_MANAGED"] = "1"


def _seed_license(db) -> None:
    """Тимчасова ліцензія на цю машину — інакше гейт `license_gate` у web.py
    відповідає 303 на кожен URL і знімок виходить порожній.

    Ключ підписується ОДНОРАЗОВОЮ парою: публічний ключ у процесі підміняється
    так само, як це роблять tests/test_license.py. Справжня ліцензія не
    зачіпається — вона в іншій базі, а ця пара живе лише в памʼяті процесу.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from app import license as license_module
    from app.settings_store import set_setting

    private = Ed25519PrivateKey.generate()
    license_module._PUBLIC_KEY_BYTES = private.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    payload = {
        "machine_id": license_module.get_machine_id(),
        "customer": "baseline",
        "issued_at": "2026-01-01T00:00:00",
        "expires_at": "2099-01-01T00:00:00",
    }
    set_setting(db, "license_key", license_module.encode_license_key(payload, private))


def _seed_users() -> None:
    from app.auth import hash_password
    from app.db import Base, engine, get_session
    from app.models import User

    Base.metadata.create_all(engine)
    with get_session() as db:
        _seed_license(db)
        for username, password, role in (ADMIN, OPERATOR):
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    full_name=username,
                    role=role,
                )
            )
        db.commit()


def _client():
    # Клієнт спільний із тестами (tests/asgi_client.py) — одна реалізація на
    # знімок і на перевірки рендеру, інакше вони розійшлися б у дрібницях.
    from app.web import app
    from tests.asgi_client import MiniClient

    return MiniClient(app)


def capture(role_name: str, username: str, password: str) -> dict:
    client = _client()
    status, _, _ = client.login(username, password)
    if status not in (200, 302, 303):
        raise SystemExit(f"{role_name}: вхід не вдався ({status})")

    pages: dict[str, dict] = {}
    for path in PAGES:
        status, headers, text = client.get(path)
        entry: dict = {"status": status}
        if status == 200 and "text/html" in headers.get("content-type", ""):
            snap = Snapshot()
            snap.feed(text)
            entry.update(snap.result())
        elif status in (302, 303, 307):
            entry["location"] = headers.get("location", "")
        pages[path] = entry
    return {"role": role_name, "pages": pages}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(ROOT / "design" / "settings-redesign"),
        help="куди покласти baseline_<роль>.json",
    )
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ignore_cleanup_errors: sqlite тримає файл відкритим до виходу процесу,
    # і Windows не дає прибрати теку — це не помилка знімка.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        _prepare_env(Path(tmpdir))
        _seed_users()
        for username, password, role in (ADMIN, OPERATOR):
            data = capture(role, username, password)
            target = out / f"baseline_{role}.json"
            target.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="",
            )
            print(f"{role}: {target}")


if __name__ == "__main__":
    main()
