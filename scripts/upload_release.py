#!/usr/bin/env python3
"""Upload Rolleracoaster Hypos 4.0.1.7 to GoFile (single full ZIP) and GitHub Releases (<2 GiB split parts)."""

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

CHUNK_SIZE_BYTES = 1900 * 1024 * 1024  # 1900 MiB (< 2048 MiB GitHub Release asset limit)
COPY_BUF = 16 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(COPY_BUF), b""):
            h.update(chunk)
    return h.hexdigest()


def split_file_for_github(src: Path, out_dir: Path, chunk_bytes: int = CHUNK_SIZE_BYTES) -> List[Path]:
    """Split a large file into .001, .002, ... parts under 2 GiB each."""
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
        f"- `{p['name']}` ({p['size_mib']:.1f} MiB) — SHA256: `{p['sha256']}`"
        for p in part_hashes
    )
    return f"""# 🎢 Rolleracoaster Hypos 4.0.1.7 — OFFICIAL (`joyeuse`)

**Perangkat:** Xiaomi Redmi Note 9 Pro (`joyeuse` / `miatoll`)
**Basis Sistem:** Xiaomi HyperOS `4.0.17.0` (Android 17 Port)
**Versi ROM:** `Rolleracoaster Hypos 4.0.1.7`
**Pembuat / Maintainer:** **Graziemille**
**Dukungan & Komunitas Telegram:** [**@Grazie_milles**](https://t.me/Grazie_milles)

---

## 📥 Link Download

### 1️⃣ Single Full ZIP (GoFile — Langsung Flash Tanpa Gabung File)
- **File:** `{full_zip_name}` (`{size_gib:.2f} GiB`)
- **Link GoFile:** [**{gofile_info['download_page']}**]({gofile_info['download_page']})
- **SHA-256 Full ZIP:** `{full_sha256}`

### 2️⃣ GitHub Releases Split Parts (`< 2 GB` per bagian)
{parts_md}

> **Cara Menggabungkan File Split GitHub Releases:**
> - **Android (ZArchiver):** Taruh semua file `.001`, `.002`, dst. di folder yang sama → ketuk file `.001` → pilih **Ekstrak / Gabungkan** atau Termux: `cat {full_zip_name}.* > {full_zip_name}`
> - **Windows (CMD):** `copy /b {full_zip_name}.* {full_zip_name}` (atau klik kanan `.001` di **7-Zip** → *Combine files*)
> - **Linux / macOS:** `cat {full_zip_name}.* > {full_zip_name} && sha256sum {full_zip_name}`

---

## 🔥 Fitur Utama & Optimasi Khusus `joyeuse`

- **Kernel Custom Pre-patched (`4.14.370-Rollercoaster-Roll.3.3`):**
  - Terintegrasi **KernelSU-Next v3.4.0 (`33294`)** + **SuSFS v2.3.0** + **NoMount VFS** + **KSUWebUI**.
  - Seluruh partisi **EROFS** dikemas ulang menggunakan kompresi **`lz4hc,9`** sehingga 100% kompatibel dengan kernel `Rollercoaster-Roll.3.3`.
- **Root Stealth & Play Store Integrity:**
  - Modul `susfs4ksu`, `PlayIntegrityFork`, `TrickyStore`, dan `PropsHook` otomatis terpasang sejak boot pertama.
  - Spoofing Bootloader Locked (`green` / `locked`) aktif otomatis.
- **Hardware `joyeuse` 100% Optimal:**
  - **2D Face Unlock** responsif, **Side-Mounted Fingerprint** (`fod=false`), **NFC (`NQNfcNci` + HCE)** khusus `joyeuse`, **30W Mi Turbo Charge**, **Jack Audio 3.5mm**, & **IR Blaster**.
  - Unlock **VoLTE / VoWiFi Operator Indonesia** + NTP GPS cepat `id.pool.ntp.org`.
- **Audio, Kamera & Tampilan:**
  - **Dolby Atmos (Cruecy + Lunaris UI Bahasa Indonesia)** + **Google Spatial Audio** anti-konflik EQ.
  - **Xiaomi Leica Camera v6.8** untuk sensor 64MP `joyeuse` (`s5kgw1`).
  - **Bootanimation Custom 60 FPS (`1080x2400`)**: Animasi huruf **`Rollercoaster`** jatuh dari atas, menyatu dengan **`OS`**, dan berkilau penuh warna dengan efek **Neon Glow**.
  - Halaman **Tentang Ponsel -> Support** menampilkan pembuat **Graziemille** beserta profil & ikon Telegram [**@Grazie_milles**](https://t.me/Grazie_milles).
  - **Bahasa Indonesia (`id-ID`)** default, **Aplikasi Tema Xiaomi Global**, & **Tema iOS Liquid Glass (`Rollercoaster_iOS_Pro.mtz`)** offline.
  - **Bebas Bloatware CN & Iklan Xiaomi**, **Google Play Ready**, **YouTube ReVanced & YT Music ReVanced** (terintegrasi KSU tanpa MicroG), **AdAway**, dan **Sandbox AOSP (Work Profile)** via **Insular**.
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
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    full_sha256 = manifest.get("output_sha256") or sha256_file(rom_zip)

    gofile_info = upload_to_gofile(rom_zip)

    split_dir = rom_zip.parent / "gh_release_assets"
    parts = split_file_for_github(rom_zip, split_dir)
    if args.delete_source_after_gofile and len(parts) > 1:
        rom_zip.unlink(missing_ok=True)

    part_hashes: List[Dict[str, Any]] = []
    sha_lines = [f"{full_sha256}  {rom_zip.name} (FULL SINGLE ZIP - GoFile)"]
    for p in parts:
        ph = sha256_file(p)
        part_hashes.append({
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "size_mib": p.stat().st_size / (1024 * 1024),
            "sha256": ph,
        })
        sha_lines.append(f"{ph}  {p.name}")

    sha_txt = split_dir / "SHA256SUMS.txt"
    sha_txt.write_text("\n".join(sha_lines) + "\n", encoding="utf-8")

    notes_md = split_dir / "RELEASE_NOTES.md"
    notes_md.write_text(
        render_release_notes(manifest, gofile_info, rom_zip.name, full_sha256, part_hashes),
        encoding="utf-8",
    )

    assets = [*parts, sha_txt, args.manifest.resolve()]
    release_title = "Rolleracoaster Hypos 4.0.1.7 | Redmi Note 9 Pro (joyeuse)"
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
