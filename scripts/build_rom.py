#!/usr/bin/env python3
"""Master End-to-End ROM Builder for Rollercoaster Hypos 4.0.1.7 (Redmi Note 9 Pro / joyeuse)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import quote, unquote, urlparse
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
    """Legacy latest-release fetcher retained for optional upstream assets."""
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
        fname = unquote(urlparse(fallback_url).path.rsplit("/", 1)[-1])
        try:
            return download_file(fallback_url, dest_dir / fname)
        except Exception as exc:
            print(f"[gh-asset] Fallback URL failed for {repo_slug}: {exc}")
    return None


def fetch_pinned_asset(
    url: str,
    dest_dir: Path,
    expected_sha256: str = "",
    expected_size_bytes: Optional[int] = None,
) -> Path:
    """Download one explicitly pinned release asset and verify its digest/size when supplied."""
    filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    if not filename:
        raise ValueError(f"Cannot determine asset filename from URL: {url}")
    asset_path = download_file(url, dest_dir / filename, expected_sha256=expected_sha256)
    if expected_size_bytes is not None and asset_path.stat().st_size != expected_size_bytes:
        actual_size = asset_path.stat().st_size
        asset_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Asset size mismatch for {filename}: expected {expected_size_bytes}, got {actual_size}"
        )
    return asset_path


def fetch_github_file(
    repo_slug: str,
    repo_path: str,
    revision: str,
    destination: Path,
    expected_sha256: str = "",
) -> Path:
    """Fetch a small pinned source file through GitHub's API (no mutable raw URL)."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError(f"GitHub file fetch requires a full pinned commit SHA, got: {revision!r}")
    api_url = (
        f"https://api.github.com/repos/{repo_slug}/contents/"
        f"{quote(repo_path, safe='/')}?ref={quote(revision, safe='')}"
    )
    payload = gh_api_json(api_url)
    if payload.get("type") != "file" or not payload.get("content"):
        raise RuntimeError(f"GitHub API did not return file content for {repo_slug}/{repo_path}@{revision}")
    try:
        content = base64.b64decode(payload["content"], validate=False)
    except Exception as exc:
        raise RuntimeError(f"GitHub API returned invalid base64 for {repo_slug}/{repo_path}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    if expected_sha256:
        actual = sha256_file(destination)
        if actual.lower() != expected_sha256.lower():
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA-256 mismatch for {repo_slug}/{repo_path}@{revision}: "
                f"expected {expected_sha256}, got {actual}"
            )
    return destination


def validate_apk_file(apk_path: Path) -> None:
    if not apk_path.is_file() or apk_path.stat().st_size == 0:
        raise RuntimeError(f"Required APK is missing or empty: {apk_path}")
    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            if zf.testzip() is not None or "AndroidManifest.xml" not in zf.namelist():
                raise RuntimeError(f"Invalid Android APK archive: {apk_path}")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Downloaded file is not a valid APK: {apk_path}") from exc


def validate_module_zip(module_zip: Path, reject_microg_payload: bool = False) -> Dict[str, str]:
    """Validate a KSU/Magisk module archive before it is embedded into the ROM."""
    if not module_zip.is_file() or module_zip.stat().st_size == 0:
        raise RuntimeError(f"Required KernelSU module archive is missing: {module_zip}")
    try:
        with zipfile.ZipFile(module_zip, "r") as zf:
            names = zf.namelist()
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"Corrupt module archive entry {bad} in {module_zip.name}")
            if any(
                name.startswith("/") or ".." in name.replace("\\", "/").split("/")
                for name in names
            ):
                raise RuntimeError(f"Unsafe path found in module archive {module_zip.name}")
            try:
                module_prop = zf.read("module.prop").decode("utf-8", errors="replace")
            except KeyError as exc:
                raise RuntimeError(f"{module_zip.name} is not an installable module (module.prop missing)") from exc
            values = dict(
                line.split("=", 1)
                for line in module_prop.replace("\r", "").splitlines()
                if "=" in line and not line.lstrip().startswith("#")
            )
            module_id = values.get("id", "").strip()
            if not module_id or not re.fullmatch(r"[A-Za-z0-9._-]+", module_id):
                raise RuntimeError(f"Invalid or missing module id in {module_zip.name}")
            if reject_microg_payload:
                suspicious = [
                    name for name in names
                    if name.lower().endswith((".apk", ".dex", ".so"))
                    and re.search(r"microg|gmscore", name, re.IGNORECASE)
                ]
                if suspicious:
                    raise RuntimeError(f"MicroG/GmsCore payload found in {module_zip.name}: {suspicious}")
            return {
                "id": module_id,
                "name": values.get("name", module_id).strip(),
                "version": values.get("version", "unknown").strip(),
                "versionCode": values.get("versionCode", "").strip(),
            }
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Downloaded module is not a valid ZIP: {module_zip}") from exc


def validate_props_hook_zip(props_hook_zip: Path) -> None:
    """PropsHook is a framework classes.dex payload, not a flashable KSU module ZIP."""
    try:
        with zipfile.ZipFile(props_hook_zip, "r") as zf:
            if zf.testzip() is not None or "classes.dex" not in zf.namelist():
                raise RuntimeError(f"PropsHook payload is missing a valid classes.dex: {props_hook_zip}")
            dex = zf.read("classes.dex")
            if b"PropsHookUtils" not in dex or b"setProps" not in dex:
                raise RuntimeError(f"Unexpected PropsHook classes.dex payload: {props_hook_zip}")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"PropsHook payload is not a valid ZIP: {props_hook_zip}") from exc


def fetch_revanced_ksu_zips(
    repo_slug: str,
    release_tag: str,
    dest_dir: Path,
    youtube_sha256: str = "",
    ytmusic_sha256: str = "",
) -> Dict[str, Path]:
    """Fetch the pinned ReVanced eXtended KSU modules for YouTube and YouTube Music."""
    rel = gh_api_json(f"https://api.github.com/repos/{repo_slug}/releases/tags/{release_tag}")
    assets = rel.get("assets", [])
    candidates = [
        asset for asset in assets
        if asset.get("name", "").lower().endswith(".zip")
        and "module" in asset.get("name", "").lower()
        and not re.search(r"microg|gmscore", asset.get("name", ""), re.IGNORECASE)
    ]
    youtube = [a for a in candidates if "youtube" in a["name"].lower() and "music" not in a["name"].lower()]
    ytmusic = [a for a in candidates if "music" in a["name"].lower() and "youtube" in a["name"].lower()]
    if not youtube or not ytmusic:
        raise RuntimeError(f"Pinned ReVanced release {repo_slug}@{release_tag} lacks both KSU module ZIPs")

    def prefer_module(asset: Dict[str, Any], target: str) -> tuple[int, str]:
        name = asset["name"].lower()
        abi_score = 3 if "arm64-v8a" in name else 2 if "-all" in name else 1 if "arm-v7a" in name else 0
        return (-abi_score, asset["name"])

    yt_asset = sorted(youtube, key=lambda a: prefer_module(a, "yt"))[0]
    ytm_asset = sorted(ytmusic, key=lambda a: prefer_module(a, "ytm"))[0]
    yt_zip = download_file(yt_asset["browser_download_url"], dest_dir / yt_asset["name"], youtube_sha256)
    ytm_zip = download_file(ytm_asset["browser_download_url"], dest_dir / ytm_asset["name"], ytmusic_sha256)
    validate_module_zip(yt_zip, reject_microg_payload=True)
    validate_module_zip(ytm_zip, reject_microg_payload=True)
    return {"yt": yt_zip, "ytm": ytm_zip}


