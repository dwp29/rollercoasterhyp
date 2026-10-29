#!/usr/bin/env python3
"""Build a deterministic, recovery-flashable bootloop log collector ZIP."""

from __future__ import annotations

import argparse
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "recovery" / "bootloop_logger"
DEFAULT_OUTPUT = ROOT / "recovery" / "Rollercoaster-Bootloop-Logger-1.0-joyeuse.zip"
ARCHIVE_FILES = (
    ("META-INF/com/google/android/update-binary", 0o755),
    ("META-INF/com/google/android/updater-script", 0o644),
    ("bootloop-logger/collect.sh", 0o644),
    ("README.txt", 0o644),
)


def build_zip(output: Path = DEFAULT_OUTPUT) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for member, mode in ARCHIVE_FILES:
            source = SOURCE / member
            if not source.is_file():
                raise FileNotFoundError(f"Missing recovery ZIP source file: {source}")
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, source.read_bytes())

    with zipfile.ZipFile(output, "r") as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise RuntimeError(f"Recovery ZIP failed CRC validation at {bad_member}")
        missing = {member for member, _ in ARCHIVE_FILES} - set(archive.namelist())
        if missing:
            raise RuntimeError(f"Recovery ZIP is missing members: {sorted(missing)}")
        update_info = archive.getinfo("META-INF/com/google/android/update-binary")
        update_mode = (update_info.external_attr >> 16) & 0o777
        if update_mode & 0o111 == 0:
            raise RuntimeError("Recovery update-binary must be executable in the ZIP")

    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = build_zip(args.output.resolve())
    print(f"[bootloop-logger] Recovery ZIP ready: {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
