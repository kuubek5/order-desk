# Збирає мокап «Графічна шапка видачі» — 3 варіанти на вибір власника.
# Тимчасовий генератор; фони — design/kuubmill_handout-*_20260830.png.
import pathlib

scratch = pathlib.Path(
    r"C:/Users/1/AppData/Local/Temp/claude/P--AI-Projects-CRM-Laba"
    r"/1f14467c-bcf8-4e0b-906d-e1cecb31030b/scratchpad"
)
b64 = {k: pathlib.Path(f"design/_h_{k}.b64").read_text() for k in "abc"}

head = """<title>Графічна шапка видачі</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap">
<style>
:root{--bg:#14100a;--line:#2c2214;--line2:#45361d;--ink:#f2e8d8;--ink2:#a89578;--ink3:#a08c68;
--acc:#ffb454;--accB:#ffc670;--accC:#ffd894;
--font:'Golos Text',sans-serif;--mono:'JetBrains Mono',monospace}
*{box-sizing:border-box}
body{margin:0;padding:34px 26px 64px;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.55;
background-image:linear-gradient(rgba(255,180,84,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(255,180,84,.04) 1px,transparent 1px);background-size:64px 64px}
.wrap{max-width:1160px;margin:0 auto}
h1{font-size:29px;font-weight:700;margin:0 0 8px;letter-spacing:-.015em}
.lede{color:var(--ink2);margin:0 0 30px;max-width:74ch}
.vh{display:flex;align-items:baseline;gap:9px;margin:34px 0 4px}
.vk{font-family:var(--mono);font-size:12px;color:var(--acc)}
.vn{font-size:17px;font-weight:600}
.vw{color:var(--ink2);font-size:13px;margin:0 0 12px;max-width:74ch}
.hbar{position:relative;border:1px solid var(--line2);border-radius:12px;overflow:hidden;isolation:isolate;
background-color:#14100a;background-size:cover;background-position:center 42%}
.hbar::before{content:"";position:absolute;inset:0;z-index:0;
background:linear-gradient(100deg,rgba(20,16,10,.45) 0%,rgba(20,16,10,.8) 46%,rgba(20,16,10,.93) 100%)}
.hin{position:relative;z-index:1;display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding:20px 22px}
.ht{font-size:21px;font-weight:700;letter-spacing:-.01em;text-shadow:0 1px 8px rgba(0,0,0,.6)}
.chips{display:flex;gap:6px;align-items:center}
.chip{font-family:var(--mono);font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--line2);
color:var(--ink2);background:rgba(20,16,10,.55);backdrop-filter:blur(2px)}
.chip.on{color:#14100a;background:var(--acc);border-color:var(--acc);font-weight:700}
.kpi{margin-left:auto;display:flex;gap:22px;align-items:baseline}
.kv{font-family:var(--mono);font-size:24px;font-weight:700;color:var(--accC);text-shadow:0 0 14px rgba(255,180,84,.35)}
.kv small{font-size:12px;font-weight:400;color:var(--ink2);font-family:var(--font);margin-left:6px}
.spine{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(255,180,84,.14);z-index:1}
.spine i{display:block;height:100%;width:34%;background:var(--acc);box-shadow:0 0 10px rgba(255,180,84,.6)}
.note{margin-top:38px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink3);font-size:13px;max-width:76ch}
</style>
<div class="wrap">
<h1>Графічна шапка видачі</h1>
<p class="lede">Три трактування шапки екрана «Ранкова видача» — тим самим прийомом, що
плитки печей: згенерований фон, поверх — живі контроли й лічильник дня
(<b>клієнти</b> та <b>роботи</b>), знизу — тонка лінія прогресу. Затемнення густішає
праворуч, де стоять числа.</p>
"""

BAR = """<div class="vh"><span class="vk">{kind}</span><span class="vn">{name}</span></div>
<p class="vw">{why}</p>
<div class="hbar" style="background-image:url(data:image/jpeg;base64,{b}){extra}">
  <div class="hin">
    <span class="ht">Ранкова видача</span>
    <span class="chips"><span class="chip">‹</span><span class="chip">28.08</span><span class="chip on">29.08</span><span class="chip">усі</span><span class="chip">›</span></span>
    <span class="kpi">
      <span class="kv">7<small>/ 20 клієнтів</small></span>
      <span class="kv">18<small>/ 52 роботи</small></span>
    </span>
  </div>
  <div class="spine"><i></i></div>
</div>"""


body = head
body += BAR.format(kind="A", name="Холодний лоток",
                   b=b64["a"], extra=";background-position:center 78%",
                   why="Спечені молочно-білі коронки на темному лотку — рівно те, що оператор шукає очима на видачі. Холодне ранкове світло, спокійний тон.")
body += BAR.format(kind="B", name="Пакетики",
                   b=b64["b"], extra=";background-position:center 55%",
                   why="Підписані пакетики на верстаті біля вікна — кінець шляху роботи, момент передачі логісту. Тепле ранкове світло.")
body += BAR.format(kind="C", name="Світанок цеху",
                   b=b64["c"], extra=";background-position:center 30%",
                   why="Широкий кадр цеху на світанку: полиці з роботами, перше сонце крізь вікно, пил у промені. Найатмосферніший, найменш предметний.")
body += """<p class="note">Спільне: числа дня (клієнти/роботи) — головний акцент праворуч, під ними
нічого не мигає; фон статичний (це шапка робочого екрана, не віджет стану — анімації тут
не мають що сигналізувати); лінія прогресу знизу лишається. Обраний варіант перегенерую
на якісній моделі й вбудую в реальний екран.</p>
</div>"""

target = scratch / "handout-header-graphic.html"
target.write_text(body, encoding="utf-8")
print(target, len(body) // 1024, "КБ")