def fetch_insular_apk(fdroid_api_url: str, dest_dir: Path) -> Path:
    try:
        req = urllib.request.Request(fdroid_api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        packages = meta.get("packages", [])
        if not packages:
            raise RuntimeError("F-Droid did not return any Insular APK builds")
        suggested = str(meta.get("suggestedVersionCode", ""))
        package = next((p for p in packages if str(p.get("versionCode", "")) == suggested), None)
        if package is None:
            package = max(packages, key=lambda p: int(p.get("versionCode", 0)))
        filename = package.get("apkName") or f"com.oasisfeng.island.fdroid_{package['versionCode']}.apk"
        apk_url = f"https://f-droid.org/repo/{filename}"
        digest = package.get("hash", "") if package.get("hashType", "sha256").lower() == "sha256" else ""
        apk = download_file(apk_url, dest_dir / filename, expected_sha256=digest)
        validate_apk_file(apk)
        return apk
    except Exception as exc:
        raise RuntimeError(f"Required Insular APK fetch/validation failed: {exc}") from exc

def clone_shallow(repo_url: str, dest_dir: Path, revision: str = "") -> Path:
    """Clone a small source tree and enforce the configured immutable commit when supplied."""
    if (dest_dir / ".git").is_dir():
        current = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(dest_dir), text=True
        ).strip()
        if not revision or current == revision:
            return dest_dir
        shutil.rmtree(dest_dir, ignore_errors=True)

    shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "clone", "--depth=1", "--single-branch", repo_url, str(dest_dir)])
    if revision:
        current = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(dest_dir), text=True
        ).strip()
        if current != revision:
            run_cmd(["git", "fetch", "--depth=1", "origin", revision], cwd=dest_dir)
            run_cmd(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=dest_dir)
        actual = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(dest_dir), text=True
        ).strip()
        if actual != revision:
            raise RuntimeError(f"Pinned source revision mismatch for {repo_url}: {actual} != {revision}")
    return dest_dir


def patch_props_hook_instrumentation_text(smali_text: str) -> tuple[str, int]:
    """Call PropsHookUtils.setProps(context) from Android's Application creation methods."""
    signatures = (
        "newApplication(Ljava/lang/ClassLoader;Ljava/lang/String;Landroid/content/Context;)Landroid/app/Application;",
        "newApplication(Ljava/lang/Class;Landroid/content/Context;)Landroid/app/Application;",
    )
    patched = 0
    text = smali_text
    for signature in signatures:
        method_pattern = re.compile(
            r"(?ms)^\.method\b[^\n]*" + re.escape(signature) + r"[^\n]*\n.*?^\.end method"
        )
        method_match = method_pattern.search(text)
        if not method_match:
            continue
        method_text = method_match.group(0)
        hook_call = "Lcom/android/internal/util/PropsHookUtils;->setProps(Landroid/content/Context;)V"
        if hook_call in method_text:
            patched += 1
            continue
        attach_pattern = re.compile(
            r"(?m)^(?P<indent>[ \t]*)invoke-virtual(?:/range)?\s+\{(?P<regs>[^}]+)\},\s*"
            r"Landroid/app/Application;->attach\(Landroid/content/Context;\)V"
        )
        attach_match = attach_pattern.search(method_text)
        if not attach_match:
            continue
        regs_text = attach_match.group("regs").strip()
        if ".." in regs_text:
            first = regs_text.split("..", 1)[0].strip()
            reg_match = re.fullmatch(r"([vp])(\d+)", first)
            if not reg_match:
                continue
            context_reg = f"{reg_match.group(1)}{int(reg_match.group(2)) + 1}"
        else:
            regs = [reg.strip() for reg in regs_text.split(",") if reg.strip()]
            if len(regs) < 2:
                continue
            context_reg = regs[1]
        indent = attach_match.group("indent")
        register_index = int(re.search(r"\d+", context_reg).group(0))
        if register_index > 15:
            invocation = f"invoke-static/range {{{context_reg} .. {context_reg}}}"
        else:
            invocation = f"invoke-static {{{context_reg}}}"
        call = (
            f"\n{indent}{invocation}, "
            "Lcom/android/internal/util/PropsHookUtils;->setProps(Landroid/content/Context;)V"
        )
        patched_method = method_text[:attach_match.end()] + call + method_text[attach_match.end():]
        text = text[:method_match.start()] + patched_method + text[method_match.end():]
        patched += 1
    if patched == 0:
        raise RuntimeError("Could not locate a supported Instrumentation.newApplication attach call for PropsHook")
    return text, patched


def integrate_props_hook_framework(
    system_dir: Path,
    tools: Dict[str, Any],
    work_dir: Path,
) -> Dict[str, Any]:
    """Decompile framework.jar, inject the pinned PropsHook DEX, hook app startup, and rebuild."""
    props_zip = Path(tools["props_hook_zip"])
    validate_props_hook_zip(props_zip)
    framework_candidates = [
        path for path in system_dir.rglob("framework.jar")
        if path.is_file() and path.parent.name == "framework"
    ]
    if len(framework_candidates) != 1:
        raise RuntimeError(f"Expected one system/framework/framework.jar, found {framework_candidates}")
    framework_jar = framework_candidates[0]
    java = shutil.which("java")
    if not java:
        raise FileNotFoundError("JDK 21 / java is required to inject PropsHook into framework.jar")

    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    decoded = work_dir / "framework_decoded"
    run_cmd(
        [
            java,
            "-jar",
            str(tools["apkeditor_jar"]),
            "d",
            "-f",
            "-i",
            str(framework_jar),
            "-o",
            str(decoded),
            "-no-dex-debug",
        ]
    )
    smali_root = decoded / "smali"
    if not smali_root.is_dir():
        raise RuntimeError(f"APKEditor did not produce framework smali sources: {smali_root}")
    instrumentation_files = sorted(smali_root.rglob("Instrumentation.smali"))
    if not instrumentation_files:
        raise RuntimeError("framework.jar decode did not contain android/app/Instrumentation.smali")
    existing_hook = list(smali_root.rglob("PropsHookUtils.smali"))
    if existing_hook:
        raise RuntimeError(
            "framework.jar already contains PropsHookUtils; refusing to inject a duplicate class: "
            f"{existing_hook}"
        )
    patched_methods = 0
    for smali_file in instrumentation_files:
        text, count = patch_props_hook_instrumentation_text(
            smali_file.read_text(encoding="utf-8", errors="ignore")
        )
        smali_file.write_text(text, encoding="utf-8")
        patched_methods += count

    hook_work = work_dir / "props_hook"
    shutil.rmtree(hook_work, ignore_errors=True)
    hook_work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(props_zip, "r") as zf:
        zf.extract("classes.dex", hook_work)
    hook_smali = hook_work / "classes"
    run_cmd(
        [
            java,
            "-jar",
            str(tools["baksmali_jar"]),
            "d",
            str(hook_work / "classes.dex"),
            "-o",
            str(hook_smali),
        ]
    )
    if not (hook_smali / "com" / "android" / "internal" / "util" / "PropsHookUtils.smali").is_file():
        raise RuntimeError("PropsHook DEX did not decode to com.android.internal.util.PropsHookUtils")

    class_dirs = [path for path in smali_root.iterdir() if path.is_dir() and re.fullmatch(r"classes(?:\d+)?", path.name)]
    if not class_dirs:
        raise RuntimeError("APKEditor framework decode did not expose a dex smali directory")
    def class_index(path: Path) -> int:
        match = re.fullmatch(r"classes(\d+)?", path.name)
        return int(match.group(1) or "1") if match else 0
    target_classes = smali_root / f"classes{max(map(class_index, class_dirs)) + 1}"
    shutil.copytree(hook_smali, target_classes, dirs_exist_ok=False)

    rebuilt_jar = work_dir / "framework_propshook.jar"
    run_cmd(
        [
            java,
            "-jar",
            str(tools["apkeditor_jar"]),
            "b",
            "-f",
            "-i",
            str(decoded),
            "-o",
            str(rebuilt_jar),
        ]
    )
    if not rebuilt_jar.is_file() or rebuilt_jar.stat().st_size == 0:
        raise RuntimeError("APKEditor failed to rebuild framework.jar with PropsHook")
    with zipfile.ZipFile(rebuilt_jar, "r") as zf:
        bad = zf.testzip()
        dex_entries = [name for name in zf.namelist() if re.fullmatch(r"classes\d*\.dex", name)]
        if bad or not dex_entries:
            raise RuntimeError(f"Rebuilt framework.jar failed archive validation (bad entry: {bad})")
        if not any(b"PropsHookUtils" in zf.read(name) for name in dex_entries):
            raise RuntimeError("Rebuilt framework.jar is missing the injected PropsHook class")
        if not any(b"setProps" in zf.read(name) for name in dex_entries):
            raise RuntimeError("Rebuilt framework.jar is missing the PropsHook setProps method reference")

    safe_copy(rebuilt_jar, framework_jar)
    return {
        "framework_jar": str(framework_jar.relative_to(system_dir)),
        "framework_sha256": sha256_file(framework_jar),
        "patched_methods": patched_methods,
        "injected_classes_dex": target_classes.name,
    }


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
        out_lines.append("# --- Rollercoaster Hypos 4.0.1.7 Custom Properties ---")
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
    """Patch only the selected joyeuse feature files, leaving other miatoll SKUs untouched."""
    dirs = [
        product_dir / "etc" / "device_features",
        system_dir / "etc" / "device_features",
    ]
    count = 0
    for df_dir in dirs:
        if not df_dir.is_dir():
            continue
        joyeuse_xml = df_dir / "joyeuse.xml"
        if not joyeuse_xml.is_file():
            candidates = list(df_dir.glob("*.xml"))
            if not candidates:
                continue
            source = df_dir / "miatoll.xml" if (df_dir / "miatoll.xml").is_file() else candidates[0]
            safe_copy(source, joyeuse_xml)

        if joyeuse_xml.is_symlink() or not joyeuse_xml.is_file():
            continue
        try:
            tree = ET.parse(joyeuse_xml)
            root = tree.getroot()
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
            existing = {el.get("name"): el for el in root.findall("bool")}
            for name, value in desired_bools.items():
                if name in existing:
                    existing[name].text = value
                else:
                    ET.SubElement(root, "bool", {"name": name}).text = value

            face_regions = next(
                (el for el in root.findall("string-array") if el.get("name") == "support_face_unlock_region_global"),
                None,
            )
            if face_regions is None:
                face_regions = ET.SubElement(root, "string-array", {"name": "support_face_unlock_region_global"})
            face_values = {item.text or "" for item in face_regions.findall("item")}
            if not face_values:
                ET.SubElement(face_regions, "item").text = "ALL"
            elif "ALL" not in face_values and "ID" not in face_values:
                ET.SubElement(face_regions, "item").text = "ID"

            ET.indent(tree, space="    ")
            tree.write(joyeuse_xml, encoding="utf-8", xml_declaration=True)
            count += 1
        except Exception as exc:
            raise RuntimeError(f"Could not patch joyeuse device features in {joyeuse_xml}: {exc}") from exc
    if count == 0:
        raise RuntimeError("No product/system joyeuse.xml device-feature file was available to patch")
    return count

