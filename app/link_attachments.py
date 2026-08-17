"""Download files that arrive as SHARE LINKS in an email body instead of MIME
attachments — Gmail parks anything over ~25 MB in Google Drive and inserts a
link, and ukr.net does the same with its eDisk. IMAP only carries the small
MIME parts, so those big STL/CAD files would otherwise be invisible to the lab.

Security posture (this is the server fetching a URL taken from email content):
  * Only a strict host whitelist is ever fetched — Google Drive and ukr.net
    eDisk. Any other link in the body is ignored, so a malicious email can't
    turn this into an arbitrary-URL fetcher (SSRF).
  * Operator-initiated only (a button in triage), never automatic.
  * A hard size cap and timeout bound each download.
  * Files land in the email's own mail-spool folder and then flow through the
    normal accept path — no new export code path.
"""

from dataclasses import dataclass
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote

import requests

from app.mail_reader import safe_attachment_filename, unique_destination

# Hosts we are willing to fetch from. Google Drive redirects downloads to
# drive.usercontent.google.com, so that must be allowed for the final hop.
_ALLOWED_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
}
_ALLOWED_SUFFIXES = (".ukr.net",)  # dl.ukr.net and any eDisk subdomain

_DRIVE_ID_RE = re.compile(
    r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^\s\"'<>]*&)?id=)([A-Za-z0-9_-]{20,})"
)
_UKR_LINK_RE = re.compile(r"https?://(?:dl|edisk)\.ukr\.net/[^\s\"'<>\)]+")

_MAX_BYTES = 300 * 1024 * 1024  # 300 MB — comfortably above real STL/CAD files
_CHUNK = 65536


class LinkDownloadError(Exception):
    """A link could not be downloaded (not shared, too large, host refused…)."""


@dataclass
class LinkAttachment:
    kind: str  # "drive" | "ukrnet"
    url: str  # canonical link to show the operator
    display: str
    file_id: str | None = None  # Google Drive file id, when kind == "drive"


def _host_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in _ALLOWED_HOSTS or any(host.endswith(s) for s in _ALLOWED_SUFFIXES)


def extract_download_links(text: str | None) -> list[LinkAttachment]:
    """Pull whitelisted share links out of an email body, deduped. A forwarded
    letter repeats each link several times — we keep one per Drive file id /
    ukr.net URL."""
    if not text:
        return []
    links: list[LinkAttachment] = []
    seen: set[tuple[str, str]] = set()

    for match in _DRIVE_ID_RE.finditer(text):
        file_id = match.group(1)
        key = ("drive", file_id)
        if key in seen:
            continue
        seen.add(key)
        links.append(
            LinkAttachment(
                kind="drive",
                file_id=file_id,
                url=f"https://drive.google.com/file/d/{file_id}/view",
                display=f"Google Drive · {file_id[:12]}…",
            )
        )

    for match in _UKR_LINK_RE.finditer(text):
        url = match.group(0)
        key = ("ukrnet", url)
        if key in seen:
            continue
        seen.add(key)
        links.append(LinkAttachment(kind="ukrnet", url=url, display=url))

    return links


def _filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    # RFC 5987: filename*=UTF-8''percent%20encoded
    star = re.search(r"filename\*\s*=\s*[^']*''([^;\r\n]+)", header, re.IGNORECASE)
    if star:
        return unquote(star.group(1)).strip().strip('"')
    plain = re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', header, re.IGNORECASE)
    if plain:
        return plain.group(1).strip()
    return None


def _confirm_token(response: requests.Response) -> str | None:
    """Google Drive shows a virus-scan interstitial for large files; the real
    download needs a confirm token, carried either in a cookie or the HTML."""
    for name, value in response.cookies.items():
        if name.startswith("download_warning"):
            return value
    match = re.search(r"confirm=([0-9A-Za-z_-]+)", response.text)
    return match.group(1) if match else None


