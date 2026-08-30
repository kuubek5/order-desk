# Збирає мокап «Графічний віджет печей» — 4 варіанти на вибір власника.
# Тимчасовий генератор; фони — design/kuubmill_furnwidget-*_20260830.png.
import pathlib

scratch = pathlib.Path(
    r"C:/Users/1/AppData/Local/Temp/claude/P--AI-Projects-CRM-Laba"
    r"/1f14467c-bcf8-4e0b-906d-e1cecb31030b/scratchpad"
)
b64 = {k: pathlib.Path(f"design/_w_{k}.b64").read_text() for k in "abcd"}

head = """<title>Графічний віджет печей</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap">
<style>
:root{--bg:#14100a;--line:#2c2214;--line2:#45361d;--ink:#f2e8d8;--ink2:#a89578;--ink3:#a08c68;
--acc:#ffb454;--accB:#ffc670;--accC:#ffd894;--alarmInk:#ffc4b6;
--font:'Golos Text',sans-serif;--mono:'JetBrains Mono',monospace}
*{box-sizing:border-box}
body{margin:0;padding:34px 26px 64px;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.55;
background-image:linear-gradient(rgba(255,180,84,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(255,180,84,.04) 1px,transparent 1px);background-size:64px 64px}
.wrap{max-width:1160px;margin:0 auto}
h1{font-size:29px;font-weight:700;margin:0 0 8px;letter-spacing:-.015em}
.lede{color:var(--ink2);margin:0 0 28px;max-width:72ch}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:26px}
.vh{display:flex;align-items:baseline;gap:9px;margin:0 0 4px}
.vk{font-family:var(--mono);font-size:12px;color:var(--acc)}
.vn{font-size:17px;font-weight:600}
.vw{color:var(--ink2);font-size:13px;margin:0 0 12px;min-height:60px}
.panel{width:300px;background:#0f0b06;border:1px solid var(--line);border-radius:12px;padding:8px;margin:0 auto}
.shead{display:flex;align-items:center;gap:8px;font-size:13.5px;font-weight:500;padding:4px 6px 10px}
.scount{margin-left:auto;font-family:var(--mono);font-size:11px;padding:1px 7px;border-radius:999px;background:#221a0e;color:var(--ink2)}
.tile{position:relative;border-radius:10px;overflow:hidden;border:1px solid var(--line2);margin-bottom:8px;min-height:118px;
background-size:cover;background-position:center;isolation:isolate}
.tile::before{content:"";position:absolute;inset:0;background:linear-gradient(100deg,rgba(20,16,10,.15) 0%,rgba(20,16,10,.82) 62%,rgba(20,16,10,.95) 100%);z-index:0}
.tin{position:relative;z-index:1;display:flex;flex-direction:column;gap:2px;padding:12px 14px;min-height:118px}
.tname{font-size:12px;color:var(--ink2);letter-spacing:.05em;text-transform:uppercase}
.ttemp{font-family:var(--mono);font-size:34px;font-weight:700;color:var(--accC);line-height:1.05;text-shadow:0 0 18px rgba(255,180,84,.45)}
.ttemp small{font-size:16px;font-weight:400;color:var(--ink2)}
.tfoot{margin-top:auto;font-size:12px;color:var(--ink2)}
.tfoot b{font-family:var(--mono);color:var(--ink)}
.pill{position:absolute;top:10px;right:10px;z-index:2;font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;padding:3px 8px;border-radius:999px}
.pill.run{color:#0f0b06;background:var(--acc);box-shadow:0 0 14px rgba(255,180,84,.55)}
.pill.wait{color:var(--ink2);background:rgba(34,26,14,.85);border:1px solid var(--line2)}
.pill.err{color:var(--alarmInk);background:rgba(255,107,82,.14);border:1px solid rgba(255,107,82,.45)}
.tile.wait{filter:saturate(.35) brightness(.75)}
.tile.wait .ttemp{color:var(--ink2);text-shadow:none}
.tile.err{border-color:rgba(255,107,82,.5)}
.tile.err::before{background:linear-gradient(100deg,rgba(40,12,8,.55),rgba(20,16,10,.95) 70%)}
.terr{font-size:12px;color:var(--alarmInk)}
.vA .tile.run::after{content:"";position:absolute;inset:0;z-index:0;background:radial-gradient(60% 80% at 26% 55%,rgba(255,150,60,.35),transparent 70%);animation:breath 3.2s ease-in-out infinite}
@keyframes breath{50%{opacity:.35}}
.spark{position:absolute;width:3px;height:3px;border-radius:50%;background:var(--accB);z-index:1;bottom:18px;filter:blur(.4px);animation:fly 3.6s linear infinite;opacity:0}
@keyframes fly{0%{transform:translateY(0);opacity:0}12%{opacity:.9}100%{transform:translateY(-86px) translateX(14px);opacity:0}}
.vC .tile.run::after{content:"";position:absolute;inset:0;z-index:0;background:linear-gradient(180deg,transparent 46%,rgba(255,198,112,.14) 50%,transparent 54%);background-size:100% 300%;animation:scan 4.5s linear infinite}
@keyframes scan{from{background-position:0 130%}to{background-position:0 -30%}}
.vD .tile{background-size:135%}
.vD .tile.run{animation:drift 26s ease-in-out infinite alternate}
@keyframes drift{from{background-position:12% 40%}to{background-position:80% 62%}}
.arc{position:absolute;right:12px;bottom:10px;z-index:2}
.arc circle{fill:none;stroke-width:3}
.arc .bgc{stroke:rgba(255,180,84,.15)}
.arc .fgc{stroke:var(--accB);stroke-linecap:round;stroke-dasharray:88;stroke-dashoffset:26;filter:drop-shadow(0 0 4px rgba(255,180,84,.6))}
.note{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink3);font-size:13px;max-width:76ch}
@media (prefers-reduced-motion:reduce){.tile,.tile::after,.spark{animation:none!important}}
</style>
<div class="wrap">
<h1>Графічний віджет печей</h1>
<p class="lede">Чотири трактування секції «Пічки» в правій панелі черги. Фони згенеровано в палітрі
Amber Forge; числа — живий шар поверх, як у застосунку. У кожному варіанті три стани:
<b>працює</b> (анімований), <b>вільна</b> (притишена) і <b>збій</b> із причиною.</p>
<div class="grid">
"""

