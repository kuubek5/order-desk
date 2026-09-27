"""Схожі пари з набору назв — спільне ядро для екрана «Можливі дублікати».

Клієнти (`client_merge`) і теки export (`folder_merge`) шукають дублікати за тим
самим правилом: схожість — це БІЛЬША з двох оцінок, по сирих формах у нижньому
регістрі й по згорнутих (`match_key`, він же зводить гомогліфи кирилиця/латиниця).
Правило тут одне на обох, щоб два екрани не розійшлись у тому, що вважати дублем.

Чому не подвійний цикл на Python. Порівнянь квадратично: 335 імен — це майже 56
тисяч пар, і кожна викликала `token_set_ratio` двічі плюс тричі `match_key`. Увесь
цей час екран «Можливі дублікати» просто стояв (замір 27.09.26: 0.56 с на 335
іменах, з них SQL — 0.04 с). `process.cdist` рахує ту саму матрицю в C і одразу
відсікає все нижче порога, а `match_key` тут рахується РАЗ на ім'я, а не раз на
пару. Той самий прийом уже рятував підказки клієнта — див. попередній відсів у
`app/client_matcher.py` і коментар там про «GET /clients took 768s».

Матриця йде смугами (`_BAND`): у теці export назв може бути кілька тисяч, і повна
матриця n×n float32 тоді важить сотні мегабайт. Смуга тримає пам'ять у межах
`_BAND × n` незалежно від того, скільки назв прийшло.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import numpy as np
from rapidfuzz import fuzz, process

from app.material_classifier import match_key

#: Скільки рядків матриці рахуємо за раз. Пам'ять смуги — `_BAND × len(names)`
#: float32, тобто при 5000 назв це ~10 МБ, а не 100.
_BAND = 512


def fold_keys(names: Sequence[str]) -> list[str]:
    """`match_key` по кожній назві РАЗ. Викликач переюзує цей список і далі —
    у старому коді той самий ключ рахувався заново на кожну пару."""
    return [match_key(name) for name in names]


def similar_pairs(
    names: Sequence[str], *, threshold: float, keys: Sequence[str] | None = None
) -> Iterator[tuple[int, int, float]]:
    """Індекси пар `i < j`, чия схожість не менша за `threshold`, зі схожістю.

    Порядок видачі — за зростанням `i`, потім `j`: той самий обхід, що давав
    старий подвійний цикл, тож стабільне сортування викликача не перетасовує
    однакові оцінки інакше, ніж раніше.

    `keys` можна передати готовими (`fold_keys`), якщо вони вже потрібні
    викликачу для інших перевірок — інакше рахуємо самі.
    """
    total = len(names)
    if total < 2:
        return
    lowered = [name.lower() for name in names]
    folded = list(keys) if keys is not None else fold_keys(names)

    for start in range(0, total, _BAND):
        stop = min(start + _BAND, total)
        # `score_cutoff` кладе все нижче порога в 0, тож максимум двох матриць
        # лишається чесним: одна форма могла не дотягти, а друга дотягла.
        raw_band = process.cdist(
            lowered[start:stop], lowered, scorer=fuzz.token_set_ratio,
            score_cutoff=threshold, workers=-1, dtype=np.float32,
        )
        folded_band = process.cdist(
            folded[start:stop], folded, scorer=fuzz.token_set_ratio,
            score_cutoff=threshold, workers=-1, dtype=np.float32,
        )
        band = np.maximum(raw_band, folded_band)
        # Лише верхній трикутник: пара (i, j) з i < j, кожна рівно один раз.
        rows, cols = np.nonzero(band)
        for row, col in zip(rows.tolist(), cols.tolist()):
            i = start + row
            if col <= i:
                continue
            yield i, col, float(band[row, col])
