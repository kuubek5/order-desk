# -*- coding: utf-8 -*-
"""Розрізати маску «зупинений шпиндель» на два рухомі шари.

Перша версія різала прямокутним стовпцем — і забрала шпинделю шматки дуг
тримача, що проходять ЗА фрезою. На екрані вони їхали разом із нею, наче
з фрези стирчать горизонтальні вуса. Тут поділ по РУНАХ: у смузі кінчика
шпинделю дістаються лише вузькі, приблизно вертикальні штрихи самої фрези,
а все широке (дуги кільця й диска) лишається базі.
"""
from PIL import Image
import numpy as np, base64, io, json, pathlib

SRC = pathlib.Path(r"P:\AI-Projects\CRM_Laba\design\mask_d2-spindle.png")
OUT = SRC.parent
SP = pathlib.Path(r"C:\Users\1\AppData\Local\Temp\claude\P--AI-Projects-CRM-Laba\1f14467c-bcf8-4e0b-906d-e1cecb31030b\scratchpad")

SPLIT = 166       # рядок, де починається кільце тримача
TIP_BOT = 205     # докуди тягнеться кінчик фрези
MAX_RUN = 8       # ширший горизонтальний пробіг — це вже дуга, не фреза
NEAR = 7          # наскільки пробіг може відходити від осі фрези

a = np.array(Image.open(SRC).split()[-1]).astype(np.uint8)
H, W = a.shape
lit = a > 24

band = lit[140:164]
cols = band.sum(axis=0)
cx = int(np.argmax(np.convolve(cols, np.ones(9), "same")))

spin = np.zeros_like(a)
base = np.zeros_like(a)
spin[:SPLIT] = a[:SPLIT]
base[SPLIT:] = a[SPLIT:]

moved = kept = 0
for y in range(SPLIT, TIP_BOT):
    row = lit[y]
    x = 0
    while x < W:
        if not row[x]:
            x += 1
            continue
        x0 = x
        while x < W and row[x]:
            x += 1
        x1 = x                      # [x0, x1)
        width = x1 - x0
        centre = (x0 + x1) / 2
        is_burr = width <= MAX_RUN and abs(centre - cx) <= NEAR
        if is_burr:
            spin[y, x0:x1] = a[y, x0:x1]
            base[y, x0:x1] = 0
            moved += width
        else:
            kept += width

print(f"вісь фрези x={cx}; у шар шпинделя пішло {moved} пікселів, "
      f"дугам лишилось {kept}")

out = {}
for name, arr in (("spindle", spin), ("base", base)):
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    img.putalpha(Image.fromarray(arr))
    img.save(OUT / f"mask_spindle-{name}.png")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    out[name] = base64.b64encode(buf.getvalue()).decode()
    print(name, int((arr > 24).sum()), "пікселів |", len(out[name]) // 1024, "КБ b64")

(SP / "spindle_layers.json").write_text(json.dumps(out), encoding="utf-8")

# контрольний кадр: сам шар шпинделя, збільшено
insp = Image.open(OUT / "mask_spindle-spindle.png").crop((150, 80, 270, 215))
insp = insp.resize((480, 540), Image.NEAREST)
bg = Image.new("RGB", insp.size, (0, 0, 0))
bg.paste(insp, (0, 0), insp)
bg.save(OUT / "_inspect_burr.png")
print("контрольний кадр: design/_inspect_burr.png")
