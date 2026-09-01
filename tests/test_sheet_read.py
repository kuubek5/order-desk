"""read_all_values — читання вкладки маленькими шматками, щоб кешуючий/обрізаючий
проксі лабы не з'їв хвіст великої вкладки (бойовий випадок 01.09.26: 31.08 мала
120 робіт, а CRM через проксі бачила ~100)."""
import re

from app.sheets import read_all_values


class _FakeWS:
    """Мінімальний worksheet: grid із row_count, .get(range) як у gspread
    (обрізає хвостові порожні рядки), .get_all_values для fallback."""

    def __init__(self, row_count, fill):
        self.row_count = row_count
        self.grid = [[""] * 28 for _ in range(row_count)]
        fill(self.grid)
        self.calls = []

    def get(self, rng):
        m = re.match(r"A(\d+):[A-Z]+(\d+)", rng)
        s, e = int(m.group(1)), int(m.group(2))
        self.calls.append((s, e))
        block = [list(self.grid[i]) for i in range(s - 1, min(e, len(self.grid)))]
        while block and not any(c.strip() for c in block[-1]):
            block.pop()
        return block

    def get_all_values(self):
        return self.grid


def _work(grid, first_idx, n):
    for i in range(first_idx, first_idx + n):
        grid[i][1] = "N" + str(i)      # наряд
        grid[i][4] = "анатомія"        # вид


def test_reads_all_work_rows_in_chunks_with_alignment():
    ws = _FakeWS(300, lambda g: _work(g, 6, 10))  # 10 works at rows 7..16
    out = read_all_values(ws, 50)
    work = [r for r in out if len(r) > 1 and r[1].startswith("N")]
    assert len(work) == 10
    assert out[6][1] == "N6"  # absolute row alignment preserved


def test_stops_after_work_ends_not_at_grid_bottom():
    def fill(g):
        _work(g, 6, 10)
        g[250][5] = "Всього"  # stray cell far down must not force a full read
    ws = _FakeWS(300, fill)
    out = read_all_values(ws, 50)
    # stopped a couple chunks past the work, well before row 250 / grid end
    assert len(out) < 250
    assert max(e for _, e in ws.calls) < 250


def test_falls_back_to_get_all_values_without_grid_size():
    class NoGrid:
        row_count = 0
        def get_all_values(self):
            return [["", "", ""], ["", "24122", "2"]]
    assert read_all_values(NoGrid()) == [["", "", ""], ["", "24122", "2"]]


def test_tolerates_non_int_row_count_test_double():
    # A Mock-style double whose row_count isn't a real int must not crash —
    # read_all_values falls back to get_all_values (what the sync tests mock).
    from unittest.mock import Mock
    ws = Mock()
    ws.get_all_values.return_value = [["a"], ["b"]]
    assert read_all_values(ws) == [["a"], ["b"]]
