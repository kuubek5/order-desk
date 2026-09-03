"""Safe path tokens for the hover STL preview feature.

Templates already build client-side `file://` links (`app/order_folder.py`)
that the *browser*, not the server, resolves — fine for "open in Explorer"
but useless for a server route that needs to actually read file bytes to
stream a 3D preview.

For that we need a server-side route that (a) lists `.stl` files in a
folder and (b) serves one file's bytes — and neither may ever trust a raw
filesystem path handed back by the client, or a directory-traversal
`../../` payload could read arbitrary files off the disk.

The scheme here mirrors `app/order_folder.py::resolve_email_attachment_folder`
(the existing traversal-safe pattern in this codebase — lexical root check,
per-segment symlink/junction rejection, then a `resolve(strict=True)` +
`relative_to()` confirmation) but adds one more layer: the token handed to
the browser never carries an absolute path. It only carries

    "<root_key>:<relative/posix/path>"

base64url-encoded. `root_key` is one of a fixed, known set ("export",
"mail", "tech"); the *current* absolute root for that key is always looked
up server-side from settings at request time — the client can never supply
or influence the root itself, only (a validated) sub-path under it. Treat
every decoded token as fully attacker-controlled: it is echoed back by the
browser on every hover, so nothing about its contents is trusted until the
segment/symlink/resolve checks below all pass.
"""

import base64
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import MAIL_ATTACHMENTS_PATH
from app.settings_store import get_export_folder_path, get_technician_files_path

STL_EXTENSION = ".stl"

# root_key -> resolver(db) -> current absolute root path (str) or None/"" if unset.
_ROOT_RESOLVERS = {
    "export": lambda db: get_export_folder_path(db),
    "mail": lambda db: str(MAIL_ATTACHMENTS_PATH),
    "tech": lambda db: get_technician_files_path(db),
}


def _is_link(path: Path) -> bool:
    """Treat both regular symlinks and Windows junctions as links.

    Duplicated from app/order_folder.py (same five lines) rather than
    imported, so this module stays independently reviewable — it is the
    security boundary for a route that streams raw file bytes.
    """
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _lexical_and_resolved_root(root_value: str | None) -> tuple[Path, Path] | None:
    """Validate a configured root directory, returning (lexical, resolved) or None."""
    if not root_value or not root_value.strip():
        return None
    root = Path(root_value)
    try:
        lexical_root = root.absolute()
        if _is_link(lexical_root):
            return None
        resolved_root = lexical_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved_root.is_dir():
        return None
    return lexical_root, resolved_root


def _valid_relative_segments(relative: Path) -> list[str] | None:
    """Reject anything that isn't a plain, non-escaping list of path segments."""
    segments = relative.parts
    if not segments:
        return None
    for segment in segments:
        if segment in ("", ".", "..") or "\\" in segment or ":" in segment:
            return None
        if Path(segment).is_absolute():
            return None
    return list(segments)


def _walk_without_links(lexical_root: Path, segments: list[str]) -> Path | None:
    """Join segments onto the root one at a time, rejecting any symlink/junction hop."""
    current = lexical_root
    for segment in segments:
        current = current / segment
        try:
            if _is_link(current):
                return None
        except OSError:
            return None
    return current


def validate_preview_roots(roots: dict[str, str | None]) -> dict[str, tuple[Path, Path]]:
    """Перевірити корені ОДИН раз на пакет — {ключ: (лексичний, розвʼязаний)}.

    Перевірка одного кореня коштує ~4 звернення до диска (is_symlink,
    is_junction, resolve(strict=True), is_dir), а корені для всіх рядків
    ОДНАКОВІ — вони беруться з налаштувань, не з рядка. Раніше
    build_preview_token перевіряв їх заново на КОЖНУ роботу: виміряно
    03.09.26 — 18 звернень на одну поштову роботу, тобто 900 на 50 рядків,
    і все це на мережевій шарі, ще й у поллі кожні 15 с.

    Перевірка не ослаблена — просто зроблена один раз замість N. Належність
    самої теки кореню (прохід сегментами без переходу за лінками, resolve,
    is_dir) лишається ПОРЯДКОВОЮ, бо тека в кожного рядка своя."""
    validated: dict[str, tuple[Path, Path]] = {}
    for root_key, root_value in roots.items():
        if root_key not in _ROOT_RESOLVERS:
            continue
        pair = _lexical_and_resolved_root(root_value)
        if pair is not None:
            validated[root_key] = pair
    return validated


