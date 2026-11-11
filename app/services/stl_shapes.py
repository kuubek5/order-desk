"""Які STL лежать у роботах цеху — для 3D на табло (власник 11.11.26).

3D-коронку на плитці верстата будуємо лише зі справжнього STL роботи. Мокап
зроблено на коронках Exocad, а в цеху фрезерують ще мости, абатменти, вкладки,
капи й віск, і частина файлів приходить з інших CAD. Перш ніж обіцяти анімацію
для них, треба знати про кожен вид три речі:

* чи модель замкнена (заливка стінки на зрізі працює лише на замкненій);
* чи її можна поставити рівно (край вниз) — орієнтації у файлі не довіряємо;
* як вона виглядає після цього — картинка, щоб оцінити оком.

Модуль лише ЧИТАЄ файли на цеховому ПК і віддає заміри та малюнок. Назв файлів
і тек не віддає: у них імена пацієнтів і клієнтів.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 60_000_000
"""Більший STL — не зубна робота (модель щелепи, сцена), його не читаємо."""

_FACET = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])


def read_stl(path: Path) -> np.ndarray | None:
    """Трикутники (n, 3, 3) у мм або None, якщо файл не STL / не читається."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count and 84 + count * 50 == len(data):
            rec = np.frombuffer(data, dtype=_FACET, count=count, offset=84)
            return rec["v"].astype(np.float64)
    if data[:5].lower() == b"solid":
        verts = []
        for line in data.decode("ascii", "ignore").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                try:
                    verts.append([float(p) for p in parts[1:]])
                except ValueError:
                    return None
        if verts and len(verts) % 3 == 0:
            return np.asarray(verts, dtype=np.float64).reshape(-1, 3, 3)
    return None


def _welded(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    uniq, inv = np.unique(np.round(tris.reshape(-1, 3), 4), axis=0, return_inverse=True)
    return uniq, inv.reshape(-1, 3)


def open_edge_count(tris: np.ndarray) -> int:
    """Ребра без пари в протилежному напрямку: 0 — модель замкнена."""
    _, idx = _welded(tris)
    edges = np.concatenate([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
    have = set(map(tuple, edges.tolist()))
    return sum(1 for a, b in have if (b, a) not in have)


def _sphere(n: int) -> np.ndarray:
    i = np.arange(n) + 0.5
    y = 1 - 2 * i / n
    r = np.sqrt(1 - y * y)
    a = i * 2.399963229728653
    return np.stack([r * np.cos(a), y, r * np.sin(a)], axis=1)


@dataclass
class Upright:
    rotation: np.ndarray
    rim_points: int
    rim: np.ndarray | None = None


def upright(tris: np.ndarray, directions: int = 48) -> Upright:
    """Поворот, що ставить край роботи вниз (вісь введення — вниз). Геометрії не чіпає.

    Той самий спосіб, що в мокапі (`design/tv-board-3d-tools/export_models.py`):
    зовнішню поверхню видно майже з усіх боків, куди вона обернена, стінку
    порожнини — лише крізь отвір. Межа між ними — край; його площина задає
    вісь. `rim_points == 0` — краю не знайшли (модель без порожнини), поворот
    одиничний."""
    nrm = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-12)[:, None]
    pts = np.concatenate([tris.mean(1), tris[:, 0], tris[:, 1], tris[:, 2]])
    nt, px, eps = len(tris), 0.10, 0.35

    def outer(sign: float) -> np.ndarray:
        facing = np.zeros(nt)
        seen = np.zeros(nt)
        for d in _sphere(directions):
            u = np.cross(d, [0, 1, 0] if abs(d[1]) < 0.9 else [1, 0, 0])
            u /= np.linalg.norm(u)
            w = np.cross(d, u)
            pu = np.floor(pts @ u / px).astype(np.int64)
            pw = np.floor(pts @ w / px).astype(np.int64)
            pu -= pu.min()
            pw -= pw.min()
            cell = pu * (pw.max() + 1) + pw
            dep = pts @ d
            zb = np.full(cell.max() + 1, -1e9)
            np.maximum.at(zb, cell, dep)
            f = sign * (nrm @ d) > 0.05
            facing += f
            seen += f & (dep[:nt] >= zb[cell[:nt]] - eps)
        return seen >= 0.5 * np.maximum(facing, 1)

    keep = outer(1.0)
    if keep.mean() < 0.2:  # нормалі у файлі навиворіт
        keep = outer(-1.0)
    kept = tris[keep]
    if not len(kept):
        return Upright(np.eye(3), 0)
    uniq, idx = _welded(kept)
    edges = np.concatenate([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
    have = set(map(tuple, edges.tolist()))
    boundary = [(a, b) for a, b in have if (b, a) not in have]
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in boundary:
        parent[find(a)] = find(b)
    loops: dict[int, list[int]] = {}
    for a, _b in boundary:
        loops.setdefault(find(a), []).append(a)
    if not loops:
        return Upright(np.eye(3), 0)
    # Міст має по краю на кожну опору, і всі вони лежать в одній площині
    # введення. Площина за ОДНИМ краєм нахиляла міст (12 мм у файлі ставали
    # 17.6). Тому беремо всі великі краї разом; дрібні петлі — шум сітки.
    biggest = max(len(v) for v in loops.values())
    rim_ids = {i for v in loops.values() if len(v) >= 0.3 * biggest for i in v}
    loop = uniq[list(rim_ids)]
    centre = loop.mean(0)
    axis = np.linalg.svd(loop - centre)[2][2]
    if axis @ (centre - kept.reshape(-1, 3).mean(0)) < 0:
        axis = -axis
    target = np.array([0.0, -1.0, 0.0])
    c, x = float(axis @ target), np.cross(axis, target)
    if np.linalg.norm(x) > 1e-6:
        k = np.array([[0, -x[2], x[1]], [x[2], 0, -x[0]], [-x[1], x[0], 0]])
        rot = np.eye(3) + k + k @ k * ((1 - c) / (x @ x))
    else:
        rot = np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    # Правило вибору між цим поворотом і положенням з файлу (`AS_FILE`) ще не
    # знайдене: коронки Exocad лежать у файлі під нахилом і поворот їх рятує,
    # а гвинтовий SLM-міст у файлі стоїть правильно, і за «край» тут бралися
    # отвори каналів під гвинти — поворот клав його на бік (11.11.26). Тому
    # інструмент MCP малює обидва положення; правило — після звірки на
    # справжніх роботах кожного виду.
    return Upright(rot, len(loop), loop)


AS_FILE = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])
"""Положення з файлу: Z угору (CAD) → Y угору (малюнок і табло)."""


def render(tris: np.ndarray, size: int = 240, label: str = "") -> Any:
    """Малюнок моделі в три чверті (Y — угору), плоске освітлення.

    Без відеокарти: трикутники фарбуються від дальніх до ближніх (painter's).
    Для оцінки форми оком цього досить."""
    from PIL import Image, ImageDraw

    v = tris.reshape(-1, 3)
    v = v - (v.min(0) + v.max(0)) / 2
    ay, ax = np.radians(35), np.radians(28)
    ry = np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])
    rx = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    v = v @ (rx @ ry).T
    t = v.reshape(-1, 3, 3)
    span = float(np.abs(v[:, :2]).max()) or 1.0
    scale = (size * 0.42) / span
    nrm = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-12)[:, None]
    light = np.array([0.35, 0.6, 0.72])
    light /= np.linalg.norm(light)
    shade = np.clip(np.abs(nrm @ light), 0, 1)
    order = np.argsort(t[:, :, 2].mean(1))
    img = Image.new("RGB", (size, size), (24, 26, 34))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    for i in order:
        g = int(70 + 170 * shade[i])
        poly = [(cx + p[0] * scale, cy - p[1] * scale) for p in t[i]]
        draw.polygon(poly, fill=(g, g, int(g * 0.96)))
    if label:
        draw.text((6, 4), label, fill=(240, 220, 160))
    return img


