"""Helpers for safely changing read-only Android dynamic partition fstabs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Set, Tuple

DYNAMIC_PARTITIONS = {
    "system": "/system",
    "system_ext": "/system_ext",
    "product": "/product",
    "vendor": "/vendor",
}

_EXT4_ONLY_MOUNT_OPTIONS = {
    "barrier",
    "commit",
    "data",
    "delalloc",
    "discard",
    "errors",
    "init_itable",
    "inode_readahead_blks",
    "journal_async_commit",
    "journal_checksum",
    "max_batch_time",
    "min_batch_time",
    "nodelalloc",
    "noauto_da_alloc",
    "stripe",
}


def _partition_name_from_source(source: str) -> str:
    value = source.strip().rstrip("/").lower()
    leaf = re.split(r"[/:]", value)[-1]
    leaf = leaf.split("@", 1)[0]
    if leaf.endswith(("_a", "_b")):
        leaf = leaf[:-2]
    return leaf


def _target_partition(source: str, mount_point: str) -> str | None:
    normalized_mount = mount_point.rstrip("/") or "/"
    source_partition = _partition_name_from_source(source)
    for partition, expected_mount in DYNAMIC_PARTITIONS.items():
        if normalized_mount == expected_mount or source_partition == partition:
            return partition
    return None


def normalize_erofs_mount_options(options: str, context: str = "fstab") -> str:
    """Remove ext4-only options, require read-only semantics, and reject rw mounts."""
    kept = []
    for option in options.split(","):
        value = option.strip()
        if not value:
            continue
        key = value.split("=", 1)[0].lower()
        if key in _EXT4_ONLY_MOUNT_OPTIONS:
            continue
        if value.lower() == "rw":
            raise RuntimeError(
                f"Dynamic system partition {context} requests rw; refusing to convert "
                "a writable ext4 mount to read-only EROFS"
            )
        kept.append(value)
    if not any(option.lower() == "ro" for option in kept):
        kept.insert(0, "ro")
    return ",".join(kept) or "ro"


def normalize_dynamic_fsmgr_flags(flags: str, slot_suffixed: bool = False) -> str:
    """Remove AVB flags, and stale slot selection only for an unsuffixed layout."""
    removed_slot_flags = set() if slot_suffixed else {"slotselect", "slotselect_other"}
    kept = [
        flag
        for flag in flags.split(",")
        if flag
        and not flag.startswith("avb")
        and not flag.startswith("verify")
        and flag != "avb_keys=/avb/q-gsi.avbpubkey:/avb/r-gsi.avbpubkey:/avb/s-gsi.avbpubkey"
        and flag not in removed_slot_flags
    ]
    return ",".join(kept) if kept else "defaults"


def strip_avb_verify_fstab_flags(text: str) -> str:
    """Strip AVB/verify tokens from Android fstab fs_mgr flags."""
    output_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output_lines.append(line)
            continue
        columns = line.split()
        if len(columns) >= 5:
            updated_flags = normalize_dynamic_fsmgr_flags(columns[4], slot_suffixed=True)
            if updated_flags != columns[4]:
                columns[4] = updated_flags
                line = "\t".join(columns)
        output_lines.append(line)
    if not output_lines:
        return text
    return "\n".join(output_lines) + ("\n" if text.endswith("\n") else "")


def patch_dynamic_fstab_text(text: str, slot_suffixed: bool = False) -> Tuple[str, Set[str], Set[str]]:
    """Convert read-only dynamic partitions from ext4 to EROFS.

    Returns the patched text, all target partitions found, and the subset actually
    changed from ext4. Any target line using an unexpected filesystem is rejected.
    """
    output_lines = []
    found: Set[str] = set()
    changed: Set[str] = set()

    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output_lines.append(line)
            continue
        columns = line.split()
        if len(columns) < 3:
            output_lines.append(line)
            continue
        partition = _target_partition(columns[0], columns[1])
        if partition is None:
            output_lines.append(line)
            continue
        filesystem = columns[2].lower()
        if filesystem not in ("ext4", "erofs"):
            raise RuntimeError(
                f"Unexpected filesystem '{columns[2]}' for dynamic partition {partition} "
                f"in fstab line {line_number}; expected ext4 or erofs"
            )
        found.add(partition)
        line_changed = False
        if filesystem == "ext4":
            columns[2] = "erofs"
            changed.add(partition)
            line_changed = True

        if len(columns) >= 4:
            mount_options = normalize_erofs_mount_options(columns[3], f"{partition} fstab line {line_number}")
            if mount_options != columns[3]:
                columns[3] = mount_options
                line_changed = True
        else:
            columns.append("ro")
            line_changed = True

        if len(columns) >= 5:
            fs_mgr_flags = normalize_dynamic_fsmgr_flags(columns[4], slot_suffixed=slot_suffixed)
            if fs_mgr_flags != columns[4]:
                columns[4] = fs_mgr_flags
                line_changed = True

        output_lines.append("\t".join(columns) if line_changed else line)

    trailing_newline = text.endswith("\n") or not text
    patched = "\n".join(output_lines)
    if trailing_newline and output_lines:
        patched += "\n"
    return patched, found, changed


def patch_partition_fstabs_to_erofs(
    partition_roots: Dict[str, Path],
    slot_suffixed: bool = False,
) -> Tuple[int, Set[str], Set[str]]:
    """Patch real fstab.* files under extracted partition roots; reject symlink fstabs."""
    patched_files = 0
    found: Set[str] = set()
    changed: Set[str] = set()
    visited: Set[Path] = set()

    for root in partition_roots.values():
        if not root.is_dir():
            continue
        for fstab in sorted(root.rglob("fstab*")):
            if fstab in visited or not fstab.is_file():
                continue
            visited.add(fstab)
            if fstab.is_symlink():
                raise RuntimeError(f"Refusing to patch symlink fstab: {fstab}")
            original = fstab.read_text(encoding="utf-8", errors="ignore")
            patched, file_found, file_changed = patch_dynamic_fstab_text(
                original,
                slot_suffixed=slot_suffixed,
            )
            found.update(file_found)
            changed.update(file_changed)
            if patched != original:
                fstab.write_text(patched, encoding="utf-8")
                patched_files += 1

    return patched_files, found, changed
