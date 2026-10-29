#!/usr/bin/env python3
"""Comprehensive local & CI pre-flight test suite for Rollercoaster Hypos 4.0.1.7 builder."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
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
    assemble_split_camera_apk,
    debloat_cn_packages,
    integrate_cruecy_dolby_and_spatial,
    integrate_leica_camera,
    merge_dolby_and_spatial_audio_effects,
    patch_build_prop_file,
    patch_device_features_for_joyeuse,
    patch_gps_indonesia,
    patch_props_hook_instrumentation_text,
    patch_vendor_fstabs_strip_avb,
    safe_copy,
    safe_remove,
    seed_ksu_and_modules,
    validate_dolby_selinux_policy,
    validate_module_zip,
    validate_props_hook_zip,
)
from build_support_and_overlays import DOLBY_ID_TRANSLATIONS, write_indonesian_dolby_strings
from generate_ios_theme import generate_mtz
from patch_boot_kernel import disable_vbmeta_verification, patch_boot_image, sha256_file
from rom_container import (
    EROFS_MAGIC,
    EXT4_MAGIC,
    MAX_SUPER_DEVICE_SIZE_BYTES,
    SPARSE_MAGIC,
    detect_fs_type,
    erofs_uses_non_lz4_compression,
    escape_fc_regex,
    parse_lpdump_geometry,
    sdat2img,
    sparse_img_to_sdat,
    sync_fs_config_and_contexts,
)
from upload_release import (
    CHUNK_SIZE_BYTES,
    MAX_REQUESTED_ASSET_BYTES,
    render_release_notes,
    split_file_for_github,
)


class TestRollercoasterPipeline(unittest.TestCase):
    def test_01_config_and_prebuilt_kernel(self) -> None:
        cfg_path = REPO_ROOT / "config" / "rollercoaster_joyeuse.json"
        self.assertTrue(cfg_path.is_file(), "Missing config/rollercoaster_joyeuse.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

        self.assertEqual(cfg["rom"]["version_display"], "Rollercoaster Hypos 4.0.1.7")
        self.assertEqual(cfg["rom"]["maintainer"], "Graziemille")
        self.assertEqual(cfg["rom"]["telegram_username"], "@Grazie_milles")
        self.assertEqual(cfg["rom"]["device"], "joyeuse")
        workflow_path = REPO_ROOT / ".github" / "workflows" / "build-rollercoaster-rom.yml"
        workflow_text = workflow_path.read_text(encoding="utf-8")
        self.assertEqual(
            workflow_text.count(cfg["rom"]["output_zip_name"]),
            2,
            "Workflow artifact and publisher paths must match the configured output ZIP name",
        )
        self.assertEqual(len(cfg["sources"]["cruecy_audio_effects_commit"]), 40)
        self.assertEqual(len(cfg["sources"]["hyperos_port_tools_commit"]), 40)
        self.assertEqual(len(cfg["sources"]["otatools_zip_sha256"]), 64)
        self.assertGreater(cfg["sources"]["ksu_webui_apk_size_bytes"], 0)
        self.assertGreater(cfg["sources"]["adaway_apk_size_bytes"], 0)

        sys_props = cfg["props"]["system"]
        prod_props = cfg["props"]["product"]
        self.assertEqual(sys_props["ro.build.display.id"], "Rollercoaster Hypos 4.0.1.7")
        self.assertEqual(sys_props["ro.mi.os.version.show"], "Rollercoaster Hypos 4.0.1.7")
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
            namespace = "http://schemas.android.com/audio/audio_effects_conf/v2_0"
            source_xml = tdir / "cruecy_audio_effects.xml"
            source_xml.write_text(
                f'''<?xml version="1.0" encoding="UTF-8"?>
<audio_effects_conf xmlns="{namespace}" version="2.0">
  <libraries>
    <library name="dap" path="libswdap.so"/>
    <library name="dvl" path="libdlbvol.so"/>
    <library name="spatializer" path="libspatialaudio.so"/>
  </libraries>
  <effects>
    <effect name="dap" library="dap" uuid="9d4921da-8225-4f29-aefa-39537a04bcaa"/>
    <effect name="dlb_music_listener" library="dvl" uuid="40f66c8b-5aa5-4345-8919-53ec431aaa98"/>
    <effect name="spatializer" library="spatializer" uuid="cc4677de-ff72-11eb-9a03-0242ac130003"/>
    <effect name="music_helper" library="bundle" uuid="08b8b058-0590-11e5-ac71-0025b32654a0"/>
  </effects>
  <postprocess><stream type="music"><apply effect="music_helper"/></stream></postprocess>
  <preprocess><stream type="voice_communication"><apply effect="aec"/></stream></preprocess>
</audio_effects_conf>
''',
                encoding="utf-8",
            )
            ae_xml.write_text(
                f'''<?xml version="1.0" encoding="UTF-8"?>
<audio_effects_conf xmlns="{namespace}" version="2.0">
  <libraries><library name="bundle" path="libbundlewrapper.so"/></libraries>
  <effects><effect name="bassboost" library="bundle" uuid="8631f300-72e2-11df-b57e-0002a5d5c51b"/></effects>
  <postprocess><stream type="music"><apply effect="dlb_music_listener"/></stream></postprocess>
</audio_effects_conf>
''',
                encoding="utf-8",
            )
            self.assertTrue(merge_dolby_and_spatial_audio_effects(ae_xml, source_xml))
            merged_text = ae_xml.read_text(encoding="utf-8")
            merged_root = ET.parse(ae_xml).getroot()
            self.assertNotIn('effect="dlb_music_listener"', merged_text)
            self.assertIn('name="spatializer"', merged_text)
            self.assertIsNotNone(merged_root.find(f"{{{namespace}}}preprocess"))

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


    def test_09_props_hook_smali_and_payload_validation(self) -> None:
        smali = """.method public newApplication(Ljava/lang/ClassLoader;Ljava/lang/String;Landroid/content/Context;)Landroid/app/Application;
    .registers 4
    invoke-virtual {v1, v3}, Landroid/app/Application;->attach(Landroid/content/Context;)V
    return-object v1
