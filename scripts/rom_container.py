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
  - `SuperGeometry` clamps dynamic group max_size to `dev_size - 4 MiB` so `lpmake` never overflows.
"""

from __future__ import annotations

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


@dataclass
class SuperGeometry:
    dev_size: int = 9126805504  # Standard miatoll super partition size (8.5 GiB = 9,126,805,504 B)
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


def parse_lpdump_geometry(raw_super: Path, lpdump_bin: Optional[Path], tool_env: Optional[Dict[str, str]] = None) -> SuperGeometry:
    geom = SuperGeometry(dev_size=raw_super.stat().st_size)
    max_group_budget = max(geom.dev_size - 4 * 1024 * 1024, 0)
    geom.groups = {"qti_dynamic_partitions": max_group_budget}

    if not lpdump_bin or not lpdump_bin.is_file():
        return geom
    try:
        env = os.environ.copy()
        if tool_env:
            env.update(tool_env)
        out = subprocess.check_output([str(lpdump_bin), str(raw_super)], env=env, text=True, stderr=subprocess.DEVNULL)
        m_size = re.search(r"Size:\s*(\d+)\s*bytes", out)
        if m_size:
            geom.dev_size = int(m_size.group(1))
            max_group_budget = max(geom.dev_size - 4 * 1024 * 1024, 0)
        m_meta = re.search(r"Metadata max size:\s*(\d+)\s*bytes", out)
        if m_meta:
            geom.metadata_size = int(m_meta.group(1))
        m_slots = re.search(r"Metadata slot count:\s*(\d+)", out)
        if m_slots:
            geom.metadata_slots = int(m_slots.group(1))
            geom.is_ab = geom.metadata_slots >= 3

        groups: Dict[str, int] = {}
        cur_g = None
        for line in out.splitlines():
            mg = re.search(r"^\s*Name:\s*(\S+)", line)
            if mg:
                cur_g = mg.group(1)
            ms = re.search(r"^\s*Maximum size:\s*(\d+)\s*bytes", line)
            if ms and cur_g and cur_g != "default":
                g_sz = int(ms.group(1))
                if g_sz > 0:
                    groups[cur_g] = min(g_sz, max_group_budget)
        if groups:
            geom.groups = groups

        cur_p = None
        for line in out.splitlines():
            mp = re.search(r"^\s*Name:\s*([a-zA-Z0-9_]+)$", line)
            if mp:
                cur_p = mp.group(1)
            mpg = re.search(r"^\s*Group:\s*(\S+)", line)
            if mpg and cur_p:
                geom.part_to_group[cur_p] = mpg.group(1)
    except Exception as exc:
        print(f"[lpdump] Geometry fallback ({exc})")
    return geom


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
                    new_fsc_lines.append(f"{fsc_key} 0 {gid} 0755")
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
) -> Path:
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
        mke2fs_bin = shutil.which("mke2fs") or "/sbin/mke2fs"
        e2fsdroid_bin = shutil.which("e2fsdroid")
        if e2fsdroid_bin:
            run_cmd([mke2fs_bin, "-O", "^has_journal", "-L", rec.base_name, "-t", "ext4", "-b", "4096", str(out_raw_img), str(blocks_4k)])
            run_cmd(
                [
                    e2fsdroid_bin,
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
        else:
            run_cmd(["truncate", "-s", str(img_bytes), str(out_raw_img)])
            run_cmd([mke2fs_bin, "-F", "-O", "^has_journal", "-L", rec.base_name, "-t", "ext4", "-b", "4096", "-d", str(rec.extract_dir), str(out_raw_img)])
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

        super_geom = parse_lpdump_geometry(raw_super, tools.get("lpdump"), tools.get("_env"))  # type: ignore[arg-type]
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
        rebuild_partition_img(rec, out_img, tools["mkfs.erofs"])
        shutil.rmtree(rec.extract_dir.parent, ignore_errors=True)
        rebuilt_imgs[base] = out_img

    if ws.container_type == "super":
        assert ws.super_geom is not None
        geom = ws.super_geom
        primary_group = next(iter(geom.groups.keys()))
        total_parts_bytes = sum(p.stat().st_size for p in rebuilt_imgs.values()) + sum(
            p.stat().st_size for p, _ in ws.unmodified_super_imgs.values()
        )
        group_max = geom.groups[primary_group]
        if total_parts_bytes > group_max:
            geom.dev_size = ((total_parts_bytes + 128 * 1024 * 1024 + 4095) // 4096) * 4096
            group_max = geom.dev_size - 4 * 1024 * 1024
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
