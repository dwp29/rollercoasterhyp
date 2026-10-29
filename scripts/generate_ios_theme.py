#!/usr/bin/env python3
"""Offline HyperOS / MIUI iOS Liquid Glass Theme (.mtz) Generator.

Produces a complete, self-contained `Rollercoaster_iOS_Pro.mtz` containing:
  - description.xml (HyperOS Global & CN compatible metadata)
  - icons (squircle iOS icon mask + crisp vector icons for core system & Google apps)
  - lockscreen (MAML clean iOS-style clock + date lockscreen)
  - com.android.systemui (iOS statusbar/control-center accent module)
  - wallpaper/default_wallpaper.jpg & default_lock_wallpaper.jpg (1080x2400 iOS Liquid Glass)
  - preview/preview_cover_0.jpg, preview_icons_0.jpg, preview_lockscreen_0.jpg
"""

from __future__ import annotations

import argparse
import io
import math
import zipfile
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def get_font(sz: int) -> ImageFont.FreeTypeFont:
    if Path(FONT_BOLD).is_file():
        return ImageFont.truetype(FONT_BOLD, sz)
    return ImageFont.load_default()


def render_ios_wallpaper(w: int = 1080, h: int = 2400, lock_variant: bool = False) -> bytes:
    sw, sh = w // 4, h // 4
    img = Image.new("RGB", (sw, sh), (10, 18, 46))
    d = ImageDraw.Draw(img)
    for y in range(sh):
        t = y / max(1, sh - 1)
        r = int(14 + 45 * math.sin(t * math.pi) + (28 if lock_variant else 0))
        g = int(32 + 125 * (1.0 - abs(t - 0.42)))
        b = int(110 + 135 * (1.0 - t * 0.45))
        d.line([(0, y), (sw, y)], fill=(min(255, r), min(255, g), min(255, b)))

    overlay = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse((-40, int(sh * 0.12), int(sw * 0.92), int(sh * 0.62)), fill=(56, 189, 248, 130))
    od.ellipse((int(sw * 0.18), int(sh * 0.34), int(sw * 1.18), int(sh * 0.88)), fill=(168, 85, 247, 135))
    od.ellipse((-20, int(sh * 0.56), int(sw * 0.85), int(sh * 1.05)), fill=(236, 72, 153, 110))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=28))

    merged = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    full = merged.resize((w, h), Image.Resampling.BICUBIC)
    buf = io.BytesIO()
    full.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def make_squircle_mask(size: int = 192, radius: int = 44) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((4, 4, size - 5, size - 5), radius=radius, fill=255)
    return mask


