"""
One-shot icon generator for PC Game Roulette.

Draws a stylized die with 6 pips, each colored to evoke one of the six
supported launchers (Steam, GOG, Epic, Battle.net, EA, Ubisoft).  Exports a
multi-resolution .ico file (256, 64, 48, 32, 16 px) wired to main.py via the
``icon=`` arg on the pywebview window.

Re-run only when you want to change the icon; the resulting .ico is checked
into the repo so end users never need Pillow installed.

    python tools/generate_icon.py
"""

import os
import math
from PIL import Image, ImageDraw

# Brand-evocative pip colors (one per launcher)
PIP_COLORS = [
    "#66c0f4",   # Steam — light blue
    "#a259ff",   # GOG — purple
    "#ffffff",   # Epic — white
    "#148eff",   # Battle.net — cyan
    "#ff3b4b",   # EA — red
    "#3aa7ff",   # Ubisoft — bright blue
]
DIE_COLOR = "#171c27"        # matches the app's --surface
DIE_BORDER = "#4c9be8"       # accent
DIE_HIGHLIGHT = "#2a3349"    # subtle inner highlight

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "app.ico")


def draw_icon(size):
    """Render a die with launcher-colored pips at the requested square size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Die outline — rounded square taking ~90% of canvas
    pad = max(2, size // 14)
    die_box = (pad, pad, size - pad, size - pad)
    radius = max(2, size // 10)
    d.rounded_rectangle(die_box, radius=radius, fill=DIE_COLOR,
                        outline=DIE_BORDER, width=max(1, size // 64))

    # Inner highlight gradient — fake it with a slightly smaller rounded rect
    inner_pad = pad + max(1, size // 30)
    d.rounded_rectangle(
        (inner_pad, inner_pad, size - inner_pad, size - inner_pad),
        radius=max(1, radius - 2),
        outline=DIE_HIGHLIGHT, width=max(1, size // 80),
    )

    # Six pips arranged in the classic dice-6 pattern (2 columns of 3)
    # Coordinates are normalised (0-1) then scaled to die_box
    pip_layout = [
        (0.32, 0.27), (0.68, 0.27),
        (0.32, 0.50), (0.68, 0.50),
        (0.32, 0.73), (0.68, 0.73),
    ]
    pip_radius = max(2, size // 9)
    inner = (die_box[0] + radius, die_box[1] + radius,
             die_box[2] - radius, die_box[3] - radius)
    iw = inner[2] - inner[0]
    ih = inner[3] - inner[1]

    for (nx, ny), color in zip(pip_layout, PIP_COLORS):
        cx = inner[0] + nx * iw
        cy = inner[1] + ny * ih
        # Outer glow for visual punch on bigger sizes
        if size >= 48:
            for r_off in range(pip_radius + max(1, size // 32),
                               pip_radius - 1, -1):
                alpha = max(0, 80 - (r_off - pip_radius) * 30)
                d.ellipse((cx - r_off, cy - r_off, cx + r_off, cy + r_off),
                          fill=(*Image.new("RGB", (1, 1), color).getpixel((0, 0)), alpha))
        d.ellipse(
            (cx - pip_radius, cy - pip_radius,
             cx + pip_radius, cy + pip_radius),
            fill=color,
            outline="#000000" if color == "#ffffff" else None,
            width=max(1, size // 96) if color == "#ffffff" else 0,
        )

    return img


def main():
    sizes = [256, 128, 64, 48, 32, 16]
    images = [draw_icon(s) for s in sizes]
    out = os.path.abspath(OUTPUT_PATH)
    images[0].save(out, format="ICO",
                   sizes=[(s, s) for s in sizes],
                   append_images=images[1:])
    print(f"Wrote {out}  (sizes: {sizes})")


if __name__ == "__main__":
    main()
