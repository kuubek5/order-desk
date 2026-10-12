"""«Sum3D усім» у формі «Додати» (queue.js): поведінка в node + наявність поля.

queue.js — глобальний скрипт без модулів, тож беремо з нього лише дві
функції і проганяємо їх на мінімальному підробному DOM.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
QUEUE_JS = ROOT / "app" / "static" / "js" / "queue.js"

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function grab(name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\) \\{[\\s\\S]*?\\r?\\n\\}\\r?\\n"));
  if (!m) throw new Error("no " + name);
  return m[0];
}
eval(grab("applyAddworkSum3dAll") + grab("refreshAddworkRows"));

function mkInput(v) { return { value: v || "", dataset: {}, hidden: false }; }
function mkRow() {
  const s3 = mkInput(), rm = { hidden: false };
  return { s3, rm, querySelector(q) {
    if (q === '[name="sum3d_id"]') return s3;
    if (q === "[data-addwork-removerow]") return rm;
    return null;
  } };
}
const all = { hidden: true }, allInput = mkInput();
let rows = [mkRow()];
const form = { querySelectorAll: () => rows, querySelector(q) {
  if (q === "[data-addwork-s3all]") return all;
  if (q === "[data-addwork-s3all-input]") return allInput;
  return null;
} };
const out = {};
refreshAddworkRows(form); out.hidden1 = all.hidden;
rows.push(mkRow()); rows.push(mkRow());
rows[1].s3.value = "09-00-00";            // вписано руками
refreshAddworkRows(form); out.shown3 = !all.hidden;
allInput.value = "12-01-45"; applyAddworkSum3dAll(form);
out.filled = rows.map((r) => r.s3.value);
allInput.value = "13-00-00"; applyAddworkSum3dAll(form);   // перезаписує лише «з усіх»
out.refilled = rows.map((r) => r.s3.value);
delete rows[2].s3.dataset.fromAll; rows[2].s3.value = "07-07-07";  // правка руками
allInput.value = "14-00-00"; applyAddworkSum3dAll(form);
out.afterManual = rows.map((r) => r.s3.value);
rows.push(mkRow()); refreshAddworkRows(form);              // новий рядок бере поточне
out.newRow = rows[3].s3.value;
rows = [rows[0]]; refreshAddworkRows(form);
out.hiddenBack = all.hidden; out.allCleared = allInput.value; out.keptRow = rows[0].s3.value;
console.log(JSON.stringify(out));
"""


def _run(tmp_path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не знайдено")
    script = tmp_path / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    res = subprocess.run(
        [node, str(script), str(QUEUE_JS)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_sum3d_all_behaviour(tmp_path):
    out = _run(tmp_path)
    assert out["hidden1"] is True
    assert out["shown3"] is True
    assert out["filled"] == ["12-01-45", "09-00-00", "12-01-45"]
    assert out["refilled"] == ["13-00-00", "09-00-00", "13-00-00"]
    assert out["afterManual"] == ["14-00-00", "09-00-00", "07-07-07"]
    assert out["newRow"] == "14-00-00"
    assert out["hiddenBack"] is True
    assert out["allCleared"] == ""
    assert out["keptRow"] == "14-00-00"


def test_template_has_unnamed_sum3d_all_field():
    html = (ROOT / "app" / "templates" / "queue.html").read_text(encoding="utf-8")
    m = re.search(r"<label[^>]*data-addwork-s3all[^>]*>.*?</label>", html, re.S)
    assert m, "у формі «Додати» немає поля «Sum3D усім»"
    block = m.group(0)
    assert "hidden" in block.split(">")[0], "поле має бути сховане, доки рядок один"
    assert "data-addwork-s3all-input" in block
    assert "name=" not in block, "поле не має їхати в payload"
    assert "Sum3D усім" in block
    assert html.index("data-addwork-addrow") < html.index("data-addwork-s3all") < html.index("class=\"addwork-submit apex\"")
