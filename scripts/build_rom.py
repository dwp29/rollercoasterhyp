#!/usr/bin/env python3
"""Master End-to-End ROM Builder for Rolleracoaster Hypos 4.0.1.7 (Redmi Note 9 Pro / joyeuse)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_support_and_overlays import (
    build_all_overlays,
    build_support_apk,
    inject_indonesian_dolby_strings_and_build,
)
from generate_bootanimation import generate_bootanimation
from generate_ios_theme import generate_mtz
from patch_boot_kernel import disable_vbmeta_verification, patch_boot_image, sha256_file
from rom_container import repack_rom_workspace, run_cmd, unpack_rom_zip


def safe_remove(target: Path) -> None:
    """Safely remove any file, read-only EROFS directory, or dangling symlink."""
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    elif target.is_dir():
        for root, _dirs, files in os.walk(target, followlinks=False):
            rp = Path(root)
            try:
                if not rp.is_symlink():
                    os.chmod(rp, rp.stat().st_mode | 0o700)
            except OSError:
                pass
            for fn in files:
                fp = rp / fn
                try:
                    if not fp.is_symlink():
                        os.chmod(fp, fp.stat().st_mode | 0o200)
                except OSError:
                    pass
        shutil.rmtree(target)


def safe_copy(src: Path, dst: Path) -> None:
    """Copy `src` to `dst`, unlinking `dst` first if it is a symlink or read-only EROFS file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        safe_remove(dst)
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)
        try:
            os.chmod(dst, dst.stat().st_mode | 0o200)
        except OSError:
            pass