def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child(parent: ET.Element, local_name: str) -> Optional[ET.Element]:
    return next((child for child in list(parent) if _xml_local_name(child.tag) == local_name), None)


def _xml_children(parent: ET.Element, local_name: str) -> List[ET.Element]:
    return [child for child in list(parent) if _xml_local_name(child.tag) == local_name]


def merge_dolby_and_spatial_audio_effects(
    audio_effects_xml: Path,
    cruecy_audio_effects_xml: Optional[Path] = None,
) -> bool:
    """Merge the pinned Cruecy config into the ROM's effects table without fake UUIDs."""
    if audio_effects_xml.is_symlink():
        raise RuntimeError(f"Refusing to patch symlinked audio effects XML: {audio_effects_xml}")
    if not audio_effects_xml.is_file():
        if not cruecy_audio_effects_xml or not cruecy_audio_effects_xml.is_file():
            return False
        safe_copy(cruecy_audio_effects_xml, audio_effects_xml)

    try:
        target_tree = ET.parse(audio_effects_xml)
        target_root = target_tree.getroot()
        source_root = ET.parse(cruecy_audio_effects_xml).getroot() if cruecy_audio_effects_xml else None
    except Exception as exc:
        raise RuntimeError(f"Malformed Dolby audio effects XML: {exc}") from exc

    if source_root is None:
        raise RuntimeError("Pinned Cruecy audio_effects.xml is required for Dolby integration")
    target_libraries = _xml_child(target_root, "libraries")
    target_effects = _xml_child(target_root, "effects")
    source_libraries = _xml_child(source_root, "libraries")
    source_effects = _xml_child(source_root, "effects")
    if source_libraries is None or source_effects is None:
        raise RuntimeError("Cruecy audio effects source lacks libraries/effects sections")

    namespace = target_root.tag.split("}", 1)[0] + "}" if "}" in target_root.tag else ""
    if target_libraries is None:
        target_libraries = ET.SubElement(target_root, f"{namespace}libraries")
    if target_effects is None:
        target_effects = ET.SubElement(target_root, f"{namespace}effects")

    def clone_for_target_namespace(element: ET.Element) -> ET.Element:
        clone = ET.Element(f"{namespace}{_xml_local_name(element.tag)}", dict(element.attrib))
        clone.text = element.text
        clone.tail = element.tail
        for child in list(element):
            clone.append(clone_for_target_namespace(child))
        return clone

    def merge_entries(target_parent: ET.Element, source_parent: ET.Element, accepted: Set[str]) -> None:
        for source_entry in list(source_parent):
            kind = _xml_local_name(source_entry.tag)
            if kind not in accepted:
                continue
            src_name = source_entry.get("name", "")
            src_uuid = source_entry.get("uuid", "").lower()
            duplicate = next(
                (
                    existing for existing in list(target_parent)
                    if _xml_local_name(existing.tag) == kind
                    and (
                        (src_name and existing.get("name", "") == src_name)
                        or (src_uuid and existing.get("uuid", "").lower() == src_uuid)
                    )
                ),
                None,
            )
            if duplicate is not None:
                target_parent.remove(duplicate)
            target_parent.append(clone_for_target_namespace(source_entry))

    merge_entries(target_libraries, source_libraries, {"library"})
    merge_entries(target_effects, source_effects, {"effect", "effectProxy"})

    postprocess = _xml_child(target_root, "postprocess")
    if postprocess is not None:
        for stream in _xml_children(postprocess, "stream"):
            for apply_entry in _xml_children(stream, "apply"):
                if apply_entry.get("effect", "").lower().startswith("dlb_"):
                    stream.remove(apply_entry)

    def merge_processing_section(section_name: str) -> None:
        source_section = _xml_child(source_root, section_name)
        if source_section is None:
            return
        target_section = _xml_child(target_root, section_name)
        if target_section is None:
            target_section = ET.SubElement(target_root, f"{namespace}{section_name}")
        for source_stream in _xml_children(source_section, "stream"):
            stream_type = source_stream.get("type", "")
            target_stream = next(
                (item for item in _xml_children(target_section, "stream") if item.get("type", "") == stream_type),
                None,
            )
            if target_stream is None:
                target_stream = ET.SubElement(target_section, f"{namespace}stream", dict(source_stream.attrib))
            existing_effects = {
                item.get("effect", "") for item in _xml_children(target_stream, "apply")
            }
            for source_apply in _xml_children(source_stream, "apply"):
                effect_name = source_apply.get("effect", "")
                if effect_name and effect_name not in existing_effects:
                    target_stream.append(clone_for_target_namespace(source_apply))
                    existing_effects.add(effect_name)

    merge_processing_section("postprocess")
    merge_processing_section("preprocess")

    library_names = {entry.get("name", "") for entry in _xml_children(target_libraries, "library")}
    required_libraries = {"dap", "dvl", "spatializer"}
    if not required_libraries.issubset(library_names):
        raise RuntimeError(f"Merged audio effects config is missing libraries: {sorted(required_libraries - library_names)}")
    effect_names = {entry.get("name", "") for entry in _xml_children(target_effects, "effect")}
    if not {"dap", "spatializer", "dlb_music_listener"}.issubset(effect_names):
        raise RuntimeError("Merged audio effects config is missing Dolby DAP/listener or the Cruecy spatializer effect")

    ET.indent(target_tree, space="    ")
    target_tree.write(audio_effects_xml, encoding="utf-8", xml_declaration=True)
    return True


def _merge_media_codec_include(vendor_dir: Path, include_name: str) -> Path:
    candidates = [vendor_dir / "etc" / "media_codecs_vendor.xml", vendor_dir / "etc" / "media_codecs.xml"]
    config = next((path for path in candidates if path.is_file() and not path.is_symlink()), candidates[0])
    if config.is_file():
        tree = ET.parse(config)
        root = tree.getroot()
    else:
        root = ET.Element("MediaCodecs")
        tree = ET.ElementTree(root)
    if _xml_local_name(root.tag) != "MediaCodecs":
        raise RuntimeError(f"Unexpected media codec config root in {config}: {root.tag}")
    if not any(
        _xml_local_name(item.tag) == "Include" and item.get("href") == include_name
        for item in list(root)
    ):
        ET.SubElement(root, "Include", {"href": include_name})
    ET.indent(tree, space="    ")
    config.parent.mkdir(parents=True, exist_ok=True)
    tree.write(config, encoding="utf-8", xml_declaration=True)
    return config