TILE = """<section class="v{kind}">
<div class="vh"><span class="vk">{kind}</span><span class="vn">{name}</span></div>
<p class="vw">{why}</p>
<div class="panel">
  <div class="shead">Пічки <span class="scount">1 у роботі</span></div>
  <div class="tile run" style="background-image:url(data:image/jpeg;base64,{b})">
    <span class="pill run">RUN</span>{sparks}{arc}
    <div class="tin">
      <span class="tname">Бочка</span>
      <span class="ttemp">1462<small>°C</small></span>
      <span class="tfoot">ще <b>2:41:07</b> · відкриється <b>01:17</b></span>
    </div>
  </div>
  <div class="tile wait" style="background-image:url(data:image/jpeg;base64,{b})">
    <span class="pill wait">WAIT</span>
    <div class="tin">
      <span class="tname">Друга</span>
      <span class="ttemp">93<small>°C</small></span>
      <span class="tfoot">вільна</span>
    </div>
  </div>
  <div class="tile err" style="background-image:url(data:image/jpeg;base64,{b})">
    <span class="pill err">ЗБІЙ</span>
    <div class="tin">
      <span class="tname">Третя</span>
      <span class="ttemp" style="color:var(--ink3);text-shadow:none">—<small>°C</small></span>
      <span class="terr">не відповіла за 20 с</span>
    </div>
  </div>
</div>
</section>"""


def panel(kind, key, name, why):
    sparks = ""
    if key == "b":
        sparks = "".join(
            f'<i class="spark" style="left:{18 + i * 13}%;animation-delay:-{i * 0.7}s"></i>'
            for i in range(6)
        )
    arc = ""
    if key == "d":
        arc = (
            '<svg class="arc" width="34" height="34">'
            '<circle class="bgc" cx="17" cy="17" r="14"></circle>'
            '<circle class="fgc" cx="17" cy="17" r="14" transform="rotate(-90 17 17)"></circle>'
            "</svg>"
        )
    return TILE.format(kind=kind, name=name, why=why, b=b64[key], sparks=sparks, arc=arc)


body = head
body += panel("A", "a", "Ілюмінатор",
              "Справжнє вікно печі: зарево дихає, коли йде програма. Найбуквальніший варіант — видно саму піч.")
body += panel("B", "b", "Жар коронок",
              "Розжарені коронки на лотку — те, що реально всередині печі. Іскри летять лише у стані RUN.")
body += panel("C", "c", "Креслення",
              "Технічний розріз печі тонким бурштиновим штрихом — перегук із блюпринт-сіткою застосунку. Скан-лінія проходить, коли піч працює.")
body += panel("D", "d", "Терморіка",
              "Абстрактна плазма тече за плиткою; дуга праворуч — залишок програми. Найдинамічніший і найменш буквальний.")
body += """</div>
<p class="note">Спільне: температура — головне число; статус — капсула в куті; «вільна» плитка
притишена; збій називає причину і фарбується тривожним. Фон завжди під шаром градієнта, щоб
числа читались на будь-якій ділянці арту. Анімації живуть лише на стані «працює» і вимикаються
prefers-reduced-motion.</p>
</div>"""

target = scratch / "furnace-widget-graphic.html"
target.write_text(body, encoding="utf-8")
print(target, len(body) // 1024, "КБ")
