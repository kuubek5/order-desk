"""Найменший клієнт до ASGI-застосунку — без httpx і starlette.testclient.

`starlette.testclient` вимагає httpx, якого в цьому venv немає, а решта тестів
кличе роути напряму (fake `Request`), тому клієнт нікому й не був потрібен.
Але дві перевірки без справжнього рендеру не робляться: «плита стану має
потрібний клас у HTML» і «оператор не отримує форм, яких йому не можна».
Для них — оце: GET/POST, cookie сесії, сирий текст відповіді.

Lifespan НЕ запускається навмисно: інакше піднялися б фонові воркери (пошта,
синк, VNC), і тест пішов би в мережу.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode, urlsplit


class MiniClient:
    def __init__(self, app):
        self.app = app
        self.cookies: dict[str, str] = {}

    def get(self, path: str, headers: dict | None = None):
        return self._run("GET", path, None, headers)

    def post(self, path: str, data: dict | None = None, headers: dict | None = None):
        return self._run("POST", path, data or {}, headers)

    def login(self, username: str, password: str):
        return self.post("/login", {"username": username, "password": password})

    def _run(self, method: str, path: str, data: dict | None, extra: dict | None = None):
        return asyncio.run(self._call(method, path, data, extra))

    async def _call(self, method: str, path: str, data: dict | None, extra: dict | None = None):
        split = urlsplit(path)
        body = b""
        # client=127.0.0.1: частина роутів свідомо працює лише «за цим ПК»
        # (`loopback=True` у require_admin/require_settings_edit).
        headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8000")]
        for name, value in (extra or {}).items():
            headers.append((name.lower().encode(), str(value).encode()))
        if self.cookies:
            jar = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            headers.append((b"cookie", jar.encode()))
        if data is not None:
            body = urlencode(data).encode()
            headers.append((b"content-type", b"application/x-www-form-urlencoded"))
            headers.append((b"content-length", str(len(body)).encode()))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": split.path,
            "raw_path": split.path.encode(),
            "query_string": split.query.encode(),
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 51000),
            "server": ("127.0.0.1", 8000),
        }

        sent = False
        messages: list[dict] = []

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)

        status = 500
        raw_headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []
        for message in messages:
            if message["type"] == "http.response.start":
                status = message["status"]
                raw_headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        out: dict[str, str] = {}
        for key, value in raw_headers:
            name = key.decode().lower()
            if name == "set-cookie":
                cookie = value.decode().split(";", 1)[0]
                cname, _, cvalue = cookie.partition("=")
                if cvalue:
                    self.cookies[cname] = cvalue
                else:
                    self.cookies.pop(cname, None)
            else:
                out[name] = value.decode()
        return status, out, b"".join(chunks).decode("utf-8", "replace")


__all__ = ["MiniClient"]
