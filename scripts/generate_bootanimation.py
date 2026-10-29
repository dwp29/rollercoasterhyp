#!/usr/bin/env python3
"""Procedural 1080x2400 @ 60fps Bootanimation Generator for Rollercoaster Hypos 4.0.1.7.

Renders 'Rollercoaster' letters falling from above with bounce physics, merging together
with 'OS', and shimmering in full-spectrum RGB neon glow + sparkles.
Packed strictly with ZIP_STORED (compression method 0) as required by Android bootanimation.
"""

from __future__ import annotations

import argparse
import colorsys
import io
import math
import random
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 2400
FPS = 60
INTRO_FRAMES = 60
LOOP_FRAMES = 60

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if Path(p).is_file():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def hsv_rgb(h: float, s: float = 0.95, v: float = 1.0) -> Tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def ease_out_back(t: float) -> float:
    t = max(0.0, min(1.0, t))
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * ((t - 1.0) ** 3) + c1 * ((t - 1.0) ** 2)


def make_sparkle_field(seed: int = 42, count: int = 75) -> List[Tuple[float, float, float, float, float]]:
    rng = random.Random(seed)
    pts = []
    for _ in range(count):
        x = rng.uniform(90, W - 90)
        y = rng.uniform(H * 0.34, H * 0.66)
        phase = rng.uniform(0.0, math.tau)
        radius = rng.uniform(2.2, 6.2)
        hue = rng.uniform(0.0, 1.0)
        pts.append((x, y, phase, radius, hue))
    return pts


