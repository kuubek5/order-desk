"""Архів фрез Sum3D (`sum3d_tools`) і його зв'язок зі стратегією. Лише читання.

Стратегія нового стилю називає фрезу й набір параметрів, а оберти, подачі й
кроки лежать у `Tools.arl`; назви полів тіла операції — у `params_codes.txt`.
Файли тут складені за будовою справжніх.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services import sum3d_explore, sum3d_strategy, sum3d_tools
from app.services.machine_files import mask
from app.services.mcp_common import ToolError
from app.services.mcp_tools import TOOLS_BY_NAME
from tests.test_sum3d_strategy import body, num, strategy_file, text

TOLIK = "CORiTEC 250i PRO+_Tolik"


def cutter(ident: int, *, machine: str = TOLIK, group: str = "ZR", kind: int = 2, code: str = "",
           description: str = "1mm ball", number: int = 3, sets: tuple[tuple[str, dict], ...] = ()) -> str:
    lines = ["<TOOLSTART>", f"  ID={ident}", f"  Machine={machine}", f"  ToolGroup={group}", f"  ToolType={kind}",
             "  Diameter=1", "  Length=14", f"  ToolNumber={number}", f"  Description={description}"]
    if code:
        lines.append(f"  StrategyCode={code}")
    for name, params in sets:
        lines += ["  <TOOLSTARTPARMAT>", f"    MaterialName={name}", f"    Enabled={params.get('Enabled', 1)}"]
        lines += [f"    {key}={value}" for key, value in params.items() if key != "Enabled"]
        lines.append("  <TOOLENDPARMAT>")
    return "\r\n".join(lines + ["<TOOLEND>"])


ROUGH = {"RotationSpeed": 28000, "FeedXY": 1800, "FeedZ": 1400, "MillMaxStepXY": 45, "MillMaxStepZ": 0.8}
FINE = {"RotationSpeed": 35000, "FeedXY": 1500, "FeedZ": 1500, "MillMaxStepXY": 0.04, "MillMaxStepZ": 0.04}
ARCHIVE = "\r\n".join([
    "R4SUM_TOOL 1 4",
    cutter(1, code="D2B-?", description="Coated (ZDB-100D)", sets=(("Roughing", ROUGH), ("Finishing_3d", FINE))),
    cutter(2, code="D2B-?", group="PMMA", description="Uncoated", sets=(("Roughing", {**ROUGH, "FeedXY": 2400}),)),
    cutter(3, code="D2B-?", machine="CORiTEC 150i PRO-Olejka", sets=(("Roughing", {**ROUGH, "FeedXY": 1200}),)),
    cutter(4, kind=5, description="0.6mm radius (conical)/1D06C-zr", number=15,
           sets=(("finish", FINE), ("drill", {**FINE, "Enabled": 0}))),
    cutter(5, code="D06B-?", sets=(("Anatomical_Restmill", FINE), ("Anatomical_Restmill", {**FINE, "FeedXY": 700}))),
    cutter(6, code="D1B-?", description="Фреза 1мм", sets=()),
]) + "\r\n"


def test_archive_is_parsed_with_codes_shapes_and_sets():
    cutters = sum3d_tools.parse(ARCHIVE)
    assert [c.code for c in cutters] == ["D2B-?", "D2B-?", "D2B-?", "1D06C-zr", "D06B-?", "D1B-?"]
    report = sum3d_tools.tools_report(cutters, mask, machine="tolik", group="zr")
    assert report["фрез_у_файлі"] == 6 and report["знайдено"] == 4
    assert report["верстати"] == {TOLIK: 5, "CORiTEC 150i PRO-Olejka": 1} and report["групи"] == {"ZR": 4, "PMMA": 1}
    conical = next(row for row in report["фрези"] if row["№"] == 4)
    assert conical["форма"] == "конічна" and conical["гніздо"] == 15 and conical["наборів"] == 2
    assert conical["вимкнених"] == 1
    assert "Фреза" not in json.dumps(report, ensure_ascii=False), "літери поза латиницею затерто"
    one = sum3d_tools.tool_report(cutters, 1, mask)
    assert one["набори"][0] == {"набір": "Roughing", **ROUGH} and one["поля"]["StrategyCode"] == "D2B-?"
    assert sum3d_tools.tools_report(cutters, mask, query="conical")["знайдено"] == 1


def test_not_an_archive_is_refused():
    with pytest.raises(sum3d_tools.ToolsError):
        sum3d_tools.parse("PK\x03\x04 not a tool archive")
    with pytest.raises(sum3d_tools.ToolsError):
        sum3d_tools.tool_report(sum3d_tools.parse(ARCHIVE), 99, mask)


def test_link_finds_parameters_by_tool_code_and_set_name():
    cutters = sum3d_tools.parse(ARCHIVE)
    link = sum3d_tools.link_report(cutters, mask, code="d2b-?", set_name="Roughing", machine=TOLIK)
    assert link["фрез_з_таким_кодом"] == 2 and [row["FeedXY"] for row in link["рядки"]] == [1800, 2400]
    narrowed = sum3d_tools.link_report(cutters, mask, code="D2B-?", set_name="Roughing", machine=TOLIK, group="ZR")
    assert narrowed["рядків"] == 1 and narrowed["рядки"][0]["RotationSpeed"] == 28000
    missing = sum3d_tools.link_report(cutters, mask, code="D2B-?", set_name="Engraving", machine=TOLIK)
    assert missing["рядків"] == 0 and missing["фрез_без_цього_набору"] == 2
    by_text = sum3d_tools.link_report(cutters, mask, description="0.6mm radius (conical)/1D06C-zr")
    assert [row["набір"] for row in by_text["рядки"]] == ["finish", "drill"] and by_text["рядки"][1]["вимкнено"]


def test_link_keeps_both_same_named_sets_and_tolerates_a_suffix():
    """`<T:/D06B-?/Anatomical_Restmill,OPT>`: набір шукається й без допису після коми."""
    link = sum3d_tools.link_report(sum3d_tools.parse(ARCHIVE), mask, code="D06B-?", set_name="Anatomical_Restmill,OPT")
    assert [(row["FeedXY"], row["однойменний"]) for row in link["рядки"]] == [(1500, "1 з 2"), (700, "2 з 2")]
    assert "увага" in link


def test_diff_reports_only_real_changes():
    old = sum3d_tools.parse(ARCHIVE)
    new = copy.deepcopy(old[1:])                       # фрезу 1 прибрано
    new[3].sets[1]["FeedXY"] = "650"                   # другий з однойменних наборів
    new[2].fields["Length"] = "30"
    del new[2].sets[1]
    new[0].sets.append({"MaterialName": "Engraving", "Enabled": "1", "FeedXY": "300"})
    report = sum3d_tools.diff_report(old, new, mask)
    assert report["фрез"] == [6, 5] and report["прибрано"] == 1 and report["додано"] == 0 and report["змінено"] == 3
    assert report["прибрані"][0]["№"] == 1
    rows = {row["№"]: row for row in report["змінені"]}
    assert rows[5]["набори"] == [{"набір": "Anatomical_Restmill", "ключ": "FeedXY", "було": 700, "стало": 650}]
    assert rows[4]["поля"] == [{"ключ": "Length", "було": 14, "стало": 30}]
    assert rows[4]["набори"] == [{"набір": "drill", "дія": "прибрано"}]
    assert rows[2]["набори"] == [{"набір": "Engraving", "дія": "додано"}]
    assert sum3d_tools.diff_report(old, copy.deepcopy(old), mask)["змінено"] == 0


CODES = """Group:0
// Machining name and type
  210=Machining name
  19=Machining Type
  12=Oversize
