#!/usr/bin/env python3
"""Inspect the pinned source ROM's boot/DTB fstabs without building or modifying a ROM."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import List, Set

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fstab_utils import patch_dynamic_fstab_text, strip_avb_verify_fstab_flags
from patch_boot_kernel import list_cpio_fstab_entries, patch_fdt_dynamic_fstabs


BOOT_IMAGE_NAMES = {"boot.img", "vendor_boot.img", "init_boot.img", "recovery.img"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_checked(command: List[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def clone_pinned_tools(repo_url: str, revision: str, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_checked(["git", "clone", "--depth=1", "--single-branch", repo_url, str(destination)])
    current = run_checked(["git", "rev-parse", "HEAD"], cwd=destination).stdout.strip()
    if current != revision:
        run_checked(["git", "fetch", "--depth=1", "origin", revision], cwd=destination)
        run_checked(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=destination)
    actual = run_checked(["git", "rev-parse", "HEAD"], cwd=destination).stdout.strip()
    if actual != revision:
        raise RuntimeError(f"Pinned port-tools revision mismatch: {actual} != {revision}")
    return destination


def find_magiskboot(tools_repo: Path) -> Path:
    expected = tools_repo / "bin" / "linux" / "x86_64" / "magiskboot"
    if expected.is_file():
        expected.chmod(expected.stat().st_mode | 0o111)
        return expected
    candidates = sorted(
        path
        for path in tools_repo.rglob("magiskboot")
        if path.is_file() and "linux" in path.parts and "x86_64" in path.parts
    )
    if not candidates:
        raise RuntimeError(f"Pinned tool repository has no Linux x86_64 magiskboot: {tools_repo}")
    candidates[0].chmod(candidates[0].stat().st_mode | 0o111)
    return candidates[0]


def copy_zip_member(zf: zipfile.ZipFile, member: zipfile.ZipInfo, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member, "r") as source, destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def inspect_boot_image(
    magiskboot: Path,
    image_path: Path,
    display_name: str,
    report: List[str],
    slot_suffixed: bool = False,
) -> Set[str]:
    work = image_path.parent / f"inspect_{image_path.stem}"
    work.mkdir(parents=True, exist_ok=True)
    copied = work / image_path.name
    if copied.resolve() != image_path.resolve():
        shutil.copy2(image_path, copied)

    report.extend(["", f"## Image: {display_name}", f"Extracted size: {image_path.stat().st_size} bytes"])
    unpack = run_checked([str(magiskboot), "unpack", "-h", copied.name], cwd=work)
    if unpack.stdout or unpack.stderr:
        report.extend(["### MagiskBoot unpack", "```", (unpack.stdout + unpack.stderr).strip(), "```"])
    header = work / "header"
    if header.is_file():
        report.extend(["### Header", "```", header.read_text(encoding="utf-8", errors="replace").rstrip(), "```"])

    mounts: Set[str] = set()
    cpio = work / "ramdisk.cpio"
    if cpio.is_file() and cpio.stat().st_size:
        entries = list_cpio_fstab_entries(cpio)
        report.append(f"### Ramdisk CPIO fstab entries ({len(entries)})")
        for index, (archive_name, _mode) in enumerate(entries):
            output_name = f"source_fstab_{index:03d}.txt"
            extracted = run_checked(
                [str(magiskboot), "cpio", cpio.name, f"extract {archive_name} {output_name}"],
                cwd=work,
            )
            fstab_path = work / output_name
            if not fstab_path.is_file():
                raise RuntimeError(f"MagiskBoot did not extract {archive_name} from {display_name}")
            text = fstab_path.read_text(encoding="utf-8", errors="replace")
            sanitized = strip_avb_verify_fstab_flags(text)
            conversion_blocker = ""
            try:
                _preview, found, _changed = patch_dynamic_fstab_text(
                    sanitized,
                    slot_suffixed=slot_suffixed,
                )
            except RuntimeError as exc:
                found = set()
                conversion_blocker = str(exc)
            mounts.update(found)
            report.extend(
                [
                    "",
                    f"#### `{archive_name}` — dynamic mounts detected: {sorted(found)}",
                    "```fstab",
                    text.rstrip(),
                    "```",
                ]
            )
            if conversion_blocker:
                report.append(f"Fstab conversion blocker: `{conversion_blocker}`")
            if extracted.stdout or extracted.stderr:
                report.extend(["MagiskBoot extract:", "```", (extracted.stdout + extracted.stderr).strip(), "```"])
    else:
        report.append("### Ramdisk CPIO fstab entries\nNo non-empty `ramdisk.cpio` was exposed by MagiskBoot.")

    dtb = work / "dtb"
    if dtb.is_file() and dtb.stat().st_size:
        printed = run_checked([str(magiskboot), "dtb", dtb.name, "print", "-f"], cwd=work)
        dtb_text = printed.stdout + printed.stderr
        report.extend(["### DTB fstab nodes", "```", dtb_text.rstrip() or "(no fstab nodes reported)", "```"])
        try:
            _preview, dtb_mounts, _changed = patch_fdt_dynamic_fstabs(
                dtb.read_bytes(),
                slot_suffixed=slot_suffixed,
            )
            mounts.update(dtb_mounts)
            report.append(f"DTB dynamic mounts recognized by the converter: {sorted(dtb_mounts)}")
        except RuntimeError as exc:
            report.append(f"DTB conversion blocker: {exc}")
    else:
        report.append("### DTB fstab nodes\nNo non-empty `dtb` was exposed by MagiskBoot.")

    report.append(f"### Summary\nDetected boot-environment dynamic mounts: {sorted(mounts)}")
    return mounts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/rollercoaster_joyeuse.json"))
    parser.add_argument("--work-dir", type=Path, default=Path("source_inspection"))
    parser.add_argument("--out-report", type=Path, default=Path("out/source_fstab_report.txt"))
    parser.add_argument("--base-rom-override", type=Path, default=None)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    config = json.loads(config_path.read_text(encoding="utf-8"))
    source = config["sources"]
    work_dir = args.work_dir.resolve()
    report_path = args.out_report if args.out_report.is_absolute() else repo_root / args.out_report
    report: List[str] = [
        "# Source ROM fstab inspection (diagnostic only)",
        f"Config: `{config_path}`",
        "This job does not repack partitions, create a ROM, or publish a release.",
        f"Expected base ROM SHA-256: `{source['base_rom_sha256']}`",
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        tools_repo = clone_pinned_tools(
            source["hyperos_port_tools_repo"],
            source["hyperos_port_tools_commit"],
            work_dir / "HyperOS-Port-Python",
        )
        magiskboot = find_magiskboot(tools_repo)
        if args.base_rom_override:
            base_rom = args.base_rom_override.resolve()
        else:
            base_rom = work_dir / source["base_rom_filename"]
            run_checked(
                [
                    "curl",
                    "-fL",
                    "--retry",
                    "5",
                    "--retry-delay",
                    "5",
                    "-o",
                    str(base_rom),
                    source["base_rom_url"],
                ]
            )
        if not base_rom.is_file():
            raise FileNotFoundError(f"Base ROM not found: {base_rom}")
        actual_sha = sha256_file(base_rom)
        report.extend([f"Base ROM: `{base_rom.name}`", f"Actual base ROM SHA-256: `{actual_sha}`"])
        if actual_sha.lower() != source["base_rom_sha256"].lower():
            raise RuntimeError(
                f"Base ROM SHA-256 mismatch: expected {source['base_rom_sha256']}, got {actual_sha}"
            )

        image_dir = work_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(base_rom, "r") as zf:
            members = [
                member
                for member in zf.infolist()
                if PurePosixPath(member.filename).name.lower() in BOOT_IMAGE_NAMES
                and not member.is_dir()
            ]
            if not members:
                raise RuntimeError("No boot/vendor_boot/init_boot/recovery image found in the base ROM ZIP")
            total_mounts: Set[str] = set()
            for index, member in enumerate(members, start=1):
                safe_name = PurePosixPath(member.filename).name
                extracted_image = image_dir / f"{index:02d}_{safe_name}"
                copy_zip_member(zf, member, extracted_image)
                report.append(f"Archive member: `{member.filename}`")
                total_mounts.update(
                    inspect_boot_image(
                        magiskboot,
                        extracted_image,
                        member.filename,
                        report,
                        slot_suffixed=False,
                    )
                )
            report.extend(
                [
                    "",
                    "## Overall source coverage",
                    f"Detected boot-environment mounts: {sorted(total_mounts)}",
                    f"Missing core mounts (`system`, `vendor`): {sorted({'system', 'vendor'} - total_mounts)}",
                    f"Missing dynamic mounts (`system`, `system_ext`, `product`, `vendor`): "
                    f"{sorted({'system', 'system_ext', 'product', 'vendor'} - total_mounts)}",
                ]
            )
        report.append("\nInspection completed. Treat the report as source-layout evidence only; it is not a boot test.")
        report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"[inspect] Wrote source fstab report: {report_path}")
        print("[inspect] Detected boot-environment mounts:", sorted(total_mounts))
    except Exception as exc:
        report.extend(["", "## INSPECTION ERROR", f"`{type(exc).__name__}: {exc}`"])
        report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"[inspect] Partial report written: {report_path}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
