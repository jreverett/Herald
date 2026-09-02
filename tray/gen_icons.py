#!/usr/bin/env python3
"""Render the "Two Roofs" tray icons to multi-size .ico files.

The design (see icons/src/DESIGN.md) is one chevron pair that pivots 90 degrees
per state, drawn as two 3-point stroked polylines on a 32-unit canvas. Direction
is the primary signal; colour is secondary and differs per taskbar theme, so we
emit a full set for a dark taskbar and a light one. NotifyIcon can't shell-tint,
so the tray picks the matching set at runtime.
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "icons"
UNIT = 32
SS = 8               # supersample factor for the master render
MASTER = 256
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (256, 256)]
FRAME_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (48, 48)]
FRAMES = 8           # animation frames for the send/recv flow
PULSE_FLOOR = 0.3    # dimmest point of the working breath, as a fraction of full alpha
FLOW_PERIOD = 15.0   # unit spacing between marching chevrons
FLOW_FADE = 8.0      # unit width of the edge fade

# state -> (paths, stroke_units). tray key in comments.
STATES = {
    "idle":    ([[(2, 20), (8, 13), (14, 20)], [(18, 20), (24, 13), (30, 20)]], 4.0),
    "send":    ([[(4, 10), (11, 16), (4, 22)], [(19, 10), (26, 16), (19, 22)]], 4.0),
    "recv":    ([[(13, 10), (6, 16), (13, 22)], [(28, 10), (21, 16), (28, 22)]], 4.0),
    "offline": ([[(2, 13), (8, 20), (14, 13)], [(18, 13), (24, 20), (30, 13)]], 3.5),
    # working keeps idle's geometry and only breathes: an agent turn is running,
    # which is not a direction and must not read as traffic.
    "work":    ([[(2, 20), (8, 13), (14, 20)], [(18, 20), (24, 13), (30, 20)]], 4.0),
    # blocked converges the pair, a direction neither send nor recv uses, so the
    # state survives greyscale rather than resting on red alone.
    "blocked": ([[(4, 10), (11, 16), (4, 22)], [(28, 10), (21, 16), (28, 22)]], 4.0),
}

# per taskbar theme: idle uses the bar's foreground; the rest per DESIGN.md.
COLOURS = {
    "dark":  {"idle": "#FFFFFF", "send": "#5B96EA", "recv": "#5FB383", "offline": "#7D838B",
              "work": "#E8A33D", "blocked": "#E5484D"},
    "light": {"idle": "#1A1A1A", "send": "#2F6FD0", "recv": "#2F7D4F", "offline": "#8A8376",
              "work": "#A85F00", "blocked": "#C0292E"},
}


def draw_state(paths, stroke_units, colour):
    size = MASTER * SS
    scale = size / UNIT
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = stroke_units * scale
    r = w / 2
    for path in paths:
        pts = [(x * scale, y * scale) for x, y in path]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            d.line([(x0, y0), (x1, y1)], fill=colour, width=int(round(w)))
        for x, y in pts:                    # round caps + join
            d.ellipse([x - r, y - r, x + r, y + r], fill=colour)
    return img.resize((MASTER, MASTER), Image.LANCZOS)


def _chevron(cx, pointing_right):
    """Three points of a chevron centred at (cx, 16), matching the design's span."""
    if pointing_right:
        return [(cx - 3.5, 10), (cx + 3.5, 16), (cx - 3.5, 22)]
    return [(cx + 3.5, 10), (cx - 3.5, 16), (cx + 3.5, 22)]


def draw_flow_frame(pointing_right, colour, phase):
    """One frame of chevrons marching in the pointing direction, fading at both
    edges so they appear on one side and dissolve on the other."""
    size = MASTER * SS
    scale = size / UNIT
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    w = 4.0 * scale
    r = w / 2
    drift = phase * FLOW_PERIOD * (1 if pointing_right else -1)
    for k in range(-1, 4):
        cx = 4 + k * FLOW_PERIOD + drift
        edge = min(cx, UNIT - cx)
        alpha = max(0.0, min(1.0, edge / FLOW_FADE))
        if alpha <= 0:
            continue
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        pts = [(x * scale, y * scale) for x, y in _chevron(cx, pointing_right)]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            ld.line([(x0, y0), (x1, y1)], fill=colour, width=int(round(w)))
        for x, y in pts:
            ld.ellipse([x - r, y - r, x + r, y + r], fill=colour)
        img = Image.alpha_composite(img, Image.blend(
            Image.new("RGBA", (size, size), (0, 0, 0, 0)), layer, alpha))
    return img.resize((MASTER, MASTER), Image.LANCZOS)


def draw_pulse_frame(paths, stroke_units, colour, phase):
    """One frame of the working breath: idle's shape fading in and out."""
    img = draw_state(paths, stroke_units, colour)
    alpha = PULSE_FLOOR + (1 - PULSE_FLOOR) * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
    img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
    return img


def main():
    for theme, palette in COLOURS.items():
        outdir = OUT / theme
        outdir.mkdir(parents=True, exist_ok=True)
        for state, (paths, stroke) in STATES.items():
            master = draw_state(paths, stroke, palette[state])
            master.save(outdir / f"{state}.ico", sizes=ICO_SIZES)
        for state, right in (("send", True), ("recv", False)):
            for i in range(FRAMES):
                frame = draw_flow_frame(right, palette[state], i / FRAMES)
                frame.save(outdir / f"{state}_{i}.ico", sizes=FRAME_SIZES)
        paths, stroke = STATES["work"]
        for i in range(FRAMES):
            frame = draw_pulse_frame(paths, stroke, palette["work"], i / FRAMES)
            frame.save(outdir / f"work_{i}.ico", sizes=FRAME_SIZES)
    print(f"wrote dark/ and light/ sets ({', '.join(STATES)}) "
          f"plus {FRAMES}-frame send/recv flows and the working breath to", OUT)


if __name__ == "__main__":
    main()
