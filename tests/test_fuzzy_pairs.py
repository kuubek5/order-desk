"""Матричний пошук схожих пар — те саме, що давав подвійний цикл.

Екран «Можливі дублікати» стояв на квадратичному циклі Python (замір 27.09.26:
0.56 с на 335 іменах клієнтів, і без обмеження за датою — на теках export
більше). Цикл замінено на `process.cdist`, і єдине, що тут справді треба
стерегти, — щоб швидша реалізація пропонувала власнику ТІ САМІ пари.
"""

from rapidfuzz import fuzz

from app.material_classifier import match_key
from app.services.fuzzy_pairs import _BAND, fold_keys, similar_pairs

THRESHOLD = 82.0


def _reference(names):
    """Стара реалізація — дослівно, як вона жила в client_merge/folder_merge."""
    out = []
    for i in range(len(names)):
        key_a = match_key(names[i])
        for j in range(i + 1, len(names)):
            key_b = match_key(names[j])
            score = max(
                fuzz.token_set_ratio(names[i].lower(), names[j].lower()),
                fuzz.token_set_ratio(key_a, key_b),
            )
            if score < THRESHOLD:
                continue
            out.append((i, j, round(float(score), 4)))
    return out


def _actual(names):
    return [
        (i, j, round(score, 4))
        for i, j, score in similar_pairs(names, threshold=THRESHOLD, keys=fold_keys(names))
    ]


def test_matches_the_old_double_loop_pair_for_pair():
    names = [
        "Кривовид", "Євген Кривовид", "кривовид  ",
        "Pavlenko", "pavlenko", "PAVLENKO dental",
        "Ніколаєв", "Іван Ніколаєв", "Микола Іванов",
        "LekaLab", "leka lab", "Viktor Petrovich", "ilab",
    ]
    assert _actual(names) == _reference(names)


def test_homoglyph_forms_still_pair_up():
    """Кирилична «С» і латинська «C» — та сама назва; це ловить лише згорнута
    форма, тож максимум двох оцінок мусить пережити перехід на матрицю."""
    names = ["Cмірнов", "Смірнов"]  # перша з латинською C
    pairs = _actual(names)
    assert pairs, "гомогліфну пару перестали бачити"
    assert pairs == _reference(names)


def test_more_rows_than_one_band():
    """Матриця йде смугами по `_BAND` рядків — пари на стику смуг не губляться
    і не задвоюються."""
    names = [f"Клієнт {n:04d}" for n in range(_BAND + 40)]
    actual = _actual(names)
    assert actual == _reference(names)
    assert len({(i, j) for i, j, _ in actual}) == len(actual)
    assert all(i < j for i, j, _ in actual)


def test_short_input_is_not_a_crash():
    assert _actual([]) == []
    assert _actual(["Один"]) == []
