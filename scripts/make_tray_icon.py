"""Generate assets/kuubmill.ico — the app/tray icon.

Run once (or after a brand change) to regenerate the committed .ico:
    python scripts/make_tray_icon.py

Kept as a script, not a build step, so the icon is a stable committed asset the
PyInstaller spec just bundles. On-brand: the dark app background (#0d1117) with
the teal accent (#14b8a6) as a radar-style disc — the same motif as the login
screen. Multi-size so Windows picks a crisp variant for the tray, taskbar and
Alt-Tab."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

_BG = (13, 17, 23, 255)      # --bg  #0d1117
_ACCENT = (20, 184, 166, 255)  # --accent #14b8a6
_ACCENT_SOFT = (20, 184, 166, 70)

_OUT = Path(__file__).resolve().parents[1] / "assets" / "kuubmill.ico"


def _render(size: int) -> Image.Image:
    # Supersample 4× then downscale for smooth edges at small sizes.
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=_BG)

    cx, cy = s * 0.5, s * 0.5
    # Soft outer ring + solid inner disc — the radar sweep motif.
    r_outer = s * 0.34
    d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
              outline=_ACCENT_SOFT, width=max(1, int(s * 0.03)))
    r_mid = s * 0.24
    d.ellipse([cx - r_mid, cy - r_mid, cx + r_mid, cy + r_mid],
              outline=_ACCENT, width=max(1, int(s * 0.035)))
    r_dot = s * 0.10
    d.ellipse([cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot], fill=_ACCENT)
    # Sweep line from centre to upper-right.
    d.line([cx, cy, cx + r_outer * 0.72, cy - r_outer * 0.72],
           fill=_ACCENT, width=max(1, int(s * 0.03)))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    # Render at the largest size; Pillow embeds every entry in `sizes` by
    # downscaling from it. (append_images is silently ignored for ICO.)
    master = _render(256)
    master.save(_OUT, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    main()
