#!/usr/bin/env python3
"""Offline smoke tests for the recovery-flashable bootloop log collector."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "recovery" / "bootloop_logger" / "bootloop-logger" / "collect.sh"
UPDATE_BINARY = ROOT / "recovery" / "bootloop_logger" / "META-INF" / "com" / "google" / "android" / "update-binary"
ZIP_PATH = ROOT / "recovery" / "Rollercoaster-Bootloop-Logger-1.0-joyeuse.zip"

sys.path.insert(0, str(ROOT / "scripts"))
from build_bootloop_logger_zip import ARCHIVE_FILES, build_zip  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    for path in (COLLECTOR, UPDATE_BINARY):
        result = subprocess.run(["sh", "-n", str(path)], text=True, capture_output=True)
        require(result.returncode == 0, f"Shell syntax error in {path}: {result.stderr}")

    with tempfile.TemporaryDirectory(prefix="rcbl-test-") as temp_name:
        temp = Path(temp_name)
        fake_root = temp / "device-root"
        output_dir = temp / "collected"
        fixtures = {
            "sys/fs/pstore/console-ramoops-0": "previous kernel console\n",
            "sys/fs/pstore/dmesg-ramoops-0": "previous kernel dmesg\n",
            "proc/last_kmsg": "legacy previous kernel log\n",
            "proc/cmdline": "console=ttyMSM0 androidboot.selinux=enforcing\n",
            "proc/mounts": "",
            "cache/recovery/last_log": "recovery fixture log\n",
            "data/tombstones/tombstone_01": "sample native crash\n",
            "data/system/dropbox/system_server_crash.txt": "sample dropbox crash\n",
        }
        for relative, content in fixtures.items():
            destination = fake_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

        env = os.environ.copy()
        env["RCBL_ROOT"] = str(fake_root)
        env["RCBL_OUTPUT_DIR"] = str(output_dir)
        result = subprocess.run(
            ["sh", str(COLLECTOR)],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        require(result.returncode == 0, f"Collector failed: {result.stderr}\n{result.stdout}")
        require((output_dir / "kernel/pstore/console-ramoops-0").read_text(encoding="utf-8") == fixtures["sys/fs/pstore/console-ramoops-0"], "pstore console was not collected")
        require((output_dir / "kernel/pstore/dmesg-ramoops-0").is_file(), "pstore dmesg was not collected")
        require((output_dir / "kernel/last_kmsg").is_file(), "last_kmsg was not collected")
        require((output_dir / "recovery/last_log").is_file(), "recovery log was not collected")
        require((output_dir / "android/tombstones/tombstone_01").is_file(), "tombstone was not collected")
        require((output_dir / "android/dropbox_recent/system_server_crash.txt").is_file(), "DropBox entry was not collected")
        info = (output_dir / "collection-info.txt").read_text(encoding="utf-8")
        require("output_is_temporary=0" in info, "collection metadata missing")
        require("previous-boot" in (output_dir / "README.txt").read_text(encoding="utf-8").lower(), "limitation note missing")
        require((fake_root / "proc/cmdline").read_text(encoding="utf-8") == fixtures["proc/cmdline"], "source fixture was modified")

        fallback_env = env.copy()
        fallback_env.pop("RCBL_OUTPUT_DIR", None)
        fallback = subprocess.run(
            ["sh", str(COLLECTOR)],
            env=fallback_env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        require(fallback.returncode == 0, f"Temporary-storage fallback failed: {fallback.stderr}\\n{fallback.stdout}")
        fallback_dirs = list((fake_root / "tmp/BootloopLogs").glob("bootloop-*"))
        require(len(fallback_dirs) == 1, "collector did not use the /tmp fallback when persistent storage was unavailable")
        fallback_info = (fallback_dirs[0] / "collection-info.txt").read_text(encoding="utf-8")
        require("output_is_temporary=1" in fallback_info, "temporary fallback was not marked in its metadata")

        zip_path = build_zip(ZIP_PATH)
        with zipfile.ZipFile(zip_path) as archive:
            require(archive.testzip() is None, "recovery ZIP CRC check failed")
            require(set(archive.namelist()) == {member for member, _ in ARCHIVE_FILES}, "unexpected recovery ZIP contents")
            update_info = archive.getinfo("META-INF/com/google/android/update-binary")
            require(((update_info.external_attr >> 16) & 0o111) != 0, "update-binary is not executable")

        # Exercise the actual recovery entrypoint with a host fixture and the built ZIP.
        installer_output = temp / "installer-output"
        env["RCBL_OUTPUT_DIR"] = str(installer_output)
        install = subprocess.run(
            ["sh", str(UPDATE_BINARY), "3", "1", str(zip_path)],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        require(install.returncode == 0, f"Recovery installer smoke test failed: {install.stderr}\n{install.stdout}")
        require((installer_output / "kernel/pstore/console-ramoops-0").is_file(), "installer did not invoke the collector")
        require("Collection complete" in install.stdout, "installer did not report completion")

    print("Bootloop logger checks passed: shell syntax, fixture collection, ZIP integrity, executable entrypoint, and recovery-installer smoke test.")


if __name__ == "__main__":
    main()
