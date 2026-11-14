"""Памʼять акаунта оператора (власник 14.11.26): фільтр матеріалу пошти й
налаштування вигляду, які раніше жили лише в браузері."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from app.services import account_store
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

ROOT = Path(__file__).resolve().parents[1]


def test_js_and_server_sync_the_same_keys():
    """Перелік у storage.js і в сервісі — одна множина: розійдуться, і ключ або
    мовчки не збережеться (422), або не підтягнеться з акаунта."""
    js = (ROOT / "app/static/js/storage.js").read_text(encoding="utf-8")
    block = re.search(r"var SYNCED = \[(.*?)\];", js, re.S)
    assert block is not None
    assert re.findall(r'"([^"]+)"', block.group(1)) == list(account_store.SYNCED_KEYS)


def test_put_accepts_only_known_keys_and_clears():
    user = SimpleNamespace(ui_store="")
    assert account_store.put(user, "railCollapsed", "1")
    assert not account_store.put(user, "queueSeenIds", "[1,2]"), "«побачене» — не налаштування"
    assert not account_store.put(user, "queueColWidths", "x" * (account_store.MAX_VALUE_CHARS + 1))
    assert account_store.load(user) == {"railCollapsed": "1"}
    assert account_store.put(user, "railCollapsed", None)
    assert account_store.load(user) == {}
    assert account_store.load(SimpleNamespace(ui_store="{зіпсовано")) == {}


def _user(factory):
    from app.models import User

    with factory() as db:
        return db.query(User).filter_by(username=ADMIN[0]).one()


def test_ui_store_route_and_page_injection(app_db):  # noqa: F811
    app, factory = app_db
    anon = MiniClient(app)
    assert anon.post("/account/ui-store", {"key": "railCollapsed", "value": "1"})[0] == 401
    login_page = anon.get("/login")[2]
    assert "window.KM_ACCOUNT_STORE = null;" in login_page, "не увійшов — браузер нічого не чіпає"

    admin = MiniClient(app)
    admin.login(*ADMIN)
    assert admin.post("/account/ui-store", {"key": "railCollapsed", "value": "1"})[0] == 204
    assert admin.post("/account/ui-store", {"key": "queueColWidths", "value": '{"a":120}'})[0] == 204
    assert admin.post("/account/ui-store", {"key": "mailSyncAt", "value": "123"})[0] == 422
    assert json.loads(_user(factory).ui_store) == {"queueColWidths": '{"a":120}', "railCollapsed": "1"}

    page = admin.get("/account")[2]
    line = re.search(r"window\.KM_ACCOUNT_STORE = (.*?);</script>", page)
    assert line is not None and json.loads(line.group(1))["railCollapsed"] == "1"

    assert admin.post("/account/ui-store", {"key": "railCollapsed", "clear": "1"})[0] == 204
    assert "railCollapsed" not in json.loads(_user(factory).ui_store)


def test_mail_material_filter_is_remembered(app_db):  # noqa: F811
    """Прийняття листа веде на голий `/mail` — вибір «лише цирконій» мусить
    пережити це й новий вхід (власник 14.11.26)."""
    app, factory = app_db
    admin = MiniClient(app)
    admin.login(*ADMIN)
    assert admin.get("/mail?view=pending&fam=zr")[0] == 200
    assert _user(factory).mail_fam == "zr"

    # Полл несе вже обрізаний вибір — він памʼять не переписує.
    admin.get("/mail?view=pending&fam=pmma&partial=list")
    assert _user(factory).mail_fam == "zr"

    # Голий /mail (як після прийняття) і новий вхід — вибір той самий.
    again = MiniClient(app)
    again.login(*ADMIN)
    assert again.get("/mail")[0] == 200
    assert _user(factory).mail_fam == "zr"

    # «Усі» — власне слово, бо порожнє значення = «поля не було».
    assert again.get("/mail?view=pending&fam=all")[0] == 200
    assert _user(factory).mail_fam == ""


def test_remembered_fam_applies_only_where_chips_are_visible():
    from app.routers.mail import remembered_fam

    user = SimpleNamespace(mail_fam="zr")
    no_fam = SimpleNamespace(query_params={})
    assert remembered_fam(no_fam, None, user, "pending", "", None) == "zr"
    assert remembered_fam(no_fam, None, user, "auto", "", None) == "zr"
    assert remembered_fam(no_fam, None, user, "processed", "", None) == "", "прихований фільтр там, де ярликів немає"


def test_family_chip_links_never_drop_the_param():
    """Посилання ярлика без `fam` повертало б збережений вибір замість «Усі»."""
    html = (ROOT / "app/templates/mail_triage.html").read_text(encoding="utf-8")
    macro = html.split("{% macro famchips_nav() %}", 1)[1].split("{% endmacro %}", 1)[0]
    hrefs = re.findall(r'href="([^"]*)"', macro)
    assert hrefs and all("fam=" in h for h in hrefs), hrefs
