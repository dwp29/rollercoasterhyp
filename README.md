# 🎢 Rollercoaster Hypos 4.0.1.7 — ROM builder (`joyeuse`)

Proyek ini menyiapkan ROM untuk **Xiaomi Redmi Note 9 Pro (`joyeuse`, keluarga `miatoll`)**, dengan target basis Xiaomi HyperOS `4.0.17.0` / Android 17. Nilai tersebut adalah target yang ditulis di konfigurasi; kompatibilitas akhir tetap harus dipastikan terhadap arsip ROM sumber dan perangkat nyata.

- **Maintainer:** Graziemille — [@Grazie_milles](https://t.me/Grazie_milles)
- **Konfigurasi:** `config/rollercoaster_joyeuse.json`
- **Kernel:** `prebuilt/kernel/Rollercoaster-Roll.3.3.zip`

## Integrasi yang disiapkan

- Mengganti kernel di `boot.img` menggunakan `magiskboot`, memeriksa checksum kernel, batas ukuran partisi boot 128 MiB, dan hasil round-trip. Metadata `vbmeta` yang ditemukan dipatch untuk menonaktifkan verifikasi/hash-tree.
- Mengubah partisi dinamis `system`, `product`, `system_ext`, dan `vendor` menjadi EROFS terkompresi LZ4HC (`mkfs.erofs -zlz4hc,9`) untuk kernel yang dipin. Sebelum repack, pipeline memeriksa dukungan EROFS/LZ4 pada kernel, mengonversi fstab di pohon partisi serta ramdisk/DTB gambar boot yang ditemukan, dan memverifikasi round-trip; filesystem/fstab yang tidak cocok atau mount inti yang tidak tercakup ditolak. Metadata `super.img` divalidasi mengikuti AOSP liblp v10—termasuk checksum SHA-256, batas tabel, kapasitas perangkat, dan slot—dan batas grup dihitung dari geometri super sumber yang benar-benar dipakai. Guard kapasitas tetap keras; pipeline tidak membesarkan `super.img` dan menolak layout A/B/Virtual A/B.
- Mengintegrasikan PropsHook ke `framework.jar` dengan hook pada `Instrumentation`, tanpa menerapkan patch signature-bypass dari proyek port sumber.
- Menyiapkan modul KernelSU melalui `ksud`. Modul yang sudah ada dengan ID sama tidak ditimpa. Pengaktifan modul dan layanan root tetap perlu dicek pada perangkat.
- Menambal properti dan file fitur khusus `joyeuse`, GPS Indonesia, serta daftar aplikasi CN. Perubahan flag fitur tidak membuktikan bahwa NFC, face unlock, VoLTE/VoWiFi, atau fungsi hardware lain bekerja.
- Menggabungkan audio effects/codec Dolby dan spatializer dari sumber yang dipin, membangun DAX UI Bahasa Indonesia, serta menyusun APK Leica Camera v6.8 dari potongan sumber. Policy HIDL DMS yang sempit ditambahkan lewat KernelSU sebelum kelas HAL dimulai; layanan DMS tetap dinonaktifkan jika policy atau label runtime tidak tervalidasi. Tidak ada domain SELinux yang dibuat permissive. Integrasi file tidak menjamin HAL, izin, codec, kamera, atau efek audio berfungsi di perangkat.
- Menyertakan Rollercoaster Support/overlay, tema MTZ, bootanimation, KernelSU Manager/WebUI, AdAway, HeliBoard, Insular, serta modul ReVanced yang diperiksa terhadap checksum dan payload MicroG.

## Mengumpulkan log setelah bootloop

ZIP recovery `recovery/Rollercoaster-Bootloop-Logger-1.0-joyeuse.zip` mengumpulkan pstore/ramoops dan `/proc/last_kmsg` jika tersedia, tombstone/ANR/DropBox yang dapat dibaca dari `/data`, serta log dan konteks recovery. ZIP ini hanya menulis folder hasil log; tidak mem-flash atau mengubah partisi ROM.

Cara pakai: boot ke TWRP/OrangeFox setelah bootloop, jangan wipe sebelum mengumpulkan log, lalu flash ZIP tersebut. Jika recovery meminta dekripsi `/data`, buka kunci dahulu agar log aplikasi dapat dibaca. Hasil biasanya berada di `BootloopLogs/bootloop-...`; salin melalui MTP atau `adb pull`. Jika hasil sementara ditulis ke `/tmp/BootloopLogs`, ambil dengan ADB sebelum keluar dari recovery.

**Batasan:** Android logcat biasa dan dmesg yang hanya berada di RAM hilang saat reboot. Log dari boot gagal hanya dapat diambil bila kernel/perangkat menyimpannya ke pstore/ramoops atau menyediakan `/proc/last_kmsg`. Log dapat berisi informasi privat; tinjau sebelum dibagikan. ZIP belum diuji pada recovery fisik joyeuse dan harus digunakan melalui TWRP/OrangeFox, bukan fastboot.

## Verifikasi dan build

Push perubahan hanya menjalankan pemeriksaan sintaks dan rangkaian tes offline. Sebelum build penuh, jalankan inspeksi sumber melalui GitHub Actions (`workflow_dispatch`) dengan `run_full_build=false` dan `publish_release=false`; artifact `source-fstab-inspection` memuat fstab ramdisk/DTB ROM sumber tanpa repack atau publikasi. Build ROM lengkap tetap manual, dan `publish_release` hanya boleh diaktifkan setelah build serta pemeriksaan hasil dinilai aman.

Pre-flight dapat dijalankan dengan Python 3 dan Pillow:

```sh
python -m py_compile scripts/*.py
python scripts/test_pipeline.py
```

Build lengkap memerlukan JDK 21, Android SDK API 35/build-tools 35, `erofs-utils`, `android-sdk-libsparse-utils`, otatools, arsip ROM dasar yang SHA-256-nya cocok dengan konfigurasi, akses unduhan sumber yang dipin, serta ruang disk yang cukup. Hasil build menghasilkan ZIP ROM dan `build_manifest.json`.

## Rilis

Jika publikasi dipilih, pipeline mengunggah satu ZIP lengkap ke GoFile, membagi aset GitHub Release menjadi potongan di bawah 1,9 GiB, dan menyertakan `SHA256SUMS.txt`, manifest, serta catatan rilis Bahasa Indonesia.

## Peringatan kompatibilitas

- Flash hanya pada `joyeuse`; jangan gunakan pada `curtana`, `gram`, `excalibur`, atau perangkat lain.
- ROM memodifikasi boot dan menonaktifkan pemeriksaan AVB pada metadata yang dipatch. Ikuti prosedur resmi untuk membuka bootloader, buat cadangan, dan pahami risiko kehilangan data/ketidakmampuan boot sebelum memasang.
- Pengubahan properti yang dilaporkan Android tidak mengunci bootloader atau memulihkan rantai kepercayaan kriptografis. **Play Integrity/attestation, boot, audio, kamera, dan fitur radio belum dijamin**; hasilnya bergantung pada perangkat, firmware, region, operator, serta layanan pihak ketiga.
- Manifest build mencatat bahwa pengujian perangkat fisik belum dilakukan kecuali statusnya secara eksplisit diubah setelah pengujian.