def build_preview_token(
    folder: Path,
    roots: dict[str, str | None],
    *,
    validated_roots: dict[str, tuple[Path, Path]] | None = None,
) -> str | None:
    """Return an opaque preview token for `folder` if it sits under one of `roots`.

    `roots` maps root_key -> current absolute root path (as configured), e.g.
    {"export": get_export_folder_path(db), "mail": str(MAIL_ATTACHMENTS_PATH)}.
    Tries each root in order and returns the first match; returns None if the
    folder does not resolve safely under any of them (silent degradation,
    matching the rest of this codebase's folder-link helpers — no preview
    offered rather than raising).

    `validated_roots` — результат validate_preview_roots() для тих самих
    коренів. Передавай його, коли токенів у циклі багато: перевірка коренів
    тоді робиться раз на пакет, а не раз на рядок (див. коментар там).
    """
    try:
        lexical_folder = Path(folder).absolute()
    except (OSError, RuntimeError):
        return None

    if validated_roots is None:
        validated_roots = validate_preview_roots(roots)

    for root_key in roots:
        validated_root = validated_roots.get(root_key)
        if validated_root is None:
            continue
        lexical_root, resolved_root = validated_root

        try:
            relative = lexical_folder.relative_to(lexical_root)
        except ValueError:
            continue

        segments = _valid_relative_segments(relative)
        if segments is None:
            continue

        walked = _walk_without_links(lexical_root, segments)
        if walked is None:
            continue

        try:
            resolved_folder = walked.resolve(strict=True)
            resolved_folder.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved_folder.is_dir():
            continue

        payload = f"{root_key}:{relative.as_posix()}"
        token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        return token

    return None


def build_preview_token_lexical(
    folder: Path,
    roots: dict[str, str | None],
    validated_roots: dict[str, tuple[Path, Path]],
) -> str | None:
    """Токен БЕЗ звернень до диска — лише лексична перевірка належності кореню.

    Навіщо: build_preview_token нижче ще й ходить на диск — проходить кожен
    сегмент шляху, перевіряючи, чи не лінк, потім resolve(strict=True) і
    is_dir(). На теці `клієнт/дата/матеріал` це ~9 звернень, і виконується
    воно в ЦИКЛІ по рядках черги й по партіях видачі, на мережевій шарі, ще й
    у поллі кожні 15 с. Виміряно 03.09.26: 18 звернень на одну поштову
    роботу, 900 на 50 рядків.

    Чому це не послаблює захист. Токен НЕ Є ПЕРЕПУСТКОЮ: усі три роути, що
    віддають байти (app/routers/stl.py), ідуть через resolve_preview_folder,
    а той бере корінь заново з налаштувань (_ROOT_RESOLVERS, не з токена),
    перевіряє сегменти, проходить шлях без переходу за лінками, робить
    resolve(strict=True) і вимагає, щоб результат лишався під розвʼязаним
    коренем. Тобто перевірка на диску нікуди не поділась — вона там, де
    вирішується доступ, а не там, де малюється підказка.

    Найгірше, що може дати необережний токен: оператор наведе мишу й не
    побачить прев'ю, бо роут відмовить. Це дешевше за секунди очікування на
    кожному відкритті черги.
    """
    try:
        lexical_folder = Path(folder).absolute()
    except (OSError, RuntimeError):
        return None

    for root_key in roots:
        validated_root = validated_roots.get(root_key)
        if validated_root is None:
            continue
        lexical_root, _resolved_root = validated_root
        try:
            relative = lexical_folder.relative_to(lexical_root)
        except ValueError:
            continue
        segments = _valid_relative_segments(relative)
        if segments is None:
            continue
        payload = f"{root_key}:{relative.as_posix()}"
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return None


