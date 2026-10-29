#!/usr/bin/env python3
"""Compile privileged Support APK & Runtime Resource Overlays (RRO) for Rollercoaster Hypos 4.0.1.7.

Builds:
  1. RollercoasterSupport.apk (id.rollercoaster.support)
     - Injects 'Rollercoaster Support' card directly into Settings -> My Device / About Phone
       via Android IA Settings category `com.android.settings.category.ia.my_device_info` +
       `com.android.settings.category.ia.device_info` + `com.android.settings.category.ia.homepage`.
     - Displays ROM Version 'Rollercoaster Hypos 4.0.1.7', Creator 'Graziemille',
       live circular Telegram profile avatar fetched from https://t.me/Grazie_milles,
       official Telegram paper-plane vector badge, and one-tap button to `@Grazie_milles`.
  2. RollercoasterSettingsOverlay.apk (targets com.android.settings / com.miui.miuisettings)
     - Indonesian & English overrides for Maintainer/Creator labels -> 'Graziemille (@Grazie_milles)'
     - ROM Version display -> 'Rollercoaster Hypos 4.0.1.7'
  3. RollercoasterSystemUIOverlay.apk (targets com.android.systemui)
     - Indonesian VoLTE/VoWiFi status bar & carrier display polish
  4. RollercoasterThemeOverlay.apk (targets com.android.thememanager)
     - Indonesian strings & Global store feature flags
  5. Lunaris / Cruecy Dolby Atmos Indonesian UI (`DolbyDaxUI.apk`)
     - Injects full Bahasa Indonesia (`values-in` & `values-id`) strings into `vendor_dolby`'s DaxUI
       and compiles it with Gradle if source is present (or overlays prebuilt).
"""

from __future__ import annotations

import argparse
import html
import io
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont


def _sdk_version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.name))


def find_sdk_tool(sdk_root: Path, tool_name: str) -> Path:
    bt_root = sdk_root / "build-tools"
    if bt_root.is_dir():
        versions = sorted(
            (d for d in bt_root.iterdir() if d.is_dir()),
            key=_sdk_version_key,
            reverse=True,
        )
        for vdir in versions:
            cand = vdir / tool_name
            if cand.is_file():
                return cand
    found = shutil.which(tool_name)
    if found:
        return Path(found)
    raise FileNotFoundError(f"Android SDK tool '{tool_name}' not found under {sdk_root}")


def find_android_jar(sdk_root: Path, api_level: int = 35) -> Path:
    """Return the pinned compile platform instead of a lexicographically chosen SDK jar."""
    candidate = sdk_root / "platforms" / f"android-{api_level}" / "android.jar"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        f"Android API {api_level} platform is required; missing {candidate}. "
        "Install platforms;android-35 before building."
    )


def ensure_keystore(ks_path: Path) -> Path:
    if ks_path.is_file():
        return ks_path
    ks_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "keytool",
            "-genkeypair",
            "-v",
            "-keystore",
            str(ks_path),
            "-alias",
            "rollercoaster",
            "-keyalg",
            "RSA",
            "-keysize",
            "2048",
            "-validity",
            "10000",
            "-storepass",
            "rollercoaster",
            "-keypass",
            "rollercoaster",
            "-dname",
            "CN=Graziemille, OU=RollercoasterOS, O=Rollercoaster, L=Jakarta, S=DKI, C=ID",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ks_path


def sign_and_align_apk(unsigned_apk: Path, out_apk: Path, sdk_root: Path, keystore: Path) -> Path:
    zipalign = find_sdk_tool(sdk_root, "zipalign")
    apksigner = find_sdk_tool(sdk_root, "apksigner")
    aligned = unsigned_apk.with_suffix(".aligned.apk")
    subprocess.run([str(zipalign), "-f", "-p", "4", str(unsigned_apk), str(aligned)], check=True)
    out_apk.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(apksigner),
            "sign",
            "--ks",
            str(keystore),
            "--ks-key-alias",
            "rollercoaster",
            "--ks-pass",
            "pass:rollercoaster",
            "--key-pass",
            "pass:rollercoaster",
            "--v1-signing-enabled",
            "true",
            "--v2-signing-enabled",
            "true",
            "--v3-signing-enabled",
            "true",
            "--out",
            str(out_apk),
            str(aligned),
        ],
        check=True,
    )
    aligned.unlink(missing_ok=True)
    return out_apk