.end method
"""
        patched, count = patch_props_hook_instrumentation_text(smali)
        self.assertEqual(count, 1)
        self.assertIn(
            "invoke-static {v3}, Lcom/android/internal/util/PropsHookUtils;->setProps(Landroid/content/Context;)V",
            patched,
        )
        self.assertLess(patched.index("Application;->attach"), patched.index("PropsHookUtils;->setProps"))
        with self.assertRaises(RuntimeError):
            patch_props_hook_instrumentation_text(".method public missing()V\n.end method\n")

        with tempfile.TemporaryDirectory() as td:
            hook_zip = Path(td) / "PropsHook.zip"
            with zipfile.ZipFile(hook_zip, "w") as zf:
                zf.writestr("classes.dex", b"PropsHookUtils setProps")
            validate_props_hook_zip(hook_zip)

    def test_10_lpdump_geometry_is_capped_for_joyeuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            super_img = tdir / "super.img"
            super_img.write_bytes(b"super")
            lpdump = tdir / "lpdump"
            lpdump.write_text(
                "#!/bin/sh\ncat <<'EOF'\n"
                "Metadata max size: 65536 bytes\n"
                "Metadata slot count: 2\n"
                "Partition table:\n"
                "  Name: system\n"
                "  Group: qti_dynamic_partitions\n"
                "Group table:\n"
                "  Name: qti_dynamic_partitions\n"
                "  Maximum size: 99999999999 bytes\n"
                "Block device table:\n"
                "  Size: 99999999999 bytes\n"
                "EOF\n",
                encoding="utf-8",
            )
            lpdump.chmod(0o755)
            geometry = parse_lpdump_geometry(super_img, lpdump)
            self.assertEqual(geometry.dev_size, MAX_SUPER_DEVICE_SIZE_BYTES)
            self.assertLessEqual(geometry.groups["qti_dynamic_partitions"], MAX_SUPER_DEVICE_SIZE_BYTES - 4 * 1024 * 1024)
            self.assertFalse(geometry.is_ab)
            self.assertEqual(geometry.part_to_group["system"], "qti_dynamic_partitions")

    def test_11_split_camera_apk_and_device_integration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cam_vendor = root / "camera-vendor"
            cam_device = root / "camera-device"
            app_dir = cam_vendor / "proprietary" / "system" / "priv-app" / "MiuiCamera"
            app_dir.mkdir(parents=True)
            original_apk = root / "source.apk"
            with zipfile.ZipFile(original_apk, "w") as zf:
                zf.writestr("AndroidManifest.xml", b"test-manifest")
                zf.writestr("classes.dex", b"test-dex")
            apk_bytes = original_apk.read_bytes()
            midpoint = len(apk_bytes) // 2
            (app_dir / "MiuiCamera.apk.00").write_bytes(apk_bytes[:midpoint])
            (app_dir / "MiuiCamera.apk.01").write_bytes(apk_bytes[midpoint:])

            lib_dir = cam_vendor / "proprietary" / "system" / "lib64"
            lib_dir.mkdir(parents=True)
            camera_libs = (
                "libcamera_algoup_jni.xiaomi.so",
                "libcamera_mianode_jni.xiaomi.so",
                "libcamera_jpegutil_jni.xiaomi.so",
            )
            for lib in camera_libs:
                (lib_dir / lib).write_bytes(b"native-library-fixture")

            config_dir = cam_device / "configs"
            config_dir.mkdir(parents=True)
            for name in (
                "privapp-permissions-miuicamera.xml",
                "privapp-permissions-mimoji.xml",
                "miuicamera-hiddenapi-package-allowlist.xml",
            ):
                (config_dir / name).write_text("<config/>\n", encoding="utf-8")
            (config_dir / "public.libraries-xiaomi.txt").write_text(
                "libcamera_algoup_jni.xiaomi.so\n", encoding="utf-8"
            )

            assembled, count = assemble_split_camera_apk(cam_vendor, root / "assembled")
            self.assertEqual(count, 2)
            self.assertEqual(assembled.read_bytes(), apk_bytes)

            system = root / "system"
            product = root / "product"
            system_ext = root / "system_ext"
            for part in (system, product, system_ext):
                part.mkdir()
            result = integrate_leica_camera(
                cam_vendor,
                cam_device,
                system,
                product,
                system_ext,
                root / "camera-work",
            )
            installed = system / "priv-app" / "MiuiCamera" / "MiuiCamera.apk"
            self.assertTrue(installed.is_file())
            self.assertEqual(result["split_part_count"], 2)
            for lib in camera_libs:
                self.assertTrue((system / "priv-app" / "MiuiCamera" / "lib" / "arm64" / lib).is_symlink())
            self.assertTrue((system / "etc" / "permissions" / "privapp-permissions-miuicamera.xml").is_file())

    def test_12_seed_ksu_modules_through_ksud(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            system = root / "system"
            core = root / "core"
            core.mkdir()
            (core / "module.prop").write_text(
                "id=rollercoaster_core\nname=Rollercoaster Core\nversion=v1\nversionCode=1\n",
                encoding="utf-8",
            )
            (core / "service.sh").write_text("#!/system/bin/sh\nexit 0\n", encoding="utf-8")
            (core / "post-fs-data.sh").write_text("#!/system/bin/sh\nexit 0\n", encoding="utf-8")
            (core / "sepolicy.rule").write_text("# fixture\n", encoding="utf-8")
            (core / "system" / "etc").mkdir(parents=True)
            (core / "system" / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")

            module_zip = root / "sample_module.zip"
            with zipfile.ZipFile(module_zip, "w") as zf:
                zf.writestr(
                    "module.prop",
                    "id=sample_module\nname=Sample Module\nversion=v1\nversionCode=1\n",
                )
                zf.writestr("service.sh", "#!/system/bin/sh\nexit 0\n")
            self.assertEqual(validate_module_zip(module_zip)["id"], "sample_module")
            ksud = root / "ksud"
            ksud.write_bytes(b"fixture-ksud")

            metadata = seed_ksu_and_modules(system, ksud, core, [module_zip])
            self.assertEqual([item["id"] for item in metadata], ["rollercoaster_core", "sample_module"])
            seed_root = system / "etc" / "rollercoaster_seed"
            with zipfile.ZipFile(seed_root / "modules" / "00_rollercoaster_core.zip") as zf:
                self.assertIn("module.prop", zf.namelist())
                self.assertIn("service.sh", zf.namelist())
            script_path = system / "bin" / "rollercoaster_seed.sh"
            script = script_path.read_text(encoding="utf-8")
            subprocess.run(["sh", "-n", str(script_path)], check=True)
            self.assertIn('module install "$module_zip"', script)
            self.assertIn("Preserving existing module", script)
            self.assertIn("--late)", script)
            self.assertTrue((system / "etc" / "init" / "rollercoaster_seed.rc").is_file())

    def test_13_dolby_translation_coverage_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resources = Path(td)
            values = resources / "values"
            values.mkdir()
            root = ET.Element("resources")
            for name, translated in DOLBY_ID_TRANSLATIONS.items():
                ET.SubElement(root, "string", {"name": name}).text = translated
            ET.ElementTree(root).write(values / "strings.xml", encoding="utf-8", xml_declaration=True)
            self.assertEqual(write_indonesian_dolby_strings(resources), len(DOLBY_ID_TRANSLATIONS))
            for locale in ("values-in", "values-id"):
                output = ET.parse(resources / locale / "strings.xml").getroot()
                self.assertEqual(len(output.findall("string")), len(DOLBY_ID_TRANSLATIONS))

    def test_14_boot_kernel_patch_round_trip_with_magiskboot_fixture(self) -> None:
        cfg = json.loads((REPO_ROOT / "config" / "rollercoaster_joyeuse.json").read_text(encoding="utf-8"))
        kernel_zip = REPO_ROOT / "prebuilt" / "kernel" / cfg["sources"]["kernel_zip_name"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            magiskboot = root / "magiskboot"
            magiskboot.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, struct, sys\n"
                "args=sys.argv[1:]\n"
                "def pack(data): return b'FAKEBOOT' + struct.pack('<I', len(data)) + data\n"
                "def unpack(path):\n"
                " p=pathlib.Path(path); data=p.read_bytes()\n"
                " if data.startswith(b'FAKEBOOT') and len(data)>=12:\n"
                "  n=struct.unpack('<I',data[8:12])[0]; kernel=data[12:12+n]\n"
                " else: kernel=b'original-kernel'\n"
                " pathlib.Path('kernel').write_bytes(kernel)\n"
                " return\n"
                "if args[0]=='unpack': unpack(args[-1])\n"
                "elif args[0]=='repack': pathlib.Path(args[-1]).write_bytes(pack(pathlib.Path('kernel').read_bytes()))\n"
                "else: raise SystemExit(2)\n",
                encoding="utf-8",
            )
            magiskboot.chmod(0o755)
            original_boot = root / "boot.img"
            original_boot.write_bytes(b"fixture-boot")
            out_ksud = root / "out" / "ksud"
            patched_boot = root / "out" / "boot.img"
            patch_boot_image(
                magiskboot,
                original_boot,
                kernel_zip,
                cfg["sources"]["kernel_zip_sha256"],
                patched_boot,
                out_ksud,
                root / "work",
            )
            self.assertTrue(patched_boot.is_file())
            self.assertTrue(out_ksud.is_file())
            self.assertTrue(out_ksud.stat().st_mode & 0o111)
            self.assertIn(b"4.14.370-Rollercoaster-Roll.3.3", patched_boot.read_bytes())

    def test_15_release_notes_and_safe_part_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "rom.zip"
            source.write_bytes(bytes(range(256)) * 4)
            parts = split_file_for_github(source, root / "parts", chunk_bytes=300)
            self.assertGreater(len(parts), 1)
            self.assertLess(CHUNK_SIZE_BYTES, MAX_REQUESTED_ASSET_BYTES)
            self.assertTrue(all(part.stat().st_size < MAX_REQUESTED_ASSET_BYTES for part in parts))
            with self.assertRaises(ValueError):
                split_file_for_github(source, root / "bad-parts", chunk_bytes=MAX_REQUESTED_ASSET_BYTES)

            manifest = {
                "output_size_bytes": source.stat().st_size,
                "base_version": "OS4.0.17.0.XEOCNXM",
                "android_version": "17",
                "physical_device_tested": False,
                "play_integrity_result": "belum diuji; hasil attestation tidak dijamin",
                "integration": {
                    "leica_camera": {"apk_size_bytes": 1024 * 1024, "split_part_count": 3},
                    "dolby_spatial_audio": {"vendor_blob_count": 53},
                    "props_hook": {"patched_methods": 1},
                    "module_metadata": {
                        "susfs": {"name": "SuSFS"},
                        "play_integrity_fork": {"name": "Play Integrity Fork"},
                    },
                },
            }
            part_hashes = [
                {"name": part.name, "size_mib": part.stat().st_size / (1024 * 1024), "sha256": sha256_file(part)}
                for part in parts
            ]
            notes = render_release_notes(
                manifest,
                {"download_page": "https://gofile.example/download"},
                source.name,
                sha256_file(source),
                part_hashes,
            )
            self.assertIn("belum diuji pada perangkat fisik", notes)
            self.assertIn("belum diuji", notes)
            self.assertIn("di bawah 1,9 GiB", notes)
            self.assertIn("https://gofile.example/download", notes)

    def test_16_dolby_vendor_blob_and_config_integration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "vendor-dolby"
            blob_root = repo / "prebuilts" / "proprietary" / "vendor"
            for relative in (
                "lib64/soundfx/libswdap.so",
                "lib64/soundfx/libdlbvol.so",
                "lib64/soundfx/libswgamedap.so",
                "lib64/soundfx/libswvqe.so",
                "bin/hw/vendor.dolby.hardware.dms@2.0-service",
                "bin/hw/vendor.dolby.dms.service",
                "etc/init/dms-service.rc",
            ):
                file_path = blob_root / relative
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(b"dolby-fixture")
            for relative, content in (
                ("soundfx/dax-default.xml", "<dax/>\n"),
                ("media/media_codecs_dolby_audio.xml", "<MediaCodecs/>\n"),
                ("hidl/vendor.dolby.hardware.dms.xml", "<manifest/>\n"),
                ("hidl/vendor.dolby.media.c2@1.0-service.xml", "<manifest/>\n"),
                (
                    "DolbyDaxUI/permissions/privapp-permissions-com.dolby.daxappui.xml",
                    "<permissions/>\n",
                ),
                ("DolbyDaxUI/sysconfig/config-com.dolby.daxappui.xml", "<config/>\n"),
            ):
                file_path = repo / relative
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")

            vendor = root / "vendor"
            system_ext = root / "system_ext"
            vendor.mkdir()
            system_ext.mkdir()
            selinux_dir = vendor / "etc" / "selinux"
            selinux_dir.mkdir(parents=True)
            (selinux_dir / "precompiled_sepolicy").write_bytes(
                b"hal_dms_hwservice hal_dms_default hal_dms_default_exec "
                b"hal_dms_client hal_dms_server vendor_dolby_file"
            )
            (selinux_dir / "vendor_hwservice_contexts").write_text(
                "vendor.dolby.hardware.dms::IDms u:object_r:hal_dms_hwservice:s0" + chr(10),
                encoding="utf-8",
            )
            slash = chr(92)
            service_context = (
                "/(vendor|system/vendor)/bin/hw/vendor"
                + slash + ".dolby" + slash + ".hardware" + slash + ".dms@2"
                + slash + ".0-service u:object_r:hal_dms_default_exec:s0"
            )
            (selinux_dir / "vendor_file_contexts").write_text(
                "/data/vendor/dolby(/.*)? u:object_r:vendor_dolby_file:s0"
                + chr(10) + service_context + chr(10),
                encoding="utf-8",
            )
            self.assertIn("hal_dms_hwservice", validate_dolby_selinux_policy(vendor)["required_types"])
            spatial_lib = vendor / "lib64" / "soundfx" / "libspatialaudio.so"
            spatial_lib.parent.mkdir(parents=True)
            spatial_lib.write_bytes(b"spatializer-fixture")

            source_xml = root / "audio_effects.xml"
            source_xml.write_text(
                '''<?xml version="1.0"?>
<audio_effects_conf version="2.0">
  <libraries>
    <library name="dap" path="libswdap.so"/>
    <library name="dvl" path="libdlbvol.so"/>
    <library name="spatializer" path="libspatialaudio.so"/>
  </libraries>
  <effects>
    <effect name="dap" library="dap" uuid="9d4921da-8225-4f29-aefa-39537a04bcaa"/>
    <effect name="dlb_music_listener" library="dvl" uuid="40f66c8b-5aa5-4345-8919-53ec431aaa98"/>
    <effect name="spatializer" library="spatializer" uuid="cc4677de-ff72-11eb-9a03-0242ac130003"/>
  </effects>
</audio_effects_conf>
''',
                encoding="utf-8",
            )
            dax_apk = root / "DolbyDaxUI.apk"
            with zipfile.ZipFile(dax_apk, "w") as zf:
                zf.writestr("AndroidManifest.xml", b"manifest-fixture")
                zf.writestr("classes.dex", b"dex-fixture")

            result = integrate_cruecy_dolby_and_spatial(
                repo,
                vendor,
                system_ext,
                dax_apk,
                source_xml,
            )
            self.assertEqual(result["vendor_blob_count"], 5)
            self.assertIn("hal_dms_hwservice", result["selinux_policy_validation"]["required_types"])
            self.assertFalse((vendor / "bin" / "hw" / "vendor.dolby.dms.service").exists())
            self.assertFalse((vendor / "etc" / "init" / "dms-service.rc").exists())
            self.assertTrue((system_ext / "priv-app" / "DolbyDaxUI" / "DolbyDaxUI.apk").is_file())
            codec_config = ET.parse(vendor / "etc" / "media_codecs_vendor.xml").getroot()
            self.assertTrue(any(item.get("href") == "media_codecs_dolby_audio.xml" for item in codec_config))
            merged = ET.parse(vendor / "etc" / "audio_effects.xml").getroot()
            self.assertTrue(any(item.get("name") == "spatializer" for item in merged.iter()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