def build_preview_token_for_known_child(root_key: str, name: str) -> str | None:
    """Токен для теки, про яку ВЖЕ відомо, що вона прямий нащадок кореня.

    Навіщо окремий вхід: build_preview_token вище перевіряє належність теки
    кореню сам, і ця перевірка коштує ~9 звернень до диска (валідація кореня,
    прохід сегментами без переходу за лінками, resolve, is_dir). На екрані
    черги вона виконувалась на КОЖЕН рядок — а тека техніків лежить на
    мережевій шарі, де кожне звернення це round-trip. Виміряно 03.09.26:
    саме це й було головною причиною «вкладка відкривається 5 секунд».

    Коли назва теки прийшла з обходу самого кореня (див.
    order_folder._tech_root_children), належність уже доведена — доводити її
    ще раз означає платити мережею за відоме.

    Це БЕЗПЕЧНО, бо токен не є перепусткою: resolve_preview_folder нижче
    заново бере корінь із налаштувань (_ROOT_RESOLVERS, не з токена),
    перевіряє сегменти, проходить без переходу за лінками й вимагає, щоб
    розвʼязаний шлях лишався під розвʼязаним коренем. Тобто дешевий токен не
    дає доступу нікуди, куди не дав би дорогий — просто не платить за
    перевірку двічі.

    `name` усе одно перевіряється як ОДИН безпечний сегмент: якщо колись
    зʼявиться інший постачальник назв, помилка не проскочить мовчки.
    """
    if root_key not in _ROOT_RESOLVERS:
        return None
    if _valid_relative_segments(Path(name)) != [name]:
        return None
    payload = f"{root_key}:{name}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def resolve_preview_folder(db: Session, token: str) -> Path | None:
    """Decode a preview token back into a trusted, existing directory, or None.

    `token` is fully attacker-controlled (it round-trips through the
    browser); every step here treats it that way. The root path itself is
    never taken from the token — only the root_key, which is looked up
    against the fixed `_ROOT_RESOLVERS` map to fetch the *current* setting.
    """
    if not token:
        return None

    padded = token + "=" * (-len(token) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None

    root_key, sep, relative_str = payload.partition(":")
    if not sep or root_key not in _ROOT_RESOLVERS:
        return None

    root_value = _ROOT_RESOLVERS[root_key](db)
    validated_root = _lexical_and_resolved_root(root_value)
    if validated_root is None:
        return None
    lexical_root, resolved_root = validated_root

    relative = Path(relative_str)
    segments = _valid_relative_segments(relative)
    if segments is None:
        return None

    walked = _walk_without_links(lexical_root, segments)
    if walked is None:
        return None

    try:
        resolved_folder = walked.resolve(strict=True)
        resolved_folder.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved_folder.is_dir():
        return None
    return resolved_folder


def list_stl_files(folder: Path) -> list[str]:
    """Return sorted `.stl` filenames directly inside `folder` (non-recursive)."""
    try:
        entries = list(Path(folder).iterdir())
    except (OSError, PermissionError):
        return []
    names = [
        entry.name
        for entry in entries
        if entry.is_file() and entry.suffix.lower() == STL_EXTENSION
    ]
    return sorted(names)


def resolve_stl_file(folder: Path, filename: str) -> Path | None:
    """Validate `filename` is a plain `.stl` file directly inside `folder`.

    Rejects path separators, traversal segments, non-`.stl` extensions, and
    anything that doesn't resolve to an existing regular file whose parent is
    exactly `folder` (defense in depth even though `folder` itself was
    already validated by `resolve_preview_folder`).
    """
    if not filename or "/" in filename or "\\" in filename:
        return None
    if filename in (".", ".."):
        return None
    if Path(filename).suffix.lower() != STL_EXTENSION:
        return None

    folder = Path(folder)
    try:
        if _is_link(folder):
            return None
        resolved_folder = folder.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    candidate = folder / filename
    try:
        if _is_link(candidate):
            return None
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    if resolved_candidate.parent != resolved_folder:
        return None
    if not resolved_candidate.is_file():
        return None
    return resolved_candidate
