#!/usr/bin/env python3
"""Comprehensive local & CI pre-flight test suite for Rolleracoaster Hypos 4.0.1.7 builder."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_rom import (
    debloat_cn_packages,
    merge_dolby_and_spatial_audio_effects,
    patch_build_prop_file,
    patch_device_features_for_joyeuse,
    patch_gps_indonesia,
    patch_vendor_fstabs_strip_avb,
    safe_copy,
    safe_remove,
    seed_ksu_and_modules,
)
from generate_ios_theme import generate_mtz
from patch_boot_kernel import disable_vbmeta_verification, sha256_file
from rom_container import (
    EROFS_MAGIC,
    EXT4_MAGIC,
    SPARSE_MAGIC,
    detect_fs_type,
    erofs_uses_non_lz4_compression,
    escape_fc_regex,
    sdat2img,
    sparse_img_to_sdat,
    sync_fs_config_and_contexts,
)
from upload_release import render_release_notes, split_file_for_github


class TestRollercoasterPipeline(unittest.TestCase):
    def test_01_config_and_prebuilt_kernel(self) -> None:
        cfg_path = REPO_ROOT / "config" / "rollercoaster_joyeuse.json"
        self.assertTrue(cfg_path.is_file(), "Missing config/rollercoaster_joyeuse.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

        self.assertEqual(cfg["rom"]["version_display"], "Rolleracoaster Hypos 4.0.1.7")
        self.assertEqual(cfg["rom"]["maintainer"], "Graziemille")
        self.assertEqual(cfg["rom"]["telegram_username"], "@Grazie_milles")
        self.assertEqual(cfg["rom"]["device"], "joyeuse")

        sys_props = cfg["props"]["system"]
        prod_props = cfg["props"]["product"]
        self.assertEqual(sys_props["ro.build.display.id"], "Rolleracoaster Hypos 4.0.1.7")
        self.assertEqual(sys_props["ro.mi.os.version.show"], "Rolleracoaster Hypos 4.0.1.7")
        self.assertEqual(sys_props["persist.sys.locale"], "id-ID")
        self.assertEqual(sys_props["persist.sys.timezone"], "Asia/Jakarta")
        self.assertIn("IS_INTERNATIONAL_BUILD:true", prod_props["persist.prophook.com.android.thememanager"])
        self.assertIn("IS_INTERNATIONAL_BUILD:true", prod_props["persist.prophook.com.miui.securitycenter"])

        kzip = REPO_ROOT / "prebuilt" / "kernel" / cfg["sources"]["kernel_zip_name"]
        self.assertTrue(kzip.is_file(), f"Missing {kzip}")
        self.assertEqual(sha256_file(kzip), cfg["sources"]["kernel_zip_sha256"])

        with zipfile.ZipFile(kzip, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("Image.gz", names)
            self.assertIn("anykernel.sh", names)
            self.assertTrue(any(n.endswith("ksud") for n in names))

    def test_02_sparse_sdat_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            blk_sz = 4096
            b0 = b"A" * blk_sz
            b2_val = 0x12345678
            b2 = struct.pack("<I", b2_val) * (blk_sz // 4)
            b3 = b"Z" * blk_sz

            c0 = struct.pack("<HHII", 0xCAC1, 0, 1, 12 + blk_sz) + b0
            c1 = struct.pack("<HHII", 0xCAC3, 0, 1, 12)
            c2 = struct.pack("<HHII", 0xCAC2, 0, 1, 16) + struct.pack("<I", b2_val)
            c3 = struct.pack("<HHII", 0xCAC1, 0, 1, 12 + blk_sz) + b3

            hdr = struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12, blk_sz, 4, 4, 0)
            sparse_path = tdir / "test.sparse.img"
            sparse_path.write_bytes(hdr + c0 + c1 + c2 + c3)

            out_prefix = tdir / "system"
            sparse_img_to_sdat(sparse_path, out_prefix)
            reconstructed = tdir / "reconstructed.raw.img"
            sdat2img(tdir / "system.transfer.list", tdir / "system.new.dat", reconstructed)

            expected = b0 + (b"\x00" * blk_sz) + b2 + b3
            self.assertEqual(reconstructed.read_bytes(), expected)

    def test_03_fs_detection_and_erofs_lzma_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            erofs_img = tdir / "part_erofs.img"
            buf = bytearray(4096)
            struct.pack_into("<I", buf, 1024, EROFS_MAGIC)
            struct.pack_into("<H", buf, 1024 + 0x42, 0x01)
            erofs_img.write_bytes(buf)
            self.assertEqual(detect_fs_type(erofs_img), "erofs")
            self.assertFalse(erofs_uses_non_lz4_compression(erofs_img))

            struct.pack_into("<H", buf, 1024 + 0x42, 0x02)
            erofs_img.write_bytes(buf)
            self.assertTrue(erofs_uses_non_lz4_compression(erofs_img))

            ext4_img = tdir / "part_ext4.img"
            buf2 = bytearray(4096)
            struct.pack_into("<H", buf2, 1024 + 0x38, EXT4_MAGIC)
            ext4_img.write_bytes(buf2)
            self.assertEqual(detect_fs_type(ext4_img), "ext4")

    def test_04_fs_config_and_selinux_contexts_sync(self) -> None:
        self.assertEqual(
            escape_fc_regex("/product/priv-app/Camera+Pro(1)/Camera.apk"),
            r"/product/priv-app/Camera\+Pro\(1\)/Camera\.apk",
        )
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            part_dir = tdir / "product"
            (part_dir / "bin").mkdir(parents=True)
            (part_dir / "bin" / "helper.sh").write_text("#!/system/bin/sh\n", encoding="utf-8")
            (part_dir / "priv-app" / "RollercoasterSupport").mkdir(parents=True)
            (part_dir / "priv-app" / "RollercoasterSupport" / "RollercoasterSupport.apk").write_bytes(b"PK\x03\x04")

            fsc = tdir / "product_fs_config"
            fc = tdir / "product_file_contexts"
            fsc.write_text("/ 0 0 0755\nproduct 0 0 0755\n", encoding="utf-8")
            fc.write_text("/ u:object_r:system_file:s0\n/product(/.*)? u:object_r:system_file:s0\n", encoding="utf-8")

            sync_fs_config_and_contexts("product", part_dir, fsc, fc)
            self.assertIn("product/bin/helper.sh 0 2000 0755", fsc.read_text(encoding="utf-8"))
            self.assertIn(r"/product/priv-app/RollercoasterSupport/RollercoasterSupport\.apk u:object_r:system_file:s0", fc.read_text(encoding="utf-8"))

    def test_05_vbmeta_patcher(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vbm = Path(td) / "vbmeta.img"
            raw = bytearray(256)
            raw[:4] = b"AVB0"
            raw[123] = 0x00
            vbm.write_bytes(raw)
            self.assertTrue(disable_vbmeta_verification(vbm))
            self.assertEqual(vbm.read_bytes()[123], 0x03)

    def test_06_props_fstab_gps_device_features_and_dolby_xml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            vprop = tdir / "build.prop"
            vprop.write_text("ro.product.vendor.device=miatoll\nro.vendor.test=old\n", encoding="utf-8")
            patch_build_prop_file(
                vprop,
                {"ro.product.vendor.device": "joyeuse", "ro.vendor.test": "new"},
                preserve_keys={"ro.product.vendor.device"},
            )
            self.assertIn("ro.product.vendor.device=miatoll", vprop.read_text(encoding="utf-8"))

            vdir = tdir / "vendor"
            (vdir / "etc").mkdir(parents=True)
            fstab = vdir / "etc" / "fstab.qcom"
            fstab.write_text("system /system erofs ro wait,slotselect,avb=vbmeta_system,logical,first_stage_mount\n", encoding="utf-8")
            patch_vendor_fstabs_strip_avb(vdir)
            self.assertNotIn("avb", fstab.read_text(encoding="utf-8"))

            patch_gps_indonesia(vdir)
            self.assertIn("NTP_SERVER=id.pool.ntp.org", (vdir / "etc" / "gps.conf").read_text(encoding="utf-8"))

            pdir = tdir / "product"
            sdir = tdir / "system"
            df_dir = pdir / "etc" / "device_features"
            df_dir.mkdir(parents=True)
            (df_dir / "miatoll.xml").write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n<features>\n'
                '    <bool name="support_nfc">false</bool>\n'
                '    <bool name="is_suppot_finger_fod">true</bool>\n'
                '</features>\n',
                encoding="utf-8",
            )
            patch_device_features_for_joyeuse(pdir, sdir)
            jroot = ET.parse(df_dir / "joyeuse.xml").getroot()
            bools = {el.get("name"): el.text for el in jroot.findall("bool")}
            self.assertEqual(bools["support_nfc"], "true")
            self.assertEqual(bools["support_hce"], "true")
            self.assertEqual(bools["is_suppot_finger_fod"], "false")

            ae_xml = vdir / "etc" / "audio_effects.xml"
            ae_xml.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<audio_effects_conf version="2.0">\n'
                '    <libraries><library name="bundle" path="libbundlewrapper.so"/></libraries>\n'
                '    <effects><effect name="bassboost" library="bundle" uuid="8631f300-72e2-11df-b57e-0002a5d5c51b"/></effects>\n'
                '    <postprocess><stream type="music"><apply effect="dlb_music_listener"/></stream></postprocess>\n'
                '</audio_effects_conf>\n',
                encoding="utf-8",
            )
            self.assertTrue(merge_dolby_and_spatial_audio_effects(ae_xml))
            self.assertNotIn('effect="dlb_music_listener"', ae_xml.read_text(encoding="utf-8"))

    def test_07_debloat_guard_and_ksu_seed_and_mtz_and_split(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            pdir = tdir / "product"
            for pkg in ("SogouInput", "MSA", "AnalyticsCore", "MIUIThemeManager", "NQNfcNci"):
                (pdir / "app" / pkg).mkdir(parents=True)
                (pdir / "app" / pkg / f"{pkg}.apk").write_bytes(b"apk")

            cfg = json.loads((REPO_ROOT / "config" / "rollercoaster_joyeuse.json").read_text(encoding="utf-8"))
            removed = debloat_cn_packages([pdir], cfg["debloat_dirs"], cfg["keep_packages_guard"])
            self.assertEqual(len(removed), 3)
            self.assertTrue((pdir / "app" / "MIUIThemeManager").is_dir())

            mtz_out = tdir / "Rollercoaster_iOS_Pro.mtz"
            generate_mtz(mtz_out)
            with zipfile.ZipFile(mtz_out, "r") as zf:
                self.assertIn("description.xml", zf.namelist())

    def test_08_symlink_and_readonly_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            ro_pkg = tdir / "product" / "app" / "SogouInput"
            ro_pkg.mkdir(parents=True)
            ro_apk = ro_pkg / "SogouInput.apk"
            ro_apk.write_bytes(b"readonly")
            os.chmod(ro_apk, 0o444)
            os.chmod(ro_pkg, 0o555)
            safe_remove(ro_pkg)
            self.assertFalse(ro_pkg.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