def _stream_to_file(response: requests.Response, dest_dir: Path, filename: str) -> Path:
    dest = unique_destination(dest_dir, filename)
    total = 0
    try:
        with open(dest, "wb") as fh:
            for chunk in response.iter_content(_CHUNK):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise LinkDownloadError("файл завеликий (понад ліміт)")
                fh.write(chunk)
    except LinkDownloadError:
        dest.unlink(missing_ok=True)
        raise
    if total == 0:
        dest.unlink(missing_ok=True)
        raise LinkDownloadError("порожня відповідь (файл недоступний)")
    return dest


def _finish(
    response: requests.Response, dest_dir: Path, filename: str, existing_names: frozenset[str]
) -> Path | None:
    """Skip (return None) when a file with the same name is already attached —
    a repeat click on "download links" must not pile up duplicates. Otherwise
    stream it to disk."""
    safe = safe_attachment_filename(filename, 1, "")
    if safe in existing_names:
        response.close()
        return None
    return _stream_to_file(response, dest_dir, safe)


def _download_drive(
    file_id: str, dest_dir: Path, session: requests.Session, timeout: int,
    existing_names: frozenset[str],
) -> Path | None:
    base = "https://drive.google.com/uc?export=download"
    response = session.get(base, params={"id": file_id}, stream=True, timeout=timeout)
    if not _host_allowed(response.url):
        raise LinkDownloadError("недозволений хост у перенаправленні")
    if "text/html" in (response.headers.get("content-type") or "").lower():
        token = _confirm_token(response)
        response.close()
        if not token:
            raise LinkDownloadError("файл не розшарено «всім за посиланням» або недоступний")
        response = session.get(
            base, params={"id": file_id, "confirm": token}, stream=True, timeout=timeout
        )
        if not _host_allowed(response.url):
            raise LinkDownloadError("недозволений хост у перенаправленні")
        if "text/html" in (response.headers.get("content-type") or "").lower():
            raise LinkDownloadError("Google повернув сторінку, не файл (доступ закритий?)")
    filename = _filename_from_content_disposition(
        response.headers.get("content-disposition")
    ) or f"drive_{file_id}.bin"
    return _finish(response, dest_dir, filename, existing_names)


def _download_direct(
    url: str, dest_dir: Path, session: requests.Session, timeout: int,
    existing_names: frozenset[str],
) -> Path | None:
    response = session.get(url, stream=True, timeout=timeout)
    if not _host_allowed(response.url):
        raise LinkDownloadError("недозволений хост у перенаправленні")
    filename = (
        _filename_from_content_disposition(response.headers.get("content-disposition"))
        or Path(urlsplit(url).path).name
        or "download.bin"
    )
    return _finish(response, dest_dir, filename, existing_names)


def download_link(
    link: LinkAttachment,
    dest_dir: Path,
    *,
    session: requests.Session | None = None,
    timeout: int = 90,
    existing_names: frozenset[str] = frozenset(),
) -> Path | None:
    """Download one whitelisted link into ``dest_dir``; return the saved path, or
    None if a same-named file is already attached (repeat-click dedup). Raises
    LinkDownloadError on any refusal (not shared, too big, non-whitelisted
    redirect). The host of the ORIGINAL link is re-checked here as a
    belt-and-braces guard on top of extract_download_links only ever emitting
    whitelisted links."""
    if not _host_allowed(link.url):
        raise LinkDownloadError("хост не в білому списку")
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Default to the legacy-renegotiation session so the download survives the
    # lab PC's TLS-inspecting proxy (Windows cert store + unsafe-legacy-reneg) —
    # a plain requests.Session() dies with SSLEOFError/ConnectionError there,
    # which is exactly the "немає з'єднання з drive.google.com" failure. Same
    # seam the Sheets client and the GitHub update check use. Lazy import to
    # avoid pulling gspread into this module's import graph.
    if session is None:
        from app.sheets import new_legacy_session

        session = new_legacy_session()
    # (connect, read): fail fast (~10s) when the host is unreachable — a blocked
    # proxy or offline link must not hang the request for the full read window —
    # while still allowing a slow-but-alive transfer up to `timeout` seconds.
    request_timeout = (10, timeout)
    if link.kind == "drive" and link.file_id:
        return _download_drive(link.file_id, dest_dir, session, request_timeout, existing_names)
    return _download_direct(link.url, dest_dir, session, request_timeout, existing_names)
