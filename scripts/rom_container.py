#!/usr/bin/env python3
"""Universal HyperOS / Android 17 ROM Container Engine for miatoll / joyeuse.

Supports ALL Xiaomi & miatoll custom/port ROM packaging formats automatically:
  1. Recovery dynamic `super.img` / `super.img.zst` / Brotli `super.new.dat.br`
  2. Recovery split `.new.dat.br` / `.transfer.list` per partition
  3. Fastboot/hybrid `images/*.img` / `images/*.img.zst`
  4. OTA `payload.bin`

Key guarantee for Rollercoaster-Roll.3.3 (`4.14.370`):
  - Kernel Roll 3.3 supports `CONFIG_EROFS_FS_ZIP=y` (LZ4 / LZ4HC) and `CONFIG_EXT4_FS=y`,
    while `CONFIG_EROFS_FS_ZIP_LZMA` is NOT set.
  - Therefore, EVERY modified orLZMA-compressed EROFS partition is repacked cleanly with
    `mkfs.erofs -zlz4hc,9` (high-compression LZ4HC, 100% kernel-compatible, zero bootloops!).
  - Extracted EROFS trees have read-only 0444/0555 modes normalized via `make_tree_writable`
    so downstream modding & cleanup never fail with PermissionError.
  - `SuperGeometry` keeps dynamic groups within the device cap, compacting ext4 images and
    only expanding a single non-A/B group into otherwise unused super space when necessary.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SPARSE_MAGIC = 0xED26FF3A
EROFS_MAGIC = 0xE0F5E1E2
EXT4_MAGIC = 0xEF53

TARGET_PARTITIONS = ("system", "system_ext", "product", "vendor", "odm", "mi_ext")
MAX_SUPER_DEVICE_SIZE_BYTES = 9126805504  # joyeuse: 8.5 GiB logical super
SUPER_METADATA_RESERVE_BYTES = 4 * 1024 * 1024


@dataclass
class SuperGeometry:
    dev_size: int = 9126805504  # Standard miatoll super partition size (8.5 GiB = 9,126,805,504 B)
    reported_dev_size: int = 0
    metadata_size: int = 65536
    metadata_slots: int = 2
    is_ab: bool = False
    slot_suffix: str = ""
    groups: Dict[str, int] = field(default_factory=lambda: {"qti_dynamic_partitions": 9122611200})
    part_to_group: Dict[str, str] = field(default_factory=dict)
    was_sparse: bool = False
    was_zstd: bool = False
    was_brotli_dat: bool = False
    rel_path_in_rom: str = "super.img"


@dataclass
class PartitionRecord:
    base_name: str
    raw_name: str
    raw_img_path: Path
    fs_type: str
    extract_dir: Path
    fs_config_path: Path
    file_contexts_path: Path
    source_format: str
    rel_prefix_in_rom: str = ""
    patch_dat_bytes: bytes = b""


@dataclass
class RomWorkspace:
    rom_extract_dir: Path
    work_dir: Path
    container_type: str
    boot_img_path: Path
    vbmeta_paths: List[Path]
    partitions: Dict[str, PartitionRecord]
    super_geom: Optional[SuperGeometry] = None
    unmodified_super_imgs: Dict[str, Tuple[Path, str]] = field(default_factory=dict)


def run_cmd(cmd: List[str], cwd: Optional[Path] = None, env_extra: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    print(f"[cmd] {' '.join(cmd)}")
    env = None
    if env_extra:
        env = os.environ.copy()
        env.update(env_extra)
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=True)


def find_host_tool(name: str) -> Optional[Path]:
    """Locate a host utility even when /usr/sbin is missing from PATH."""
    found = shutil.which(name)
    if found:
        return Path(found)
    for directory in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def compact_ext4_image(
    image_path: Path,
    resize2fs_bin: Optional[Path] = None,
    e2fsck_bin: Optional[Path] = None,
) -> Path:
    """Shrink a freshly built read-only ext4 partition to its minimum valid size."""
    resize2fs = resize2fs_bin or find_host_tool("resize2fs")
    e2fsck = e2fsck_bin or find_host_tool("e2fsck")
    if resize2fs is None or e2fsck is None:
        missing = [name for name, path in (("resize2fs", resize2fs), ("e2fsck", e2fsck)) if path is None]
        raise RuntimeError(f"Required e2fsprogs utilities are unavailable for safe ext4 compaction: {missing}")
    if not image_path.is_file() or image_path.stat().st_size == 0:
        raise RuntimeError(f"Cannot compact missing or empty ext4 image: {image_path}")

    original_size = image_path.stat().st_size
    run_cmd([str(resize2fs), "-M", str(image_path)])
    compacted_size = image_path.stat().st_size
    if compacted_size <= 0 or compacted_size > original_size or compacted_size % 4096:
        raise RuntimeError(
            f"resize2fs produced an invalid compacted image size for {image_path.name}: "
            f"{compacted_size} bytes (original {original_size})"
        )
    run_cmd([str(e2fsck), "-f", "-n", str(image_path)])
    print(
        f"[repack-fs] Compacted {image_path.name}: "
        f"{original_size / (1024**2):.1f} -> {compacted_size / (1024**2):.1f} MiB"
    )
    return image_path


def make_tree_writable(root: Path) -> None:
    """Ensure owner write/exec bits are set on extracted EROFS read-only (0444/0555) trees."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        try:
            if not dp.is_symlink():
                st = dp.stat().st_mode
                if (st & 0o700) != 0o700:
                    os.chmod(dp, st | 0o700)
        except OSError:
            pass
        for fn in filenames:
            fp = dp / fn
            try:
                if not fp.is_symlink():
                    st = fp.stat().st_mode
                    if not (st & 0o200):
                        os.chmod(fp, st | 0o200)
            except OSError:
                pass


