#!/usr/bin/env python3
"""Offline boot.img patcher for Rollercoaster-Roll.3.3 + AVB vbmeta disabler.

Verified on miatoll 128 MB boot.img (134,217,728 bytes):
  1. Verifies Rollercoaster-Roll.3.3.zip SHA-256.
  2. Unpacks ROM boot.img with `magiskboot unpack -h`.
  3. Replaces `kernel` with decompressed `Image` from Rollercoaster-Roll.3.3's `Image.gz`.
  4. Preserves ROM's native `dtb` unless it matches 3.0.304.0 SHA-256 (exact Roll 3.3 rule).
  5. Removes stale memory-map and SELinux-disable/permissive tokens; forces enforcing mode.
  6. Repacks & round-trip verifies the kernel banner and enforcing boot cmdline.
  7. Extracts Roll 3.3's `ksud` binary for ROM seeding.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import stat
import struct
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import List, Set, Tuple

from fstab_utils import (
    DYNAMIC_PARTITIONS,
    normalize_dynamic_fsmgr_flags,
    normalize_erofs_mount_options,
    patch_dynamic_fstab_text,
    strip_avb_verify_fstab_flags,
)

DTB_3040_SHA256 = "6ae7105b5ad512e40af1f14e53a8c27010b2d7caf0934d53c58cfcc24cc3f239"
MAX_MIATOLL_BOOT_BYTES = 128 * 1024 * 1024
FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9


def _fdt_string(value: bytes, context: str) -> str:
    try:
        return value.split(b"\0", 1)[0].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Non-UTF-8 string in DTB {context}") from exc


def _fdt_target_partition(node_stack: List[str]) -> str | None:
    for index in range(len(node_stack) - 1, -1, -1):
        node = node_stack[index].split("@", 1)[0].lower()
        if node != "fstab":
            continue
        if index + 1 >= len(node_stack):
            return None
        candidate = node_stack[index + 1].split("@", 1)[0].lower()
        if candidate.endswith(("_a", "_b")):
            candidate = candidate[:-2]
        return candidate if candidate in DYNAMIC_PARTITIONS else None
    return None


def patch_fdt_dynamic_fstabs(
    blob: bytes,
    slot_suffixed: bool = False,
) -> Tuple[bytes, Set[str], Set[str]]:
    """Patch target fstab nodes in one standard FDT blob and rebuild its offsets."""
    if len(blob) < 40:
        raise RuntimeError("DTB is shorter than the FDT header")
    header = list(struct.unpack(">10I", blob[:40]))
    if header[0] != FDT_MAGIC:
        raise RuntimeError("DTB is not a single standard FDT blob")
    total_size, off_struct, off_strings, _off_reserve, version = header[1], header[2], header[3], header[4], header[5]
    size_strings, size_struct = header[8], header[9]
    if version < 17:
        raise RuntimeError(f"Unsupported old FDT version {version}; refusing an unverified DTB rewrite")
    if total_size != len(blob):
        raise RuntimeError(
            f"DTB contains trailing data or a table wrapper (FDT totalsize {total_size}, file size {len(blob)}); "
            "refusing to rewrite it as a single blob"
        )
    if (
        off_struct < 40
        or off_strings < off_struct + size_struct
        or off_strings + size_strings > total_size
    ):
        raise RuntimeError("DTB structure/string block offsets are invalid")

    structure = blob[off_struct : off_struct + size_struct]
    strings = blob[off_strings : off_strings + size_strings]
    trailing = blob[off_strings + size_strings : total_size]
    cursor = 0
    stack: List[str] = []
    target_nodes: Set[str] = set()
    target_types: Set[str] = set()
    changed_types: Set[str] = set()
    rebuilt_structure = bytearray()

    while cursor < len(structure):
        token_start = cursor
        if cursor + 4 > len(structure):
            raise RuntimeError("Truncated DTB structure token")
        token = struct.unpack_from(">I", structure, cursor)[0]
        cursor += 4

        if token == FDT_BEGIN_NODE:
            node_end = structure.find(b"\0", cursor)
            if node_end < 0:
                raise RuntimeError("Unterminated DTB node name")
            try:
                node_name = structure[cursor:node_end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("Non-UTF-8 DTB node name") from exc
            cursor = (node_end + 1 + 3) & ~3
            stack.append(node_name)
            target = _fdt_target_partition(stack)
            if target:
                target_nodes.add(target)
            rebuilt_structure.extend(structure[token_start:cursor])
            continue

        if token == FDT_END_NODE:
            if not stack:
                raise RuntimeError("Unbalanced DTB END_NODE token")
            stack.pop()
            rebuilt_structure.extend(structure[token_start:cursor])
            continue

        if token == FDT_PROP:
            if cursor + 8 > len(structure):
                raise RuntimeError("Truncated DTB property header")
            value_size, name_offset = struct.unpack_from(">II", structure, cursor)
            value_start = cursor + 8
            value_end = value_start + value_size
            value_padded_end = (value_end + 3) & ~3
            if value_padded_end > len(structure) or name_offset >= len(strings):
                raise RuntimeError("DTB property exceeds its structure/string blocks")
            name_end = strings.find(b"\0", name_offset)
            if name_end < 0:
                raise RuntimeError("Unterminated DTB property name")
            try:
                property_name = strings[name_offset:name_end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("Non-UTF-8 DTB property name") from exc
            value = structure[value_start:value_end]
            target = _fdt_target_partition(stack)
            updated_value = value
            if target:
                target_nodes.add(target)
                context = f"fstab node {target}.{property_name}"
                if property_name == "type":
                    target_types.add(target)
                    filesystem = _fdt_string(value, context).lower()
                    if filesystem not in ("ext4", "erofs"):
                        raise RuntimeError(
                            f"Unexpected DTB fstab filesystem {filesystem!r} for {target}; expected ext4 or erofs"
                        )
                    if filesystem == "ext4":
                        updated_value = b"erofs\0" + value[value.find(b"\0") + 1 :]
                        changed_types.add(target)
                elif property_name == "mnt_flags":
                    options = normalize_erofs_mount_options(_fdt_string(value, context), context)
                    updated_value = options.encode("utf-8") + b"\0"
                elif property_name == "fsmgr_flags":
                    flags = normalize_dynamic_fsmgr_flags(
                        _fdt_string(value, context),
                        slot_suffixed=slot_suffixed,
                    )
                    updated_value = flags.encode("utf-8") + b"\0"

            if updated_value == value:
                rebuilt_structure.extend(structure[token_start:value_padded_end])
            else:
                rebuilt_structure.extend(struct.pack(">III", FDT_PROP, len(updated_value), name_offset))
                rebuilt_structure.extend(updated_value)
                rebuilt_structure.extend(b"\0" * ((-len(rebuilt_structure)) % 4))
            cursor = value_padded_end
            continue

        if token == FDT_NOP:
            rebuilt_structure.extend(structure[token_start:cursor])
            continue

        if token == FDT_END:
            rebuilt_structure.extend(structure[token_start:])
            cursor = len(structure)
            break

        raise RuntimeError(f"Unknown DTB structure token 0x{token:08x}")

    missing_types = sorted(target_nodes - target_types)
    if missing_types:
        raise RuntimeError(f"DTB fstab nodes lack a filesystem type for dynamic partitions: {missing_types}")
    if not changed_types and rebuilt_structure == structure:
        return blob, target_types, changed_types

    new_structure = bytes(rebuilt_structure)
    new_off_strings = (off_struct + len(new_structure) + 3) & ~3
    new_total_size = new_off_strings + len(strings) + len(trailing)
    header[1] = new_total_size
    header[3] = new_off_strings
    header[9] = len(new_structure)
    prefix = bytearray(blob[:off_struct])
    prefix[:40] = struct.pack(">10I", *header)
    output = prefix + new_structure
    output.extend(b"\0" * (new_off_strings - len(output)))
    output.extend(strings)
    output.extend(trailing)
    if len(output) != new_total_size:
        raise RuntimeError("Internal DTB rebuild size mismatch")
    return bytes(output), target_types, changed_types


def patch_boot_dtb_fstabs(
    magiskboot: Path,
    dtb_path: Path,
    work_dir: Path,
    slot_suffixed: bool = False,
) -> Tuple[Set[str], Set[str]]:
    """Use MagiskBoot to identify/verify DTB fstabs, then safely patch their types/flags."""
    before = subprocess.run(
        [str(magiskboot), "dtb", str(dtb_path), "print", "-f"],
        cwd=str(work_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    before_output = before.stdout + before.stderr
    if before.returncode != 0:
        raise RuntimeError(f"MagiskBoot could not inspect boot DTB fstab: {before_output.strip()}")
    if "Found fstab" not in before_output:
        return set(), set()

    original = dtb_path.read_bytes()
    patched, mounts, changed = patch_fdt_dynamic_fstabs(original, slot_suffixed=slot_suffixed)
    if mounts:
        dtb_path.write_bytes(patched)
        verified, verified_mounts, verified_changed = patch_fdt_dynamic_fstabs(
            dtb_path.read_bytes(),
            slot_suffixed=slot_suffixed,
        )
        if verified != patched or verified_mounts != mounts or verified_changed:
            raise RuntimeError("Round-trip verification failed for dynamic-partition fstab nodes in DTB")

    after = subprocess.run(
        [str(magiskboot), "dtb", str(dtb_path), "print", "-f"],
        cwd=str(work_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    after_output = after.stdout + after.stderr
    if after.returncode != 0 or (mounts and "Found fstab" not in after_output):
        raise RuntimeError(f"MagiskBoot could not verify patched boot DTB fstab: {after_output.strip()}")
    return mounts, changed


def verify_kernel_erofs_lz4_support(kernel_image: bytes) -> None:
    """Require the pinned kernel binary to contain EROFS and LZ4 decompressor code."""
    required = (b"erofs_fill_super", b"z_erofs_lz4_decompress")
    missing = [token.decode("ascii") for token in required if token not in kernel_image]
    if missing:
        raise RuntimeError(
            "Pinned kernel does not expose the EROFS/LZ4 support required by the "
            f"dynamic-partition conversion; missing kernel symbols: {missing}"
        )


def list_cpio_fstab_entries(cpio_path: Path) -> List[Tuple[str, int]]:
    """Return (archive path, permission bits) for regular fstab files in newc CPIO."""
    data = cpio_path.read_bytes()
    offset = 0
    saw_header = False
    entries: List[Tuple[str, int]] = []

    while offset < len(data):
        while offset < len(data) and data[offset] == 0:
            offset += 1
        if offset >= len(data):
            break
        if offset + 110 > len(data):
            raise RuntimeError(f"Truncated CPIO header in {cpio_path} at offset {offset}")
        magic = data[offset : offset + 6]
        if magic not in (b"070701", b"070702"):
            raise RuntimeError(
                f"Unsupported ramdisk CPIO format in {cpio_path} at offset {offset}: {magic!r}"
            )
        try:
            fields = [int(data[offset + 6 + index * 8 : offset + 14 + index * 8], 16) for index in range(13)]
        except ValueError as exc:
            raise RuntimeError(f"Invalid hexadecimal CPIO header in {cpio_path} at offset {offset}") from exc
        mode = fields[1]
        file_size = fields[6]
        name_size = fields[11]
        if name_size < 1:
            raise RuntimeError(f"Invalid zero-length CPIO name in {cpio_path} at offset {offset}")
        name_start = offset + 110
        name_end = name_start + name_size
        if name_end > len(data) or data[name_end - 1] != 0:
            raise RuntimeError(f"Invalid CPIO pathname in {cpio_path} at offset {offset}")
        try:
            name = data[name_start : name_end - 1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Non-UTF-8 CPIO pathname in {cpio_path} at offset {offset}") from exc
        content_start = (name_end + 3) & ~3
        content_end = content_start + file_size
        if content_end > len(data):
            raise RuntimeError(f"Truncated CPIO payload for {name!r} in {cpio_path}")
        offset = (content_end + 3) & ~3
        saw_header = True

        if name == "TRAILER!!!":
            continue
        normalized_name = name
        while normalized_name.startswith("./"):
            normalized_name = normalized_name[2:]
        path = PurePosixPath(normalized_name)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"Unsafe pathname in ramdisk CPIO: {name!r}")
        if not stat.S_ISREG(mode):
            continue
        basename = path.name
        if basename == "fstab" or basename.startswith("fstab."):
            if any(char.isspace() for char in name):
                raise RuntimeError(f"Whitespace in ramdisk fstab path is unsupported: {name!r}")
            entries.append((name, stat.S_IMODE(mode)))

    if not saw_header:
        raise RuntimeError(f"No newc CPIO entries found in ramdisk archive: {cpio_path}")
    return entries


def _magiskboot_cpio(
    magiskboot: Path,
    cpio_path: Path,
    command: str,
    work_dir: Path,
) -> None:
    result = subprocess.run(
        [str(magiskboot), "cpio", str(cpio_path), command],
        cwd=str(work_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"magiskboot cpio {command.split()[0]} failed: {detail}")


def patch_boot_ramdisk_fstabs(
    magiskboot: Path,
    cpio_path: Path,
    work_dir: Path,
    slot_suffixed: bool = False,
) -> Tuple[Set[str], int]:
    """Convert dynamic-partition fstab rows inside the unpacked boot ramdisk."""
    entries = list_cpio_fstab_entries(cpio_path)
    if not entries:
        return set(), 0

    edit_dir = work_dir / "fstab_edits"
    edit_dir.mkdir(parents=True, exist_ok=True)
    found: Set[str] = set()
    changed_files = 0
    for index, (archive_name, mode) in enumerate(entries):
        extracted = edit_dir / f"fstab_{index:03d}.txt"
        extracted_arg = extracted.relative_to(work_dir).as_posix()
        _magiskboot_cpio(
            magiskboot,
            cpio_path,
            f"extract {archive_name} {extracted_arg}",
            work_dir,
        )
        if not extracted.is_file():
            raise RuntimeError(f"magiskboot did not extract ramdisk fstab {archive_name}")
        original = extracted.read_text(encoding="utf-8")
        avb_stripped = strip_avb_verify_fstab_flags(original)
        patched, file_found, _file_changed = patch_dynamic_fstab_text(
            avb_stripped,
            slot_suffixed=slot_suffixed,
        )
        found.update(file_found)
        if patched == original:
            continue

        extracted.write_text(patched, encoding="utf-8")
        _magiskboot_cpio(
            magiskboot,
            cpio_path,
            f"add {mode:04o} {archive_name} {extracted_arg}",
            work_dir,
        )
        verified = edit_dir / f"fstab_{index:03d}.verified"
        verified_arg = verified.relative_to(work_dir).as_posix()
        _magiskboot_cpio(
            magiskboot,
            cpio_path,
            f"extract {archive_name} {verified_arg}",
            work_dir,
        )
        if not verified.is_file():
            raise RuntimeError(f"magiskboot could not re-extract patched ramdisk fstab {archive_name}")
        verified_text = verified.read_text(encoding="utf-8")
        verified_text, verified_found, verified_changed = patch_dynamic_fstab_text(
            verified_text,
            slot_suffixed=slot_suffixed,
        )
        if (
            verified_text != patched
            or verified_changed
            or strip_avb_verify_fstab_flags(verified_text) != verified_text
        ):
            raise RuntimeError(f"Round-trip verification failed for ramdisk fstab {archive_name}")
        found.update(verified_found)
        changed_files += 1

    return found, changed_files


def patch_boot_environment_fstabs(
    magiskboot: Path,
    image_path: Path,
    work_dir: Path,
    slot_suffixed: bool = False,
) -> Set[str]:
    """Patch and round-trip verify fstabs in non-primary bootable images."""
    shutil.rmtree(work_dir, ignore_errors=True)
    unpack_dir = work_dir / "unpack"
    verify_dir = work_dir / "verify"
    unpack_dir.mkdir(parents=True)
    verify_dir.mkdir(parents=True)
    input_image = unpack_dir / "input.img"
    shutil.copy2(image_path, input_image)

    unpack_result = subprocess.run(
        [str(magiskboot), "unpack", "-h", "input.img"],
        cwd=str(unpack_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if unpack_result.returncode != 0:
        raise RuntimeError(
            f"MagiskBoot could not unpack {image_path.name}: "
            f"{(unpack_result.stdout + unpack_result.stderr).strip()}"
        )

    mounts: Set[str] = set()
    cpio_files = sorted(path for path in unpack_dir.glob("*.cpio") if path.is_file() and path.stat().st_size)
    cpio_changed = 0
    for cpio_path in cpio_files:
        cpio_mounts, changed_count = patch_boot_ramdisk_fstabs(
            magiskboot,
            cpio_path,
            unpack_dir,
            slot_suffixed=slot_suffixed,
        )
        mounts.update(cpio_mounts)
        cpio_changed += changed_count

    dtb_path = unpack_dir / "dtb"
    dtb_before = dtb_path.read_bytes() if dtb_path.is_file() and dtb_path.stat().st_size else b""
    dtb_mounts: Set[str] = set()
    if dtb_before:
        dtb_mounts, _dtb_changed_types = patch_boot_dtb_fstabs(
            magiskboot,
            dtb_path,
            unpack_dir,
            slot_suffixed=slot_suffixed,
        )
        mounts.update(dtb_mounts)

    dtb_changed = bool(dtb_before and dtb_path.read_bytes() != dtb_before)
    if not cpio_changed and not dtb_changed:
        return mounts

    repack_result = subprocess.run(
        [str(magiskboot), "repack", "input.img", "repacked.img"],
        cwd=str(unpack_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    repacked = unpack_dir / "repacked.img"
    if repack_result.returncode != 0 or not repacked.is_file():
        raise RuntimeError(
            f"MagiskBoot could not repack {image_path.name}: "
            f"{(repack_result.stdout + repack_result.stderr).strip()}"
        )
    if repacked.stat().st_size > image_path.stat().st_size:
        raise RuntimeError(
            f"Patched {image_path.name} exceeds its source image size; refusing to risk partition overflow "
            f"({repacked.stat().st_size} > {image_path.stat().st_size} bytes)"
        )

    verify_result = subprocess.run(
        [str(magiskboot), "unpack", "-h", str(repacked)],
        cwd=str(verify_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if verify_result.returncode != 0:
        raise RuntimeError(
            f"Round-trip verification could not unpack {image_path.name}: "
            f"{(verify_result.stdout + verify_result.stderr).strip()}"
        )

    verified_mounts: Set[str] = set()
    for cpio_name in (path.name for path in cpio_files):
        verified_cpio = verify_dir / cpio_name
        if not verified_cpio.is_file() or verified_cpio.stat().st_size == 0:
            raise RuntimeError(f"Round-trip verification lost {cpio_name} from {image_path.name}")
        cpio_mounts, changed_count = patch_boot_ramdisk_fstabs(
            magiskboot,
            verified_cpio,
            verify_dir,
            slot_suffixed=slot_suffixed,
        )
        if changed_count:
            raise RuntimeError(f"Round-trip verification found an unpatched fstab in {image_path.name}")
        verified_mounts.update(cpio_mounts)

    verified_dtb = verify_dir / "dtb"
    if dtb_mounts:
        if not verified_dtb.is_file():
            raise RuntimeError(f"Round-trip verification lost DTB fstab from {image_path.name}")
        verified_blob, verified_dtb_mounts, verified_changes = patch_fdt_dynamic_fstabs(
            verified_dtb.read_bytes(),
            slot_suffixed=slot_suffixed,
        )
        if verified_blob != verified_dtb.read_bytes() or verified_dtb_mounts != dtb_mounts or verified_changes:
            raise RuntimeError(f"Round-trip verification found an unpatched DTB fstab in {image_path.name}")
        verified_mounts.update(verified_dtb_mounts)
    if verified_mounts != mounts:
        raise RuntimeError(f"Round-trip fstab coverage mismatch for {image_path.name}")

    shutil.copy2(repacked, image_path)
    print(f"[boot-patcher] Patched and verified {image_path.name} fstab mounts: {sorted(mounts)}")
    return mounts


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_boot_cmdline(cmdline: str, force_enforcing: bool = True) -> str:
    """Remove legacy disable/permissive boot arguments and optionally force enforcing mode."""
    tokens = [
        token
        for token in cmdline.split()
        if not token.startswith(("memmap=", "ram.mem_address=", "androidboot.selinux=", "selinux="))
        and token != "enforcing=0"
    ]
    if force_enforcing:
        tokens.append("androidboot.selinux=enforcing")
    return " ".join(tokens)


def patch_boot_image(
    magiskboot: Path,
    rom_boot_img: Path,
    kernel_zip: Path,
    expected_kernel_sha256: str,
    out_boot_img: Path,
    out_ksud: Path,
    work_dir: Path,
    convert_dynamic_fstabs: bool = False,
    slot_suffixed: bool = False,
) -> Set[str]:
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
    verify_kernel_erofs_lz4_support(raw_image)
    if b"add_type: alloc type_datum failed." not in raw_image:
        raise RuntimeError(
            "Pinned kernel lacks KernelSU live SELinux type creation required by the Dolby DMS policy"
        )

    magiskboot = magiskboot.resolve()
    magiskboot.chmod(0o755)

    orig_boot = unpack_dir / "boot.img"
    shutil.copy2(rom_boot_img, orig_boot)
    if orig_boot.stat().st_size > MAX_MIATOLL_BOOT_BYTES:
        raise RuntimeError(
            f"Input boot.img exceeds the 128 MiB joyeuse boot partition capacity "
            f"({orig_boot.stat().st_size} > {MAX_MIATOLL_BOOT_BYTES} bytes)"
        )

    res = subprocess.run(
        [str(magiskboot), "unpack", "-h", "boot.img"],
        cwd=str(unpack_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    print(f"[boot-patcher] magiskboot unpack:\n{res.stdout}{res.stderr}")

    boot_fstab_mounts: Set[str] = set()
    ramdisk_fstab_mounts: Set[str] = set()
    dtb_fstab_mounts: Set[str] = set()
    if convert_dynamic_fstabs:
        ramdisk_cpio = unpack_dir / "ramdisk.cpio"
        if not ramdisk_cpio.is_file() or ramdisk_cpio.stat().st_size == 0:
            raise RuntimeError("Cannot convert dynamic partitions: boot ramdisk CPIO is missing or empty")
        ramdisk_fstab_mounts, fstab_files_changed = patch_boot_ramdisk_fstabs(
            magiskboot,
            ramdisk_cpio,
            unpack_dir,
            slot_suffixed=slot_suffixed,
        )
        boot_fstab_mounts.update(ramdisk_fstab_mounts)
        print(
            f"[boot-patcher] Converted {fstab_files_changed} boot ramdisk fstab file(s) to EROFS; "
            f"found mounts: {sorted(ramdisk_fstab_mounts)}"
        )

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

    if convert_dynamic_fstabs and rom_dtb.is_file() and rom_dtb.stat().st_size > 0:
        dtb_fstab_mounts, dtb_fstab_types_changed = patch_boot_dtb_fstabs(
            magiskboot,
            rom_dtb,
            unpack_dir,
            slot_suffixed=slot_suffixed,
        )
        boot_fstab_mounts.update(dtb_fstab_mounts)
        print(
            f"[boot-patcher] DTB fstab mounts verified: {sorted(dtb_fstab_mounts)}; "
            f"filesystem types changed from ext4={sorted(dtb_fstab_types_changed)}"
        )

    header_file = unpack_dir / "header"
    if not header_file.is_file():
        raise RuntimeError("magiskboot did not expose the boot header needed to enforce SELinux mode")
    hdr_text = header_file.read_text(encoding="utf-8", errors="ignore")
    new_lines = []
    cmdline_seen = False
    for line in hdr_text.splitlines():
        field = next((key for key in ("cmdline=", "extra_cmdline=") if line.startswith(key)), None)
        if field is not None:
            cmd_val = sanitize_boot_cmdline(
                line[len(field) :],
                force_enforcing=not cmdline_seen,
            )
            new_lines.append(f"{field}{cmd_val}")
            cmdline_seen = True
        else:
            new_lines.append(line)
    if not cmdline_seen:
        raise RuntimeError("Boot header has no cmdline or extra_cmdline field to sanitize")
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
    if boot_size > MAX_MIATOLL_BOOT_BYTES:
        raise RuntimeError(
            f"Repacked boot.img exceeds the 128 MiB joyeuse boot partition "
            f"({boot_size} > {MAX_MIATOLL_BOOT_BYTES} bytes)"
        )

    shutil.copy2(repacked, verify_dir / "boot.img")
    subprocess.run([str(magiskboot), "unpack", "-h", "boot.img"], cwd=str(verify_dir), check=True)
    verified_kernel = (verify_dir / "kernel").read_bytes()
    if b"4.14.370-Rollercoaster-Roll.3.3" not in verified_kernel:
        raise RuntimeError("Round-trip verification failed: Roll 3.3 banner missing in repacked boot.img")
    verified_header_file = verify_dir / "header"
    if not verified_header_file.is_file():
        raise RuntimeError("Round-trip verification failed: repacked boot header is unavailable")
    verified_header = verified_header_file.read_text(encoding="utf-8", errors="ignore")
    verified_cmdline = " ".join(
        line.split("=", 1)[1]
        for line in verified_header.splitlines()
        if line.startswith(("cmdline=", "extra_cmdline="))
    ).split()
    if (
        verified_cmdline.count("androidboot.selinux=enforcing") != 1
        or any(token.startswith("androidboot.selinux=") and token != "androidboot.selinux=enforcing" for token in verified_cmdline)
        or any(token.startswith("selinux=") or token == "enforcing=0" for token in verified_cmdline)
    ):
        raise RuntimeError("Round-trip verification failed: boot cmdline is not explicitly SELinux enforcing")
    if convert_dynamic_fstabs:
        verified_cpio = verify_dir / "ramdisk.cpio"
        if not verified_cpio.is_file() or verified_cpio.stat().st_size == 0:
            raise RuntimeError("Round-trip verification failed: repacked boot ramdisk CPIO is missing")
        verified_ramdisk_mounts, verified_ramdisk_changes = patch_boot_ramdisk_fstabs(
            magiskboot,
            verified_cpio,
            verify_dir,
            slot_suffixed=slot_suffixed,
        )
        if verified_ramdisk_mounts != ramdisk_fstab_mounts or verified_ramdisk_changes:
            raise RuntimeError("Round-trip verification failed: repacked boot ramdisk fstabs are not EROFS-clean")
        verified_dtb_path = verify_dir / "dtb"
        if dtb_fstab_mounts:
            if not verified_dtb_path.is_file():
                raise RuntimeError("Round-trip verification failed: repacked DTB is missing")
            verified_dtb, verified_dtb_mounts, verified_dtb_changes = patch_fdt_dynamic_fstabs(
                verified_dtb_path.read_bytes(),
                slot_suffixed=slot_suffixed,
            )
            if (
                verified_dtb != verified_dtb_path.read_bytes()
                or verified_dtb_mounts != dtb_fstab_mounts
                or verified_dtb_changes
            ):
                raise RuntimeError("Round-trip verification failed: repacked DTB fstab is not EROFS-clean")
    print(
        "[boot-patcher] Verified repacked boot header: SELinux enforcing; "
        "permissive/disable cmdline tokens absent"
    )

    out_boot_img.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repacked, out_boot_img)

    ksud_src = ak_dir / "ksud"
    if not ksud_src.is_file() or ksud_src.stat().st_size == 0:
        raise RuntimeError("ksud binary is missing from the pinned Rollercoaster kernel ZIP")
    ksud_bytes = ksud_src.read_bytes()
    if b"Failed to parse policy statement:" not in ksud_bytes or b"apply sepolicy batch partially succeeded" not in ksud_bytes:
        raise RuntimeError("Pinned ksud lacks the KernelSU sepolicy parser needed by the Dolby DMS policy")
    out_ksud.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ksud_src, out_ksud)
    out_ksud.chmod(out_ksud.stat().st_mode | 0o111)

    print(
        f"[boot-patcher] Patched boot.img ({boot_size / (1024 * 1024):.2f} MB) with "
        f"Rollercoaster-Roll.3.3 + extracted ksud ({out_ksud.stat().st_size / 1024:.0f} KB)"
    )
    return boot_fstab_mounts


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