def validate_dolby_selinux_policy(vendor_dir: Path) -> Dict[str, Any]:
    """Require the base vendor policy to support the pinned HIDL DMS service."""
    selinux_dir = vendor_dir / "etc" / "selinux"
    if not selinux_dir.is_dir():
        raise RuntimeError("Base vendor partition has no etc/selinux policy directory for Dolby DMS")

    policy_paths = sorted(
        path
        for path in selinux_dir.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (
            path.name in {"precompiled_sepolicy", "sepolicy", "vendor_sepolicy"}
            or path.name.endswith("_sepolicy.cil")
        )
    )
    if not policy_paths:
        raise RuntimeError("Base vendor partition has no inspectable compiled or CIL SELinux policy for Dolby DMS")
    policy_blob = b"\n".join(path.read_bytes() for path in policy_paths)
    required_types = (
        "hal_dms_hwservice",
        "hal_dms_default",
        "hal_dms_default_exec",
        "hal_dms_client",
        "hal_dms_server",
        "vendor_dolby_file",
    )
    missing_types = [name for name in required_types if name.encode("ascii") not in policy_blob]
    if missing_types:
        raise RuntimeError(
            "Base vendor SELinux policy does not contain the pinned Dolby DMS domains/types "
            f"{missing_types}; refusing to ship an unlabelled or nonfunctional HAL"
        )

    hwservice_context_paths = sorted(
        path
        for path in selinux_dir.rglob("*hwservice_contexts")
        if path.is_file() and not path.is_symlink()
    )
    has_hwservice_context = any(
        "vendor.dolby.hardware.dms::IDms" in line and "hal_dms_hwservice" in line
        for path in hwservice_context_paths
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not has_hwservice_context:
        raise RuntimeError(
            "Base vendor SELinux hwservice_contexts does not label vendor.dolby.hardware.dms::IDms"
        )

    file_context_paths = sorted(
        path
        for path in selinux_dir.rglob("*file_contexts")
        if path.is_file() and not path.is_symlink()
    )
    file_context_lines = [
        line
        for path in file_context_paths
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    has_service_file_context = any(
        "vendor.dolby.hardware.dms@2.0-service" in line.replace("\\", "")
        and "hal_dms_default_exec" in line
        for line in file_context_lines
    )
    has_dolby_data_context = any(
        "/data/vendor/dolby" in line and "vendor_dolby_file" in line
        for line in file_context_lines
    )
    if not has_service_file_context or not has_dolby_data_context:
        missing = []
        if not has_service_file_context:
            missing.append("HIDL DMS executable label")
        if not has_dolby_data_context:
            missing.append("/data/vendor/dolby data label")
        raise RuntimeError(f"Base vendor SELinux file_contexts lacks required Dolby entries: {missing}")

    return {
        "policy_files": [str(path.relative_to(vendor_dir)) for path in policy_paths],
        "required_types": list(required_types),
        "hwservice_context_files": [str(path.relative_to(vendor_dir)) for path in hwservice_context_paths],
        "file_context_files": [str(path.relative_to(vendor_dir)) for path in file_context_paths],
    }


def integrate_cruecy_dolby_and_spatial(
    dolby_repo: Path,
    vendor_dir: Path,
    system_ext_dir: Path,
    custom_dax_apk: Path,
    cruecy_audio_effects_xml: Path,
) -> Dict[str, Any]:
    """Install Dolby blobs only when the base vendor policy can label and run the HIDL DMS HAL."""
    selinux_policy_validation = validate_dolby_selinux_policy(vendor_dir)
    blob_roots = [
        dolby_repo / "prebuilts" / "proprietary" / "vendor",
        dolby_repo / "proprietary" / "vendor",
        dolby_repo / "vendor",
    ]
    blob_root = next((path for path in blob_roots if path.is_dir()), None)
    if blob_root is None:
        raise RuntimeError("AndroidOne vendor_dolby blobs not found (expected prebuilts/proprietary/vendor)")

    copied = 0
    excluded_aidl_files = {"bin/hw/vendor.dolby.dms.service", "etc/init/dms-service.rc"}
    for source in blob_root.rglob("*"):
        relative = source.relative_to(blob_root).as_posix()
        if source.is_file() and relative not in excluded_aidl_files:
            safe_copy(source, vendor_dir / source.relative_to(blob_root))
            copied += 1

    required_blob_relpaths = (
        "lib64/soundfx/libswdap.so",
        "lib64/soundfx/libdlbvol.so",
        "lib64/soundfx/libswgamedap.so",
        "bin/hw/vendor.dolby.hardware.dms@2.0-service",
    )
    missing_blobs = [rel for rel in required_blob_relpaths if not (vendor_dir / rel).is_file()]
    if missing_blobs:
        raise RuntimeError(f"Dolby vendor blob integration is incomplete: {missing_blobs}")

    required_effect_libraries = (
        "libswdap.so",
        "libdlbvol.so",
        "libspatialaudio.so",
        "libswgamedap.so",
        "libswvqe.so",
    )
    missing_effect_libraries = [
        name
        for name in required_effect_libraries
        if not any(
            (vendor_dir / lib_dir / "soundfx" / name).is_file()
            for lib_dir in ("lib64", "lib")
        )
    ]
    if missing_effect_libraries:
        raise RuntimeError(
            "Dolby/spatial audio configuration references missing vendor libraries: "
            f"{missing_effect_libraries}"
        )

    source_root = dolby_repo
    extra_files = (
        (source_root / "soundfx" / "dax-default.xml", vendor_dir / "etc" / "dolby" / "dax-default.xml"),
        (source_root / "media" / "media_codecs_dolby_audio.xml", vendor_dir / "etc" / "media_codecs_dolby_audio.xml"),
        (source_root / "hidl" / "vendor.dolby.hardware.dms.xml", vendor_dir / "etc" / "vintf" / "manifest" / "vendor.dolby.hardware.dms.xml"),
        (source_root / "hidl" / "vendor.dolby.media.c2@1.0-service.xml", vendor_dir / "etc" / "vintf" / "manifest" / "vendor.dolby.media.c2@1.0-service.xml"),
    )
    for source, target in extra_files:
        if not source.is_file():
            raise RuntimeError(f"Required AndroidOne Dolby integration file is missing: {source}")
        safe_copy(source, target)

    media_codec_include = vendor_dir / "etc" / "media_codecs_dolby_audio.xml"
    _merge_media_codec_include(vendor_dir, media_codec_include.name)

    if not cruecy_audio_effects_xml.is_file():
        raise RuntimeError(f"Pinned Cruecy audio effects XML is missing: {cruecy_audio_effects_xml}")
    audio_effect_candidates = [
        vendor_dir / "etc" / "audio_effects.xml",
        vendor_dir / "etc" / "audio" / "audio_effects.xml",
        vendor_dir / "etc" / "audio" / "sku_atoll" / "audio_effects.xml",
    ]
    existing_audio_effects = [path for path in audio_effect_candidates if path.is_file() and not path.is_symlink()]
    if not existing_audio_effects:
        existing_audio_effects = [audio_effect_candidates[0]]
    for audio_effects in existing_audio_effects:
        merge_dolby_and_spatial_audio_effects(audio_effects, cruecy_audio_effects_xml)

    if not custom_dax_apk.is_file():
        raise RuntimeError("Compiled Indonesian DolbyDaxUI.apk is required; prebuilt/overlay fallback is disabled")
    validate_apk_file(custom_dax_apk)
    safe_copy(custom_dax_apk, system_ext_dir / "priv-app" / "DolbyDaxUI" / "DolbyDaxUI.apk")

    dax_root = source_root / "DolbyDaxUI"
    permission_xml = dax_root / "permissions" / "privapp-permissions-com.dolby.daxappui.xml"
    sysconfig_xml = dax_root / "sysconfig" / "config-com.dolby.daxappui.xml"
    for source, destination in (
        (permission_xml, system_ext_dir / "etc" / "permissions" / permission_xml.name),
        (sysconfig_xml, system_ext_dir / "etc" / "sysconfig" / sysconfig_xml.name),
    ):
        if not source.is_file():
            raise RuntimeError(f"Required Dolby DAX UI permission/config file is missing: {source}")
        safe_copy(source, destination)

    print(f"[dolby] Integrated {copied} vendor blobs, Cruecy effects XML, codec registration, and Indonesian DAX UI")
    return {
        "vendor_blob_count": copied,
        "required_blobs": list(required_blob_relpaths),
        "selinux_policy_validation": selinux_policy_validation,
        "audio_effects_paths": [str(path.relative_to(vendor_dir)) for path in existing_audio_effects],
        "audio_effects_source_sha256": sha256_file(cruecy_audio_effects_xml),
        "dax_apk": "system_ext/priv-app/DolbyDaxUI/DolbyDaxUI.apk",
        "privapp_permissions": permission_xml.name,
        "sysconfig": sysconfig_xml.name,
    }

def assemble_split_camera_apk(cam_vendor_repo: Path, work_dir: Path) -> tuple[Path, int]:
    singles = sorted(path for path in cam_vendor_repo.rglob("MiuiCamera.apk") if path.is_file())
    if singles:
        apk = singles[0]
        validate_apk_file(apk)
        return apk, 0

    parts = [
        path for path in cam_vendor_repo.rglob("MiuiCamera.apk.*")
        if path.is_file() and re.fullmatch(r"MiuiCamera\.apk\.\d{2,}", path.name)
    ]
    if not parts:
        raise RuntimeError("Leica Camera v6.8 source did not contain MiuiCamera.apk or numbered split parts")
    parts.sort(key=lambda path: int(path.name.rsplit(".", 1)[1]))
    indices = [int(path.name.rsplit(".", 1)[1]) for path in parts]
    expected = list(range(indices[0], indices[0] + len(indices)))
    if indices[0] not in (0, 1) or indices != expected:
        raise RuntimeError(f"Leica Camera APK chunks are not a contiguous .00/.01/... sequence: {indices}")

    work_dir.mkdir(parents=True, exist_ok=True)
    assembled = work_dir / "MiuiCamera.apk"
    assembled.unlink(missing_ok=True)
    with assembled.open("wb") as output:
        for part in parts:
            with part.open("rb") as source:
                shutil.copyfileobj(source, output, length=4 * 1024 * 1024)
    try:
        with zipfile.ZipFile(assembled, "r") as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"Reassembled Leica Camera APK has a corrupt ZIP entry: {bad}")
            required_entries = {"AndroidManifest.xml"}
            if not required_entries.issubset(zf.namelist()):
                raise RuntimeError("Reassembled Leica Camera APK lacks AndroidManifest.xml")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Concatenated Leica Camera chunks did not produce a valid APK") from exc
    print(f"[camera] Reassembled {len(parts)} numbered APK parts ({assembled.stat().st_size / (1024**2):.1f} MiB)")
    return assembled, len(parts)


def integrate_leica_camera(
    cam_vendor_repo: Path,
    cam_device_repo: Path,
    system_dir: Path,
    product_dir: Path,
    system_ext_dir: Path,
    work_dir: Path,
) -> Dict[str, Any]:
    """Install the split v6.8 camera APK, native libraries, allowlists and app-private links."""
    camera_apk, split_part_count = assemble_split_camera_apk(cam_vendor_repo, work_dir)
    prop_root = cam_vendor_repo / "proprietary"
    search_root = prop_root if prop_root.is_dir() else cam_vendor_repo
    targets = {"system": system_dir, "product": product_dir, "system_ext": system_ext_dir}

    for part_dir in targets.values():
        for app_sub in ("app", "priv-app", "data-app"):
            for app_name in ("MiuiCamera", "Camera2", "Flash"):
                old = part_dir / app_sub / app_name
                if old.exists() or old.is_symlink():
                    safe_remove(old)

    copied = 0
    for sub, target in targets.items():
        source_dir = search_root / sub
        if not source_dir.is_dir():
            continue
        for source in source_dir.rglob("*"):
            if not source.is_file():
                continue
            if re.fullmatch(r"MiuiCamera\.apk\.\d{2,}", source.name):
                continue
            relative = source.relative_to(source_dir)
            safe_copy(source, target / relative)
            copied += 1

    camera_dest_dir = system_dir / "priv-app" / "MiuiCamera"
    safe_copy(camera_apk, camera_dest_dir / "MiuiCamera.apk")

    device_configs = (
        ("configs/privapp-permissions-miuicamera.xml", system_dir / "etc" / "permissions" / "privapp-permissions-miuicamera.xml"),
        ("configs/privapp-permissions-mimoji.xml", system_dir / "etc" / "permissions" / "privapp-permissions-mimoji.xml"),
        ("configs/miuicamera-hiddenapi-package-allowlist.xml", system_dir / "etc" / "sysconfig" / "miuicamera-hiddenapi-package-allowlist.xml"),
    )
    for relative, destination in device_configs:
        source = cam_device_repo / relative
        if not source.is_file():
            raise RuntimeError(f"Leica camera device config is missing: {source}")
        safe_copy(source, destination)

    public_libs = cam_device_repo / "configs" / "public.libraries-xiaomi.txt"
    if not public_libs.is_file():
        raise RuntimeError(f"Leica camera public library list is missing: {public_libs}")
    public_libs_dest = system_dir / "etc" / "public.libraries-xiaomi.txt"
    existing_lines = public_libs_dest.read_text(encoding="utf-8", errors="ignore").splitlines() if public_libs_dest.is_file() else []
    merged_lines = list(existing_lines)
    for line in public_libs.read_text(encoding="utf-8", errors="ignore").splitlines():
        value = line.strip()
        if value and value not in merged_lines:
            merged_lines.append(value)
    safe_write_text(public_libs_dest, "\n".join(merged_lines) + "\n")

    camera_libs = (
        "libcamera_algoup_jni.xiaomi.so",
        "libcamera_mianode_jni.xiaomi.so",
        "libcamera_jpegutil_jni.xiaomi.so",
    )
    app_lib_dir = camera_dest_dir / "lib" / "arm64"
    for library in camera_libs:
        system_library = system_dir / "lib64" / library
        if not system_library.is_file():
            raise RuntimeError(f"Required Leica camera native library is missing: {system_library}")
        app_link = app_lib_dir / library
        if app_link.is_symlink() or app_link.exists():
            safe_remove(app_link)
        app_link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(f"/system/lib64/{library}", app_link)

    installed_apk = camera_dest_dir / "MiuiCamera.apk"
    validate_apk_file(installed_apk)
    print(f"[camera] Installed Xiaomi Leica Camera v6.8 ({copied} source files, {split_part_count} source chunks)")
    return {
        "apk_path": "system/priv-app/MiuiCamera/MiuiCamera.apk",
        "apk_sha256": sha256_file(installed_apk),
        "apk_size_bytes": installed_apk.stat().st_size,
        "split_part_count": split_part_count,
        "copied_source_files": copied,
        "native_libraries": list(camera_libs),
        "permission_allowlists": [destination.name for _, destination in device_configs[:2]],
        "hidden_api_allowlist": device_configs[2][0].split("/")[-1],
    }

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
        raise RuntimeError(f"Required prebuilt APK is missing: {apk_path}")
    validate_apk_file(apk_path)
    sub = "priv-app" if priv else "app"
    dest = base_dir / sub / folder_name / f"{folder_name}.apk"
    safe_copy(apk_path, dest)
    print(f"[apk] Installed {folder_name}.apk -> {base_dir.name}/{sub}/{folder_name}/")


def seed_ksu_and_modules(
    system_dir: Path,
    ksud_bin: Path,
    core_module_src: Path,
    module_zips: List[Path],
) -> List[Dict[str, str]]:
    """Stage genuine KSU modules and install them using ksud so each module installer runs."""
    if not ksud_bin.is_file() or ksud_bin.stat().st_size == 0:
        raise RuntimeError("KernelSU ksud binary was not extracted from the pinned kernel package")
    seed_root = system_dir / "etc" / "rollercoaster_seed"
    mods_dir = seed_root / "modules"
    mods_dir.mkdir(parents=True, exist_ok=True)
    safe_copy(ksud_bin, seed_root / "ksud")

    core_zip = mods_dir / "00_rollercoaster_core.zip"
    with zipfile.ZipFile(core_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for source in sorted(core_module_src.rglob("*")):
            if source.is_file():
                zf.write(source, source.relative_to(core_module_src).as_posix())

    metadata: List[Dict[str, str]] = []
    core_metadata = validate_module_zip(core_zip)
    metadata.append({**core_metadata, "archive": core_zip.name})
    seen_ids = {core_metadata["id"]}
    for index, source_zip in enumerate(module_zips, start=1):
        module_metadata = validate_module_zip(source_zip, reject_microg_payload=source_zip.name.lower().startswith("youtube"))
        if module_metadata["id"] in seen_ids:
            raise RuntimeError(f"Duplicate KSU module id in seed list: {module_metadata['id']}")
        seen_ids.add(module_metadata["id"])
        destination = mods_dir / f"{index:02d}_{source_zip.name}"
        safe_copy(source_zip, destination)
        metadata.append({**module_metadata, "archive": destination.name})

    seed_sh = system_dir / "bin" / "rollercoaster_seed.sh"
    safe_write_text(
        seed_sh,
        """#!/system/bin/sh
# Prepare KernelSU userspace and install validated modules through the ksud installer.
SEED_DIR="/system/etc/rollercoaster_seed"
STATE_DIR="/data/adb/rollercoaster_seed_state"
LOG="/data/adb/rollercoaster_seed.log"
KSUD="/data/adb/ksu/bin/ksud"

prepare_ksud() {
    mkdir -p /data/adb/ksu/bin /data/adb/ksu/log /data/adb/ksu/cache /data/adb/modules /data/adb/modules_update "$STATE_DIR" 2>/dev/null
    if [ -f "$SEED_DIR/ksud" ]; then
        cp -f "$SEED_DIR/ksud" /data/adb/ksud 2>/dev/null
        chmod 0755 /data/adb/ksud 2>/dev/null
        ln -sf /data/adb/ksud "$KSUD" 2>/dev/null
        ln -sf /data/adb/ksud /data/adb/ksu/bin/resetprop 2>/dev/null
    fi
}

install_seed_modules() {
    [ "$(getprop sys.boot_completed)" = "1" ] || return 0
    tries=0
    while [ ! -x "$KSUD" ] && [ "$tries" -lt 30 ]; do
        sleep 2
        tries=$((tries + 1))
    done
    [ -x "$KSUD" ] || { echo "ksud not ready after 60s" >> "$LOG"; return 1; }
    failed=0
    for module_zip in "$SEED_DIR"/modules/*.zip; do
        [ -f "$module_zip" ] || continue
        module_id=$(unzip -p "$module_zip" module.prop 2>/dev/null | grep '^id=' | head -n1 | cut -d= -f2 | tr -d '\r')
        [ -n "$module_id" ] || { echo "missing module.prop: $module_zip" >> "$LOG"; failed=1; continue; }
        state_file="$STATE_DIR/$module_id.installed"
        [ -f "$state_file" ] && continue
        if [ -d "/data/adb/modules/$module_id" ] || [ -d "/data/adb/modules_update/$module_id" ]; then
            echo "Preserving existing module $module_id; not replacing user data" >> "$LOG"
            touch "$state_file"
            continue
        fi
        echo "Installing $module_id from $(basename "$module_zip")" >> "$LOG"
        if "$KSUD" module install "$module_zip" >> "$LOG" 2>&1; then
            touch "$state_file"
        else
            echo "Install failed for $module_id; will retry on next boot" >> "$LOG"
            failed=1
        fi
    done
    if [ "$failed" -eq 0 ]; then
        touch /data/adb/.rollercoaster_4_0_1_7_seeded
    fi
    return "$failed"
}

case "$1" in
    --prepare)
        prepare_ksud
        ;;
    --install)
        install_seed_modules
        ;;
    --late)
        if [ -f "/data/adb/modules/rollercoaster_core/service.sh" ] || [ -f "/data/adb/modules_update/rollercoaster_core/service.sh" ]; then
            exit 0
        fi
        /system/bin/sh /system/etc/rollercoaster_late.sh
        ;;
    *)
        prepare_ksud
        install_seed_modules
        ;;
esac
""",
    )

    init_rc = system_dir / "etc" / "init" / "rollercoaster_seed.rc"
    safe_write_text(
        init_rc,
        """on post-fs-data
    exec_background u:r:su:s0 root root -- /system/bin/sh /system/bin/rollercoaster_seed.sh --prepare

on property:sys.boot_completed=1
    exec_background u:r:su:s0 root root -- /system/bin/sh /system/bin/rollercoaster_seed.sh --install
    exec_background u:r:su:s0 root root -- /system/bin/sh /system/bin/rollercoaster_seed.sh --late
""",
    )
    safe_copy(core_module_src / "service.sh", system_dir / "etc" / "rollercoaster_late.sh")
    print(f"[ksu-seed] Staged ksud and {len(metadata)} validated modules for ksud installation")
    return metadata

def prepare_port_tools(
    repo_url: str,
    otatools_url: str,
    cache_dir: Path,
    revision: str = "",
    otatools_sha256: str = "",
) -> Dict[str, Any]:
    tdir = clone_shallow(repo_url, cache_dir / "HyperOS-Port-Python", revision=revision)
    bin_dir = tdir / "bin" / "linux" / "x86_64"
    if not bin_dir.is_dir():
        candidates = list(tdir.rglob("magiskboot"))
        bin_dir = candidates[0].parent if candidates else Path("/usr/bin")

    tools: Dict[str, Any] = {"port_tools_dir": tdir}
    for name in ("magiskboot", "mkfs.erofs", "extract.erofs", "lpunpack"):
        candidate = bin_dir / name
        if candidate.is_file():
            candidate.chmod(0o755)
            tools[name] = candidate
        elif shutil.which(name):
            tools[name] = Path(shutil.which(name))

    # lpmake is intentionally taken from the configured OTA tools bundle,
    # not silently mixed with a tool bundled by the porting helper repository.
    if not otatools_url:
        raise RuntimeError("Configured otatools ZIP is required for lpmake")
    ota_dir = cache_dir / "otatools"
    ota_bin = ota_dir / "bin"
    ota_zip = download_file(
        otatools_url,
        cache_dir / "otatools.zip",
        expected_sha256=otatools_sha256,
    )
    if not all((ota_bin / name).is_file() for name in ("lpmake", "lpunpack")):
        ota_dir.mkdir(parents=True, exist_ok=True)
        run_cmd(["unzip", "-oq", str(ota_zip), "-d", str(ota_dir)])
    if not (ota_bin / "lpmake").is_file():
        raise RuntimeError("Configured otatools ZIP does not contain bin/lpmake")

    def find_ota_tool(name: str) -> Optional[Path]:
        direct = ota_bin / name
        if direct.is_file():
            return direct
        candidates = sorted(path for path in ota_dir.rglob(name) if path.is_file() and not path.is_symlink())
        if not candidates:
            return None
        host_candidates = [
            path for path in candidates
            if any(part in ("linux-x86_64", "linux-x86") for part in path.parts)
        ]
        return (host_candidates or candidates)[0]

    for name in ("lpmake", "lpunpack"):
        candidate = find_ota_tool(name)
        if candidate and (name == "lpmake" or name not in tools):
            candidate.chmod(candidate.stat().st_mode | 0o111)
            tools[name] = candidate
    ota_lib = ota_dir / "lib64"
    if ota_lib.is_dir():
        current_ld = os.environ.get("LD_LIBRARY_PATH", "")
        tools["_env"] = {
            "LD_LIBRARY_PATH": f"{ota_lib}:{current_ld}" if current_ld else str(ota_lib)
        }

    props_hook = tdir / "devices" / "common" / "PropsHook.zip"
    if props_hook.is_file():
        tools["props_hook_zip"] = props_hook
    for name in ("APKEditor.jar", "baksmali.jar", "smali.jar"):
        candidate = tdir / "bin" / name
        if candidate.is_file():
            tools[name.lower().replace(".jar", "_jar")] = candidate

    required = (
        "magiskboot",
        "mkfs.erofs",
        "extract.erofs",
        "lpunpack",
        "lpmake",
        "props_hook_zip",
        "apkeditor_jar",
        "baksmali_jar",
        "smali_jar",
    )
    missing = [name for name in required if name not in tools or not Path(tools[name]).is_file()]
    if missing:
        raise RuntimeError(f"Missing required pinned ROM integration tools: {missing}")
    validate_props_hook_zip(tools["props_hook_zip"])
    return tools

def main() -> None:
    parser = argparse.ArgumentParser(description="Build Rollercoaster Hypos 4.0.1.7 ROM for joyeuse")
    parser.add_argument("--config", default=Path("config/rollercoaster_joyeuse.json"), type=Path)
    parser.add_argument("--work-dir", default=Path("work"), type=Path)
    parser.add_argument("--out-dir", default=Path("out"), type=Path)
    parser.add_argument("--sdk-root", default=Path(os.environ.get("ANDROID_HOME", "/usr/local/lib/android/sdk")), type=Path)
    parser.add_argument("--base-rom-override", default=None, type=Path)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    rom_cfg = cfg["rom"]
    src_cfg = cfg["sources"]

    required_sha_keys = (
        "base_rom_sha256",
        "kernel_zip_sha256",
        "cruecy_audio_effects_sha256",
        "ksu_manager_sha256",
        "heliboard_sha256",
        "play_integrity_fork_sha256",
        "tricky_store_sha256",
        "susfs_module_sha256",
        "otatools_zip_sha256",
        "revanced_youtube_module_sha256",
        "revanced_ytmusic_module_sha256",
    )
    for key in required_sha_keys:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(src_cfg.get(key, ""))):
            raise RuntimeError(f"Configuration is missing a valid SHA-256 pin: sources.{key}")
    for key in (
        "dolby_vendor_commit",
        "miuicamera_vendor_commit",
        "miuicamera_device_commit",
        "hyperos_port_tools_commit",
    ):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(src_cfg.get(key, ""))):
            raise RuntimeError(f"Configuration is missing an immutable commit pin: sources.{key}")

    work_dir = args.work_dir.resolve()
    dl_dir = work_dir / "downloads"
    repos_dir = work_dir / "repos"
    gen_dir = work_dir / "generated"
    out_dir = args.out_dir.resolve()
    for directory in (dl_dir, repos_dir, gen_dir, out_dir):
        directory.mkdir(parents=True, exist_ok=True)

    print("==================================================================")
    print(f" Building {rom_cfg['version_display']} for {rom_cfg['model']} ({rom_cfg['device']})")
    print(f" Maintainer: {rom_cfg['maintainer']} ({rom_cfg['telegram_username']})")
    print("==================================================================")

    tools = prepare_port_tools(
        src_cfg["hyperos_port_tools_repo"],
        src_cfg["otatools_zip"],
        repos_dir,
        revision=src_cfg["hyperos_port_tools_commit"],
        otatools_sha256=src_cfg["otatools_zip_sha256"],
    )
    dolby_repo = clone_shallow(
        src_cfg["dolby_vendor_repo"],
        repos_dir / "vendor_dolby",
        revision=src_cfg["dolby_vendor_commit"],
    )
    cam_vendor = clone_shallow(
        src_cfg["miuicamera_vendor_repo"],
        repos_dir / "miuicamera_vendor",
        revision=src_cfg["miuicamera_vendor_commit"],
    )
    cam_device = clone_shallow(
        src_cfg["miuicamera_device_repo"],
        repos_dir / "miuicamera_device",
        revision=src_cfg["miuicamera_device_commit"],
    )

    def pinned_apk(key: str, checksum_key: str = "", size_key: str = "") -> Path:
        expected_size = int(src_cfg[size_key]) if size_key and src_cfg.get(size_key) else None
        apk = fetch_pinned_asset(
            src_cfg[key],
            dl_dir,
            str(src_cfg.get(checksum_key, "")) if checksum_key else "",
            expected_size_bytes=expected_size,
        )
        validate_apk_file(apk)
        return apk

    ksu_apk = pinned_apk("ksu_manager_apk", "ksu_manager_sha256")
    webui_apk = pinned_apk("ksu_webui_apk", "ksu_webui_sha256", "ksu_webui_apk_size_bytes")
    adaway_apk = pinned_apk("adaway_apk", "adaway_sha256", "adaway_apk_size_bytes")
    heliboard_apk = pinned_apk("heliboard_apk", "heliboard_sha256")
    insular_apk = fetch_insular_apk(src_cfg["insular_fdroid_api"], dl_dir)
    validate_apk_file(insular_apk)

    pif_zip = fetch_pinned_asset(
        src_cfg["play_integrity_fork_zip"], dl_dir, src_cfg["play_integrity_fork_sha256"]
    )
    tricky_zip = fetch_pinned_asset(
        src_cfg["tricky_store_zip"], dl_dir, src_cfg["tricky_store_sha256"]
    )
    susfs_zip = fetch_pinned_asset(
        src_cfg["susfs_module_zip"], dl_dir, src_cfg["susfs_module_sha256"]
    )
    pif_metadata = validate_module_zip(pif_zip)
    tricky_metadata = validate_module_zip(tricky_zip)
    susfs_metadata = validate_module_zip(susfs_zip)
    revanced_zips = fetch_revanced_ksu_zips(
        src_cfg["revanced_repo"],
        src_cfg["revanced_release_tag"],
        dl_dir,
        src_cfg["revanced_youtube_module_sha256"],
        src_cfg["revanced_ytmusic_module_sha256"],
    )
    revanced_metadata = {
        name: validate_module_zip(path, reject_microg_payload=True)
        for name, path in revanced_zips.items()
    }

    audio_effects_source = fetch_github_file(
        "AndroidOne-Experience/device_xiaomi_miatoll",
        "audio/audio_effects.xml",
        src_cfg["cruecy_audio_effects_commit"],
        gen_dir / "cruecy_audio_effects.xml",
        src_cfg["cruecy_audio_effects_sha256"],
    )

    bootanim_zip = generate_bootanimation(gen_dir / src_cfg["bootanimation_zip_name"])
    ios_mtz = generate_mtz(gen_dir / src_cfg["ios_theme_mtz_name"])
    support_apk = build_support_apk(
        gen_dir / "RollercoasterSupport.apk",
        args.sdk_root,
        gen_dir / "_apk_work" / "support",
    )
    validate_apk_file(support_apk)
    overlay_apks = build_all_overlays(gen_dir / "overlays", args.sdk_root, gen_dir / "_apk_work")
    for overlay_apk in overlay_apks:
        validate_apk_file(overlay_apk)
    dax_apk = inject_indonesian_dolby_strings_and_build(
        dolby_repo,
        gen_dir / "DolbyDaxUI.apk",
        args.sdk_root,
        gen_dir / "_apk_work" / "dolby",
    )
    validate_apk_file(dax_apk)

    if args.base_rom_override:
        base_rom_zip = args.base_rom_override.resolve()
        if not base_rom_zip.is_file():
            raise FileNotFoundError(f"Base ROM override does not exist: {base_rom_zip}")
        expected_base_sha = src_cfg["base_rom_sha256"]
        actual_base_sha = sha256_file(base_rom_zip)
        if actual_base_sha.lower() != expected_base_sha.lower():
            raise RuntimeError(
                "Base ROM override SHA-256 mismatch: "
                f"expected {expected_base_sha}, got {actual_base_sha}"
            )
    else:
        base_rom_zip = download_file(
            src_cfg["base_rom_url"],
            dl_dir / src_cfg["base_rom_filename"],
            expected_sha256=src_cfg["base_rom_sha256"],
        )
    base_rom_sha256 = sha256_file(base_rom_zip)

    ws = unpack_rom_zip(base_rom_zip, work_dir / "rom_ws", tools)
    if not ws.boot_img_path.is_file():
        raise RuntimeError(f"Base ROM is missing boot.img: {ws.boot_img_path}")
    if ws.container_type == "super":
        if ws.super_geom is None:
            raise RuntimeError("LP metadata parser did not produce super geometry; refusing to build an unchecked super image")
        if ws.super_geom.is_ab:
            raise RuntimeError(
                "The selected base ROM uses an A/B dynamic-partition layout. joyeuse is built as A-only; "
                "refusing to repack or flash an incompatible super image."
            )
    required_partitions = ("system", "product", "system_ext", "vendor")
    missing_partitions = [name for name in required_partitions if name not in ws.partitions]
    if missing_partitions:
        raise RuntimeError(f"Base ROM is missing required joyeuse partitions: {missing_partitions}")

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
    patched_vbmeta = []
    for vbmeta_path in ws.vbmeta_paths:
        if not disable_vbmeta_verification(vbmeta_path):
            raise RuntimeError(f"Could not safely disable AVB verification in {vbmeta_path.name}")
        patched_vbmeta.append(vbmeta_path.name)

    sys_rec = ws.partitions["system"]
    prod_rec = ws.partitions["product"]
    sext_rec = ws.partitions["system_ext"]
    vend_rec = ws.partitions["vendor"]
    s_root = sys_rec.extract_dir
    s_dir = (s_root / "system") if (s_root / "system" / "build.prop").is_file() else s_root
    p_dir = prod_rec.extract_dir
    se_dir = sext_rec.extract_dir
    v_dir = vend_rec.extract_dir

    system_build_prop = s_dir / "build.prop"
    vendor_build_prop = v_dir / "build.prop"
    if not system_build_prop.is_file() or not vendor_build_prop.is_file():
        raise RuntimeError(
            "Required system/vendor build.prop files were not found in extracted partitions"
        )
    patch_build_prop_file(system_build_prop, cfg["props"]["system"])
    product_props = [
        product_prop
        for product_prop in (p_dir / "etc" / "build.prop", p_dir / "build.prop")
        if product_prop.is_file() and not product_prop.is_symlink()
    ]
    if not product_props:
        raise RuntimeError("Required product build.prop was not found in the extracted product partition")
    for product_prop in product_props:
        patch_build_prop_file(product_prop, cfg["props"]["product"])
    patch_build_prop_file(
        vendor_build_prop,
        cfg["props"]["vendor"],
        preserve_keys={
            "ro.product.vendor.device",
            "ro.product.vendor.name",
            "ro.vendor.build.fingerprint",
        },
    )
    for partition_record in ws.partitions.values():
        for build_prop in (
            partition_record.extract_dir / "etc" / "build.prop",
            partition_record.extract_dir / "build.prop",
        ):
            if (
                build_prop.is_file()
                and not build_prop.is_symlink()
                and partition_record.base_name not in ("vendor", "odm")
            ):
                patch_build_prop_file(
                    build_prop,
                    {
                        "ro.mi.os.version.show": rom_cfg["version_display"],
                        "ro.build.display.id": rom_cfg["version_display"],
                    },
                )

    fstab_count = patch_vendor_fstabs_strip_avb(v_dir)
    patch_gps_indonesia(v_dir)
    feature_file_count = patch_device_features_for_joyeuse(p_dir, s_dir)
    removed_cn_packages = debloat_cn_packages(
        [s_dir, p_dir, se_dir], cfg["debloat_dirs"], cfg["keep_packages_guard"]
    )

    safe_copy(bootanim_zip, p_dir / "media" / "bootanimation.zip")
    safe_copy(bootanim_zip, s_dir / "media" / "bootanimation.zip")
    safe_copy(ios_mtz, s_dir / "rollercoaster" / "themes" / src_cfg["ios_theme_mtz_name"])
    safe_copy(ios_mtz, p_dir / "etc" / "precust_theme" / src_cfg["ios_theme_mtz_name"])

    core_src = repo_root / "modules" / "rollercoaster_core"
    safe_copy(core_src / "system" / "etc" / "hosts", s_dir / "etc" / "hosts")
    safe_copy(
        core_src / "system" / "product" / "etc" / "sysconfig" / "rollercoaster_gphotos_unlimited.xml",
        p_dir / "etc" / "sysconfig" / "rollercoaster_gphotos_unlimited.xml",
    )

    install_prebuilt_apk(support_apk, "RollercoasterSupport", p_dir, priv=True)
    for overlay_apk in overlay_apks:
        safe_copy(overlay_apk, p_dir / "overlay" / overlay_apk.name)
    install_prebuilt_apk(ksu_apk, "KernelSUNext", p_dir)
    install_prebuilt_apk(webui_apk, "KsuWebUI", p_dir)
    install_prebuilt_apk(adaway_apk, "AdAway", p_dir)
    install_prebuilt_apk(heliboard_apk, "HeliBoard", p_dir)
    install_prebuilt_apk(insular_apk, "Insular", p_dir)

    dolby_manifest = integrate_cruecy_dolby_and_spatial(
        dolby_repo=dolby_repo,
        vendor_dir=v_dir,
        system_ext_dir=se_dir,
        custom_dax_apk=dax_apk,
        cruecy_audio_effects_xml=audio_effects_source,
    )
    camera_manifest = integrate_leica_camera(
        cam_vendor_repo=cam_vendor,
        cam_device_repo=cam_device,
        system_dir=s_dir,
        product_dir=p_dir,
        system_ext_dir=se_dir,
        work_dir=gen_dir / "camera",
    )
    props_hook_manifest = integrate_props_hook_framework(
        s_dir,
        tools,
        work_dir / "props_hook",
    )

    module_zips = [susfs_zip, pif_zip, tricky_zip, revanced_zips["yt"], revanced_zips["ytm"]]
    seeded_modules = seed_ksu_and_modules(
        system_dir=s_dir,
        ksud_bin=ksud_out,
        core_module_src=core_src,
        module_zips=module_zips,
    )

    final_zip = out_dir / rom_cfg["output_zip_name"]
    repack_rom_workspace(ws, final_zip, tools)
    if not final_zip.is_file() or final_zip.stat().st_size == 0:
        raise RuntimeError(f"ROM repacker did not produce a non-empty ZIP: {final_zip}")
    with zipfile.ZipFile(final_zip, "r") as zf:
        bad_member = zf.testzip()
        if bad_member:
            raise RuntimeError(f"Final ROM ZIP failed CRC validation at {bad_member}")
        final_members = zf.namelist()
        if not any(name.endswith("boot.img") for name in final_members):
            raise RuntimeError("Final ROM ZIP is missing the patched boot.img")

    source_revisions = {
        "dolby_vendor": src_cfg["dolby_vendor_commit"],
        "miuicamera_vendor": src_cfg["miuicamera_vendor_commit"],
        "miuicamera_device": src_cfg["miuicamera_device_commit"],
        "hyperos_port_tools": src_cfg["hyperos_port_tools_commit"],
        "cruecy_audio_effects": src_cfg["cruecy_audio_effects_commit"],
        "revanced_release": f"{src_cfg['revanced_repo']}@{src_cfg['revanced_release_tag']}",
    }
    asset_hashes = {
        "kernel_zip": src_cfg["kernel_zip_sha256"],
        "base_rom_zip": base_rom_sha256,
        "ksu_manager_apk": sha256_file(ksu_apk),
        "ksu_webui_apk": sha256_file(webui_apk),
        "adaway_apk": sha256_file(adaway_apk),
        "heliboard_apk": sha256_file(heliboard_apk),
        "insular_apk": sha256_file(insular_apk),
        "play_integrity_fork_zip": sha256_file(pif_zip),
        "tricky_store_zip": sha256_file(tricky_zip),
        "susfs_module_zip": sha256_file(susfs_zip),
        "youtube_module_zip": sha256_file(revanced_zips["yt"]),
        "youtube_music_module_zip": sha256_file(revanced_zips["ytm"]),
        "otatools_zip": src_cfg["otatools_zip_sha256"],
        "cruecy_audio_effects_xml": src_cfg["cruecy_audio_effects_sha256"],
    }
    manifest = {
        "rom_name": rom_cfg["version_display"],
        "base_version": rom_cfg["base_version"],
        "android_version": rom_cfg["android_version"],
        "device": rom_cfg["device"],
        "device_family": rom_cfg["device_family"],
        "model": rom_cfg["model"],
        "maintainer": rom_cfg["maintainer"],
        "telegram": rom_cfg["telegram_username"],
        "output_zip": str(final_zip),
        "output_size_bytes": final_zip.stat().st_size,
        "output_sha256": sha256_file(final_zip),
        "container_type": ws.container_type,
        "super_metadata_validation": {
            "performed": ws.super_geom is not None,
            "method": "AOSP liblp v10 parser; SHA-256 geometry/header/tables and bounds checks",
        },
        "repacked_partitions": sorted(ws.partitions.keys()),
        "patched_vbmeta": patched_vbmeta,
        "patched_vendor_fstab_count": fstab_count,
        "patched_joyeuse_feature_file_count": feature_file_count,
        "removed_cn_packages": removed_cn_packages,
        "source_revisions": source_revisions,
        "source_asset_sha256": asset_hashes,
        "generated_asset_sha256": {
            "bootanimation_zip": sha256_file(bootanim_zip),
            "ios_theme_mtz": sha256_file(ios_mtz),
            "support_apk": sha256_file(support_apk),
            "dolby_dax_ui_apk": sha256_file(dax_apk),
            "kernel_su_daemon": sha256_file(ksud_out),
            "overlays": {apk.name: sha256_file(apk) for apk in overlay_apks},
        },
        "integration": {
            "props_hook": props_hook_manifest,
            "dolby_spatial_audio": dolby_manifest,
            "leica_camera": camera_manifest,
            "seeded_ksu_modules": seeded_modules,
            "module_metadata": {
                "susfs": susfs_metadata,
                "play_integrity_fork": pif_metadata,
                "tricky_store": tricky_metadata,
                "youtube": revanced_metadata["yt"],
                "youtube_music": revanced_metadata["ytm"],
            },
        },
        "physical_device_tested": False,
        "play_integrity_result": "belum diuji; hasil attestation tidak dijamin",
    }
    manifest_path = out_dir / "build_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"[done] ROM ZIP: {final_zip} ({final_zip.stat().st_size / (1024**3):.2f} GiB)")
    print(f"[done] SHA-256: {manifest['output_sha256']}")
    print(f"[done] Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