def render_frame(
    frame_idx: int,
    is_loop: bool,
    font_title: ImageFont.FreeTypeFont,
    font_os: ImageFont.FreeTypeFont,
    font_sub: ImageFont.FreeTypeFont,
    sparkles: List[Tuple[float, float, float, float, float]],
) -> bytes:
    # Render at half resolution for ultra-smooth multi-pass Gaussian neon glow, then upscale to 1080x2400
    sw, sh = W // 2, H // 2
    base = Image.new("RGBA", (sw, sh), (4, 6, 15, 255))
    glow_layer = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    fg_layer = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))

    gd = ImageDraw.Draw(glow_layer)
    fd = ImageDraw.Draw(fg_layer)

    word = "Rollercoaster"
    hue_shift = (frame_idx / 60.0) if is_loop else (frame_idx / 90.0)

    # Measure total width of "Rollercoaster"
    advances = [fd.textlength(ch, font=font_title) for ch in word]
    spacing_settled = 1.5
    total_w = sum(advances) + spacing_settled * (len(word) - 1)
    center_y = int(sh * 0.455)

    if not is_loop:
        merge_t = max(0.0, min(1.0, (frame_idx - 34) / 22.0))
        extra_spread = (1.0 - ease_out_back(merge_t)) * 8.0
    else:
        merge_t = 1.0
        extra_spread = 0.0

    cur_total_w = total_w + extra_spread * (len(word) - 1)
    start_x = (sw - cur_total_w) / 2.0

    # Ambient neon aura behind logo
    aura_alpha = int(55 + 25 * math.sin(hue_shift * math.tau)) if is_loop else int(55 * merge_t)
    if aura_alpha > 4:
        c_aura = hsv_rgb(hue_shift + 0.55, 0.9, 1.0)
        gd.ellipse((sw * 0.14, center_y - 68, sw * 0.86, center_y + 96), fill=(*c_aura, aura_alpha))

    x_cursor = start_x
    for idx, ch in enumerate(word):
        hue = (idx / len(word) + hue_shift) % 1.0
        neon_col = hsv_rgb(hue, 0.92, 1.0)
        core_col = hsv_rgb(hue, 0.28, 1.0)

        if not is_loop:
            start_f = idx * 2.1
            local_t = (frame_idx - start_f) / 22.0
            if local_t <= 0.0:
                x_cursor += advances[idx] + spacing_settled + extra_spread
                continue
            prog = ease_out_back(min(1.0, local_t))
            y_pos = -90.0 + (center_y + 90.0) * prog
            alpha = int(255 * min(1.0, local_t * 2.4))
        else:
            wave = math.sin(hue_shift * math.tau + idx * 0.45) * 3.0
            y_pos = center_y + wave
            alpha = 255

        gd.text((x_cursor, y_pos), ch, font=font_title, fill=(*neon_col, alpha), stroke_width=4, stroke_fill=(*neon_col, alpha))
        fd.text((x_cursor, y_pos), ch, font=font_title, fill=(*core_col, alpha), stroke_width=1, stroke_fill=(*neon_col, alpha))
        x_cursor += advances[idx] + spacing_settled + extra_spread

    # "OS" badge rising & locking below "Rollercoaster"
    os_alpha = 255 if is_loop else int(255 * max(0.0, min(1.0, (frame_idx - 32) / 18.0)))
    if os_alpha > 0:
        os_text = "OS"
        os_w = fd.textlength(os_text, font=font_os)
        os_x = (sw - os_w) / 2.0
        os_y_offset = 0.0 if is_loop else (1.0 - ease_out_back(min(1.0, max(0.0, (frame_idx - 32) / 20.0)))) * 35.0
        os_y = center_y + 58 + os_y_offset
        os_col = hsv_rgb(hue_shift + 0.12, 0.95, 1.0)
        os_core = hsv_rgb(hue_shift + 0.12, 0.22, 1.0)
        gd.text((os_x, os_y), os_text, font=font_os, fill=(*os_col, os_alpha), stroke_width=4, stroke_fill=(*os_col, os_alpha))
        fd.text((os_x, os_y), os_text, font=font_os, fill=(*os_core, os_alpha), stroke_width=1, stroke_fill=(*os_col, os_alpha))

    # Sparkle particles
    sp_gate = 1.0 if is_loop else max(0.0, min(1.0, (frame_idx - 26) / 24.0))
    if sp_gate > 0.0:
        t_rad = (frame_idx / FPS) * math.tau
        for sx, sy, phase, rad, shue in sparkles:
            twinkle = (0.5 + 0.5 * math.sin(t_rad * 1.5 + phase)) * sp_gate
            if twinkle < 0.22:
                continue
            px, py = sx / 2.0, sy / 2.0
            r = rad * (0.5 + 0.7 * twinkle) * 0.55
            scol = hsv_rgb(shue + hue_shift, 0.85, 1.0)
            a = int(235 * twinkle)
            gd.ellipse((px - r * 2, py - r * 2, px + r * 2, py + r * 2), fill=(*scol, a // 2))
            fd.line([(px - r * 2.6, py), (px + r * 2.6, py)], fill=(255, 255, 255, a), width=1)
            fd.line([(px, py - r * 2.6), (px, py + r * 2.6)], fill=(255, 255, 255, a), width=1)

    # Subtitle credit
    sub_alpha = 215 if is_loop else int(215 * max(0.0, min(1.0, (frame_idx - 42) / 16.0)))
    if sub_alpha > 0:
        sub_txt = "Hypos 4.0.1.7 • by Graziemille"
        sub_w = fd.textlength(sub_txt, font=font_sub)
        fd.text(((sw - sub_w) / 2.0, sh * 0.86), sub_txt, font=font_sub, fill=(186, 230, 253, sub_alpha))

    glow_wide = glow_layer.filter(ImageFilter.GaussianBlur(radius=13))
    glow_tight = glow_layer.filter(ImageFilter.GaussianBlur(radius=5))
    merged_glow = ImageChops.screen(glow_wide, glow_tight)

    comp = Image.alpha_composite(base, merged_glow)
    comp = Image.alpha_composite(comp, fg_layer)
    full = comp.convert("RGB").resize((W, H), Image.Resampling.BILINEAR)

    buf = io.BytesIO()
    full.save(buf, format="JPEG", quality=88, subsampling=0, optimize=False)
    return buf.getvalue()


def generate_bootanimation(out_zip: Path, font_cache_dir: Optional[Path] = None) -> Path:
    del font_cache_dir
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    font_title = load_font(52)
    font_os = load_font(40)
    font_sub = load_font(16)
    sparkles = make_sparkle_field()

    desc_txt = f"{W} {H} {FPS}\nc 1 0 part0\nc 0 0 part1\n"

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("desc.txt", desc_txt.encode("ascii"), compress_type=zipfile.ZIP_STORED)
        for i in range(INTRO_FRAMES):
            jpg = render_frame(i, False, font_title, font_os, font_sub, sparkles)
            zf.writestr(f"part0/{i:04d}.jpg", jpg, compress_type=zipfile.ZIP_STORED)
        for i in range(LOOP_FRAMES):
            jpg = render_frame(i, True, font_title, font_os, font_sub, sparkles)
            zf.writestr(f"part1/{i:04d}.jpg", jpg, compress_type=zipfile.ZIP_STORED)

    print(f"[bootanim] Generated {out_zip} ({out_zip.stat().st_size / (1024*1024):.2f} MB, ZIP_STORED)")
    return out_zip


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Rollercoaster Neon Glow bootanimation.zip")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    generate_bootanimation(args.out)


if __name__ == "__main__":
    main()
