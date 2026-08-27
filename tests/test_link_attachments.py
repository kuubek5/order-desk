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
    def __init__(self, url, headers=None, cookies=None, text="", chunks=None):
        self.url = url
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.text = text
        self._chunks = chunks or [b""]

    def iter_content(self, _size):
        yield from self._chunks

    def close(self):
        pass


class _Session:
    """Returns queued responses in order, one per .get() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, stream=False, timeout=None):
        self.calls.append((url, params))
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
