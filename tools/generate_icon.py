"""
Icon generator for PC Game Roulette.

Draws a 5-pip dice face (like the "5" side of a real die — four corners plus
a centre pip).  Each pip is coloured to evoke one of the supported launchers:

    Top-left  → Steam        (blue    #66c0f4)
    Top-right → GOG          (purple  #a259ff)
    Centre    → Gold / fate  (gold    #f0c040)  — the "Leave It To Fate" wildcard
    Bot-left  → Epic         (white   #f0f0f0)
    Bot-right → Retroarch    (orange  #f08c00)  — reserved for future launcher

The die body matches the app's --surface colour scheme.  Exported as a
multi-resolution .ico (256 / 128 / 64 / 48 / 32 / 16 px) and checked into
the repo so end-users don't need Pillow.

    python tools/generate_icon.py
"""

import math
import os

from PIL import Image, ImageDraw

# ── Pip layout (5-face pattern: four corners + centre) ───────────────────────
#
# Normalised (0–1) positions inside the die face's inner bounding box.
# TL = top-left, TR = top-right, C = centre, BL = bot-left, BR = bot-right
PIP_LAYOUT = [
    (0.17, 0.17),   # top-left    → Steam blue
    (0.83, 0.17),   # top-right   → GOG purple
    (0.50, 0.50),   # centre      → Gold (wildcard / LITF)
    (0.17, 0.83),   # bottom-left → Epic white
    (0.83, 0.83),   # bottom-right→ Retroarch orange
]

PIP_COLORS = [
    "#3d96cc",   # Steam   — medium blue (slightly darker than Steam's native #66c0f4)
    "#a259ff",   # GOG     — purple
    "#f0c040",   # Fate    — gold / wildcard
    "#f0f0f0",   # Epic    — white
    "#f08c00",   # Retroarch — orange (placeholder for future tab)
]

# Die body colours (matches the app's dark theme)
DIE_COLOR     = "#171c27"   # --surface
DIE_BORDER    = "#4c9be8"   # --accent (Steam blue by default)
DIE_HIGHLIGHT = "#2a3349"   # --border (subtle inner bevel ring)

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "app.ico")


def draw_icon(size: int) -> Image.Image:
    """Render a 5-pip die at *size* × *size* pixels (RGBA)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # ── Die body ──────────────────────────────────────────────────────────────
    pad    = max(2, size // 12)
    radius = max(2, size // 8)
    border = max(1, size // 64)

    die_box = (pad, pad, size - pad, size - pad)
    d.rounded_rectangle(die_box, radius=radius,
                        fill=DIE_COLOR, outline=DIE_BORDER, width=border)

    # Inner bevel ring (purely cosmetic; gives depth at large sizes)
    ip = pad + max(1, size // 28)
    d.rounded_rectangle(
        (ip, ip, size - ip, size - ip),
        radius=max(1, radius - 2),
        outline=DIE_HIGHLIGHT,
        width=max(1, size // 80),
    )

    # ── Pip positions ─────────────────────────────────────────────────────────
    # Scale normalised coords to pixels inside the inner bounding box
    inner = (die_box[0] + radius, die_box[1] + radius,
             die_box[2] - radius, die_box[3] - radius)
    iw = inner[2] - inner[0]
    ih = inner[3] - inner[1]
    pip_r = max(2, size // 13)  # radius of each pip circle (sized so pips don't overlap)

    for (nx, ny), color in zip(PIP_LAYOUT, PIP_COLORS):
        cx = inner[0] + nx * iw
        cy = inner[1] + ny * ih

        # Soft glow halo at larger sizes
        if size >= 48:
            glow_r = pip_r + max(1, size // 32)
            for r_off in range(glow_r, pip_r - 1, -1):
                alpha = max(0, 80 - (r_off - pip_r) * 30)
                rgb   = Image.new("RGB", (1, 1), color).getpixel((0, 0))
                d.ellipse(
                    (cx - r_off, cy - r_off, cx + r_off, cy + r_off),
                    fill=(*rgb, alpha),
                )

        # Pip body
        outline_color = "#000000" if color in ("#f0f0f0", "#ffffff") else None
        d.ellipse(
            (cx - pip_r, cy - pip_r, cx + pip_r, cy + pip_r),
            fill=color,
            outline=outline_color,
            width=max(1, size // 96) if outline_color else 0,
        )

    return img


def main() -> None:
    sizes  = [256, 128, 64, 48, 32, 16]
    images = [draw_icon(s) for s in sizes]
    out    = os.path.abspath(OUTPUT_PATH)
    images[0].save(
        out,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"Wrote {out}  (sizes: {sizes})")


if __name__ == "__main__":
    main()
