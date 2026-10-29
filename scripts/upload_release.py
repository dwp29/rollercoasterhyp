#!/usr/bin/env python3
"""Upload Rollercoaster Hypos 4.0.1.7 to GoFile (single full ZIP) and GitHub Releases (<2 GiB split parts)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

CHUNK_SIZE_BYTES = 1900 * 1024 * 1024  # Safely below the requested 1.9 GiB per GitHub asset
MAX_REQUESTED_ASSET_BYTES = int(1.9 * (1024**3))
COPY_BUF = 16 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(COPY_BUF), b""):
            h.update(chunk)
    return h.hexdigest()


def split_file_for_github(src: Path, out_dir: Path, chunk_bytes: int = CHUNK_SIZE_BYTES) -> List[Path]:
    """Split a large file into .001, .002, ... parts under 2 GiB each."""
    if chunk_bytes <= 0 or chunk_bytes >= MAX_REQUESTED_ASSET_BYTES:
        raise ValueError(
            f"GitHub part size must be positive and below 1.9 GiB ({chunk_bytes} bytes)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    total_size = src.stat().st_size
    if total_size <= chunk_bytes:
        single_copy = out_dir / src.name
        if single_copy.resolve() != src.resolve():
            shutil.copy2(src, single_copy)
        return [single_copy]

    parts: List[Path] = []
    idx = 1
    with src.open("rb") as fin:
        while True:
            part_path = out_dir / f"{src.name}.{idx:03d}"
            written = 0
            with part_path.open("wb") as fout:
                while written < chunk_bytes:
                    to_read = min(COPY_BUF, chunk_bytes - written)
                    buf = fin.read(to_read)
                    if not buf:
                        break
                    fout.write(buf)
                    written += len(buf)
            if written == 0:
                part_path.unlink(missing_ok=True)
                break
            print(f"[split] Created {part_path.name} ({written / (1024**2):.1f} MiB)")
            parts.append(part_path)
            if written < chunk_bytes:
                break
            idx += 1
    return parts


def discover_gofile_endpoints() -> List[str]:
    endpoints = ["https://upload.gofile.io/uploadfile"]
    try:
        req = urllib.request.Request(
            "https://api.gofile.io/servers",
            headers={"User-Agent": "RollercoasterROM-Uploader/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") == "ok":
            servers = data.get("data", {}).get("servers", [])
            for srv in servers[:4]:
                name = srv.get("name")
                if name:
                    endpoints.append(f"https://{name}.gofile.io/contents/uploadfile")
                    endpoints.append(f"https://{name}.gofile.io/uploadfile")
    except Exception as exc:
        print(f"[gofile] Server discovery fallback ({exc}); using universal endpoint")
    seen = set()
    uniq = []
    for ep in endpoints:
        if ep not in seen:
            seen.add(ep)
            uniq.append(ep)
    return uniq


def upload_to_gofile(rom_zip: Path) -> Dict[str, Any]:
    """Stream full ROM ZIP to GoFile via curl multipart upload with multi-server fallback."""
    endpoints = discover_gofile_endpoints()
    last_err = ""

    for attempt in range(1, 4):
        for ep in endpoints:
            print(f"[gofile] Uploading {rom_zip.name} ({rom_zip.stat().st_size / (1024**3):.2f} GiB) -> {ep} (attempt {attempt})...")
            cmd = [
                "curl",
                "--http1.1",
                "-sS",
                "-L",
                "--connect-timeout", "30",
                "--max-time", "3600",
                "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) RollercoasterBuilder/4.0",
                "-F", f"file=@{rom_zip}",
                ep,
            ]
            proc = subprocess.run(cmd, text=True, capture_output=True)
            if proc.returncode != 0:
                last_err = proc.stderr.strip() or f"curl exit {proc.returncode}"
                print(f"[gofile] Warning: {ep} failed: {last_err}")
                continue
            raw_out = proc.stdout.strip()
            try:
                json_start = raw_out.find("{")
                payload = json.loads(raw_out[json_start:] if json_start >= 0 else raw_out)
            except Exception as exc:
                last_err = f"Non-JSON response from {ep}: {raw_out[:240]} ({exc})"
                print(f"[gofile] Warning: {last_err}")
                continue

            if payload.get("status") == "ok" and payload.get("data", {}).get("downloadPage"):
                dl_page = payload["data"]["downloadPage"]
                print(f"[gofile] SUCCESS! Download page: {dl_page}")
                return {
                    "provider": "GoFile",
                    "download_page": dl_page,
                    "code": payload["data"].get("code", ""),
                    "file_id": payload["data"].get("id") or payload["data"].get("fileId", ""),
                    "endpoint": ep,
                }
            last_err = f"Unexpected GoFile status: {raw_out[:240]}"
            print(f"[gofile] Warning: {last_err}")
        time.sleep(5 * attempt)

    raise RuntimeError(f"All GoFile upload attempts exhausted. Last error: {last_err}")


def render_release_notes(
    manifest: Dict[str, Any],
    gofile_info: Dict[str, Any],
    full_zip_name: str,
    full_sha256: str,
    part_hashes: List[Dict[str, Any]],
) -> str:
    size_gib = manifest["output_size_bytes"] / (1024 ** 3)
    parts_md = "\n".join(
        f"- `{part['name']}` ({part['size_mib']:.1f} MiB) — SHA-256: `{part['sha256']}`"
        for part in part_hashes
    )
    integration = manifest.get("integration", {})
    module_metadata = integration.get("module_metadata", {})
    module_names = [
        module_metadata.get(key, {}).get("name", key)
        for key in ("susfs", "play_integrity_fork", "tricky_store", "youtube", "youtube_music")
        if module_metadata.get(key)
    ]
    module_list = ", ".join(f"`{name}`" for name in module_names) or "modul yang terdaftar di manifest build"
    camera = integration.get("leica_camera", {})
    dolby = integration.get("dolby_spatial_audio", {})
    props_hook = integration.get("props_hook", {})
    camera_line = (
        f"- **Kamera Leica v6.8:** APK tervalidasi ({camera.get('apk_size_bytes', 0) / (1024**2):.1f} MiB), "
        f"{camera.get('split_part_count', 0)} potongan sumber digabung dan library/allowlist disiapkan."
        if camera else "- Integrasi kamera tidak tercatat di manifest."
    )
    dolby_line = (
        "- **Dolby Atmos dan audio spasial:** blob vendor, konfigurasi efek/codec dan UI DAX berbahasa Indonesia "
        "disiapkan dari sumber yang dipin. Dukungan runtime bergantung pada HAL vendor dan perangkat."
        if dolby else "- Integrasi Dolby/audio tidak tercatat di manifest."
    )
    hook_line = (
        "- **PropsHook:** DEX disisipkan ke `framework.jar` dan panggilan Instrumentation lolos validasi arsip."
        if props_hook else "- PropsHook tidak tercatat di manifest."
    )
    physical_tested = bool(manifest.get("physical_device_tested", False))
    physical_note = (
        "Build ini telah diuji pada perangkat fisik."
        if physical_tested
        else "Build ini belum diuji pada perangkat fisik; keberhasilan boot, kamera, audio, NFC, jaringan seluler, dan fitur regional belum dikonfirmasi."
    )
    integrity_result = manifest.get(
        "play_integrity_result",
        "belum diuji; hasil attestation tidak dijamin",
    )
    return f"""# 🎢 Rollercoaster Hypos 4.0.1.7 — `joyeuse`