// Parameters for "POCKETING":
Group:2
  21=Z step increment
// Parameters for "DRILLING"
Group:12
  305=Operation
"""


def test_vendor_codes_name_the_fields_and_own_keeps_the_relevant_ones():
    names = sum3d_strategy.parse_codes(CODES)
    assert names[19] == ("Machining Type", frozenset({0})) and names[21][1] == frozenset({2})
    payload = body(*[num(0.0)] * 11, num(0.5), *[num(0.0)] * 6, text("POCKETING"), num(0.0), num(0.8))
    items, _tail = sum3d_strategy.fields(payload)
    rows = sum3d_strategy.field_rows(items, mask, names)
    assert {"№": 12, "назва": "Oversize", "число": 0.5} in rows
    assert {"№": 19, "назва": "Machining Type", "текст": "POCKETING"} in rows
    assert {"№": 21, "назва": "Z step increment", "число": 0.8} in rows and rows[0] == {"№": 0, "текст": "TECN 2.0"}
    own = sum3d_strategy.field_rows(items, mask, names, kind=2, own=True)
    assert [row["№"] for row in own] == [12, 19, 21]
    drilling = sum3d_strategy.field_rows(items, mask, names, kind=12, own=True)
    assert [row["№"] for row in drilling] == [12, 19], "поле чужого типу обробки не показано"
    assert sum3d_strategy.field_text(payload, 19) == "POCKETING" and sum3d_strategy.field_text(payload, 12) is None


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """Встановлення: архів фрез і назви полів у корені, стратегія — у теці верстата."""
    folder = tmp_path / "Dental" / "CAM-system" / "ST" / TOLIK
    folder.mkdir(parents=True)
    new_style = body(*[num(0.0)] * 11, num(0.08), *[num(0.0)] * 6, text("POCKETING"))
    old_style = body(*[num(0.0)] * 310, text("0.6mm radius (conical)/1D06C-zr"))
    strategy = folder / "Zirkon V14.cfg"
    strategy.write_bytes(strategy_file([
        ("949<TS13><T:/D2B-?/Roughing><MO+0.5>", 2, new_style),
        ("Finish06<# Fissure fine with 0.6mm Tool /$3>", 6, old_style),
        ("__________Basis___<TS99>", 14, body(text("WIRE EROSION"))),
    ]))
    archive = tmp_path / "Tools.arl"
    archive.write_bytes(ARCHIVE.encode("cp1251"))
    older = tmp_path / "Tools.arl.250908122720"
    older.write_bytes(ARCHIVE.replace("FeedXY=1800", "FeedXY=1600").encode("cp1251"))
    (tmp_path / "params_codes.txt").write_text(CODES, encoding="cp1252")
    monkeypatch.setattr(sum3d_explore, "_last", [str(strategy), str(archive), str(older)])
    return strategy, archive, older


def _run(**args):
    return TOOLS_BY_NAME["kmill_sum3d_strategy"].run(None, args)


def test_op_shows_what_the_cutter_will_do_on_this_machine(shop):
    before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in shop]
    report = _run(what="op", file=1, op=1, own=True)
    assert report["назви_полів"] == "з params_codes.txt"
    assert report["поля"] == [{"№": 12, "назва": "Oversize", "число": 0.08},
                              {"№": 19, "назва": "Machining Type", "текст": "POCKETING"},
                              {"№": 21, "назва": "Z step increment", "число": 0}]
    link =report["архів_фрез"]
    assert link["верстат"] == TOLIK and link["за"] == "кодом фрези", "верстат узято з назви теки стратегії"
    assert [(row["група"], row["FeedXY"]) for row in link["рядки"]] == [("ZR", 1800), ("PMMA", 2400)]
    assert _run(what="op", file=1, op=1, group="PMMA")["архів_фрез"]["рядків"] == 1
    assert _run(what="op", file=1, op=1, machine="Olejka")["архів_фрез"]["рядки"][0]["FeedXY"] == 1200
    old = _run(what="op", file=1, op=2)["архів_фрез"]
    assert old["за"] == "описом фрези" and [row["набір"] for row in old["рядки"]] == ["finish", "drill"]
    assert _run(what="op", file=1, op=3)["архів_фрез"] == "операція не називає фрезу"
    assert [(p.stat().st_mtime_ns, p.read_bytes()) for p in shop] == before, "нічого не змінено"


def test_op_without_archive_and_codes_still_answers(shop):
    _strategy, archive, _older = shop
    archive.unlink()
    (archive.parent / "params_codes.txt").unlink()
    report = _run(what="op", file=1, op=1)
    assert report["назви_полів"].startswith("немає") and isinstance(report["архів_фрез"], str)
    assert {"№": 12, "число": 0.08} in report["поля"]


def test_tool_reads_and_compares_archives(shop):
    listing = _run(what="tools", file=2, group="ZR", machine="Tolik")
    assert listing["файл"] == "Tools.arl" and listing["знайдено"] == 4
    assert _run(what="tool", file=2, tool=4)["набори"][1]["вимкнено"] is True
    diff = _run(what="tooldiff", file=3, other=2)
    assert diff["змінено"] == 1 and diff["змінені"][0]["набори"] == [
        {"набір": "Roughing", "ключ": "FeedXY", "було": 1600, "стало": 1800}]


def test_tool_refuses_wrong_files_and_arguments(shop):
    with pytest.raises(ToolError, match="arl"):
        _run(what="tools", file=1)
    with pytest.raises(ToolError, match="cfg"):
        _run(what="ops", file=2)
    with pytest.raises(ToolError):
        _run(what="tool", file=2)
    with pytest.raises(ToolError):
        _run(what="tool", file=2, tool=99)
    with pytest.raises(ToolError):
        _run(what="tooldiff", file=2)


def test_archive_reader_has_no_way_to_write_or_run_anything():
    source = Path(sum3d_tools.__file__).read_text(encoding="utf-8")
    for word in ("subprocess", "os.remove", "os.replace", "os.rename", "shutil", ".write(", "write_bytes",
                 "write_text", "os.system", "Popen", '"wb"', '"ab"', '"w"', "'wb'", "'w'"):
        assert word not in source, f"у читачі архіву фрез не має бути «{word}»"
