# -*- coding: utf-8 -*-
"""Маски для фону сторінки входу + прев'ю-галерея.

Кадри з kie.ai приходять як біла графіка на чорному, але модель час від часу
інвертує кадр сама. Тому яскравість тут не довіряється промпту, а міряється:
світлий кадр перевертається. Далі яскравість стає альфою — і PNG можна
фарбувати акцентом теми через mask-image.
"""
from PIL import Image, ImageOps
import numpy as np, pathlib, base64, io, json

D = pathlib.Path(r"P:\AI-Projects\CRM_Laba\design")
IMG = pathlib.Path(r"P:\AI-Projects\CRM_Laba\app\static\img")
SP = pathlib.Path(r"C:\Users\1\AppData\Local\Temp\claude\P--AI-Projects-CRM-Laba\1f14467c-bcf8-4e0b-906d-e1cecb31030b\scratchpad")

SRC = {
    "disc":     "login_disc-top_20260830-1240.png",
    "milling":  "login_milling-angled_20260830-1245.png",
    "abutment": "login_abutments_20260830-1250.png",
    "furnace":  "login_furnace-trays_20260830-1300.png",
}
SIZE = 512

b64 = {}
for key, name in SRC.items():
    im = Image.open(D / name).convert("L")
    inverted = False
    if np.array(im).mean() > 127:          # світлий кадр = модель перевернула
        im = ImageOps.invert(im)
        inverted = True
    a = np.array(im).astype(np.float32)
    lo, hi = a.min(), a.max()
    if hi > lo:
        a = (a - lo) * 255.0 / (hi - lo)
    a[a < 26] = 0                          # прибрати сірий шум фону
    im = Image.fromarray(a.astype(np.uint8))

    bbox = im.point(lambda p: 255 if p > 20 else 0).getbbox()
    if bbox:
        im = im.crop(bbox)
    im.thumbnail((SIZE, SIZE), Image.LANCZOS)
    canvas = Image.new("L", (SIZE, SIZE), 0)
    canvas.paste(im, ((SIZE - im.width) // 2, (SIZE - im.height) // 2))

    mask = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
    mask.putalpha(canvas)
    out = IMG / f"login-{key}.png"
    mask.save(out, optimize=True)
    buf = io.BytesIO(); mask.save(buf, "PNG", optimize=True)
    b64[key] = base64.b64encode(buf.getvalue()).decode()
    print(f"{key}: {'перевернуто, ' if inverted else ''}{out.stat().st_size // 1024} КБ")

(SP / "login_masks.json").write_text(json.dumps(b64), encoding="utf-8")
print("json готовий")