def safe_write_text(dst: Path, content: str) -> None:
    """Write text to `dst`, replacing any dangling symlink or read-only file cleanly."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink(missing_ok=True)
    dst.write_text(content, encoding="utf-8")


def download_file(url: str, dest: Path, expected_sha256: str = "") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        if not expected_sha256 or sha256_file(dest).lower() == expected_sha256.lower():
            print(f"[dl] Cached {dest.name} ({dest.stat().st_size / (1024**2):.1f} MiB)")
            return dest
        dest.unlink()

    print(f"[dl] Downloading {url} -> {dest}")
    if shutil.which("aria2c") and "pixeldrain" in url:
        run_cmd(
            [
                "aria2c",
                "-x4",
                "-s4",
                "-k4M",
                "--file-allocation=none",
                "--max-tries=5",
                "--retry-wait=5",
                "-d",
                str(dest.parent),
                "-o",
                dest.name,
                url,
            ]
        )
    else:
        run_cmd(["curl", "-fL", "--retry", "5", "--retry-delay", "5", "-o", str(dest), url])

    if expected_sha256:
        got = sha256_file(dest)
        if got.lower() != expected_sha256.lower():
            raise RuntimeError(f"SHA-256 mismatch for {dest.name}: expected {expected_sha256}, got {got}")
    return dest


def gh_api_json(url: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "RollercoasterROM-Builder/4.0",
    }
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest_github_asset(
    repo_slug: str,
    name_regex: str,
    dest_dir: Path,
    fallback_url: str = "",
) -> Optional[Path]:
    try:
        rel = gh_api_json(f"https://api.github.com/repos/{repo_slug}/releases/latest")
        pat = re.compile(name_regex, re.IGNORECASE)
        for asset in rel.get("assets", []):
            aname = asset.get("name", "")
            if pat.search(aname):
                return download_file(asset["browser_download_url"], dest_dir / aname)
    except Exception as exc:
        print(f"[gh-asset] API lookup note on {repo_slug} ({exc})")

    if fallback_url:
        fname = fallback_url.rstrip("/").split("/")[-1]
        try:
            return download_file(fallback_url, dest_dir / fname)
        except Exception as exc:
            print(f"[gh-asset] Fallback URL failed for {repo_slug}: {exc}")
    return None


def fetch_revanced_ksu_zips(repos: List[str], dest_dir: Path) -> Dict[str, Path]:
    found: Dict[str, Path] = {}
    for slug in repos:
        try:
            releases = gh_api_json(f"https://api.github.com/repos/{slug}/releases?per_page=8")
            for rel in releases:
                for asset in rel.get("assets", []):
                    aname = asset.get("name", "")
                    low = aname.lower()
                    if not low.endswith(".zip") or ("magisk" not in low and "revanced" not in low):
                        continue
                    if "microg" in low or "gmscore" in low:
                        continue
                    if "yt" not in found and (
                        ("youtube-revanced" in low or "youtube-rvx" in low or re.search(r"^youtube.*magisk.*\.zip$", low))
                        and "music" not in low
                    ):
                        found["yt"] = download_file(asset["browser_download_url"], dest_dir / aname)
                    elif "ytm" not in found and (
                        "music" in low and ("revanced" in low or "rvx" in low or "magisk" in low)
                    ):
                        found["ytm"] = download_file(asset["browser_download_url"], dest_dir / aname)
                if "yt" in found and "ytm" in found:
                    return found
        except Exception as exc:
            print(f"[revanced] Note querying {slug}: {exc}")
    return found


def fetch_insular_apk(fdroid_api_url: str, dest_dir: Path) -> Optional[Path]:
    try:
        req = urllib.request.Request(fdroid_api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        vcode = meta.get("suggestedVersionCode") or meta["packages"][0]["versionCode"]
        apk_url = f"https://f-droid.org/repo/com.oasisfeng.island.fdroid_{vcode}.apk"
        return download_file(apk_url, dest_dir / f"Insular_{vcode}.apk")
    except Exception as exc:
        print(f"[insular] Warning fetching Insular from F-Droid: {exc}")
        return None


def clone_shallow(repo_url: str, dest_dir: Path) -> Path:
    if (dest_dir / ".git").is_dir():
        return dest_dir
    shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "clone", "--depth=1", "--single-branch", repo_url, str(dest_dir)])
    return dest_dir


def patch_build_prop_file(prop_path: Path, updates: Dict[str, str], preserve_keys: Optional[Set[str]] = None) -> None:
    if prop_path.is_symlink() or not prop_path.is_file():
        return
    preserve = preserve_keys or set()
    lines = prop_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    applied: Set[str] = set()
    out_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in preserve:
            out_lines.append(line)
            applied.add(k)
        elif k in updates:
            out_lines.append(f"{k}={updates[k]}")
            applied.add(k)
        else:
            out_lines.append(line)

    missing = [k for k in updates if k not in applied and k not in preserve]
    if missing:
        out_lines.append("")
        out_lines.append("# --- Rolleracoaster Hypos 4.0.1.7 Custom Properties ---")
        for k in missing:
            out_lines.append(f"{k}={updates[k]}")

    safe_write_text(prop_path, "\n".join(out_lines) + "\n")


def patch_vendor_fstabs_strip_avb(vendor_dir: Path) -> int:
    patched = 0
    for fstab in list((vendor_dir / "etc").glob("fstab.*")):
        if fstab.is_symlink() or not fstab.is_file():
            continue
        orig = fstab.read_text(encoding="utf-8", errors="ignore")
        new_lines = []
        for ln in orig.splitlines():
            if not ln.strip() or ln.lstrip().startswith("#"):
                new_lines.append(ln)
                continue
            cols = ln.split()
            if len(cols) >= 5:
                flags = [
                    fl
                    for fl in cols[4].split(",")
                    if not fl.startswith("avb")
                    and not fl.startswith("verify")
                    and fl != "avb_keys=/avb/q-gsi.avbpubkey:/avb/r-gsi.avbpubkey:/avb/s-gsi.avbpubkey"
                ]
                cols[4] = ",".join(flags) if flags else "defaults"
                ln = "\t".join(cols)
            new_lines.append(ln)
        updated = "\n".join(new_lines) + "\n"
        if updated != orig:
            safe_write_text(fstab, updated)
            patched += 1
    return patched


def patch_gps_indonesia(vendor_dir: Path) -> None:
    gps_conf = vendor_dir / "etc" / "gps.conf"
    ntp_block = (
        "\n# Rollercoaster Indonesia Fast AGPS/NTP\n"
        "NTP_SERVER=id.pool.ntp.org\n"
        "NTP_SERVER_1=0.id.pool.ntp.org\n"
        "NTP_SERVER_2=asia.pool.ntp.org\n"
        "NTP_SERVER_3=time.google.com\n"
        "INTERMEDIATE_POS=1\n"
        "ACCURACY_THRES=5000\n"
    )
    if gps_conf.is_file() and not gps_conf.is_symlink():
        txt = gps_conf.read_text(encoding="utf-8", errors="ignore")
        txt = re.sub(r"^NTP_SERVER\s*=.*$", "NTP_SERVER=id.pool.ntp.org", txt, flags=re.MULTILINE)
        if "id.pool.ntp.org" not in txt:
            txt += ntp_block
        safe_write_text(gps_conf, txt)
    else:
        safe_write_text(gps_conf, ntp_block.lstrip())


def patch_device_features_for_joyeuse(product_dir: Path, system_dir: Path) -> int:
    dirs = [
        product_dir / "etc" / "device_features",
        system_dir / "etc" / "device_features",
    ]
    count = 0
    for df_dir in dirs:
        if not df_dir.is_dir():
            continue
        xmls = list(df_dir.glob("*.xml"))
        if not xmls:
            continue
        joyeuse_xml = df_dir / "joyeuse.xml"
        if not joyeuse_xml.is_file():
            src_xml = df_dir / "miatoll.xml" if (df_dir / "miatoll.xml").is_file() else xmls[0]
            safe_copy(src_xml, joyeuse_xml)
            xmls.append(joyeuse_xml)

        desired_bools = {
            "support_nfc": "true",
            "support_hce": "true",
            "support_ese": "true",
            "support_uicc": "true",
            "support_se_route": "true",
            "is_suppot_finger_fod": "false",
            "is_support_finger_fod": "false",
            "is_SideFingerprint": "true",
            "support_ir": "true",
            "support_headset": "true",
            "support_ai_task": "true",
            "support_screen_recorder_high_fps": "true",
            "support_dolby": "true",
        }
        for xp in set(xmls):
            if xp.is_symlink() or not xp.is_file():
                continue
            try:
                tree = ET.parse(xp)
                root = tree.getroot()
                existing = {el.get("name"): el for el in root.findall("bool")}
                for bname, bval in desired_bools.items():
                    if bname in existing:
                        existing[bname].text = bval
                    else:
                        el = ET.SubElement(root, "bool", {"name": bname})
                        el.text = bval
                ET.indent(tree, space="    ")
                tree.write(xp, encoding="utf-8", xml_declaration=True)
                count += 1
            except Exception as exc:
                print(f"[device_features] Warning parsing {xp}: {exc}")
    return count


def merge_dolby_and_spatial_audio_effects(audio_effects_xml: Path) -> bool:
    if audio_effects_xml.is_symlink() or not audio_effects_xml.is_file():
        return False
    try:
        tree = ET.parse(audio_effects_xml)
        root = tree.getroot()
    except Exception as exc:
        print(f"[dolby-xml] Skipping non-XML or malformed {audio_effects_xml}: {exc}")
        return False

    libs_el = root.find("libraries")
    effs_el = root.find("effects")
    if libs_el is None or effs_el is None:
        return False

    def ensure_lib(name: str, path: str) -> None:
        for el in libs_el.findall("library"):
            if el.get("name") == name:
                el.set("path", path)
                return
        ET.SubElement(libs_el, "library", {"name": name, "path": path})

    def ensure_effect(name: str, lib: str, uuid: str) -> None:
        for el in effs_el.findall("effect"):
            if el.get("name") == name or el.get("uuid", "").lower() == uuid.lower():
                el.set("name", name)
                el.set("library", lib)
                el.set("uuid", uuid)
                return
        ET.SubElement(effs_el, "effect", {"name": name, "library": lib, "uuid": uuid})

    ensure_lib("dap", "libswdap.so")
    ensure_lib("dvl", "libdlbvol.so")
    ensure_lib("spatializer", "libspatialaudio.so")

    ensure_effect("dap", "dap", "9d4921da-8225-4f29-aefa-39537a04bcaa")
    ensure_effect("dlb_music_listener", "dvl", "40f66c8b-5aa5-4345-8919-53ec431aaa98")
    ensure_effect("dlb_movie_listener", "dvl", "4638f700-be15-11e5-a837-0800200c9a66")
    ensure_effect("dlb_ring_listener", "dvl", "21d14087-558a-4f21-94a9-5002dce64bce")
    ensure_effect("dlb_alarm_listener", "dvl", "6aff229c-30c6-4cc8-9957-dbfe5c1bd7f6")
    ensure_effect("dlb_notification_listener", "dvl", "1f0091e3-6ad8-40fe-9b09-5948f9a26e7e")
    ensure_effect("spatializer", "spatializer", "ccd4cf09-a79d-46c2-9aae-06a1698d6c8f")

    postproc = root.find("postprocess")
    if postproc is not None:
        for stream in postproc.findall("stream"):
            for apply_el in list(stream.findall("apply")):
                if apply_el.get("effect", "").startswith("dlb_"):
                    stream.remove(apply_el)

    ET.indent(tree, space="    ")
    tree.write(audio_effects_xml, encoding="utf-8", xml_declaration=True)
    return True


def integrate_cruecy_dolby_and_spatial(
    dolby_repo: Path,
    vendor_dir: Path,
    system_ext_dir: Path,
    product_dir: Path,
    custom_dax_apk: Optional[Path],
    custom_dolby_overlay: Optional[Path],
) -> None:
    copy_count = 0
    blob_roots = [dolby_repo / "proprietary", dolby_repo]
    for broot in blob_roots:
        v_src = broot / "vendor"
        if v_src.is_dir():
            for src_f in v_src.rglob("*"):
                if src_f.is_file():
                    rel = src_f.relative_to(v_src)
                    safe_copy(src_f, vendor_dir / rel)
                    copy_count += 1
            break

    vintf_frag = vendor_dir / "etc" / "vintf" / "manifest" / "dolby_dms.xml"
    if not vintf_frag.is_file():
        safe_write_text(
            vintf_frag,
            """<manifest version="1.0" type="device">
    <hal format="hidl">
        <name>vendor.dolby.hardware.dms</name>
        <transport>hwbinder</transport>
        <version>2.0</version>
        <interface>
            <name>IDms</name>
            <instance>default</instance>
        </interface>
    </hal>
