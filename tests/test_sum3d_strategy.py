"""Читач файлів стратегій Sum3D (`sum3d_strategy`, `kmill_sum3d_strategy`). Лише читання.

Файли тут складені за будовою справжніх стратегій: шапка з таблицями «-1»,
таблиця операцій з кроком 178 або 278 байт, тіла з міткою `TECN 2.0` на
зсуві 64 і полями «текст до 0x0A / число float32».
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from app.services import sum3d_explore, sum3d_strategy
from app.services.machine_files import mask
from app.services.mcp_common import ToolError
from app.services.mcp_tools import TOOLS_BY_NAME

HEADER = 0x30D8F


def num(value: float) -> bytes:
    return struct.pack("<f", value)


def text(value: str) -> bytes:
    return value.encode("cp1252") + b"\n"


def body(*parts: bytes, pad: int = 80) -> bytes:
    return bytes(64) + text("TECN 2.0") + b"".join(parts) + num(0.0) * pad


def strategy_file(ops: list[tuple[str | bytes, int, bytes]], *, stride: int = 178, gap: int = 4000) -> bytes:
    """ops: (назва, байт типу обробки, тіло)."""
    header = b"TECN 2.0\x00\x00" + b"\xff\xff\xff\xff" * ((HEADER - 10) // 4)
    header += bytes(HEADER - len(header)) + b"BLOCK_POSITION".ljust(452, b"\x00")
    table = b""
    off = 0
    for name, kind, payload in ops:
        record = b"\xff\xff\xff\xff" + bytes([0, kind, 0, 0]) + bytes(8) + struct.pack("<II", off, len(payload))
        raw = name if isinstance(name, bytes) else name.encode("cp1252")
        table += record + raw.ljust(stride - 24, b"\x00")
        off += len(payload)
    return header + table + bytes(gap) + b"".join(payload for _n, _k, payload in ops)


ROUGH = body(text("YES"), num(10.0), text("YES"), num(1510.0), text("POCKETING"), num(0.5), text("Roughing"))
FINE = body(text("NO"), num(0.08), text(""), num(0.9), text("Superfinishing"), num(0.15), text("Z-LEVEL PROFILING"))
OPS = [
    ("__________Basis___<TS99>", 14, body(text("WIRE EROSION"))),
    ("Rough180 2.5mm<MO+0.5>", 2, ROUGH),
    ("Finish180 1mm<IOF-0.05><TS-17,-18>", 1, FINE),
    ("Finish06<TS9,10><# Fissure fine with 0.6mm Tool /$3>", 6, body(text("PENCIL MILLING"), num(0.1))),
]


@pytest.mark.parametrize("stride", [178, 278])
def test_table_is_found_for_both_record_sizes(stride):
    data = strategy_file(OPS, stride=stride)
    parsed = sum3d_strategy.parse(data)
    assert parsed.stride == stride and [op.name for op in parsed.ops] == [name for name, _k, _b in OPS]
    assert parsed.base is not None and parsed.label_hits == parsed.label_checked == 4
    assert parsed.body(data, parsed.ops[1]) == ROUGH
    assert not parsed.notes


def test_header_full_of_minus_ones_is_not_mistaken_for_records():
    parsed = sum3d_strategy.parse(strategy_file(OPS))
    assert parsed.table_at == HEADER + 452, "таблиця починається з першого запису, не з шапки"


def test_not_a_strategy_is_refused():
    with pytest.raises(sum3d_strategy.StrategyError):
        sum3d_strategy.parse(b"PUMA 1.1" + bytes(5000))


def test_file_without_operations_reports_it_instead_of_guessing():
    parsed = sum3d_strategy.parse(b"TECN 2.0\x00\x00" + bytes(40000))
    assert parsed.ops == [] and parsed.notes


def _values(payload: bytes) -> list:
    items, tail = sum3d_strategy.fields(payload)
    assert tail == 0
    return [f.text if f.text is not None else round(f.number, 4) for f in items]


def test_number_ending_in_a_letter_byte_does_not_swallow_the_next_text():
    """10.0 = 00 00 20 41: хвіст « A» не має приклеїтись до «YES»."""
    assert _values(ROUGH)[:5] == ["TECN 2.0", "YES", 10.0, "YES", 1510.0]


def test_number_starting_with_newline_byte_stays_a_number():
    """0.08 = 0A D7 A3 3D — це число, а не порожній рядок і сміття за ним."""
    values = _values(FINE)
    assert values[:7] == ["TECN 2.0", "NO", 0.08, "", 0.9, "Superfinishing", 0.15]


def test_printable_round_number_before_text_is_a_number():
    """0.9 = «fff?» — усі байти друковані; текст за ним лишається текстом."""
    payload = body(num(0.9), text("YES"), num(12.4), text(""), text("NO"))
    assert _values(payload)[1:6] == [0.9, "YES", 12.4, "", "NO"]


def test_gcode_and_digit_strings_are_text():
    gcode = "G53 Z0.$\xa3G53 X0. Y0.$\xa3M3 $\xa3G4 X2.0"
    stamp = "13061303333130626000.131114113503"
    payload = body(text(gcode), text("M5 $\xa3M30"), text(stamp), text("25"))
    assert _values(payload)[1:5] == [gcode, "M5 $\xa3M30", stamp, "25"]


def test_leftover_bytes_at_the_end_are_counted_not_guessed():
    items, tail = sum3d_strategy.fields(ROUGH + b"\x41")
    assert tail == 1 and items[-1].number == 0.0


def test_name_codes_are_split_without_interpretation():
    info = sum3d_strategy.describe_name("742<TS1,2><T:/D2xB-?/Rest_Material><SL2><IOF-0.08><#1RME/$415><R>")
    assert info["типи"] == [1, 2] and info["фреза"] == "D2xB-?" and info["набір"] == "Rest_Material"
    assert info["опція"] == "1RME" and info["група_опції"] == "415" and info["коди"] == ["SL2", "IOF-0.08", "R"]
    old = sum3d_strategy.describe_name("Finish180 1mm<IOF-0.05><TYPESEL-17,-18>")
    assert old["крім_типів"] == [17, 18] and "типи" not in old and old["текст"] == "Finish180 1mm"
    option = sum3d_strategy.describe_name("Finish06<# Fissure fine with 0.6mm Tool /$3>")
    assert option["опція"] == "Fissure fine with 0.6mm Tool" and option["група_опції"] == "3"


def test_ops_report_names_types_and_masks_letters():
    # Кирилична назва лежить у файлі байтами cp1251.
    ops = OPS + [("Чистова<TS9>".encode("cp1251"), 1, body(text("Z-LEVEL PROFILING")))]
    data = strategy_file(ops)
    report = sum3d_strategy.ops_report(data, sum3d_strategy.parse(data), mask)
    rows = report["операції"]
    assert [row["тип_обробки"] for row in rows[:4]] == ["WIRE EROSION", "POCKETING", "Z-LEVEL PROFILING", "PENCIL MILLING"]
    assert rows[2]["крім_типів"] == [17, 18] and rows[3]["опція"] == "Fissure fine with 0.6mm Tool"
    assert "Чистова" not in json.dumps(report, ensure_ascii=False) and "•" in rows[4]["назва"]
    assert report["будова"]["операцій"] == 5 and report["типи_обробки"]["Z-LEVEL PROFILING"] == 2


def test_op_report_hides_work_file_names():
    payload = body(text("Petrenko_Ivan_upper.stl"), text("C:\\sum\\cam\\job.cam"), text("ball cutter 1mm"), num(0.15))
    data = strategy_file([("A", 1, payload), ("B", 1, payload)])
    report = sum3d_strategy.op_report(data, sum3d_strategy.parse(data), 1, mask)
    dump = json.dumps(report, ensure_ascii=False)
    assert "Petrenko" not in dump and "job.cam" not in dump
    assert "‹назва файлу .stl›" in dump and "ball cutter 1mm" in dump
    assert report["зайвих_байтів_у_кінці"] == 0 and {"№": 4, "число": 0.15} in report["поля"]


def test_diff_finds_added_removed_renamed_and_changed_fields():
    changed = body(text("YES"), num(10.0), text("YES"), num(1510.0), text("POCKETING"), num(0.3), text("Roughing"))
    other = [
        OPS[0],
        ("Rough180 2.5mm<MO+0.5>", 2, changed),
        ("Finish180 1mm<IOF-0.08><TS-17,-18>", 1, FINE),
        ("Finish03<# Fissure extra fine /$5>", 6, body(text("PENCIL MILLING"))),
    ]
    a, b = strategy_file(OPS), strategy_file(other, stride=278)
    pa, pb = sum3d_strategy.parse(a), sum3d_strategy.parse(b)
    report = sum3d_strategy.diff_report(a, pa, b, pb, mask)
    assert report["однакових"] == 1 and report["змінених"] == 2
    assert [row["назва"] for row in report["лише_в_першому"]] == [OPS[3][0]]
    assert [row["назва"] for row in report["лише_в_другому"]] == ["Finish03<# Fissure extra fine /$5>"]
    rough = next(row for row in report["змінені"] if row["№"] == 2)
    assert rough["відмінностей_у_полях"] == 1 and rough["перші"] == [{"№": 6, "було": 0.5, "стало": 0.3}]
    renamed = next(row for row in report["змінені"] if row["№"] == 3)
    assert renamed["назва_в_другому"] == "Finish180 1mm<IOF-0.08><TS-17,-18>" and "відмінностей_у_полях" not in renamed
    one = sum3d_strategy.diff_report(a, pa, b, pb, mask, op=2)
    assert one["відмінностей"] == 1 and one["полів"][0] == one["полів"][1]
    with pytest.raises(sum3d_strategy.StrategyError):
        sum3d_strategy.diff_report(a, pa, b, pb, mask, op=4)


@pytest.fixture
def listed(tmp_path, monkeypatch):
    first = tmp_path / "Zirkon V14.cfg"
    second = tmp_path / "Zirkon V14 copy.cfg"
    project = tmp_path / "job.cam"
    first.write_bytes(strategy_file(OPS))
    second.write_bytes(strategy_file(OPS[:3]))
    project.write_bytes(strategy_file(OPS))
    monkeypatch.setattr(sum3d_explore, "_last", [str(first), str(second), str(project)])
    return first, second, project


def _run(**args):
    return TOOLS_BY_NAME["kmill_sum3d_strategy"].run(None, args)


def test_tool_lists_reads_and_compares_without_touching_files(listed):
    before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in listed]
    ops = _run(file=1)
    assert ops["файл"] == "Zirkon V14.cfg" and ops["будова"]["операцій"] == 4
    one = _run(what="op", file=1, op=2)
    assert one["поля"][1] == {"№": 1, "текст": "YES"} and one["тип_обробки"] == "POCKETING"
    diff = _run(what="diff", file=1, other=2)
    assert diff["однакових"] == 3 and len(diff["лише_в_першому"]) == 1
    assert [(p.stat().st_mtime_ns, p.read_bytes()) for p in listed] == before, "нічого не змінено"


def test_tool_refuses_project_files_and_bad_arguments(listed):
    with pytest.raises(ToolError, match="cfg"):
        _run(file=3)
    with pytest.raises(ToolError):
        _run()
    with pytest.raises(ToolError):
        _run(what="op", file=1)
    with pytest.raises(ToolError):
        _run(what="diff", file=1)
    with pytest.raises(ToolError):
        _run(what="write", file=1)
    with pytest.raises(ToolError):
        _run(what="op", file=1, op=99)


def test_reader_has_no_way_to_write_or_run_anything():
    source = Path(sum3d_strategy.__file__).read_text(encoding="utf-8")
    for word in ("subprocess", "os.remove", "os.replace", "os.rename", "shutil", ".write(", "write_bytes",
                 "write_text", "os.system", "Popen", '"wb"', '"ab"', '"w"', "'wb'", "'w'"):
        assert word not in source, f"у читачі стратегій не має бути «{word}»"