def is_sparse_image(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 28:
        return False
    with path.open("rb") as f:
        magic = struct.unpack("<I", f.read(4))[0]
    return magic == SPARSE_MAGIC


def detect_fs_type(raw_img: Path) -> str:
    if not raw_img.is_file() or raw_img.stat().st_size < 2048:
        return "raw"
    with raw_img.open("rb") as f:
        f.seek(1024)
        sb = f.read(1024)
    erofs_magic = struct.unpack_from("<I", sb, 0)[0]
    if erofs_magic == EROFS_MAGIC:
        return "erofs"
    ext4_magic = struct.unpack_from("<H", sb, 0x38)[0]
    if ext4_magic == EXT4_MAGIC:
        return "ext4"
    return "raw"


def erofs_uses_non_lz4_compression(raw_img: Path) -> bool:
    if detect_fs_type(raw_img) != "erofs":
        return False
    with raw_img.open("rb") as f:
        f.seek(1024)
        sb = f.read(128)
    compr_algs = struct.unpack_from("<H", sb, 0x42)[0]
    return (compr_algs & ~0x01) != 0


def sdat2img(transfer_list: Path, new_dat: Path, out_raw_img: Path) -> Path:
    lines = [ln.strip() for ln in transfer_list.read_text(encoding="utf-8").splitlines() if ln.strip()]
    version = int(lines[0])
    total_blocks = int(lines[1])
    idx = 2
    if version >= 2:
        idx = 4

    BLOCK_SIZE = 4096
    commands: List[Tuple[str, List[Tuple[int, int]]]] = []
    max_block = total_blocks

    for line in lines[idx:]:
        parts = line.split()
        cmd = parts[0]
        if cmd in ("new", "erase", "zero") and len(parts) >= 2:
            nums = [int(x) for x in parts[1].split(",")]
            cnt = nums[0]
            pairs = [(nums[i], nums[i + 1]) for i in range(1, cnt + 1, 2)]
            for _, end_b in pairs:
                if end_b > max_block:
                    max_block = end_b
            commands.append((cmd, pairs))

    out_raw_img.parent.mkdir(parents=True, exist_ok=True)
    with new_dat.open("rb") as f_dat, out_raw_img.open("wb") as f_out:
        f_out.truncate(max_block * BLOCK_SIZE)
        for cmd, pairs in commands:
            if cmd == "new":
                for begin_b, end_b in pairs:
                    byte_len = (end_b - begin_b) * BLOCK_SIZE
                    f_out.seek(begin_b * BLOCK_SIZE)
                    remaining = byte_len
                    while remaining > 0:
                        chunk = f_dat.read(min(remaining, 4 * 1024 * 1024))
                        if not chunk:
                            raise RuntimeError(f"Unexpected EOF in {new_dat}")
                        f_out.write(chunk)
                        remaining -= len(chunk)
    return out_raw_img


def sparse_img_to_sdat(sparse_img: Path, out_prefix: Path) -> Tuple[Path, Path, Path]:
    with sparse_img.open("rb") as f:
        hdr = f.read(28)
        (
            magic,
            major_v,
            _minor_v,
            file_hdr_sz,
            chunk_hdr_sz,
            blk_sz,
            total_blks,
            total_chunks,
            _crc,
        ) = struct.unpack("<IHHHHIIII", hdr)
        if magic != SPARSE_MAGIC or major_v != 1 or blk_sz != 4096:
            raise RuntimeError(f"Invalid 4096-block Android sparse image: {sparse_img}")
        if file_hdr_sz > 28:
            f.seek(file_hdr_sz - 28, os.SEEK_CUR)

        transfer_list = out_prefix.parent / f"{out_prefix.name}.transfer.list"
        new_dat = out_prefix.parent / f"{out_prefix.name}.new.dat"
        patch_dat = out_prefix.parent / f"{out_prefix.name}.patch.dat"

        new_ranges: List[Tuple[int, int]] = []
        cur_blk = 0

        with new_dat.open("wb") as f_dat:
            for _ in range(total_chunks):
                chdr = f.read(12)
                chunk_type, _reserved, chunk_sz, total_sz = struct.unpack("<HHII", chdr)
                if chunk_hdr_sz > 12:
                    f.seek(chunk_hdr_sz - 12, os.SEEK_CUR)
                data_sz = total_sz - chunk_hdr_sz

                if chunk_type == 0xCAC1:
                    if new_ranges and new_ranges[-1][1] == cur_blk:
                        new_ranges[-1] = (new_ranges[-1][0], cur_blk + chunk_sz)
                    else:
                        new_ranges.append((cur_blk, cur_blk + chunk_sz))
                    rem = data_sz
                    while rem > 0:
                        buf = f.read(min(rem, 4 * 1024 * 1024))
                        f_dat.write(buf)
                        rem -= len(buf)
                    cur_blk += chunk_sz
                elif chunk_type == 0xCAC2:
                    fill_word = f.read(4)
                    if data_sz > 4:
                        f.seek(data_sz - 4, os.SEEK_CUR)
                    if fill_word == b"\x00\x00\x00\x00":
                        cur_blk += chunk_sz
                    else:
                        if new_ranges and new_ranges[-1][1] == cur_blk:
                            new_ranges[-1] = (new_ranges[-1][0], cur_blk + chunk_sz)
                        else:
                            new_ranges.append((cur_blk, cur_blk + chunk_sz))
                        block_buf = fill_word * (blk_sz // 4)
                        for _ in range(chunk_sz):
                            f_dat.write(block_buf)
                        cur_blk += chunk_sz
                elif chunk_type == 0xCAC3:
                    if data_sz > 0:
                        f.seek(data_sz, os.SEEK_CUR)
                    cur_blk += chunk_sz
                elif chunk_type == 0xCAC4:
                    f.seek(data_sz, os.SEEK_CUR)
                else:
                    raise RuntimeError(f"Unknown sparse chunk 0x{chunk_type:04X}")

        total_written_blocks = sum(e - s for s, e in new_ranges)
        t_lines = ["4", str(total_Written := total_written_blocks), "0", "0", f"erase 2,0,{total_blks}"]
        del total_Written
        for i in range(0, len(new_ranges), 60):
            batch = new_ranges[i : i + 60]
            nums = [str(len(batch) * 2)]
            for s, e in batch:
                nums.append(str(s))
                nums.append(str(e))
            t_lines.append(f"new {','.join(nums)}")
        transfer_list.write_text("\n".join(t_lines) + "\n", encoding="utf-8")
        patch_dat.write_bytes(b"")
    return transfer_list, new_dat, patch_dat


# AOSP system/core/fs_mgr/liblp metadata_format.h + reader.cpp layout (LP metadata v10).
# Validate checksums and table bounds in-process instead of depending on a bundled lpdump binary.
LP_METADATA_GEOMETRY_MAGIC = 0x616C4467
LP_METADATA_HEADER_MAGIC = 0x414C5030
LP_METADATA_GEOMETRY_SIZE = 4096
LP_PARTITION_RESERVED_BYTES = 4096
LP_SECTOR_SIZE = 512
LP_METADATA_MAJOR_VERSION = 10
LP_METADATA_MINOR_VERSION_MAX = 2
LP_PARTITION_ATTR_SLOT_SUFFIXED = 1 << 1
LP_GROUP_SLOT_SUFFIXED = 1 << 0


class _SuperImageReader:
    """Read raw or Android-sparse super images without expanding sparse files."""

    _SPARSE_MAGIC = 0xED26FF3A
    _CHUNK_RAW = 0xCAC1
    _CHUNK_FILL = 0xCAC2
    _CHUNK_DONT_CARE = 0xCAC3
    _CHUNK_CRC32 = 0xCAC4

    def __init__(self, path: Path):
        if not path.is_file():
            raise RuntimeError(f"Super image does not exist: {path}")
        self.path = path
        self._file = path.open("rb")
        self.file_size = path.stat().st_size
        self._chunks: List[Tuple[int, int, int, int]] = []
        self._block_size = 0
        self.logical_size = self.file_size
        try:
            magic = self._read_file(0, 4)
            if len(magic) == 4 and struct.unpack("<I", magic)[0] == self._SPARSE_MAGIC:
                self._read_sparse_index()
        except Exception:
            self._file.close()
            raise

    def close(self) -> None:
        self._file.close()

    def _read_file(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.file_size:
            raise RuntimeError(f"Read outside image bounds at {offset}+{size}")
        self._file.seek(offset)
        data = self._file.read(size)
        if len(data) != size:
            raise RuntimeError(f"Short read from super image at {offset}: {len(data)} of {size} bytes")
        return data

    def _read_sparse_index(self) -> None:
        if self.file_size < 28:
            raise RuntimeError("Truncated Android sparse image header")
        header = self._read_file(0, 28)
        magic, major, _minor, file_header_size, chunk_header_size, block_size, total_blocks, total_chunks, _checksum = struct.unpack(
            "<IHHHHIIII", header
        )
        if magic != self._SPARSE_MAGIC or major != 1:
            raise RuntimeError("Unsupported Android sparse image format")
        if file_header_size < 28 or chunk_header_size < 12 or block_size < LP_SECTOR_SIZE or block_size % 4:
            raise RuntimeError("Invalid Android sparse image header sizes")
        if total_blocks <= 0 or total_chunks <= 0:
            raise RuntimeError("Android sparse image has no data blocks")

        self._block_size = block_size
        self.logical_size = block_size * total_blocks
        cursor = file_header_size
        logical_offset = 0
        for _ in range(total_chunks):
            if cursor + chunk_header_size > self.file_size:
                raise RuntimeError("Truncated Android sparse image chunk header")
            chunk_header = self._read_file(cursor, chunk_header_size)
            chunk_type, _reserved, chunk_blocks, total_size = struct.unpack_from("<HHII", chunk_header)
            if total_size < chunk_header_size or cursor + total_size > self.file_size:
                raise RuntimeError("Invalid Android sparse image chunk size")
            data_offset = cursor + chunk_header_size
            data_size = total_size - chunk_header_size
            logical_length = chunk_blocks * block_size

            if chunk_type == self._CHUNK_RAW:
                if chunk_blocks == 0 or data_size != logical_length:
                    raise RuntimeError("Invalid RAW chunk in Android sparse image")
            elif chunk_type == self._CHUNK_FILL:
                if chunk_blocks == 0 or data_size != 4:
                    raise RuntimeError("Invalid FILL chunk in Android sparse image")
            elif chunk_type == self._CHUNK_DONT_CARE:
                if chunk_blocks == 0 or data_size != 0:
                    raise RuntimeError("Invalid DONT_CARE chunk in Android sparse image")
            elif chunk_type == self._CHUNK_CRC32:
                if chunk_blocks != 0 or data_size != 4:
                    raise RuntimeError("Invalid CRC32 chunk in Android sparse image")
                logical_length = 0
            else:
                raise RuntimeError(f"Unknown Android sparse image chunk type: 0x{chunk_type:04x}")

            if logical_length:
                self._chunks.append((chunk_type, logical_offset, logical_length, data_offset))
                logical_offset += logical_length
            cursor += total_size

        if logical_offset != self.logical_size:
            raise RuntimeError(
                "Android sparse image block count mismatch: "
                f"chunks describe {logical_offset} bytes, header describes {self.logical_size}"
            )

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.logical_size:
            raise RuntimeError(f"Super metadata read outside logical image bounds at {offset}+{size}")
        if size == 0:
            return b""
        if not self._chunks:
            return self._read_file(offset, size)

        result = bytearray(size)
        end = offset + size
        covered = 0
        for chunk_type, chunk_start, chunk_size, data_offset in self._chunks:
            chunk_end = chunk_start + chunk_size
            overlap_start = max(offset, chunk_start)
            overlap_end = min(end, chunk_end)
            if overlap_start >= overlap_end:
                continue
            copy_size = overlap_end - overlap_start
            result_offset = overlap_start - offset
            chunk_offset = overlap_start - chunk_start
            if chunk_type == self._CHUNK_RAW:
                result[result_offset : result_offset + copy_size] = self._read_file(
                    data_offset + chunk_offset, copy_size
                )
            elif chunk_type == self._CHUNK_FILL:
                pattern = self._read_file(data_offset, 4)
                pattern_offset = chunk_offset % 4
                repeated = pattern * ((pattern_offset + copy_size + 3) // 4)
                result[result_offset : result_offset + copy_size] = repeated[pattern_offset : pattern_offset + copy_size]
            # DONT_CARE chunks represent zero-filled logical blocks.
            covered += copy_size
            if overlap_end == end:
                break
        if covered != size:
            raise RuntimeError(f"Sparse super image has an uncovered metadata range at {offset}+{size}")
        return bytes(result)


def _decode_lp_name(raw: bytes, label: str) -> str:
    name_bytes = raw.split(b"\0", 1)[0]
    try:
        name = name_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"LP metadata contains a non-ASCII {label} name") from exc
    if not name:
        raise RuntimeError(f"LP metadata contains an empty {label} name")
    return name


def _read_lp_geometry(reader: _SuperImageReader) -> Tuple[int, int, int]:
    errors: List[str] = []
    for offset in (LP_PARTITION_RESERVED_BYTES, LP_PARTITION_RESERVED_BYTES + LP_METADATA_GEOMETRY_SIZE):
        try:
            raw = reader.read(offset, 52)
            magic, struct_size = struct.unpack_from("<II", raw, 0)
            if magic != LP_METADATA_GEOMETRY_MAGIC:
                raise RuntimeError("invalid geometry magic")
            if struct_size != 52:
                raise RuntimeError(f"unsupported geometry structure size {struct_size}")
            expected_checksum = raw[8:40]
            normalized = bytearray(raw[:struct_size])
            normalized[8:40] = b"\0" * 32
            if hashlib.sha256(normalized).digest() != expected_checksum:
                raise RuntimeError("geometry SHA-256 checksum mismatch")
            metadata_size, metadata_slots, logical_block_size = struct.unpack_from("<III", raw, 40)
            if metadata_size <= 0 or metadata_size % LP_SECTOR_SIZE:
                raise RuntimeError("metadata maximum size is zero or not sector-aligned")
            if metadata_slots <= 0:
                raise RuntimeError("metadata slot count is zero")
            if logical_block_size < LP_SECTOR_SIZE or logical_block_size % LP_SECTOR_SIZE:
                raise RuntimeError("logical block size is invalid")
            metadata_end = LP_PARTITION_RESERVED_BYTES + 2 * (
                LP_METADATA_GEOMETRY_SIZE + metadata_size * metadata_slots
            )
            if metadata_end > reader.logical_size:
                raise RuntimeError("metadata geometry exceeds the super image bounds")
            return metadata_size, metadata_slots, logical_block_size
        except RuntimeError as exc:
            errors.append(f"offset {offset}: {exc}")
    raise RuntimeError("Invalid LP geometry in primary and backup copies: " + "; ".join(errors))


def _parse_lp_metadata_copy(
    reader: _SuperImageReader,
    metadata_size: int,
    metadata_slots: int,
    metadata_offset: int,
) -> Tuple[int, int, Dict[str, int], Dict[str, str], bool]:
    header_prefix = reader.read(metadata_offset, 128)
    magic, major_version, minor_version, header_size = struct.unpack_from("<IHHI", header_prefix, 0)
    if magic != LP_METADATA_HEADER_MAGIC:
        raise RuntimeError("invalid metadata header magic")
    if major_version != LP_METADATA_MAJOR_VERSION or minor_version > LP_METADATA_MINOR_VERSION_MAX:
        raise RuntimeError(f"unsupported LP metadata version {major_version}.{minor_version}")
    allowed_header_sizes = {128} if minor_version < 2 else {132, 256}
    if header_size not in allowed_header_sizes:
        raise RuntimeError(f"invalid metadata header size {header_size} for version 1.{minor_version}")
    header = reader.read(metadata_offset, header_size)
    expected_header_checksum = header[12:44]
    normalized_header = bytearray(header)
    normalized_header[12:44] = b"\0" * 32
    if hashlib.sha256(normalized_header).digest() != expected_header_checksum:
        raise RuntimeError("metadata header SHA-256 checksum mismatch")
    header_flags = struct.unpack_from("<I", header, 128)[0] if header_size >= 132 else 0
    has_slot_specific_layout = bool(header_flags & 0x1)  # LP_HEADER_FLAG_VIRTUAL_AB_DEVICE

    tables_size = struct.unpack_from("<I", header, 44)[0]
    expected_tables_checksum = header[48:80]
    if tables_size <= 0 or header_size + tables_size > metadata_size:
        raise RuntimeError("metadata tables exceed the configured metadata area")
    tables = reader.read(metadata_offset + header_size, tables_size)
    if hashlib.sha256(tables).digest() != expected_tables_checksum:
        raise RuntimeError("metadata tables SHA-256 checksum mismatch")

    table_specs = (
        ("partitions", 80, 52),
        ("extents", 92, 24),
        ("groups", 104, 48),
        ("block_devices", 116, 64),
    )
    descriptors: Dict[str, Tuple[int, int, int]] = {}
    ranges: List[Tuple[int, int, str]] = []
    for name, descriptor_offset, expected_entry_size in table_specs:
        table_offset, count, entry_size = struct.unpack_from("<III", header, descriptor_offset)
        if entry_size != expected_entry_size:
            raise RuntimeError(f"invalid {name} entry size {entry_size}; expected {expected_entry_size}")
        table_end = table_offset + count * entry_size
        if table_offset > tables_size or table_end > tables_size:
            raise RuntimeError(f"{name} table is outside the checksummed metadata area")
        descriptors[name] = (table_offset, count, entry_size)
        if count:
            ranges.append((table_offset, table_end, name))
    ranges.sort()
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[1]:
            raise RuntimeError(f"LP metadata tables overlap: {previous[2]} and {current[2]}")

    _, partition_count, _ = descriptors["partitions"]
    _, extent_count, _ = descriptors["extents"]
    _, group_count, _ = descriptors["groups"]
    _, block_device_count, _ = descriptors["block_devices"]
    if partition_count == 0 or group_count == 0 or block_device_count == 0:
        raise RuntimeError("LP metadata is missing partitions, groups, or block devices")

    group_offset, _, _ = descriptors["groups"]
    groups: List[Tuple[str, int, int]] = []
    group_names: Set[str] = set()
    for index in range(group_count):
        entry = tables[group_offset + index * 48 : group_offset + (index + 1) * 48]
        raw_name, flags, maximum_size = struct.unpack("<36sIQ", entry)
        name = _decode_lp_name(raw_name, "group")
        if flags & LP_GROUP_SLOT_SUFFIXED:
            name += "_a"  # AOSP liblp reads metadata slot zero by default.
            has_slot_specific_layout = True
        if name in group_names:
            raise RuntimeError(f"LP metadata contains duplicate group name: {name}")
        group_names.add(name)
        groups.append((name, flags, maximum_size))

    extent_offset, _, _ = descriptors["extents"]
    for index in range(extent_count):
        entry = tables[extent_offset + index * 24 : extent_offset + (index + 1) * 24]
        _num_sectors, target_type, _target_data, target_source = struct.unpack("<QIQI", entry)
        if target_type not in (0, 1):
            raise RuntimeError(f"LP metadata extent {index} has unknown target type {target_type}")
        if target_type == 0 and target_source >= block_device_count:
            raise RuntimeError(f"LP metadata extent {index} references an invalid block device")

    partitions_offset, _, _ = descriptors["partitions"]
    partition_to_group: Dict[str, str] = {}
    partition_names: Set[str] = set()
    valid_attributes = 0x03 | (0x0C if minor_version >= 1 else 0)
    for index in range(partition_count):
        entry = tables[partitions_offset + index * 52 : partitions_offset + (index + 1) * 52]
        raw_name, attributes, first_extent, num_extents, group_index = struct.unpack("<36sIIII", entry)
        name = _decode_lp_name(raw_name, "partition")
        if attributes & ~valid_attributes:
            raise RuntimeError(f"LP partition {name} has unsupported attributes")
        if first_extent + num_extents > extent_count:
            raise RuntimeError(f"LP partition {name} references an invalid extent range")
        if group_index >= len(groups):
            raise RuntimeError(f"LP partition {name} references an invalid group")
        if attributes & LP_PARTITION_ATTR_SLOT_SUFFIXED:
            name += "_a"  # Match AOSP metadata slot zero name adjustment.
            has_slot_specific_layout = True
        if name in partition_names:
            raise RuntimeError(f"LP metadata contains duplicate partition name: {name}")
        partition_names.add(name)
        partition_to_group[name] = groups[group_index][0]

    block_device_offset, _, _ = descriptors["block_devices"]
    device_entry = tables[block_device_offset : block_device_offset + 64]
    first_logical_sector, _alignment, _alignment_offset, device_size, _device_name, device_flags = struct.unpack(
        "<QIIQ36sI", device_entry
    )
    if device_flags & 0x1:  # LP_BLOCK_DEVICE_SLOT_SUFFIXED
        has_slot_specific_layout = True
    if device_size <= 0 or device_size % LP_SECTOR_SIZE:
        raise RuntimeError(f"LP metadata has an invalid super device size: {device_size}")
    metadata_region_size = LP_PARTITION_RESERVED_BYTES + 2 * (
        LP_METADATA_GEOMETRY_SIZE + metadata_size * metadata_slots
    )
    if first_logical_sector * LP_SECTOR_SIZE < metadata_region_size:
        raise RuntimeError("LP metadata overlaps the logical partition data area")
    if first_logical_sector * LP_SECTOR_SIZE > device_size:
        raise RuntimeError("LP metadata places logical partitions beyond the super device size")

    group_limits = {
        name: maximum_size
        for name, _flags, maximum_size in groups
        if name != "default" and maximum_size > 0
    }
    return device_size, metadata_slots, group_limits, partition_to_group, has_slot_specific_layout


def parse_super_geometry(super_image: Path) -> SuperGeometry:
    """Validate AOSP LP metadata directly, including geometry/header/table SHA-256."""
    reader = _SuperImageReader(super_image)
    try:
        metadata_size, metadata_slots, _logical_block_size = _read_lp_geometry(reader)
        primary_offset = LP_PARTITION_RESERVED_BYTES + 2 * LP_METADATA_GEOMETRY_SIZE
        backup_offset = primary_offset + metadata_size * metadata_slots
        errors: List[str] = []
        parsed: Optional[Tuple[int, int, Dict[str, int], Dict[str, str], bool]] = None
        for offset in (primary_offset, backup_offset):
            try:
                parsed = _parse_lp_metadata_copy(reader, metadata_size, metadata_slots, offset)
                break
            except RuntimeError as exc:
                errors.append(f"offset {offset}: {exc}")
        if parsed is None:
            raise RuntimeError("Invalid LP metadata in primary and backup copies: " + "; ".join(errors))

        reported_size, parsed_slots, raw_groups, part_to_group, slot_specific_layout = parsed
        if parsed_slots != metadata_slots:
            raise RuntimeError("LP metadata slot count changed while reading geometry")
        if reported_size > MAX_SUPER_DEVICE_SIZE_BYTES:
            print(
                f"[LP metadata] Clamping reported super size {reported_size} to the joyeuse maximum "
                f"{MAX_SUPER_DEVICE_SIZE_BYTES} bytes"
            )
        device_size = min(reported_size, MAX_SUPER_DEVICE_SIZE_BYTES)
        max_group_budget = max(device_size - SUPER_METADATA_RESERVE_BYTES, 0)
        groups = {
            name: min(group_size, max_group_budget)
            for name, group_size in raw_groups.items()
            if group_size > 0
        }
        if not groups:
            groups = {"qti_dynamic_partitions": max_group_budget}
        return SuperGeometry(
            dev_size=device_size,
            reported_dev_size=reported_size,
            metadata_size=metadata_size,
            metadata_slots=metadata_slots,
            is_ab=metadata_slots >= 3 or slot_specific_layout,
            groups=groups,
            part_to_group=part_to_group,
        )
    finally:
        reader.close()


def safe_primary_group_budget(geom: SuperGeometry, primary_group: str, required_bytes: int) -> int:
    """Return a safe LP group cap, using free device space only for a single non-A/B group."""
    if primary_group not in geom.groups:
        raise RuntimeError(f"Primary dynamic group is missing from LP metadata: {primary_group}")
    device_budget = min(geom.dev_size, MAX_SUPER_DEVICE_SIZE_BYTES) - SUPER_METADATA_RESERVE_BYTES
    if device_budget <= 0:
        raise RuntimeError(f"Invalid dynamic-partition capacity after metadata reserve: {device_budget}")

    source_group_budget = min(geom.groups[primary_group], device_budget)
    if (
        required_bytes > source_group_budget
        and not geom.is_ab
        and len(geom.groups) == 1
    ):
        return device_budget
    return source_group_budget


def escape_fc_regex(path_str: str) -> str:
    return re.sub(r"([.+*()\[\]^$?\\{}|])", r"\\\1", path_str)


def sync_fs_config_and_contexts(
    part_name: str,
    extract_dir: Path,
    fs_config_path: Path,
    file_contexts_path: Path,
) -> None:
    existing_fsc: Dict[str, str] = {}
    if fs_config_path.is_file():
        for ln in fs_config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not ln.strip():
                continue
            key = ln.split()[0]
            existing_fsc[key] = ln

    existing_fc_lines: List[str] = []
    existing_fc_keys: Set[str] = set()
    if file_contexts_path.is_file():
        for ln in file_contexts_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not ln.strip():
                continue
            existing_fc_lines.append(ln)
            existing_fc_keys.add(ln.split()[0])

    default_ctx = "u:object_r:vendor_file:s0" if part_name in ("vendor", "odm") else "u:object_r:system_file:s0"

    def infer_context(rel_posix: str) -> str:
        full_p = f"/{part_name}/{rel_posix}"
        if part_name in ("vendor", "odm"):
            if "/etc/init/" in full_p or full_p.endswith(".rc"):
                return "u:object_r:vendor_configs_file:s0"
            if "/etc/" in full_p:
                return "u:object_r:vendor_configs_file:s0"
            if "/bin/hw/" in full_p or "/bin/" in full_p:
                return "u:object_r:vendor_file:s0"
            if "/lib" in full_p and "/hw/" in full_p:
                return "u:object_r:same_process_hal_file:s0" if "same_process" in full_p else "u:object_r:vendor_hal_file:s0"
            if "/overlay/" in full_p:
                return "u:object_r:vendor_overlay_file:s0"
            return "u:object_r:vendor_file:s0"
        else:
            if "/bin/" in full_p or "/xbin/" in full_p:
                return "u:object_r:system_file:s0"
            if "/lib" in full_p:
                return "u:object_r:system_lib_file:s0"
            return default_ctx

    new_fsc_lines: List[str] = []
    for root_k in ("/", part_name, f"{part_name}/"):
        if root_k in existing_fsc:
            new_fsc_lines.append(existing_fsc[root_k])
    if not new_fsc_lines:
        new_fsc_lines.extend(["/ 0 0 0755", f"{part_name} 0 0 0755"])

    seen_fsc = {ln.split()[0] for ln in new_fsc_lines}

    for root, dirs, files in os.walk(extract_dir, followlinks=False):
        dirs.sort()
        files.sort()
        rel_root = Path(root).relative_to(extract_dir)
        entries = [(d, True) for d in dirs] + [(fn, False) for fn in files]
        for name, is_dir in entries:
            full_entry = Path(root) / name
            is_link = full_entry.is_symlink()
            rel_item = (rel_root / name).as_posix()
            fsc_key = f"{part_name}/{rel_item}"
            if fsc_key in existing_fsc:
                if fsc_key not in seen_fsc:
                    new_fsc_lines.append(existing_fsc[fsc_key])
                    seen_fsc.add(fsc_key)
            else:
                if is_link:
                    target = os.readlink(full_entry)
                    new_fsc_lines.append(f"{fsc_key} 0 0 0777 {target}")
                elif is_dir:
                    gid = 2000 if ("/bin" in f"/{fsc_key}" or fsc_key.endswith("/bin")) else 0
                    mode = "0700" if fsc_key == f"{part_name}/lost+found" else "0755"
                    new_fsc_lines.append(f"{fsc_key} 0 {gid} {mode}")
                else:
                    is_exec = (
                        "/bin/" in f"/{fsc_key}"
                        or "/xbin/" in f"/{fsc_key}"
                        or name in ("ksud",)
                        or (name.endswith(".sh") and "adb" in fsc_key)
                    )
                    gid = 2000 if is_exec else 0
                    mode = "0755" if is_exec else "0644"
                    new_fsc_lines.append(f"{fsc_key} 0 {gid} {mode}")
                seen_fsc.add(fsc_key)

            raw_fc_path = f"/{part_name}/{rel_item}"
            escaped_fc = escape_fc_regex(raw_fc_path)
            if escaped_fc not in existing_fc_keys and raw_fc_path not in existing_fc_keys:
                existing_fc_lines.append(f"{escaped_fc} {infer_context(rel_item)}")
                existing_fc_keys.add(escaped_fc)

    if f"/{part_name}" not in existing_fc_keys:
        existing_fc_lines.insert(0, f"/{part_name} {default_ctx}")
    if "/" not in existing_fc_keys:
        existing_fc_lines.insert(0, f"/ {default_ctx}")

    fs_config_path.parent.mkdir(parents=True, exist_ok=True)
    fs_config_path.write_text("\n".join(new_fsc_lines) + "\n", encoding="utf-8")
    file_contexts_path.parent.mkdir(parents=True, exist_ok=True)
    file_contexts_path.write_text("\n".join(existing_fc_lines) + "\n", encoding="utf-8")


def extract_partition_fs(
    part_name: str,
    raw_img: Path,
    work_dir: Path,
    extract_erofs_bin: Path,
    source_format: str,
    rel_prefix: str = "",
    patch_dat_bytes: bytes = b"",
) -> PartitionRecord:
    fs_type = detect_fs_type(raw_img)
    out_root = work_dir / "extracted_parts" / part_name
    shutil.rmtree(out_root, ignore_errors=True)
    out_root.mkdir(parents=True, exist_ok=True)

    if fs_type == "erofs":
        run_cmd([str(extract_erofs_bin), "-x", "-i", str(raw_img), "-o", str(out_root)])
        extract_dir = out_root / part_name
        if not extract_dir.is_dir():
            candidates = [d for d in out_root.iterdir() if d.is_dir() and d.name != "config"]
            extract_dir = candidates[0] if candidates else out_root
        cfg_dir = out_root / "config"
        fsc_candidates = list(cfg_dir.glob("*_fs_config"))
        fc_candidates = list(cfg_dir.glob("*_file_contexts"))
        fs_config = fsc_candidates[0] if fsc_candidates else (cfg_dir / f"{part_name}_fs_config")
        file_contexts = fc_candidates[0] if fc_candidates else (cfg_dir / f"{part_name}_file_contexts")
    elif fs_type == "ext4":
        extract_dir = out_root / part_name
        mnt_dir = out_root / "_mnt"
        extract_dir.mkdir(parents=True, exist_ok=True)
        mnt_dir.mkdir(parents=True, exist_ok=True)
        run_cmd(["sudo", "mount", "-o", "ro,loop", str(raw_img), str(mnt_dir)])
        try:
            run_cmd(["sudo", "cp", "-a", f"{mnt_dir}/.", str(extract_dir)])
            uid, gid = os.getuid(), os.getgid()
            run_cmd(["sudo", "chown", "-R", f"{uid}:{gid}", str(extract_dir)])
        finally:
            subprocess.run(["sudo", "umount", str(mnt_dir)], check=False)
        cfg_dir = out_root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        fs_config = cfg_dir / f"{part_name}_fs_config"
        file_contexts = cfg_dir / f"{part_name}_file_contexts"
    else:
        raise RuntimeError(f"Unsupported filesystem type '{fs_type}' on partition {part_name} ({raw_img})")

    make_tree_writable(extract_dir)

    return PartitionRecord(
        base_name=part_name,
        raw_name=raw_img.stem,
        raw_img_path=raw_img,
        fs_type=fs_type,
        extract_dir=extract_dir,
        fs_config_path=fs_config,
        file_contexts_path=file_contexts,
        source_format=source_format,
        rel_prefix_in_rom=rel_prefix,
        patch_dat_bytes=patch_dat_bytes,
    )


def rebuild_partition_img(
    rec: PartitionRecord,
    out_raw_img: Path,
    mkfs_erofs_bin: Path,
    timestamp: str = "1735689600",
    mke2fs_bin: Optional[Path] = None,
    e2fsdroid_bin: Optional[Path] = None,
) -> Path:
    if rec.fs_type == "ext4":
        lost_found = rec.extract_dir / "lost+found"
        if lost_found.is_symlink() or (lost_found.exists() and not lost_found.is_dir()):
            raise RuntimeError(f"Invalid ext4 lost+found entry in {rec.base_name}: {lost_found}")
        if not lost_found.exists():
            lost_found.mkdir()
            os.chmod(lost_found, 0o700)
    sync_fs_config_and_contexts(
        part_name=rec.base_name,
        extract_dir=rec.extract_dir,
        fs_config_path=rec.fs_config_path,
        file_contexts_path=rec.file_contexts_path,
    )
    out_raw_img.parent.mkdir(parents=True, exist_ok=True)
    out_raw_img.unlink(missing_ok=True)

    if rec.fs_type == "erofs":
        mount_pt = f"/{rec.base_name}"
        run_cmd(
            [
                str(mkfs_erofs_bin),
                "-zlz4hc,9",
                "-T",
                timestamp,
                f"--mount-point={mount_pt}",
                f"--fs-config-file={rec.fs_config_path}",
                f"--file-contexts={rec.file_contexts_path}",
                str(out_raw_img),
                str(rec.extract_dir),
            ]
        )
    elif rec.fs_type == "ext4":
        du_out = subprocess.check_output(["du", "-sb", str(rec.extract_dir)], text=True).split()[0]
        tree_bytes = int(du_out)
        img_bytes = max(int(tree_bytes * 1.16) + 64 * 1024 * 1024, 128 * 1024 * 1024)
        img_bytes = ((img_bytes + 4095) // 4096) * 4096
        blocks_4k = img_bytes // 4096
        mke2fs = mke2fs_bin or find_host_tool("mke2fs")
        e2fsdroid = e2fsdroid_bin or find_host_tool("e2fsdroid")
        if mke2fs is None or e2fsdroid is None:
            missing = [name for name, path in (("mke2fs", mke2fs), ("e2fsdroid", e2fsdroid)) if path is None]
            raise RuntimeError(f"Required ext4 image tools are unavailable: {missing}")
        run_cmd(
            [
                str(mke2fs),
                "-O",
                "^has_journal",
                "-m",
                "0",
                "-L",
                rec.base_name,
                "-t",
                "ext4",
                "-b",
                "4096",
                str(out_raw_img),
                str(blocks_4k),
            ]
        )
        run_cmd(
            [
                str(e2fsdroid),
                "-e",
                "-T",
                timestamp,
                "-C",
                str(rec.fs_config_path),
                "-S",
                str(rec.file_contexts_path),
                "-f",
                str(rec.extract_dir),
                "-a",
                f"/{rec.base_name}",
                str(out_raw_img),
            ]
        )
        compact_ext4_image(out_raw_img)
    else:
        raise RuntimeError(f"Unsupported fs_type {rec.fs_type}")

    print(f"[repack-fs] Rebuilt {rec.base_name} ({rec.fs_type}) -> {out_raw_img.stat().st_size / (1024**2):.1f} MiB")
    return out_raw_img


def unpack_rom_zip(
    rom_zip: Path,
    work_dir: Path,
    tools: Dict[str, Path],
) -> RomWorkspace:
    rom_dir = work_dir / "rom_unzip"
    raw_dir = work_dir / "raw_imgs"
    shutil.rmtree(rom_dir, ignore_errors=True)
    shutil.rmtree(raw_dir, ignore_errors=True)
    rom_dir.mkdir(parents=True)
    raw_dir.mkdir(parents=True)

    run_cmd(["unzip", "-q", str(rom_zip), "-d", str(rom_dir)])

    payload_bin = rom_dir / "payload.bin"
    if payload_bin.is_file() and "payload_dumper" in tools:
        p_out = rom_dir / "images"
        p_out.mkdir(parents=True, exist_ok=True)
        run_cmd([str(tools["payload_dumper"]), "--out", str(p_out), str(payload_bin)])
        payload_bin.unlink()

    boot_candidates = list(rom_dir.glob("boot.img")) + list(rom_dir.glob("images/boot.img"))
    if not boot_candidates:
        zst_boots = list(rom_dir.glob("**/boot.img.zst"))
        if zst_boots:
            out_b = zst_boots[0].with_suffix("")
            run_cmd(["zstd", "-d", "-f", "--rm", str(zst_boots[0]), "-o", str(out_b)])
            boot_candidates = [out_b]
    if not boot_candidates:
        raise RuntimeError("Could not locate boot.img inside base ROM ZIP")
    boot_img_path = boot_candidates[0]

    vbmeta_paths: List[Path] = []
    for pat in ("vbmeta*.img", "images/vbmeta*.img", "firmware-update/vbmeta*.img"):
        vbmeta_paths.extend(rom_dir.glob(pat))

    partitions: Dict[str, PartitionRecord] = {}
    unmodified_super_imgs: Dict[str, Tuple[Path, str]] = {}
    super_geom: Optional[SuperGeometry] = None

    super_img = next(iter(list(rom_dir.glob("super.img")) + list(rom_dir.glob("images/super.img"))), None)
    super_zst = next(iter(list(rom_dir.glob("super.img.zst")) + list(rom_dir.glob("images/super.img.zst"))), None)
    super_br = next(iter(list(rom_dir.glob("super.new.dat.br"))), None)

    if super_img or super_zst or super_br:
        container_type = "super"
        raw_super = raw_dir / "super.raw.img"
        geom_flags = {"was_sparse": False, "was_zstd": False, "was_brotli_dat": False, "rel_path": "super.img"}

        if super_zst:
            geom_flags["was_zstd"] = True
            geom_flags["rel_path"] = super_zst.relative_to(rom_dir).as_posix()
            tmp_s = raw_dir / "super.unzst.img"
            run_cmd(["zstd", "-d", "-f", str(super_zst), "-o", str(tmp_s)])
            super_zst.unlink()
            super_img = tmp_s
        elif super_br:
            geom_flags["was_brotli_dat"] = True
            geom_flags["rel_path"] = "super"
            tmp_dat = raw_dir / "super.new.dat"
            run_cmd(["brotli", "-d", "-f", str(super_br), "-o", str(tmp_dat)])
            super_br.unlink()
            sdat2img(rom_dir / "super.transfer.list", tmp_dat, raw_super)
            tmp_dat.unlink()

        if super_img and not raw_super.is_file():
            if not geom_flags["was_zstd"]:
                geom_flags["rel_path"] = super_img.relative_to(rom_dir).as_posix()
            if is_sparse_image(super_img):
                geom_flags["was_sparse"] = True
                run_cmd(["simg2img", str(super_img), str(raw_super)])
                super_img.unlink()
            else:
                shutil.move(str(super_img), str(raw_super))

        super_geom = parse_super_geometry(raw_super)
        super_geom.was_sparse = bool(geom_flags["was_sparse"])
        super_geom.was_zstd = bool(geom_flags["was_zstd"])
        super_geom.was_brotli_dat = bool(geom_flags["was_brotli_dat"])
        super_geom.rel_path_in_rom = str(geom_flags["rel_path"])

        lp_dir = raw_dir / "super_parts"
        lp_dir.mkdir(parents=True, exist_ok=True)
        run_cmd([str(tools["lpunpack"]), str(raw_super), str(lp_dir)], env_extra=tools.get("_env"))  # type: ignore[arg-type]
        raw_super.unlink()

        for p_img in sorted(lp_dir.glob("*.img")):
            if p_img.stat().st_size == 0:
                p_img.unlink()
                continue
            stem = p_img.stem
            base = stem[:-2] if stem.endswith("_a") else (stem[:-2] if stem.endswith("_b") else stem)
            if stem.endswith("_a"):
                super_geom.slot_suffix = "_a"
                super_geom.is_ab = True
            if stem.endswith("_b"):
                p_img.unlink()
                continue

            if base in TARGET_PARTITIONS:
                rec = extract_partition_fs(
                    part_name=base,
                    raw_img=p_img,
                    work_dir=work_dir,
                    extract_erofs_bin=tools["extract.erofs"],
                    source_format="super",
                )
                rec.raw_name = stem
                p_img.unlink()
                partitions[base] = rec
            else:
                grp = super_geom.part_to_group.get(stem, next(iter(super_geom.groups.keys())))
                unmodified_super_imgs[stem] = (p_img, grp)

        return RomWorkspace(
            rom_extract_dir=rom_dir,
            work_dir=work_dir,
            container_type=container_type,
            boot_img_path=boot_img_path,
            vbmeta_paths=vbmeta_paths,
            partitions=partitions,
            super_geom=super_geom,
            unmodified_super_imgs=unmodified_super_imgs,
        )

    br_files = list(rom_dir.glob("*.new.dat.br")) + list(rom_dir.glob("images/*.new.dat.br"))
    if br_files:
        container_type = "dat_br"
        for br in sorted(br_files):
            part = br.name[: -len(".new.dat.br")]
            if part not in TARGET_PARTITIONS:
                continue
            parent = br.parent
            tlist = parent / f"{part}.transfer.list"
            pdat = parent / f"{part}.patch.dat"
            patch_bytes = pdat.read_bytes() if pdat.is_file() else b""
            tmp_dat = raw_dir / f"{part}.new.dat"
            raw_img = raw_dir / f"{part}.raw.img"
            run_cmd(["brotli", "-d", "-f", str(br), "-o", str(tmp_dat)])
            br.unlink()
            sdat2img(tlist, tmp_dat, raw_img)
            tmp_dat.unlink()

            rec = extract_partition_fs(
                part_name=part,
                raw_img=raw_img,
                work_dir=work_dir,
                extract_erofs_bin=tools["extract.erofs"],
                source_format="dat_br",
                rel_prefix=parent.relative_to(rom_dir).as_posix(),
                patch_dat_bytes=patch_bytes,
            )
            raw_img.unlink()
            partitions[part] = rec

        return RomWorkspace(
            rom_extract_dir=rom_dir,
            work_dir=work_dir,
            container_type=container_type,
            boot_img_path=boot_img_path,
            vbmeta_paths=vbmeta_paths,
            partitions=partitions,
        )

    container_type = "images_dir"
    for part in TARGET_PARTITIONS:
        cands = (
            list(rom_dir.glob(f"{part}.img"))
            + list(rom_dir.glob(f"images/{part}.img"))
            + list(rom_dir.glob(f"{part}.img.zst"))
            + list(rom_dir.glob(f"images/{part}.img.zst"))
        )
        if not cands:
            continue
        src_p = cands[0]
        was_zst = src_p.name.endswith(".zst")
        rel_p = src_p.relative_to(rom_dir).as_posix()
        raw_img = raw_dir / f"{part}.raw.img"
        if was_zst:
            tmp_i = raw_dir / f"{part}.unzst.img"
            run_cmd(["zstd", "-d", "-f", str(src_p), "-o", str(tmp_i)])
            src_p.unlink()
            src_p = tmp_i
        if is_sparse_image(src_p):
            run_cmd(["simg2img", str(src_p), str(raw_img)])
            src_p.unlink()
        else:
            shutil.move(str(src_p), str(raw_img))

        rec = extract_partition_fs(
            part_name=part,
            raw_img=raw_img,
            work_dir=work_dir,
            extract_erofs_bin=tools["extract.erofs"],
            source_format="img_zst" if was_zst else "img_raw",
            rel_prefix=rel_p,
        )
        raw_img.unlink()
        partitions[part] = rec

    if not partitions:
        raise RuntimeError("No Android system/product/vendor partitions found in ROM archive")

    return RomWorkspace(
        rom_extract_dir=rom_dir,
        work_dir=work_dir,
        container_type=container_type,
        boot_img_path=boot_img_path,
        vbmeta_paths=vbmeta_paths,
        partitions=partitions,
    )


def repack_rom_workspace(
    ws: RomWorkspace,
    out_rom_zip: Path,
    tools: Dict[str, Path],
) -> Path:
    rebuilt_dir = ws.work_dir / "rebuilt_parts"
    rebuilt_dir.mkdir(parents=True, exist_ok=True)

    rebuilt_imgs: Dict[str, Path] = {}
    for base, rec in ws.partitions.items():
        out_img = rebuilt_dir / f"{rec.raw_name}.img"
        rebuild_partition_img(
            rec,
            out_img,
            tools["mkfs.erofs"],
            mke2fs_bin=tools.get("mke2fs"),
            e2fsdroid_bin=tools.get("e2fsdroid"),
        )
        shutil.rmtree(rec.extract_dir.parent, ignore_errors=True)
        rebuilt_imgs[base] = out_img

    if ws.container_type == "super":
        assert ws.super_geom is not None
        geom = ws.super_geom
        if geom.dev_size <= 0:
            raise RuntimeError(f"Invalid super device size: {geom.dev_size}")
        geom.dev_size = min(geom.dev_size, MAX_SUPER_DEVICE_SIZE_BYTES)
        primary_group = next(iter(geom.groups.keys()))
        total_parts_bytes = sum(p.stat().st_size for p in rebuilt_imgs.values()) + sum(
            p.stat().st_size for p, _ in ws.unmodified_super_imgs.values()
        )
        source_group_max = geom.groups[primary_group]
        group_max = safe_primary_group_budget(geom, primary_group, total_parts_bytes)
        if group_max > source_group_max:
            print(
                f"[LP metadata] Expanded the only non-A/B dynamic group {primary_group} "
                f"from {source_group_max} to {group_max} bytes, within the joyeuse super-device cap"
            )
        if total_parts_bytes > group_max:
            partition_sizes = ", ".join(
                f"{name}={path.stat().st_size}" for name, path in sorted(rebuilt_imgs.items())
            )
            raise RuntimeError(
                "Rebuilt dynamic partitions exceed the safe joyeuse super-group budget "
                f"({total_parts_bytes} > {group_max} bytes); refusing to exceed the device cap. "
                f"Rebuilt images: {partition_sizes}"
            )
        geom.groups[primary_group] = group_max

        raw_or_sparse_super = rebuilt_dir / "super_repacked.img"
        lp_cmd = [
            str(tools["lpmake"]),
            "--device-size",
            str(geom.dev_size),
            "--metadata-size",
            str(geom.metadata_size),
            "--metadata-slots",
            str(geom.metadata_slots),
        ]
        if geom.is_ab:
            lp_cmd.append("--virtual-ab")
        if geom.was_sparse or geom.was_brotli_dat:
            lp_cmd.append("--sparse")

        for g_name, g_max in geom.groups.items():
            if geom.is_ab and not g_name.endswith("_a") and not g_name.endswith("_b"):
                lp_cmd.extend(["--group", f"{g_name}_a:{g_max}", "--group", f"{g_name}_b:{g_max}"])
            else:
                lp_cmd.extend(["--group", f"{g_name}:{g_max}"])

        for base, img_path in rebuilt_imgs.items():
            pname = f"{base}{geom.slot_suffix}"
            grp = geom.part_to_group.get(pname, f"{primary_group}{geom.slot_suffix}" if geom.slot_suffix and not primary_group.endswith(geom.slot_suffix) else primary_group)
            sz = img_path.stat().st_size
            lp_cmd.extend(["--partition", f"{pname}:readonly:{sz}:{grp}", "--image", f"{pname}={img_path}"])
            if geom.is_ab and geom.slot_suffix == "_a":
                b_grp = grp[:-2] + "_b" if grp.endswith("_a") else grp
                lp_cmd.extend(["--partition", f"{base}_b:readonly:0:{b_grp}"])

        for stem, (img_path, grp) in ws.unmodified_super_imgs.items():
            sz = img_path.stat().st_size
            lp_cmd.extend(["--partition", f"{stem}:readonly:{sz}:{grp}", "--image", f"{stem}={img_path}"])

        lp_cmd.extend(["--output", str(raw_or_sparse_super)])
        run_cmd(lp_cmd, env_extra=tools.get("_env"))  # type: ignore[arg-type]
        verified_geom = parse_super_geometry(raw_or_sparse_super)
        output_device_size = verified_geom.reported_dev_size
        if output_device_size <= 0:
            raise RuntimeError(f"LP metadata reported an invalid rebuilt super size: {output_device_size}")
        if output_device_size > MAX_SUPER_DEVICE_SIZE_BYTES or output_device_size > geom.dev_size:
            raise RuntimeError(
                "lpmake output exceeds the configured joyeuse super capacity: "
                f"{output_device_size} > min({MAX_SUPER_DEVICE_SIZE_BYTES}, {geom.dev_size}) bytes"
            )
        if verified_geom.metadata_size != geom.metadata_size:
            raise RuntimeError(
                "Rebuilt super metadata size changed unexpectedly: "
                f"{verified_geom.metadata_size} != {geom.metadata_size}"
            )
        if verified_geom.metadata_slots != geom.metadata_slots:
            raise RuntimeError(
                "Rebuilt super metadata-slot count changed unexpectedly: "
                f"{verified_geom.metadata_slots} != {geom.metadata_slots}"
            )

        for p in rebuilt_imgs.values():
            p.unlink(missing_ok=True)
        for p, _ in ws.unmodified_super_imgs.values():
            p.unlink(missing_ok=True)

        if geom.was_brotli_dat:
            t_list, n_dat, p_dat = sparse_img_to_sdat(raw_or_sparse_super, ws.rom_extract_dir / "super")
            raw_or_sparse_super.unlink()
            run_cmd(["brotli", "-q", "6", "-f", "--rm", str(n_dat), "-o", str(ws.rom_extract_dir / "super.new.dat.br")])
            del t_list, p_dat
        elif geom.was_zstd:
            dest_zst = ws.rom_extract_dir / geom.rel_path_in_rom
            dest_zst.parent.mkdir(parents=True, exist_ok=True)
            run_cmd(["zstd", "-6", "-T0", "-f", "--rm", str(raw_or_sparse_super), "-o", str(dest_zst)])
        else:
            dest_super = ws.rom_extract_dir / geom.rel_path_in_rom
            dest_super.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(raw_or_sparse_super), str(dest_super))

    elif ws.container_type == "dat_br":
        for base, rec in ws.partitions.items():
            raw_img = rebuilt_imgs[base]
            sparse_img = rebuilt_dir / f"{base}.sparse.img"
            run_cmd(["img2simg", str(raw_img), str(sparse_img)])
            raw_img.unlink()

            parent_dir = ws.rom_extract_dir if rec.rel_prefix_in_rom in ("", ".") else (ws.rom_extract_dir / rec.rel_prefix_in_rom)
            parent_dir.mkdir(parents=True, exist_ok=True)
            _, n_dat, p_dat = sparse_img_to_sdat(sparse_img, parent_dir / base)
            sparse_img.unlink()
            if rec.patch_dat_bytes:
                p_dat.write_bytes(rec.patch_dat_bytes)
            run_cmd(["brotli", "-q", "6", "-f", "--rm", str(n_dat), "-o", str(parent_dir / f"{base}.new.dat.br")])

            ds_op = parent_dir / "dynamic_partitions_op_list"
            if ds_op.is_file():
                op_txt = ds_op.read_text(encoding="utf-8")
                new_sz = subprocess.check_output(["stat", "-c%s", str(rebuilt_dir)], text=True)
                del new_sz
                raw_sz = int(op_txt.splitlines()[0].split()[-1]) if False else None
                del raw_sz
                op_txt = re.sub(
                    rf"(resize\s+{re.escape(base)}(?:_a)?\s+)\d+",
                    f"\\g<1>{sparse_img_virtual_size(parent_dir / f'{base}.transfer.list')}",
                    op_txt,
                )
                ds_op.write_text(op_txt, encoding="utf-8")

    else:
        for base, rec in ws.partitions.items():
            raw_img = rebuilt_imgs[base]
            dest = ws.rom_extract_dir / rec.rel_prefix_in_rom
            dest.parent.mkdir(parents=True, exist_ok=True)
            if rec.source_format == "img_zst":
                run_cmd(["zstd", "-6", "-T0", "-f", "--rm", str(raw_img), "-o", str(dest)])
            else:
                shutil.move(str(raw_img), str(dest))

    out_rom_zip.parent.mkdir(parents=True, exist_ok=True)
    out_rom_zip.unlink(missing_ok=True)
    run_cmd(["7z", "a", "-tzip", "-mx=1", "-mmt=on", str(out_rom_zip.resolve()), "."], cwd=ws.rom_extract_dir)
    print(f"[repack-rom] Final flashable ROM ZIP ready: {out_rom_zip} ({out_rom_zip.stat().st_size / (1024**3):.2f} GiB)")
    return out_rom_zip


def sparse_img_virtual_size(transfer_list: Path) -> int:
    lines = [ln.strip() for ln in transfer_list.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("erase "):
            nums = [int(x) for x in ln.split()[1].split(",")]
            return nums[-1] * 4096
    return int(lines[1]) * 4096
