"""Link-attachment parsing + download, with a fully mocked HTTP session (no real
network — the whitelist and Drive confirm-token dance are exercised offline)."""

import pytest

from app.link_attachments import (
    LinkAttachment,
    LinkDownloadError,
    download_link,
    extract_download_links,
)


# --- extract_download_links ----------------------------------------------------

def test_extracts_and_dedupes_drive_links():
    body = (
        "<https://drive.google.com/file/d/1LIyJrFNKnY7oFyMadR1W5mRgRpAW9ivl/view?usp=drive_web>\n"
        "repeated in forward chain:\n"
        "<https://drive.google.com/file/d/1LIyJrFNKnY7oFyMadR1W5mRgRpAW9ivl/view>\n"
        "<https://drive.google.com/open?id=104xWP_qkbzSMNXZFdf_LpI8IzSpz2anh>"
    )
    links = extract_download_links(body)
    assert [lnk.file_id for lnk in links] == [
        "1LIyJrFNKnY7oFyMadR1W5mRgRpAW9ivl",
        "104xWP_qkbzSMNXZFdf_LpI8IzSpz2anh",
    ]
    assert all(lnk.kind == "drive" for lnk in links)


def test_extracts_ukrnet_edisk_link():
    body = "Файл: https://dl.ukr.net/1234abcd/big.stl ось тут"
    links = extract_download_links(body)
    assert len(links) == 1 and links[0].kind == "ukrnet"
    assert links[0].url == "https://dl.ukr.net/1234abcd/big.stl"


def test_ignores_non_whitelisted_hosts():
    body = "http://evil.example.com/malware.exe and https://random.host/file.stl"
    assert extract_download_links(body) == []


def test_empty_body():
    assert extract_download_links("") == []
    assert extract_download_links(None) == []


# --- download_link (mocked session) --------------------------------------------

class _Resp:
    def __init__(self, url, headers=None, cookies=None, text="", chunks=None, status_code=200):
        self.url = url
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.text = text
        self._chunks = chunks or [b""]
        self.status_code = status_code

    @property
    def is_redirect(self):
        """Як у requests.Response: 3xx із заголовком Location."""
        location = self.headers.get("location") or self.headers.get("Location")
        return bool(location) and self.status_code in (301, 302, 303, 307, 308)

    def iter_content(self, _size):
        yield from self._chunks

    def close(self):
        pass