**Perangkat target:** Xiaomi Redmi Note 9 Pro (`joyeuse` / keluarga `miatoll`)<br>
**Basis:** Xiaomi HyperOS `{manifest.get('base_version', 'tidak dicantumkan')}` — Android {manifest.get('android_version', 'tidak dicantumkan')}<br>
**Maintainer:** Graziemille — [@Grazie_milles](https://t.me/Grazie_milles)<br>
**Ukuran ZIP:** {size_gib:.2f} GiB<br>
**SHA-256 ZIP lengkap:** `{full_sha256}`

## Unduhan

### ZIP lengkap — GoFile
- [{full_zip_name}]({gofile_info['download_page']})

### Bagian GitHub Release (setiap aset di bawah 1,9 GiB)
{parts_md}

Unduh semua bagian ke satu folder dan gabungkan sesuai urutan nama. Linux/macOS/Termux:
`cat {full_zip_name}.* > {full_zip_name}`

Windows CMD:
`copy /b {full_zip_name}.* {full_zip_name}`

Setelah digabung, cocokkan berkas dengan nilai SHA-256 ZIP lengkap di atas. `SHA256SUMS.txt` juga menyertakan checksum setiap bagian dan manifest.

## Ringkasan build

- Kernel Roll 3.3 dipasang ke `boot.img`; hasil repack boot diverifikasi melalui round-trip `magiskboot` dan ukuran boot dibatasi sesuai partisi joyeuse.
- Modul KernelSU disertakan sebagai seed dan diproses melalui `ksud` saat boot. Modul yang sudah ada di perangkat tidak ditimpa. Periksa hasil pemasangan di aplikasi KernelSU setelah boot.
{hook_line}
{dolby_line}
{camera_line}
- Bahasa Indonesia, aplikasi Rollercoaster Support, overlay, tema MTZ, bootanimation, HeliBoard, AdAway, Insular, dan KernelSU Manager disertakan sesuai manifest.
- Modul opsional yang dipaketkan: {module_list}.

## Catatan penting

- **Khusus perangkat `joyeuse`; jangan flash pada `curtana`, `gram`, `excalibur`, atau perangkat lain.** Pastikan berkas cocok dengan varian perangkat dan simpan cadangan data/firmware sebelum memasang.
- ROM ini mengubah boot dan menonaktifkan pemeriksaan AVB pada metadata yang dipatch. Bootloader harus dibuka sesuai prosedur resmi perangkat; perubahan properti Android tidak mengunci bootloader atau memulihkan rantai kepercayaan kriptografis.
- {physical_note}
- Status Play Integrity/attestation: **{integrity_result}**. Hasil layanan pihak ketiga tidak dijamin; modul yang disertakan tidak menggantikan pengujian perangkat atau kebijakan penyedia layanan.
- Flag fitur perangkat, VoLTE/VoWiFi, NFC/HCE, face unlock, Dolby, spatializer, dan kamera dapat memerlukan dukungan hardware, firmware, region, operator, serta izin yang sesuai.
- SHA-256 di atas mengidentifikasi berkas build ini. Verifikasi checksum sebelum flashing dan hentikan pemasangan bila nilainya berbeda.
"""


def publish_github_release(
    tag: str,
    title: str,
    notes_file: Path,
    assets: List[Path],
    repo: Optional[str] = None,
) -> str:
    repo_args = ["--repo", repo] if repo else []
    view_cmd = ["gh", "release", "view", tag, *repo_args]
    exists = subprocess.run(view_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    if not exists:
        subprocess.run(["gh", "release", "create", tag, "--title", title, "--notes-file", str(notes_file), *repo_args], check=True)
    else:
        subprocess.run(["gh", "release", "edit", tag, "--title", title, "--notes-file", str(notes_file), *repo_args], check=True)

    for asset in assets:
        print(f"[gh-release] Uploading {asset.name} ({asset.stat().st_size / (1024**2):.1f} MiB)...")
        subprocess.run(["gh", "release", "upload", tag, str(asset), "--clobber", *repo_args], check=True)

    url_out = subprocess.check_output(
        ["gh", "release", "view", tag, "--json", "url", "--jq", ".url", *repo_args],
        text=True,
    ).strip()
    print(f"[gh-release] Published at: {url_out}")
    return url_out


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish Rollercoaster ROM to GoFile and GitHub Releases")
    parser.add_argument("--rom-zip", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--tag", default="v4.0.1.7-joyeuse")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--delete-source-after-gofile", action="store_true")
    args = parser.parse_args()

    rom_zip = args.rom_zip.resolve()
    manifest_path = args.manifest.resolve()
    if not rom_zip.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Both the ROM ZIP and its build manifest must exist before publishing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    full_sha256 = sha256_file(rom_zip)
    expected_sha = str(manifest.get("output_sha256", ""))
    if expected_sha and full_sha256.lower() != expected_sha.lower():
        raise RuntimeError(
            f"ROM ZIP SHA-256 does not match its build manifest: {full_sha256} != {expected_sha}"
        )
    expected_size = manifest.get("output_size_bytes")
    if expected_size is not None and int(expected_size) != rom_zip.stat().st_size:
        raise RuntimeError("ROM ZIP size does not match its build manifest")

    gofile_info = upload_to_gofile(rom_zip)

    split_dir = rom_zip.parent / "gh_release_assets"
    parts = split_file_for_github(rom_zip, split_dir)
    if args.delete_source_after_gofile and len(parts) > 1:
        rom_zip.unlink(missing_ok=True)

    part_hashes: List[Dict[str, Any]] = []
    sha_lines = [f"# GoFile full ZIP SHA-256: {full_sha256}  {rom_zip.name}"]
    for p in parts:
        ph = sha256_file(p)
        part_hashes.append({
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "size_mib": p.stat().st_size / (1024 * 1024),
            "sha256": ph,
        })
        sha_lines.append(f"{ph}  {p.name}")

    manifest_sha256 = sha256_file(manifest_path)
    sha_lines.append(f"{manifest_sha256}  {manifest_path.name}")
    sha_txt = split_dir / "SHA256SUMS.txt"
    sha_txt.write_text("\n".join(sha_lines) + "\n", encoding="utf-8")

    notes_md = split_dir / "RELEASE_NOTES.md"
    notes_md.write_text(
        render_release_notes(manifest, gofile_info, rom_zip.name, full_sha256, part_hashes),
        encoding="utf-8",
    )

    assets = [*parts, sha_txt, args.manifest.resolve()]
    release_title = "Rollercoaster Hypos 4.0.1.7 | Redmi Note 9 Pro (joyeuse)"
    gh_release_url = publish_github_release(
        tag=args.tag,
        title=release_title,
        notes_file=notes_md,
        assets=assets,
        repo=args.repo or None,
    )

    result = {
        "rom_name": manifest["rom_name"],
        "tag": args.tag,
        "github_release_url": gh_release_url,
        "gofile": gofile_info,
        "full_zip_name": rom_zip.name,
        "full_zip_size_bytes": manifest["output_size_bytes"],
        "full_zip_sha256": full_sha256,
        "github_parts": part_hashes,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"[done] Release summary written to {args.out_json}")


if __name__ == "__main__":
    main()
