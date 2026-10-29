#!/usr/bin/env python3
"""Offline boot.img patcher for Rollercoaster-Roll.3.3 + AVB vbmeta disabler.

Verified on miatoll 128 MB boot.img (134,217,728 bytes):
  1. Verifies Rollercoaster-Roll.3.3.zip SHA-256.
  2. Unpacks ROM boot.img with `magiskboot unpack -h`.
  3. Replaces `kernel` with decompressed `Image` from Rollercoaster-Roll.3.3's `Image.gz`.
  4. Preserves ROM's native `dtb` unless it matches 3.0.304.0 SHA-256 (exact Roll 3.3 rule).
  5. Strips any stale `memmap=` / `ram.mem_address=` tokens from header cmdline.
  6. Repacks & round-trip verifies `Linux version 4.14.370-Rollercoaster-Roll.3.3` inside boot.img.
  7. Extracts Roll 3.3's `ksud` binary for ROM seeding.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

DTB_3040_SHA256 = "6ae7105b5ad512e40af1f14e53a8c27010b2d7caf0934d53c58cfcc24cc3f239"
MAX_MIATOLL_BOOT_BYTES = 128 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def patch_boot_image(
    magiskboot: Path,
    rom_boot_img: Path,
    kernel_zip: Path,
    expected_kernel_sha256: str,
    out_boot_img: Path,
    out_ksud: Path,
    work_dir: Path,
) -> None:
    actual_sha = sha256_file(kernel_zip)
    if expected_kernel_sha256 and actual_sha.lower() != expected_kernel_sha256.lower():
        raise RuntimeError(
            f"Kernel ZIP SHA-256 mismatch: expected {expected_kernel_sha256}, got {actual_sha}"
        )

    shutil.rmtree(work_dir, ignore_errors=True)
    ak_dir = work_dir / "ak3"
    unpack_dir = work_dir / "unpack"
    verify_dir = work_dir / "verify"
    ak_dir.mkdir(parents=True)
    unpack_dir.mkdir(parents=True)
    verify_dir.mkdir(parents=True)

    with zipfile.ZipFile(kernel_zip, "r") as zf:
        zf.extractall(ak_dir)

    image_gz = ak_dir / "Image.gz"
    if not image_gz.is_file():
        raise RuntimeError("Image.gz missing from Rollercoaster-Roll.3.3.zip")

    raw_image = gzip.decompress(image_gz.read_bytes())
    if b"Linux version 4.14.370-Rollercoaster-Roll.3.3" not in raw_image:
        raise RuntimeError("Unexpected kernel banner inside Image.gz")

    magiskboot = magiskboot.resolve()
    magiskboot.chmod(0o755)

    orig_boot = unpack_dir / "boot.img"
    shutil.copy2(rom_boot_img, orig_boot)

    res = subprocess.run(
        [str(magiskboot), "unpack", "-h", "boot.img"],
        cwd=str(unpack_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    print(f"[boot-patcher] magiskboot unpack:\n{res.stdout}{res.stderr}")

    (unpack_dir / "kernel").write_bytes(raw_image)

    rom_dtb = unpack_dir / "dtb"
    ak_dtb = ak_dir / "dtb"
    if rom_dtb.is_file() and rom_dtb.stat().st_size > 0 and ak_dtb.is_file():
        cur_dtb_sha = sha256_file(rom_dtb)
        if cur_dtb_sha == DTB_3040_SHA256:
            shutil.copy2(ak_dtb, rom_dtb)
            print("[boot-patcher] DTB matched 3.0.304.0 -> replaced with Roll 3.3 ramoops DTB")
        else:
            print(f"[boot-patcher] Preserved ROM native DTB (sha256={cur_dtb_sha[:16]}...)")

    header_file = unpack_dir / "header"
    if header_file.is_file():
        hdr_text = header_file.read_text(encoding="utf-8", errors="ignore")
        new_lines = []
        for line in hdr_text.splitlines():
            if line.startswith("cmdline="):
                cmd_val = line[len("cmdline="):]
                cmd_val = re.sub(r"\bmemmap=\S+", "", cmd_val)
                cmd_val = re.sub(r"\bram\.mem_address=\S+", "", cmd_val)
                cmd_val = re.sub(r"\s+", " ", cmd_val).strip()
                new_lines.append(f"cmdline={cmd_val}")
            else:
                new_lines.append(line)
        header_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    subprocess.run(
        [str(magiskboot), "repack", "boot.img", "new-boot.img"],
        cwd=str(unpack_dir),
        check=True,
    )

    repacked = unpack_dir / "new-boot.img"
    if not repacked.is_file():
        raise RuntimeError("magiskboot failed to produce new-boot.img")

    boot_size = repacked.stat().st_size
    size_cap = max(MAX_MIATOLL_BOOT_BYTES, orig_boot.stat().st_size)
    if boot_size > size_cap:
        raise RuntimeError(f"Repacked boot.img exceeds miatoll boot partition ({boot_size} > {size_cap} bytes)")

    shutil.copy2(repacked, verify_dir / "boot.img")
    subprocess.run([str(magiskboot), "unpack", "boot.img"], cwd=str(verify_dir), check=True)
    verified_kernel = (verify_dir / "kernel").read_bytes()
    if b"4.14.370-Rollercoaster-Roll.3.3" not in verified_kernel:
        raise RuntimeError("Round-trip verification failed: Roll 3.3 banner missing in repacked boot.img")

    out_boot_img.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repacked, out_boot_img)

    ksud_src = ak_dir / "ksud"
    if ksud_src.is_file():
        out_ksud.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ksud_src, out_ksud)
        out_ksud.chmod(0o755)

    print(
        f"[boot-patcher] Patched boot.img ({boot_size / (1024 * 1024):.2f} MB) with "
        f"Rollercoaster-Roll.3.3 + extracted ksud ({out_ksud.stat().st_size / 1024:.0f} KB)"
    )


def disable_vbmeta_verification(vbmeta_path: Path) -> bool:
    """Set AVB flags (offset 123) to 0x03 (HASHTREE_DISABLED | VERIFICATION_DISABLED)."""
    if not vbmeta_path.is_file():
        return False
    data = bytearray(vbmeta_path.read_bytes())
    if len(data) < 256 or data[:4] != b"AVB0":
        return False
    old_flag = data[123]
    data[123] = 0x03
    vbmeta_path.write_bytes(bytes(data))
    print(f"[vbmeta] Patched {vbmeta_path.name} AVB flags: 0x{old_flag:02x} -> 0x03")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch ROM boot.img with Rollercoaster-Roll.3.3")
    parser.add_argument("--magiskboot", required=True, type=Path)
    parser.add_argument("--boot-img", required=True, type=Path)
    parser.add_argument("--kernel-zip", required=True, type=Path)
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument("--out-boot", required=True, type=Path)
    parser.add_argument("--out-ksud", required=True, type=Path)
    parser.add_argument("--work-dir", default=Path("/tmp/boot_patch_work"), type=Path)
    args = parser.parse_args()
    patch_boot_image(
        args.magiskboot,
        args.boot_img,
        args.kernel_zip,
        args.expected_sha256,
        args.out_boot,
        args.out_ksud,
        args.work_dir,
    )


if __name__ == "__main__":
    main()