class _Session:
    """Returns queued responses in order, one per .get() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.cookies = {}

    def get(self, url, params=None, stream=False, timeout=None, allow_redirects=None):
        self.calls.append((url, params, allow_redirects))
        return self._responses.pop(0)


def test_direct_download_saves_with_content_disposition_name(tmp_path):
    resp = _Resp(
        "https://dl.ukr.net/x/big.stl",
        headers={"content-type": "application/octet-stream",
                 "content-disposition": 'attachment; filename="crown.stl"'},
        chunks=[b"solid ", b"mesh"],
    )
    link = LinkAttachment(kind="ukrnet", url="https://dl.ukr.net/x/big.stl", display="…")
    path = download_link(link, tmp_path, session=_Session([resp]))
    assert path.name == "crown.stl"
    assert path.read_bytes() == b"solid mesh"


def test_drive_download_handles_confirm_token(tmp_path):
    warn = _Resp(
        "https://drive.google.com/uc?export=download&id=ID",
        headers={"content-type": "text/html; charset=utf-8"},
        cookies={"download_warning_abc": "TOKEN42"},
        text="<html>virus scan…</html>",
    )
    real = _Resp(
        "https://drive.usercontent.google.com/download",
        headers={"content-type": "application/octet-stream",
                 "content-disposition": "attachment; filename=model.stl"},
        chunks=[b"STL-BYTES"],
    )
    link = LinkAttachment(kind="drive", file_id="ID",
                          url="https://drive.google.com/file/d/ID/view", display="…")
    session = _Session([warn, real])
    path = download_link(link, tmp_path, session=session)
    assert path.name == "model.stl"
    assert path.read_bytes() == b"STL-BYTES"
    # second call carried the confirm token
    assert session.calls[1][1]["confirm"] == "TOKEN42"


def test_drive_not_shared_raises(tmp_path):
    page = _Resp(
        "https://drive.google.com/uc?export=download&id=ID",
        headers={"content-type": "text/html"},
        text="<html>You need access</html>",  # no confirm token
    )
    link = LinkAttachment(kind="drive", file_id="ID",
                          url="https://drive.google.com/file/d/ID/view", display="…")
    with pytest.raises(LinkDownloadError):
        download_link(link, tmp_path, session=_Session([page]))


def test_dedup_skips_already_attached_name(tmp_path):
    resp = _Resp(
        "https://dl.ukr.net/x/crown.stl",
        headers={"content-disposition": 'attachment; filename="crown.stl"'},
        chunks=[b"x"],
    )
    link = LinkAttachment(kind="ukrnet", url="https://dl.ukr.net/x/crown.stl", display="…")
    path = download_link(link, tmp_path, session=_Session([resp]),
                         existing_names=frozenset({"crown.stl"}))
    assert path is None  # already have it


def test_non_whitelisted_link_object_refused(tmp_path):
    link = LinkAttachment(kind="ukrnet", url="https://evil.example.com/x.stl", display="…")
    with pytest.raises(LinkDownloadError):
        download_link(link, tmp_path, session=_Session([]))


# --- SSRF: whitelist enforced on EVERY redirect hop ----------------------------

def test_redirect_to_foreign_host_is_never_requested(tmp_path):
    """Кожен хоп перевіряється ДО запиту, не лише кінцевий URL.

    Регрес, який ловить тест: повернути `session.get(url, stream=True, ...)` з
    типовим allow_redirects=True — тоді requests сам сходив би на evil.example
    (запит уже стався), а `_host_allowed(response.url)` побачив би дозволений
    ПЕРШИЙ url і пропустив файл. Тут перевіряємо і що виняток є, і що на чужий
    хост не пішло жодного запиту.
    """
    # Тіло + ім'я файлу лежать просто в 302-відповіді: якби редиректи знову
    # віддали в requests, старий код побачив би дозволений response.url і
    # спокійно зберіг цей вміст на диск — тест це й ловить.
    hop = _Resp(
        "https://dl.ukr.net/x/big.stl",
        headers={"location": "https://evil.example.com/payload.bin",
                 "content-disposition": 'attachment; filename="payload.bin"'},
        status_code=302,
        chunks=[b"PWNED"],
    )
    leaked = _Resp(
        "https://evil.example.com/payload.bin",
        headers={"content-disposition": 'attachment; filename="payload.bin"'},
        chunks=[b"PWNED"],
    )
    session = _Session([hop, leaked])
    link = LinkAttachment(kind="ukrnet", url="https://dl.ukr.net/x/big.stl", display="…")

    with pytest.raises(LinkDownloadError):
        download_link(link, tmp_path, session=session)

    requested = [call[0] for call in session.calls]
    assert requested == ["https://dl.ukr.net/x/big.stl"]
    assert all("evil.example.com" not in url for url in requested)
    assert not list(tmp_path.iterdir())


def test_redirects_are_not_delegated_to_requests(tmp_path):
    """allow_redirects мусить бути вимкнений — інакше перевірка хоста
    відбувається вже після походу по ланцюжку."""
    resp = _Resp(
        "https://dl.ukr.net/x/crown.stl",
        headers={"content-disposition": 'attachment; filename="crown.stl"'},
        chunks=[b"x"],
    )
    session = _Session([resp])
    link = LinkAttachment(kind="ukrnet", url="https://dl.ukr.net/x/crown.stl", display="…")
    download_link(link, tmp_path, session=session)
    assert session.calls[0][2] is False


def test_allowed_redirect_hop_is_followed(tmp_path):
    """Дозволений редирект (Drive → drive.usercontent.google.com) досі працює —
    ручний прохід не мусить ламати нормальне завантаження."""
    hop = _Resp(
        "https://drive.google.com/uc?export=download",
        headers={"location": "https://drive.usercontent.google.com/download?id=ID"},
        status_code=302,
    )
    real = _Resp(
        "https://drive.usercontent.google.com/download?id=ID",
        headers={"content-type": "application/octet-stream",
                 "content-disposition": "attachment; filename=model.stl"},
        chunks=[b"STL"],
    )
    session = _Session([hop, real])
    link = LinkAttachment(kind="drive", file_id="ID",
                          url="https://drive.google.com/file/d/ID/view", display="…")
    path = download_link(link, tmp_path, session=session)
    assert path.read_bytes() == b"STL"
    assert session.calls[1][0] == "https://drive.usercontent.google.com/download?id=ID"


def test_relative_redirect_resolves_against_current_url(tmp_path):
    """Відносний Location не мусить давати порожній хост (і тим самим хибну
    відмову) — доводимо його до абсолютного відносно поточного URL."""
    hop = _Resp(
        "https://dl.ukr.net/x/big.stl",
        headers={"location": "/y/real.stl"},
        status_code=302,
    )
    real = _Resp(
        "https://dl.ukr.net/y/real.stl",
        headers={"content-disposition": 'attachment; filename="real.stl"'},
        chunks=[b"STL"],
    )
    session = _Session([hop, real])
    link = LinkAttachment(kind="ukrnet", url="https://dl.ukr.net/x/big.stl", display="…")
    path = download_link(link, tmp_path, session=session)
    assert path.name == "real.stl"
    assert session.calls[1][0] == "https://dl.ukr.net/y/real.stl"


def test_redirect_loop_stops_instead_of_hanging(tmp_path):
    """Петля редиректів у межах білого списку не мусить крутитись вічно."""
    loop = [
        _Resp("https://dl.ukr.net/x/big.stl",
              headers={"location": "https://dl.ukr.net/x/big.stl"}, status_code=302)
        for _ in range(20)
    ]
    session = _Session(loop)
    link = LinkAttachment(kind="ukrnet", url="https://dl.ukr.net/x/big.stl", display="…")
    with pytest.raises(LinkDownloadError):
        download_link(link, tmp_path, session=session)
    assert len(session.calls) <= 7


# ── Сторінка входу — не файл ───────────────────────────────────────────────
# Аудит 08.09.26. Гілка Drive перевіряла content-type, пряме скачування —
# ні. Сторінка входу обмінника зберігалась ЯК ФАЙЛ: отримувала рядок
# вкладення, лист ставав «готовий», а серверний гейт «не приймати лист із
# нескачаними файлами» вважав файл скачаним. Гейт обходився не зламом, а
# хибним успіхом — оператор брав у роботу лист, де замість коронки лежить HTML.


def test_direct_download_refuses_a_web_page(tmp_path):
    resp = _Resp(
        "https://dl.ukr.net/x/big.stl",
        headers={"content-type": "text/html; charset=utf-8"},
        chunks=["<!doctype html><title>Вхід</title>".encode("utf-8")],
    )
    link = LinkAttachment(kind="ukrnet", url="https://dl.ukr.net/x/big.stl", display="…")

    with pytest.raises(LinkDownloadError) as err:
        download_link(link, tmp_path, session=_Session([resp]))

    assert "сторінка" in str(err.value).lower()
    # І головне: на диску нічого не лишилось, тож рядок вкладення не зʼявиться.
    assert not list(tmp_path.iterdir())