</manifest>
""",
        )

    for ae in [
        vendor_dir / "etc" / "audio_effects.xml",
        vendor_dir / "etc" / "audio" / "sku_atoll" / "audio_effects.xml",
    ]:
        if ae.is_file():
            merge_dolby_and_spatial_audio_effects(ae)

    if custom_dax_apk and custom_dax_apk.is_file():
        safe_copy(custom_dax_apk, system_ext_dir / "priv-app" / "DolbyDaxUI" / "DolbyDaxUI.apk")
    else:
        prebuilt_apks = list(dolby_repo.rglob("*Dolby*.apk")) + list(dolby_repo.rglob("*Dax*.apk"))
        if prebuilt_apks:
            safe_copy(prebuilt_apks[0], system_ext_dir / "priv-app" / "DolbyDaxUI" / "DolbyDaxUI.apk")

    if custom_dolby_overlay and custom_dolby_overlay.is_file():
        safe_copy(custom_dolby_overlay, product_dir / "overlay" / custom_dolby_overlay.name)

    print(f"[dolby] Integrated {copy_count} vendor Dolby/Spatial blobs + Indonesian UI")


def integrate_leica_camera(
    cam_vendor_repo: Path,
    cam_device_repo: Path,
    system_dir: Path,
    product_dir: Path,
    system_ext_dir: Path,
) -> bool:
    existing_cams = (
        list(product_dir.glob("priv-app/*MiuiCamera*/*.apk"))
        + list(system_dir.glob("priv-app/*MiuiCamera*/*.apk"))
        + list(system_ext_dir.glob("priv-app/*MiuiCamera*/*.apk"))
    )

    cam_apks = list(cam_vendor_repo.rglob("MiuiCamera.apk"))
    if not cam_apks:
        if existing_cams:
            print(f"[camera] Preserved ROM native Leica/MIUI Camera ({existing_cams[0]})")
            return True
        return False

    for old_c in existing_cams:
        safe_remove(old_c.parent)

    prop_root = cam_vendor_repo / "proprietary"
    search_root = prop_root if prop_root.is_dir() else cam_vendor_repo
    copied = 0
    for sub, target in (("product", product_dir), ("system_ext", system_ext_dir), ("system", system_dir)):
        sdir = search_root / sub
        if sdir.is_dir():
            for f in sdir.rglob("*"):
                if f.is_file() and not f.name.endswith(".mk") and not f.name.endswith(".bp"):
                    rel = f.relative_to(sdir)
                    safe_copy(f, target / rel)
                    copied += 1

    for xml_f in cam_device_repo.rglob("privapp-permissions*camera*.xml"):
        safe_copy(xml_f, product_dir / "etc" / "permissions" / xml_f.name)

    print(f"[camera] Integrated Xiaomi Leica Camera v6.8 miatoll ({copied} files)")
    return True


def debloat_cn_packages(
    part_dirs: Iterable[Path],
    debloat_names: List[str],
    keep_guard: List[str],
) -> List[str]:
    removed: List[str] = []
    debloat_low = {n.lower() for n in debloat_names}
    guard_low = {g.lower() for g in keep_guard}

    for pdir in part_dirs:
        for app_sub in ("app", "priv-app", "data-app"):
            adir = pdir / app_sub
            if not adir.is_dir():
                continue
            for item in sorted(adir.iterdir()):
                if not item.is_dir() and not item.is_symlink():
                    continue
                low = item.name.lower()
                if any(g == low or g in low for g in guard_low):
                    continue
                if low in debloat_low or any(low.startswith(d) for d in debloat_low if len(d) >= 5):
                    safe_remove(item)
                    removed.append(f"{pdir.name}/{app_sub}/{item.name}")
    print(f"[debloat] Cleanly removed {len(removed)} CN bloat/ad packages")
    return removed


def install_prebuilt_apk(apk_path: Path, folder_name: str, base_dir: Path, priv: bool = False) -> None:
    if not apk_path or not apk_path.is_file():
        return
    sub = "priv-app" if priv else "app"
    dest = base_dir / sub / folder_name / f"{folder_name}.apk"
    safe_copy(apk_path, dest)
    print(f"[apk] Installed {folder_name}.apk -> {base_dir.name}/{sub}/{folder_name}/")


def seed_ksu_and_modules(
    system_dir: Path,
    ksud_bin: Path,
    core_module_src: Path,
    module_zips: List[Path],
) -> None:
    seed_root = system_dir / "etc" / "rollercoaster_seed"
    mods_dir = seed_root / "modules"
    mods_dir.mkdir(parents=True, exist_ok=True)

    safe_copy(ksud_bin, seed_root / "ksud")

    rc_mod_zip = mods_dir / "00_rollercoaster_core.zip"
    with zipfile.ZipFile(rc_mod_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(core_module_src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(core_module_src).as_posix())

    for idx, mzip in enumerate(module_zips, start=1):
        if mzip and mzip.is_file():
            safe_copy(mzip, mods_dir / f"{idx:02d}_{mzip.name}")

    seed_sh = system_dir / "bin" / "rollercoaster_seed.sh"
    safe_write_text(
        seed_sh,
        """#!/system/bin/sh
