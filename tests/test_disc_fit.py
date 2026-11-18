"""Розстановка конструкцій на диск замість суми площ (власник 14.11.26)."""

from __future__ import annotations

import math
import time

import numpy as np

from app.services import disc_fit


def _box(w: float, d: float, step: float = 0.3) -> np.ndarray:
    xs, ys = np.meshgrid(np.arange(0, w + 1e-9, step), np.arange(0, d + 1e-9, step))
    return np.column_stack([xs.ravel(), ys.ravel()])


def _rect(cx, cy, w, h):
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


def _ring_of_cuts():
    """Диск, порізаний густою решіткою: вільно ~30 % площі, але лише вузькі
    коридори по 6 мм — одна коронка 10×10 ще знайде місце? Ні."""
    cuts = []
    for gx in range(-42, 43, 16):
        for gy in range(-42, 43, 16):
            if math.hypot(gx, gy) < 47:
                cuts.append(_rect(gx, gy, 10, 10))
    return cuts


def test_crown_fits_empty_disc_and_huge_piece_does_not():
    assert disc_fit.fits(47.0, [], [_box(10, 9)])
    assert not disc_fit.fits(47.0, [], [_box(100, 20)])


def test_scattered_free_space_is_not_enough():
    cuts = _ring_of_cuts()
    free = 1 - sum(abs(_area(c)) for c in cuts) / (math.pi * 47 ** 2)
    assert free > 0.4, "сума площ сказала б «влазить»"
    assert not disc_fit.fits(47.0, cuts, [_box(10, 9)])


def _slot(width: float):
    """Дві великі дірки зліва й справа — між ними коридор `width` мм."""
    return [_rect(-width / 2 - 30, 0, 60, 120), _rect(width / 2 + 30, 0, 60, 120)]


def test_new_work_margin_may_overlap_old_hole_by_one_mm():
    """Власник 18.11.26: рожевий контур — уже дірка, запас нової роботи може
    «трішечки» на неї зайти — до 1 мм з кожного боку. Коронка 10 мм + запас
    3 мм з боків = 16 мм; у коридорі 15 мм вона стає лише завдяки нахльосту,
    у 12 мм — ні (і з нахльостом бракує)."""
    crown = [_box(10, 10)]
    assert disc_fit.fits(47.0, _slot(15), crown)
    assert not disc_fit.fits(47.0, _slot(12), crown)


def test_overlap_rule_is_what_lets_the_crown_in(monkeypatch):
    monkeypatch.setattr(disc_fit, "HOLE_OVERLAP_MM", 0.0)
    assert not disc_fit.fits(47.0, _slot(15), [_box(10, 10)])


def test_long_bridge_turns_to_fit():
    # Вільна лише вертикальна смуга 16 мм посередині — міст 40×9 стане лише повернутим.
    cuts = [_rect(-27, 0, 38, 94), _rect(27, 0, 38, 94)]
    assert disc_fit.fits(47.0, cuts, [_box(40, 9)])


def test_several_pieces_need_room_together():
    cuts = [_rect(0, -20, 94, 54)]  # лишається верхня частина
    assert disc_fit.fits(47.0, cuts, [_box(12, 10)] * 3)
    assert not disc_fit.fits(47.0, cuts, [_box(12, 10)] * 30)


def test_shrink_scale_counts():
    cuts = [_rect(-27, 0, 38, 94), _rect(27, 0, 38, 94)]
    assert disc_fit.fits(47.0, cuts, [_box(10, 9)], scale=1.0)
    assert not disc_fit.fits(47.0, cuts, [_box(10, 9)], scale=1.6)


def test_fit_is_fast_enough_for_the_card():
    cuts = [_rect(x, y, 12, 12) for x in (-25, 0, 25) for y in (-25, 0, 25)]
    pieces = [_box(11, 9), _box(30, 10), _box(8, 8), _box(9, 9), _box(10, 8)]
    start = time.perf_counter()
    disc_fit.fits(47.0, cuts, pieces)
    assert time.perf_counter() - start < 1.5


def test_stray_far_vertex_is_rejected_without_building_a_grid(monkeypatch):
    """17.11.26: підказка листа 3709 рахувалась 3 год 9 хв і тримала процес під
    час оновлення. STL з далекою зайвою вершиною (чи другим тілом за сотні мм)
    будував сітку на мільйони клітинок. Ширше за диск — не влазить одразу."""
    crown = _box(10, 9)
    stray = np.vstack([crown, [[3000.0, 2500.0]]])

    def boom(*_a, **_k):
        raise AssertionError("сітку для конструкції, ширшої за диск, будувати не можна")

    monkeypatch.setattr(disc_fit, "piece_mask", boom)
    start = time.perf_counter()
    assert not disc_fit.fits(47.0, [], [crown, stray])
    assert time.perf_counter() - start < 0.5
    assert disc_fit.wider_than_disc(stray, 47.0)
    assert not disc_fit.wider_than_disc(crown, 47.0)
    # Діагональ коротша за діаметр — перевірка нічого справжнього не відсікає.
    assert not disc_fit.wider_than_disc(_box(66, 66), 47.0)
    assert disc_fit.wider_than_disc(_box(10, 9), 47.0, scale=11.0)


def _area(points):
    s = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        s += x1 * y2 - x2 * y1
    return s / 2


def _arch(radius: float = 22.0, width: float = 8.0, step: float = 0.3) -> np.ndarray:
    """Дуга мосту (підкова): кільце радіусом `radius`, нижня половина відкрита."""
    pts = []
    for r in np.arange(radius - width / 2, radius + width / 2 + 1e-9, step):
        for a in np.arange(0, math.pi + 1e-9, step / r):
            pts.append((r * math.cos(a), r * math.sin(a)))
    return np.array(pts)


def test_arch_bridge_stays_open_inside():
    """Підкова не заливається суцільно: усередині дуги лишається місце (14.11.26:
    заливка рядків роздувала міст до прямокутника й відсіювала диски)."""
    mask = disc_fit.piece_mask(_arch(), 0)
    h, w = mask.shape
    assert not mask[h // 4, w // 2], "середина під дугою вільна"
    assert mask.mean() < 0.6


def test_closed_hole_inside_silhouette_is_filled():
    ring = []
    for r in np.arange(8, 10.01, 0.3):
        for a in np.arange(0, 2 * math.pi, 0.3 / r):
            ring.append((r * math.cos(a), r * math.sin(a)))
    mask = disc_fit.piece_mask(np.array(ring), 0)
    h, w = mask.shape
    assert mask[h // 2, w // 2], "замкнена порожнина — частина силуету"
