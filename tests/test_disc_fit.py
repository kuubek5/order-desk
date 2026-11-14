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


def _area(points):
    s = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        s += x1 * y2 - x2 * y1
    return s / 2