# First-boot KernelSU-Next + SuSFS + PlayIntegrity + ReVanced auto-provisioner
SEED_DIR="/system/etc/rollercoaster_seed"
MARKER="/data/adb/.rollercoaster_4_0_1_7_seeded"

mkdir -p /data/adb/ksu/bin /data/adb/modules 2>/dev/null
chmod 0700 /data/adb 2>/dev/null

if [ -f "$SEED_DIR/ksud" ]; then
    cp -f "$SEED_DIR/ksud" /data/adb/ksud
    chmod 0755 /data/adb/ksud
    ln -sf /data/adb/ksud /data/adb/ksu/bin/ksud
fi

if [ ! -f "$MARKER" ]; then
    for mzip in "$SEED_DIR"/modules/*.zip; do
        [ -f "$mzip" ] || continue
        mid=$(unzip -p "$mzip" module.prop 2>/dev/null | grep '^id=' | head -n1 | cut -d= -f2 | tr -d '\r')
        [ -n "$mid" ] || continue
        mkdir -p "/data/adb/modules/$mid"
        unzip -oq "$mzip" -d "/data/adb/modules/$mid"
        rm -f "/data/adb/modules/$mid/disable" "/data/adb/modules/$mid/remove"
        chmod -R 0755 "/data/adb/modules/$mid"/*.sh 2>/dev/null
    done
    touch "$MARKER"
fi
""",
    )

    init_rc = system_dir / "etc" / "init" / "rollercoaster_seed.rc"
    safe_write_text(
        init_rc,
        """on post-fs-data
    exec_background u:r:su:s0 root root -- /system/bin/sh /system/bin/rollercoaster_seed.sh

on property:sys.boot_completed=1
    exec_background u:r:magisk:s0 root root -- /system/bin/sh /system/bin/rollercoaster_seed.sh
    exec_background u:r:su:s0 root root -- /system/bin/sh /system/etc/rollercoaster_late.sh
""",
    )
    safe_copy(core_module_src / "service.sh", system_dir / "etc" / "rollercoaster_late.sh")
    print(f"[ksu-seed] Packaged ksud + {1 + len([m for m in module_zips if m and m.is_file()])} pre-installed KSU modules")


def prepare_port_tools(repo_url: str, otatools_url: str, cache_dir: Path) -> Dict[str, Any]:
    tdir = clone_shallow(repo_url, cache_dir / "HyperOS-Port-Python")
    bin_dir = tdir / "bin" / "linux" / "x86_64"
    if not bin_dir.is_dir():
        cands = list(tdir.rglob("magiskboot"))
        bin_dir = cands[0].parent if cands else Path("/usr/bin")

    tools: Dict[str, Any] = {}
    for name in ("magiskboot", "lpunpack", "lpmake", "lpdump", "mkfs.erofs", "extract.erofs"):
        cand = bin_dir / name
        if cand.is_file():
            cand.chmod(0o755)
            tools[name] = cand
        elif shutil.which(name):
            tools[name] = Path(shutil.which(name))  # type: ignore[arg-type]

    if ("lpmake" not in tools or "lpdump" not in tools) and otatools_url:
        ota_dir = cache_dir / "otatools"
        ota_bin = ota_dir / "bin"
        if not (ota_bin / "lpmake").is_file():
            ota_zip = download_file(otatools_url, cache_dir / "otatools.zip")
            ota_dir.mkdir(parents=True, exist_ok=True)
            run_cmd(["unzip", "-oq", str(ota_zip), "-d", str(ota_dir)])
            for p in ota_bin.glob("*"):
                if p.is_file():
                    p.chmod(0o755)
        for name in ("lpmake", "lpdump", "lpunpack"):
            if name not in tools and (ota_bin / name).is_file():
                tools[name] = ota_bin / name
        ota_lib = ota_dir / "lib64"
        if ota_lib.is_dir():
            cur_ld = os.environ.get("LD_LIBRARY_PATH", "")
            tools["_env"] = {"LD_LIBRARY_PATH": f"{ota_lib}:{cur_ld}" if cur_ld else str(ota_lib)}

    props_hook = tdir / "devices" / "common" / "PropsHook.zip"
    if props_hook.is_file():
        tools["props_hook_zip"] = props_hook

    missing = [k for k in ("magiskboot", "mkfs.erofs", "extract.erofs", "lpunpack", "lpmake") if k not in tools]
    if missing:
        raise RuntimeError(f"Missing required ROM build binaries: {missing}")
    return tools


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Rolleracoaster Hypos 4.0.1.7 ROM for joyeuse")
    parser.add_argument("--config", default=Path("config/rollercoaster_joyeuse.json"), type=Path)
    parser.add_argument("--work-dir", default=Path("work"), type=Path)
    parser.add_argument("--out-dir", default=Path("out"), type=Path)
    parser.add_argument("--sdk-root", default=Path(os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")), type=Path)
    parser.add_argument("--base-rom-override", default=None, type=Path)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    cfg = json.loads((repo_root / args.config).read_text(encoding="utf-8"))
    rom_cfg = cfg["rom"]
    src_cfg = cfg["sources"]

    work_dir = args.work_dir.resolve()
    dl_dir = work_dir / "downloads"
    repos_dir = work_dir / "repos"
    gen_dir = work_dir / "generated"
    out_dir = args.out_dir.resolve()
    for d in (dl_dir, repos_dir, gen_dir, out_dir):
        d.mkdir(parents=True, exist_ok=True)

    print("==================================================================")
    print(f" Building {rom_cfg['version_display']} for {rom_cfg['model']} ({rom_cfg['device']})")
    print(f" Maintainer: {rom_cfg['maintainer']} ({rom_cfg['telegram_username']})")
    print("==================================================================")

    tools = prepare_port_tools(src_cfg["hyperos_port_tools_repo"], src_cfg.get("otatools_zip", ""), repos_dir)
    dolby_repo = clone_shallow(src_cfg["dolby_vendor_repo"], repos_dir / "vendor_dolby")
    cam_vendor = clone_shallow(src_cfg["miuicamera_vendor_repo"], repos_dir / "miuicamera_vendor")
    cam_device = clone_shallow(src_cfg["miuicamera_device_repo"], repos_dir / "miuicamera_device")

    ksu_apk = fetch_latest_github_asset("KernelSU-Next/KernelSU-Next", r"KernelSU_Next.*\.apk$", dl_dir, src_cfg["ksu_manager_apk"])
    webui_apk = fetch_latest_github_asset("5ec1cff/KsuWebUIStandalone", r"\.apk$", dl_dir, src_cfg["ksu_webui_apk"])
    adaway_apk = fetch_latest_github_asset("AdAway/AdAway", r"AdAway.*\.apk$", dl_dir, src_cfg["adaway_apk"])
    heliboard_apk = fetch_latest_github_asset("Helium314/HeliBoard", r"HeliBoard.*release\.apk$", dl_dir, src_cfg["heliboard_apk"])
    insular_apk = fetch_insular_apk(src_cfg["insular_fdroid_api"], dl_dir)

    pif_zip = fetch_latest_github_asset("osm0sis/PlayIntegrityFork", r"PlayIntegrityFork.*\.zip$", dl_dir, src_cfg["play_integrity_fork_zip"])
    tricky_zip = fetch_latest_github_asset("5ec1cff/TrickyStore", r"Tricky.*\.zip$", dl_dir, src_cfg["tricky_store_zip"])
    susfs_zip = fetch_latest_github_asset("sidex15/susfs4ksu-module", r"\.zip$", dl_dir, src_cfg["susfs_module_zip"])
    revanced_zips = fetch_revanced_ksu_zips(src_cfg["revanced_repos"], dl_dir)

    bootanim_zip = generate_bootanimation(gen_dir / "bootanimation.zip")
    ios_mtz = generate_mtz(gen_dir / src_cfg["ios_theme_mtz_name"])
    support_apk = build_support_apk(gen_dir / "RollercoasterSupport.apk", args.sdk_root, gen_dir / "_apk_work" / "support")
    overlay_apks = build_all_overlays(gen_dir / "overlays", args.sdk_root, gen_dir / "_apk_work")
    dax_apk = inject_indonesian_dolby_strings_and_build(
        dolby_repo,
        gen_dir / "DolbyDaxUI.apk",
        args.sdk_root,
        gen_dir / "_apk_work" / "dolby",
    )
    dolby_ov = gen_dir / "RollercoasterDolbyOverlay.apk"

    base_rom_zip = args.base_rom_override or download_file(
        src_cfg["base_rom_url"],
        dl_dir / src_cfg["base_rom_filename"],
        expected_sha256=src_cfg["base_rom_sha256"],
    )
    ws = unpack_rom_zip(base_rom_zip, work_dir / "rom_ws", tools)
    if args.base_rom_override is None:
        base_rom_zip.unlink(missing_ok=True)

    kernel_zip = repo_root / "prebuilt" / "kernel" / src_cfg["kernel_zip_name"]
    ksud_out = gen_dir / "ksud"
    patch_boot_image(
        magiskboot=tools["magiskboot"],
        rom_boot_img=ws.boot_img_path,
        kernel_zip=kernel_zip,
        expected_kernel_sha256=src_cfg["kernel_zip_sha256"],
        out_boot_img=ws.boot_img_path,
        out_ksud=ksud_out,
        work_dir=work_dir / "boot_patch",
    )
    for vbm in ws.vbmeta_paths:
        disable_vbmeta_verification(vbm)

    sys_rec = ws.partitions.get("system")
    prod_rec = ws.partitions.get("product")
    sext_rec = ws.partitions.get("system_ext")
    vend_rec = ws.partitions.get("vendor")
    if not sys_rec:
        raise RuntimeError("Missing 'system' partition in unpacked ROM")

    s_root = sys_rec.extract_dir
    s_dir = (s_root / "system") if (s_root / "system" / "build.prop").is_file() else s_root
    p_dir = prod_rec.extract_dir if prod_rec else (s_dir / "product")
    se_dir = sext_rec.extract_dir if sext_rec else (s_dir / "system_ext")
    v_dir = vend_rec.extract_dir if vend_rec else (s_dir / "vendor")

    patch_build_prop_file(s_dir / "build.prop", cfg["props"]["system"])
    for pp in (p_dir / "etc" / "build.prop", p_dir / "build.prop"):
        patch_build_prop_file(pp, cfg["props"]["product"])
    patch_build_prop_file(
        v_dir / "build.prop",
        cfg["props"]["vendor"],
        preserve_keys={"ro.product.vendor.device", "ro.product.vendor.name", "ro.vendor.build.fingerprint"},
    )
    for mp in ws.partitions.values():
        for bp in (mp.extract_dir / "etc" / "build.prop", mp.extract_dir / "build.prop"):
            if bp.is_file() and not bp.is_symlink() and mp.base_name not in ("vendor", "odm"):
                patch_build_prop_file(
                    bp,
                    {
                        "ro.mi.os.version.show": rom_cfg["version_display"],
                        "ro.build.display.id": rom_cfg["version_display"],
                    },
                )

    patch_vendor_fstabs_strip_avb(v_dir)
    patch_gps_indonesia(v_dir)
    patch_device_features_for_joyeuse(p_dir, s_dir)

    debloat_cn_packages([s_dir, p_dir, se_dir], cfg["debloat_dirs"], cfg["keep_packages_guard"])

    for ba_target in (p_dir / "media" / "bootanimation.zip", s_dir / "media" / "bootanimation.zip"):
        if ba_target.parent.is_dir() or ba_target == p_dir / "media" / "bootanimation.zip":
            safe_copy(bootanim_zip, ba_target)

    safe_copy(ios_mtz, s_dir / "rollercoaster" / "themes" / src_cfg["ios_theme_mtz_name"])
    safe_copy(ios_mtz, p_dir / "etc" / "precust_theme" / src_cfg["ios_theme_mtz_name"])

    safe_copy(repo_root / "modules" / "rollercoaster_core" / "system" / "etc" / "hosts", s_dir / "etc" / "hosts")
    safe_copy(
        repo_root / "modules" / "rollercoaster_core" / "system" / "product" / "etc" / "sysconfig" / "rollercoaster_gphotos_unlimited.xml",
        p_dir / "etc" / "sysconfig" / "rollercoaster_gphotos_unlimited.xml",
    )

    install_prebuilt_apk(support_apk, "RollercoasterSupport", p_dir, priv=True)
    for ov in overlay_apks:
        safe_copy(ov, p_dir / "overlay" / ov.name)

    install_prebuilt_apk(ksu_apk, "KernelSUNext", p_dir, priv=False)  # type: ignore[arg-type]
    if webui_apk:
        install_prebuilt_apk(webui_apk, "KsuWebUI", p_dir, priv=False)
    if adaway_apk:
        install_prebuilt_apk(adaway_apk, "AdAway", p_dir, priv=False)
    if heliboard_apk:
        install_prebuilt_apk(heliboard_apk, "HeliBoard", p_dir, priv=False)
    if insular_apk:
        install_prebuilt_apk(insular_apk, "Insular", p_dir, priv=False)

    integrate_cruecy_dolby_and_spatial(
        dolby_repo=dolby_repo,
        vendor_dir=v_dir,
        system_ext_dir=se_dir,
        product_dir=p_dir,
        custom_dax_apk=dax_apk,
        custom_dolby_overlay=dolby_ov if dolby_ov.is_file() else None,
    )
    integrate_leica_camera(cam_vendor, cam_device, s_dir, p_dir, se_dir)

    seed_modules: List[Path] = [
        m
        for m in [
            tools.get("props_hook_zip"),
            susfs_zip,
            pif_zip,
            tricky_zip,
            revanced_zips.get("yt"),
            revanced_zips.get("ytm"),
        ]
        if isinstance(m, Path) and m.is_file()
    ]
    seed_ksu_and_modules(
        system_dir=s_dir,
        ksud_bin=ksud_out,
        core_module_src=repo_root / "modules" / "rollercoaster_core",
        module_zips=seed_modules,
    )

    shutil.rmtree(dl_dir, ignore_errors=True)
    shutil.rmtree(repos_dir / "miuicamera_vendor", ignore_errors=True)
    shutil.rmtree(repos_dir / "vendor_dolby", ignore_errors=True)

    final_zip = out_dir / rom_cfg["output_zip_name"]
    repack_rom_workspace(ws, final_zip, tools)

    manifest = {
        "rom_name": rom_cfg["version_display"],
        "device": rom_cfg["device"],
        "maintainer": rom_cfg["maintainer"],
        "telegram": rom_cfg["telegram_username"],
        "output_zip": str(final_zip),
        "output_size_bytes": final_zip.stat().st_size,
        "output_sha256": sha256_file(final_zip),
        "container_type": ws.container_type,
        "repacked_partitions": sorted(list(ws.partitions.keys())),
        "seeded_ksu_modules": [m.name for m in seed_modules] + ["00_rollercoaster_core.zip"],
    }
    manifest_path = out_dir / "build_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"[done] Manifest written to {manifest_path}")


if __name__ == "__main__":
    main()