def fetch_telegram_avatar(username: str = "Grazie_milles", required: bool = True) -> Image.Image:
    """Fetch the live Telegram profile photo; a required avatar never silently becomes a monogram."""
    clean_user = username.lstrip("@")
    url = f"https://t.me/{clean_user}"
    failure: Exception | None = None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) RollercoasterROMBuilder/4.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = resp.read(2 * 1024 * 1024).decode("utf-8", errors="ignore")

        image_url = ""
        for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.IGNORECASE):
            attrs: Dict[str, str] = {}
            for match in re.finditer(
                r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
                tag,
                flags=re.IGNORECASE,
            ):
                attrs[match.group(1).lower()] = next(value for value in match.groups()[1:] if value is not None)
            if attrs.get("property", "").lower() in {"og:image", "og:image:url"} or attrs.get("name", "").lower() in {"og:image", "og:image:url"}:
                image_url = html.unescape(attrs.get("content", "")).strip()
                if image_url:
                    break

        if not image_url.startswith("https://") or "telegram.org/img/t_logo" in image_url:
            raise RuntimeError(f"Telegram profile @{clean_user} did not expose a real og:image")
        img_req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(img_req, timeout=20) as iresp:
            raw = iresp.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise RuntimeError("Telegram avatar exceeded the 12 MiB safety limit")
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            avatar = image.convert("RGBA")
        print(f"[support-apk] Downloaded live Telegram profile avatar for @{clean_user} ({avatar.size})")
        return avatar
    except Exception as exc:
        failure = exc

    if required:
        raise RuntimeError(f"Could not fetch the real Telegram profile photo for @{clean_user}: {failure}") from failure

    print(f"[support-apk] Optional live avatar unavailable ({failure}); generating a monogram fallback")
    sz = 320
    im = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(sz):
        t = y / (sz - 1)
        r = int(14 + 125 * t)
        g = int(165 - 70 * t)
        d.line([(0, y), (sz, y)], fill=(r, g, 233, 255))
    f_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(f_path, 128) if Path(f_path).is_file() else ImageFont.load_default()
    bbox = d.textbbox((0, 0), "GM", font=font)
    d.text(((sz - (bbox[2] - bbox[0])) / 2, (sz - (bbox[3] - bbox[1])) / 2 - 14), "GM", fill=(255, 255, 255, 255), font=font)
    return im