def render_ios_icon(top_rgb: Tuple[int, int, int], bot_rgb: Tuple[int, int, int], glyph: str, size: int = 192) -> bytes:
    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        c = tuple(int(top_rgb[i] + (bot_rgb[i] - top_rgb[i]) * t) for i in range(3)) + (255,)
        gd.line([(0, y), (size, y)], fill=c)

    font = get_font(int(size * (0.44 if len(glyph) == 1 else 0.30)))
    bbox = gd.textbbox((0, 0), glyph, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    gd.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), glyph, font=font, fill=(255, 255, 255, 248))

    mask = make_squircle_mask(size, int(size * 0.23))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def build_icons_module() -> bytes:
    app_specs: Dict[str, Tuple[Tuple[int, int, int], Tuple[int, int, int], str]] = {
        "com.android.contacts.activities.TwelveKeyDialer.png": ((52, 211, 153), (16, 185, 129), "TEL"),
        "com.android.contacts.png": ((251, 191, 36), (245, 158, 11), "ID"),
        "com.android.mms.png": ((74, 222, 128), (22, 163, 74), "SMS"),
        "com.android.settings.png": ((148, 163, 184), (71, 85, 105), "SET"),
        "com.android.camera.png": ((100, 116, 139), (30, 41, 59), "CAM"),
        "com.miui.gallery.png": ((244, 114, 182), (168, 85, 247), "IMG"),
        "com.android.thememanager.png": ((56, 189, 248), (99, 102, 241), "iOS"),
        "com.miui.securitycenter.png": ((45, 212, 191), (13, 148, 136), "SEC"),
        "com.android.fileexplorer.png": ((96, 165, 250), (37, 99, 235), "DIR"),
        "com.android.vending.png": ((56, 189, 248), (14, 165, 233), "PLAY"),
        "com.google.android.youtube.png": ((248, 113, 113), (220, 38, 38), "YT"),
        "com.google.android.apps.youtube.music.png": ((251, 113, 133), (190, 18, 60), "YTM"),
        "com.dolby.daxappui.png": ((129, 140, 248), (67, 56, 202), "DAX"),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        mask_png = io.BytesIO()
        make_squircle_mask(192, 44).save(mask_png, format="PNG")
        bg_png = render_ios_icon((241, 245, 249), (203, 213, 225), "")
        zf.writestr("res/drawable-xxhdpi/icon_mask.png", mask_png.getvalue())
        zf.writestr("res/drawable-xxhdpi/icon_bg.png", bg_png)
        for fname, (c1, c2, label) in app_specs.items():
            zf.writestr(f"res/drawable-xxhdpi/{fname}", render_ios_icon(c1, c2, label))
    return buf.getvalue()


def build_lockscreen_module() -> bytes:
    manifest_xml = """<?xml version="1.0" encoding="utf-8"?>
<Lockscreen version="1" frameRate="60" screenWidth="1080">
    <Wallpaper/>
    <Time x="540" y="340" align="center" alignV="center" size="190" color="#FFFFFFFF" format="HH:mm"/>
    <DateTime x="540" y="485" align="center" color="#E6F8FAFC" size="42" format="EEEE, d MMMM"/>
    <Unlocker name="unlocker" bounceInitSpeed="2000" bounceAcceleration="3000">
        <StartPoint x="0" y="1800" w="1080" h="600"/>
        <EndPoint x="0" y="900" w="1080" h="600">
            <Path x="0" y="1800"><Position x="0" y="1800"/><Position x="0" y="900"/></Path>
        </EndPoint>
    </Unlocker>
</Lockscreen>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("advance/manifest.xml", manifest_xml.encode("utf-8"))
    return buf.getvalue()


def build_systemui_module() -> bytes:
    theme_values = """<?xml version="1.0" encoding="utf-8"?>
<MIUI_Theme_Values>
    <color name="status_bar_clock_color">#FFFFFFFF</color>
    <dimen name="qs_control_corner_radius">28dp</dimen>
</MIUI_Theme_Values>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("theme_values.xml", theme_values.encode("utf-8"))
    return buf.getvalue()


def generate_mtz(out_mtz: Path, unpacked_dir: Optional[Path] = None) -> Path:
    out_mtz.parent.mkdir(parents=True, exist_ok=True)
    desc_xml = """<?xml version="1.0" encoding="utf-8"?>
<theme>
    <title>Rollercoaster iOS Pro</title>
    <designer>Graziemille (@Grazie_milles)</designer>
    <author>Graziemille</author>
    <version>18.4</version>
    <uiVersion>15</uiVersion>
    <description>Tema iOS Liquid Glass Offline Bawaan Rolleracoaster Hypos 4.0.1.7 (joyeuse)</description>
</theme>
"""
    home_wp = render_ios_wallpaper(1080, 2400, lock_variant=False)
    lock_wp = render_ios_wallpaper(1080, 2400, lock_variant=True)
    icons_mod = build_icons_module()
    lock_mod = build_lockscreen_module()
    sysui_mod = build_systemui_module()

    entries: Dict[str, bytes] = {
        "description.xml": desc_xml.encode("utf-8"),
        "icons": icons_mod,
        "lockscreen": lock_mod,
        "com.android.systemui": sysui_mod,
        "wallpaper/default_wallpaper.jpg": home_wp,
        "wallpaper/default_lock_wallpaper.jpg": lock_wp,
        "preview/preview_cover_0.jpg": home_wp,
        "preview/preview_icons_0.jpg": home_wp,
        "preview/preview_lockscreen_0.jpg": lock_wp,
    }

    with zipfile.ZipFile(out_mtz, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in entries.items():
            zf.writestr(arc, data)

    if unpacked_dir is not None:
        for arc, data in entries.items():
            dest = unpacked_dir / arc
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    print(f"[ios-theme] Generated {out_mtz} ({out_mtz.stat().st_size / 1024:.1f} KB)")
    return out_mtz


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate offline iOS Liquid Glass .mtz theme")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    generate_mtz(args.out)


if __name__ == "__main__":
    main()