def stl_files(folder: Path, limit: int) -> list[Path]:
    """STL у теці роботи й на рівень глибше, як шукає мірка висоти."""
    found: list[Path] = []
    try:
        for entry in sorted(folder.iterdir()):
            if entry.is_file() and entry.suffix.lower() == ".stl":
                found.append(entry)
            elif entry.is_dir():
                found.extend(sorted(s for s in entry.iterdir() if s.is_file() and s.suffix.lower() == ".stl"))
            if len(found) >= limit:
                break
    except OSError:
        return found[:limit]
    return found[:limit]


def describe(path: Path) -> tuple[dict[str, Any], tuple[np.ndarray, np.ndarray] | None]:
    """Заміри одного файлу без його назви; другий елемент — модель у положенні
    з файлу і після повороту за краєм (обидва для звірки оком)."""
    from app.services.stl_units import exocad_type

    tris = read_stl(path)
    info: dict[str, Any] = {
        "exocad": PurePath(path.name).name.lower().endswith("_cad.stl"),
        "тип_exocad": exocad_type(path.name) or None,
    }
    try:
        info["розмір_мб"] = round(path.stat().st_size / 1_048_576, 2)
    except OSError:
        pass
    if tris is None or not len(tris):
        info["прочитано"] = False
        return info, None
    dims = tris.reshape(-1, 3).max(0) - tris.reshape(-1, 3).min(0)
    info.update({
        "прочитано": True,
        "трикутників": int(len(tris)),
        "габарити_у_файлі_мм": [round(float(d), 1) for d in dims],
    })
    open_edges = open_edge_count(tris)
    info["відкритих_ребер"] = open_edges
    info["замкнена"] = open_edges == 0
    up = upright(tris)
    flat = tris.reshape(-1, 3)
    stood = (flat @ up.rotation.T).reshape(-1, 3, 3)
    as_file = (flat @ AS_FILE.T).reshape(-1, 3, 3)
    sdims = stood.reshape(-1, 3).max(0) - stood.reshape(-1, 3).min(0)
    info["край_знайдено"] = up.rim_points > 0
    info["точок_краю"] = up.rim_points
    info["висота_після_повороту_мм"] = round(float(sdims[1]), 1)
    info["висота_у_файлі_мм"] = round(float(dims[2]), 1)
    if up.rim is not None and len(up.rim):
        # Де «край» відносно моделі в кожному положенні: 0 — біля дна, 1 — нагорі.
        for name, rot in (("у_файлі", AS_FILE), ("після_повороту", up.rotation)):
            ys = flat @ rot[1]
            rim_y = up.rim @ rot[1]
            height = float(np.ptp(ys)) or 1.0
            info[f"край_над_дном_{name}"] = round((float(rim_y.mean()) - float(ys.min())) / height, 2)
    return info, (as_file, stood)