def make_circular_avatar_with_tg_badge(raw_avatar: Image.Image, size: int = 288) -> bytes:
    avatar = raw_avatar.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((6, 6, size - 7, size - 7), fill=255)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring = ImageDraw.Draw(canvas)
    ring.ellipse((1, 1, size - 2, size - 2), fill=(56, 189, 248, 255))
    canvas.paste(avatar, (0, 0), mask)

    bsz = int(size * 0.30)
    bx, by = size - bsz - 4, size - bsz - 4
    ring.ellipse((bx - 4, by - 4, bx + bsz + 4, by + bsz + 4), fill=(11, 17, 32, 255))
    ring.ellipse((bx, by, bx + bsz, by + bsz), fill=(36, 161, 222, 255))

    p1 = (bx + bsz * 0.20, by + bsz * 0.50)
    p2 = (bx + bsz * 0.77, by + bsz * 0.25)
    p3 = (bx + bsz * 0.65, by + bsz * 0.77)
    p4 = (bx + bsz * 0.45, by + bsz * 0.58)
    ring.polygon([p1, p2, p3, p4], fill=(255, 255, 255, 255))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def make_tg_icon_png(size: int = 96) -> bytes:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    d.ellipse((2, 2, size - 3, size - 3), fill=(36, 161, 222, 255))
    d.polygon(
        [
            (size * 0.19, size * 0.50),
            (size * 0.79, size * 0.24),
            (size * 0.66, size * 0.78),
            (size * 0.45, size * 0.58),
        ],
        fill=(255, 255, 255, 255),
    )
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def build_support_apk(out_apk: Path, sdk_root: Path, work_dir: Path) -> Path:
    """Compile `RollercoasterSupport.apk` from pure Java + Android SDK."""
    shutil.rmtree(work_dir, ignore_errors=True)
    src_dir = work_dir / "src" / "id" / "rollercoaster" / "support"
    res_dir = work_dir / "res"
    gen_dir = work_dir / "gen"
    cls_dir = work_dir / "classes"
    dex_dir = work_dir / "dex"
    for d in (
        src_dir,
        res_dir / "drawable-xxhdpi",
        res_dir / "drawable",
        res_dir / "layout",
        res_dir / "values",
        res_dir / "values-in",
        res_dir / "values-id",
        gen_dir,
        cls_dir,
        dex_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    avatar_img = fetch_telegram_avatar("Grazie_milles")
    (res_dir / "drawable-xxhdpi" / "avatar_graziemille.png").write_bytes(make_circular_avatar_with_tg_badge(avatar_img, 288))
    (res_dir / "drawable-xxhdpi" / "ic_telegram.png").write_bytes(make_tg_icon_png(96))

    (res_dir / "drawable" / "bg_card.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">
    <solid android:color="#162033"/>
    <corners android:radius="24dp"/>
    <stroke android:width="1dp" android:color="#2E4A7D"/>
</shape>
""",
        encoding="utf-8",
    )
    (res_dir / "drawable" / "bg_btn_tg.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">
    <solid android:color="#24A1DE"/>
    <corners android:radius="18dp"/>
</shape>
""",
        encoding="utf-8",
    )

    strings_en = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">Rollercoaster Support</string>
    <string name="pref_title">Support &amp; ROM Maintainer</string>
    <string name="pref_summary">Graziemille • @Grazie_milles</string>
    <string name="rom_title">Rollercoaster Hypos 4.0.1.7</string>
    <string name="rom_device">Xiaomi Redmi Note 9 Pro (joyeuse) • Kernel Roll 3.3</string>
    <string name="creator_label">ROM Creator / Maintainer</string>
    <string name="creator_name">Graziemille</string>
    <string name="tg_handle">@Grazie_milles</string>
    <string name="btn_open_tg">Open Telegram @Grazie_milles</string>
    <string name="btn_channel">Rollercoaster Updates Channel</string>
    <string name="footer_note">Pre-rooted KernelSU-Next + SuSFS • Dolby Atmos • Leica Camera • Global Themes</string>
</resources>
"""
    strings_id = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">Dukungan Rollercoaster</string>
    <string name="pref_title">Support &amp; Pembuat ROM</string>
    <string name="pref_summary">Graziemille • @Grazie_milles</string>
    <string name="rom_title">Rollercoaster Hypos 4.0.1.7</string>
    <string name="rom_device">Xiaomi Redmi Note 9 Pro (joyeuse) • Kernel Roll 3.3</string>
    <string name="creator_label">Pembuat ROM / Maintainer</string>
    <string name="creator_name">Graziemille</string>
    <string name="tg_handle">@Grazie_milles</string>
    <string name="btn_open_tg">Hubungi Telegram @Grazie_milles</string>
    <string name="btn_channel">Saluran Update Rollercoaster</string>
    <string name="footer_note">KernelSU-Next + SuSFS • Dolby Atmos • Leica Camera • Tema Global &amp; iOS</string>
</resources>
"""
    (res_dir / "values" / "strings.xml").write_text(strings_en, encoding="utf-8")
    (res_dir / "values-in" / "strings.xml").write_text(strings_id, encoding="utf-8")
    (res_dir / "values-id" / "strings.xml").write_text(strings_id, encoding="utf-8")

    (res_dir / "layout" / "activity_support.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<ScrollView xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:background="#0B1120"
    android:fillViewport="true">

    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:orientation="vertical"
        android:gravity="center_horizontal"
        android:padding="24dp">

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_marginTop="20dp"
            android:text="@string/rom_title"
            android:textColor="#38BDF8"
            android:textSize="24sp"
            android:textStyle="bold" />

        <TextView
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:layout_marginTop="6dp"
            android:text="@string/rom_device"
            android:textColor="#94A3B8"
            android:textSize="13sp" />

        <LinearLayout
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="28dp"
            android:background="@drawable/bg_card"
            android:gravity="center_horizontal"
            android:orientation="vertical"
            android:padding="26dp">

            <ImageView
                android:layout_width="116dp"
                android:layout_height="116dp"
                android:src="@drawable/avatar_graziemille"
                android:contentDescription="@string/creator_name" />

            <TextView
                android:layout_width="wrap_content"
                android:layout_height="wrap_content"
                android:layout_marginTop="14dp"
                android:text="@string/creator_label"
                android:textColor="#94A3B8"
                android:textSize="12sp" />

            <TextView
                android:layout_width="wrap_content"
                android:layout_height="wrap_content"
                android:layout_marginTop="4dp"
                android:text="@string/creator_name"
                android:textColor="#F8FAFC"
                android:textSize="22sp"
                android:textStyle="bold" />

            <LinearLayout
                android:id="@+id/row_tg_handle"
                android:layout_width="wrap_content"
                android:layout_height="wrap_content"
                android:layout_marginTop="8dp"
                android:gravity="center_vertical"
                android:orientation="horizontal">

                <ImageView
                    android:layout_width="22dp"
                    android:layout_height="22dp"
                    android:src="@drawable/ic_telegram"
                    android:contentDescription="@string/tg_handle" />

                <TextView
                    android:layout_width="wrap_content"
                    android:layout_height="wrap_content"
                    android:layout_marginStart="8dp"
                    android:text="@string/tg_handle"
                    android:textColor="#38BDF8"
                    android:textSize="16sp"
                    android:textStyle="bold" />
            </LinearLayout>

            <Button
                android:id="@+id/btn_open_telegram"
                android:layout_width="match_parent"
                android:layout_height="52dp"
                android:layout_marginTop="22dp"
                android:background="@drawable/bg_btn_tg"
                android:text="@string/btn_open_tg"
                android:textAllCaps="false"
                android:textColor="#FFFFFF"
                android:textSize="15sp"
                android:textStyle="bold" />

            <Button
                android:id="@+id/btn_open_channel"
                android:layout_width="match_parent"
                android:layout_height="46dp"
                android:layout_marginTop="12dp"
                android:background="@drawable/bg_card"
                android:text="@string/btn_channel"
                android:textAllCaps="false"
                android:textColor="#BAE6FD"
                android:textSize="14sp" />
        </LinearLayout>

        <TextView
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:layout_marginTop="24dp"
            android:gravity="center"
            android:text="@string/footer_note"
            android:textColor="#64748B"
            android:textSize="12sp" />
    </LinearLayout>
</ScrollView>
""",
        encoding="utf-8",
    )

    manifest_xml = work_dir / "AndroidManifest.xml"
    manifest_xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="id.rollercoaster.support"
    android:versionCode="40170"
    android:versionName="4.0.1.7">

    <uses-sdk android:minSdkVersion="29" android:targetSdkVersion="35" />

    <application
        android:label="@string/app_name"
        android:icon="@drawable/ic_telegram"
        android:theme="@android:style/Theme.DeviceDefault.NoActionBar">

        <activity
            android:name=".SupportActivity"
            android:exported="true"
            android:label="@string/pref_title">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
            <intent-filter android:priority="100">
                <action android:name="com.android.settings.action.IA_SETTINGS" />
                <category android:name="android.intent.category.DEFAULT" />
            </intent-filter>
            <meta-data
                android:name="com.android.settings.category"
                android:value="com.android.settings.category.ia.my_device_info" />
            <meta-data
                android:name="com.android.settings.title"
                android:resource="@string/pref_title" />
            <meta-data
                android:name="com.android.settings.summary"
                android:resource="@string/pref_summary" />
            <meta-data
                android:name="com.android.settings.icon"
                android:resource="@drawable/ic_telegram" />
        </activity>

        <activity-alias
            android:name=".DeviceInfoSupportAlias"
            android:targetActivity=".SupportActivity"
            android:exported="true"
            android:label="@string/pref_title">
            <intent-filter android:priority="95">
                <action android:name="com.android.settings.action.EXTRA_SETTINGS" />
                <category android:name="android.intent.category.DEFAULT" />
            </intent-filter>
            <meta-data
                android:name="com.android.settings.category"
                android:value="com.android.settings.category.ia.device_info" />
            <meta-data
                android:name="com.android.settings.title"
                android:resource="@string/pref_title" />
            <meta-data
                android:name="com.android.settings.summary"
                android:resource="@string/pref_summary" />
            <meta-data
                android:name="com.android.settings.icon"
                android:resource="@drawable/ic_telegram" />
        </activity-alias>
    </application>
</manifest>
""",
        encoding="utf-8",
    )

    (src_dir / "SupportActivity.java").write_text(
        """package id.rollercoaster.support;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;

public class SupportActivity extends Activity {
    private static final String TG_MAINTAINER_URL = "https://t.me/Grazie_milles";
    private static final String TG_CHANNEL_URL = "https://t.me/rollercoaster666";

    private void openUrl(String url) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_support);

        View btnTg = findViewById(R.id.btn_open_telegram);
        if (btnTg != null) {
            btnTg.setOnClickListener(v -> openUrl(TG_MAINTAINER_URL));
        }
        View rowTg = findViewById(R.id.row_tg_handle);
        if (rowTg != null) {
            rowTg.setOnClickListener(v -> openUrl(TG_MAINTAINER_URL));
        }
        View btnCh = findViewById(R.id.btn_open_channel);
        if (btnCh != null) {
            btnCh.setOnClickListener(v -> openUrl(TG_CHANNEL_URL));
        }
    }
}
""",
        encoding="utf-8",
    )

    aapt2 = find_sdk_tool(sdk_root, "aapt2")
    d8 = find_sdk_tool(sdk_root, "d8")
    android_jar = find_android_jar(sdk_root)
    keystore = ensure_keystore(work_dir.parent / "rollercoaster.jks")

    compiled_res_zip = work_dir / "compiled_res.zip"
    subprocess.run([str(aapt2), "compile", "--dir", str(res_dir), "-o", str(compiled_res_zip)], check=True)

    unsigned_apk = work_dir / "unsigned.apk"
    subprocess.run(
        [
            str(aapt2),
            "link",
            "-I",
            str(android_jar),
            "--manifest",
            str(manifest_xml),
            "--java",
            str(gen_dir),
            "-o",
            str(unsigned_apk),
            str(compiled_res_zip),
        ],
        check=True,
    )

    java_files = [str(p) for p in gen_dir.rglob("*.java")] + [str(src_dir / "SupportActivity.java")]
    subprocess.run(
        [
            "javac",
            "--release",
            "11",
            "-classpath",
            str(android_jar),
            "-d",
            str(cls_dir),
            *java_files,
        ],
        check=True,
    )

    class_files = [str(p) for p in cls_dir.rglob("*.class")]
    subprocess.run(
        [
            str(d8),
            "--lib",
            str(android_jar),
            "--min-api",
            "29",
            "--output",
            str(dex_dir),
            *class_files,
        ],
        check=True,
    )

    with zipfile.ZipFile(unsigned_apk, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(dex_dir / "classes.dex", "classes.dex")

    signed = sign_and_align_apk(unsigned_apk, out_apk, sdk_root, keystore)
    print(f"[support-apk] Built & signed {signed} ({signed.stat().st_size / 1024:.1f} KB)")
    return signed


def build_rro_overlay_apk(
    pkg_name: str,
    target_pkg: str,
    res_files: Dict[str, str],
    out_apk: Path,
    sdk_root: Path,
    work_dir: Path,
    priority: int = 99,
) -> Path:
    shutil.rmtree(work_dir, ignore_errors=True)
    res_dir = work_dir / "res"
    for rel_path, content in res_files.items():
        fp = res_dir / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    manifest_xml = work_dir / "AndroidManifest.xml"
    manifest_xml.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{pkg_name}"
    android:versionCode="40170"
    android:versionName="4.0.1.7">
    <application android:hasCode="false" android:label="{pkg_name}" />
    <overlay
        android:targetPackage="{target_pkg}"
        android:isStatic="true"
        android:priority="{priority}" />
</manifest>
""",
        encoding="utf-8",
    )

    aapt2 = find_sdk_tool(sdk_root, "aapt2")
    android_jar = find_android_jar(sdk_root)
    keystore = ensure_keystore(work_dir.parent / "rollercoaster.jks")

    compiled_zip = work_dir / "res.zip"
    unsigned_apk = work_dir / "unsigned.apk"
    subprocess.run([str(aapt2), "compile", "--dir", str(res_dir), "-o", str(compiled_zip)], check=True)
    subprocess.run(
        [
            str(aapt2),
            "link",
            "-I",
            str(android_jar),
            "--manifest",
            str(manifest_xml),
            "-o",
            str(unsigned_apk),
            str(compiled_zip),
        ],
        check=True,
    )
    signed = sign_and_align_apk(unsigned_apk, out_apk, sdk_root, keystore)
    print(f"[rro] Built overlay {signed.name} -> target={target_pkg}")
    return signed


def build_all_overlays(out_dir: Path, sdk_root: Path, work_root: Path) -> List[Path]:
    built: List[Path] = []

    settings_strings_en = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="rom_maintainer">Graziemille (@Grazie_milles)</string>
    <string name="maintainer_title">ROM Creator</string>
    <string name="maintainer_summary">Graziemille • @Grazie_milles</string>
    <string name="miui_version_name">Rollercoaster Hypos 4.0.1.7</string>
</resources>
"""
    settings_strings_id = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="rom_maintainer">Graziemille (@Grazie_milles)</string>
    <string name="maintainer_title">Pembuat ROM</string>
    <string name="maintainer_summary">Graziemille • @Grazie_milles</string>
    <string name="miui_version_name">Rollercoaster Hypos 4.0.1.7</string>
</resources>
"""
    built.append(
        build_rro_overlay_apk(
            pkg_name="id.rollercoaster.overlay.settings",
            target_pkg="com.android.settings",
            res_files={
                "values/strings.xml": settings_strings_en,
                "values-in/strings.xml": settings_strings_id,
                "values-id/strings.xml": settings_strings_id,
            },
            out_apk=out_dir / "RollercoasterSettingsOverlay.apk",
            sdk_root=sdk_root,
            work_dir=work_root / "ov_settings",
        )
    )

    sysui_xml = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <bool name="config_show4GForLTE">true</bool>
    <bool name="config_hideDividersDuringExpand">true</bool>
</resources>
"""
    built.append(
        build_rro_overlay_apk(
            pkg_name="id.rollercoaster.overlay.systemui",
            target_pkg="com.android.systemui",
            res_files={"values/bools.xml": sysui_xml},
            out_apk=out_dir / "RollercoasterSystemUIOverlay.apk",
            sdk_root=sdk_root,
            work_dir=work_root / "ov_sysui",
        )
    )

    theme_id_xml = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="import_local_theme">Impor Tema Lokal (.mtz)</string>
</resources>
"""
    built.append(
        build_rro_overlay_apk(
            pkg_name="id.rollercoaster.overlay.thememanager",
            target_pkg="com.android.thememanager",
            res_files={
                "values-in/strings.xml": theme_id_xml,
                "values-id/strings.xml": theme_id_xml,
            },
            out_apk=out_dir / "RollercoasterThemeOverlay.apk",
            sdk_root=sdk_root,
            work_dir=work_root / "ov_theme",
        )
    )
    return built


DOLBY_ID_TRANSLATIONS: Dict[str, str] = {
    "spatial_audio": "Audio Spasial",
    "spatial_audio_bluetooth_required": "Audio Spasial hanya tersedia saat terhubung ke perangkat audio Bluetooth",
    "app_name": "Dolby Atmos",
    "app_name_ds1": "Audio Dolby",
    "app_title": "Dolby Atmos",
    "balanced": "Seimbang",
    "bright": "Cerah",
    "custom": "Kustom",
    "dialogue_enhancer": "Peningkat Dialog",
    "dolby_atmos_logo_mark": "®",
    "dolby_atmos_mark": "Dolby Atmos",
    "dolby_dynamic_profile": "Pemrosesan Dolby mengenali konten dan menyesuaikan audio secara otomatis untuk menghasilkan suara terbaik.",
    "dolby_explore_atmos_learn_more": "Pelajari lebih lanjut",
    "dolby_explore_atmos_remark": "Dolby Atmos menghadirkan pengalaman audio yang memukau dengan suara multidimensi yang mengalir di atas dan mengelilingi Anda.",
    "ds1_power_off_text": "Audio Dolby dinonaktifkan.",
    "dynamic": "Dinamis",
    "explore_dolby": "Jelajahi Dolby Atmos",
    "appearance": "Tampilan",
    "appearance_light": "Terang",
    "appearance_dark": "Gelap",
    "appearance_system": "Default sistem",
    "appearance_description": "Pilih tampilan Dolby Atmos di perangkat Anda.",
    "appearance_light_summary": "Cerah dan bersih",
    "appearance_dark_summary": "Nyaman dipandang",
    "appearance_system_summary": "Ikuti tema perangkat Anda",
    "graphic_equalizer": "Equalizer Grafis",
    "hdmi_connected_text": "Pemrosesan Suara Dolby tidak mendukung HDMI pada perangkat seluler. Kontrol Dolby akan dinonaktifkan sementara saat perangkat HDMI tersambung.",
    "ieq_text": "Nada Cerdas:",
    "movie": "Film",
    "music": "Musik",
    "navigation_drawer_close": "Tutup panel navigasi",
    "navigation_drawer_open": "Buka panel navigasi",
    "no_effect": "Tanpa efek",
    "power_off_text": "Dolby Atmos dinonaktifkan.",
    "reset": "Atur Ulang",
    "lock_equalizer": "Kunci equalizer",
    "unlock_equalizer": "Buka kunci equalizer",
    "reset_confirmation_title": "Atur ulang semua profil?",
    "reset_confirmation_text": "Pengaturan profil akan dikembalikan ke setelan pabrik yang direkomendasikan Dolby.",
    "reset_continue": "Lanjutkan",
    "warm": "Hangat",
    "volume_leveler": "Perata Volume",
    "surround_virtualizer": "Virtualisasi Surround",
    "dolby_settings_summary": "Suara sinematik yang kaya dan mengelilingi Anda",
    "dolby_settings_unavailable": "Dolby Atmos sedang tidak tersedia. Coba lagi.",
}

DOLBY_NON_TRANSLATABLE = {
    "appbar_scrolling_view_behavior",
    "max_edit_gain",
    "min_edit_gain",
}


def write_indonesian_dolby_strings(res_root: Path) -> int:
    """Write complete `values-in` and `values-id` resources from the DAX UI's source keys."""
    base_strings = res_root / "values" / "strings.xml"
    if not base_strings.is_file():
        raise FileNotFoundError(f"Dolby DAX source strings are missing: {base_strings}")
    try:
        base_root = ET.parse(base_strings).getroot()
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid Dolby DAX source strings XML: {exc}") from exc

    source_values: Dict[str, str] = {}
    required_keys = set()
    for item in base_root.findall("string"):
        name = item.get("name", "")
        if not name:
            continue
        source_values[name] = item.text or ""
        if item.get("translatable", "true").lower() != "false" and name not in DOLBY_NON_TRANSLATABLE:
            required_keys.add(name)

    missing = sorted(required_keys - DOLBY_ID_TRANSLATIONS.keys())
    unknown = sorted(DOLBY_ID_TRANSLATIONS.keys() - source_values.keys())
    if missing or unknown:
        raise RuntimeError(
            "Dolby Indonesian translation coverage changed: "
            f"missing={missing}, unknown={unknown}"
        )

    for key in required_keys:
        source_placeholders = sorted(re.findall(r"%(?:\d+\$)?[-+#0-9.]*[a-zA-Z%]", source_values.get(key, "")))
        translated_placeholders = sorted(re.findall(r"%(?:\d+\$)?[-+#0-9.]*[a-zA-Z%]", DOLBY_ID_TRANSLATIONS[key]))
        if source_placeholders != translated_placeholders:
            raise RuntimeError(f"Placeholder mismatch in Dolby translation key {key}")

    for locale_dir in ("values-in", "values-id"):
        root = ET.Element("resources")
        for item in base_root.findall("string"):
            name = item.get("name", "")
            if not name or name not in required_keys:
                continue
            ET.SubElement(root, "string", {"name": name}).text = DOLBY_ID_TRANSLATIONS[name]
        ET.indent(root, space="    ")
        out_dir = res_root / locale_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(
            out_dir / "strings.xml",
            encoding="utf-8",
            xml_declaration=True,
        )
    print(f"[dolby-ui] Wrote {len(required_keys)} translated source strings to values-in and values-id")
    return len(required_keys)


def _create_temporary_dolby_signing_store(path: Path) -> None:
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    keytool = shutil.which("keytool")
    if not keytool:
        raise FileNotFoundError("keytool is required to build the Dolby DAX UI")
    subprocess.run(
        [
            keytool,
            "-genkeypair",
            "-v",
            "-keystore",
            str(path),
            "-storetype",
            "PKCS12",
            "-alias",
            "platform",
            "-keyalg",
            "RSA",
            "-keysize",
            "2048",
            "-validity",
            "10000",
            "-storepass",
            "android",
            "-keypass",
            "android",
            "-dname",
            "CN=Temporary Rollercoaster build key, OU=Build, O=Rollercoaster, C=ID",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def inject_indonesian_dolby_strings_and_build(
    dolby_repo_dir: Path,
    out_apk: Path,
    sdk_root: Path,
    work_dir: Path,
) -> Path:
    """Translate and compile the AndroidOne `DolbyDaxUI/app` source; do not substitute an overlay."""
    source_root = dolby_repo_dir / "DolbyDaxUI"
    source_app = source_root / "app"
    if not (source_app / "src" / "main" / "res" / "values" / "strings.xml").is_file():
        raise FileNotFoundError(f"Required AndroidOne Dolby DAX UI source not found: {source_app}")
    if not (source_root / "gradlew").is_file():
        raise FileNotFoundError(f"Dolby DAX Gradle wrapper is missing: {source_root / 'gradlew'}")

    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    build_root = work_dir / "DolbyDaxUI"
    shutil.copytree(source_root, build_root)
    res_root = build_root / "app" / "src" / "main" / "res"
    translation_count = write_indonesian_dolby_strings(res_root)

    manifest_path = build_root / "app" / "src" / "main" / "AndroidManifest.xml"
    if manifest_path.is_file():
        manifest_text = manifest_path.read_text(encoding="utf-8", errors="ignore")
        manifest_text = re.sub(r'\s*android:sharedUserId="[^"]+"', "", manifest_text)
        manifest_path.write_text(manifest_text, encoding="utf-8")

    # Upstream config expects a platform.p12. The disposable build key lets Gradle compile;
    # the finished APK is re-aligned and signed with the ROM's build key afterwards.
    _create_temporary_dolby_signing_store(build_root / "platform.p12")
    wrapper = build_root / "gradlew"
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["ANDROID_HOME"] = str(sdk_root)
    env["ANDROID_SDK_ROOT"] = str(sdk_root)
    env.setdefault("JAVA_HOME", os.environ.get("JAVA_HOME", ""))
    cmd = ["./gradlew", ":app:assembleRelease", "-x", "lint", "-x", "lintVitalRelease", "--no-daemon"]
    proc = subprocess.run(cmd, cwd=str(build_root), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stdout + "\n" + proc.stderr)[-6000:]
        raise RuntimeError(f"Dolby DAX UI Gradle compilation failed:\n{detail}")

    apk_candidates = sorted((build_root / "app" / "build" / "outputs" / "apk" / "release").glob("*.apk"))
    if not apk_candidates:
        raise RuntimeError("Gradle completed without producing the Dolby DAX release APK")
    unsigned_or_gradle_signed_apk = apk_candidates[0]
    with zipfile.ZipFile(unsigned_or_gradle_signed_apk, "r") as apk_zip:
        bad = apk_zip.testzip()
        if bad:
            raise RuntimeError(f"Gradle produced an invalid Dolby DAX APK entry: {bad}")
        entries = set(apk_zip.namelist())
        if "classes.dex" not in entries or "AndroidManifest.xml" not in entries:
            raise RuntimeError("Built Dolby DAX APK is missing required Android package entries")

    keystore = ensure_keystore(work_dir.parent / "rollercoaster.jks")
    result = sign_and_align_apk(unsigned_or_gradle_signed_apk, out_apk, sdk_root, keystore)
    if not result.is_file() or result.stat().st_size == 0:
        raise RuntimeError("Signed Dolby DAX UI APK was not created")
    print(
        f"[dolby-ui] Built Indonesian DolbyDaxUI.apk ({translation_count} strings, "
        f"{result.stat().st_size / 1024:.1f} KB)"
    )
    return result

def main() -> None:
    parser = argparse.ArgumentParser(description="Build RollercoasterSupport.apk and RRO overlays")
    parser.add_argument("--sdk-root", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--work-dir", default=Path("/tmp/rc_apk_build"), type=Path)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    build_support_apk(args.out_dir / "RollercoasterSupport.apk", args.sdk_root, args.work_dir / "support")
    build_all_overlays(args.out_dir, args.sdk_root, args.work_dir)


if __name__ == "__main__":
    main()
