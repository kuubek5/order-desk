"""Origin-стіна для мутацій (ревʼю 07.09.26, core/security HIGH-2).

`SameSite=Strict` відсікає крос-сайтові POST-и, але «site» ігнорує порт:
dev-сервер на 8002 і прод на 8000 — один site, і кука оператора летить у
POST з будь-якої локальної сторінки. Тому мутація мусить приходити з НАШОГО
origin (host:port). Запити без Origin/Sec-Fetch-Site (curl, тести) проходять.
"""

from __future__ import annotations

from app import web
from tests.asgi_client import MiniClient


def _post(headers: dict | None = None):
    client = MiniClient(web.app)
    status, resp_headers, _ = client.post(
        "/login", {"username": "nobody", "password": "x"}, headers=headers
    )
    return status, dict(resp_headers)


def test_foreign_origin_is_refused_even_on_the_same_host():
    status, _ = _post({"origin": "http://127.0.0.1:8002"})
    assert status == 403


def test_null_origin_is_refused():
    status, _ = _post({"origin": "null"})
    assert status == 403


def test_same_site_other_port_without_origin_is_refused():
    status, _ = _post({"sec-fetch-site": "same-site"})
    assert status == 403


def test_own_origin_passes():
    status, _ = _post({"origin": "http://127.0.0.1:8000"})
    assert status != 403


def test_no_origin_header_passes():
    status, _ = _post()
    assert status != 403


def test_get_is_never_blocked_and_frames_stay_same_origin():
    client = MiniClient(web.app)
    status, headers, _ = client.get("/login", headers={"origin": "http://evil.example"})
    assert status != 403
    assert dict(headers).get("x-frame-options") == "SAMEORIGIN"
