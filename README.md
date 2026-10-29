# 🎢 Rollercoaster Hypos 4.0.1.7 — ROM builder (`joyeuse`)

Proyek ini menyiapkan ROM untuk **Xiaomi Redmi Note 9 Pro (`joyeuse`, keluarga `miatoll`)**, dengan target basis Xiaomi HyperOS `4.0.17.0` / Android 17. Nilai tersebut adalah target yang ditulis di konfigurasi; kompatibilitas akhir tetap harus dipastikan terhadap arsip ROM sumber dan perangkat nyata.

- **Maintainer:** Graziemille — [@Grazie_milles](https://t.me/Grazie_milles)
- **Konfigurasi:** `config/rollercoaster_joyeuse.json`
- **Kernel:** `prebuilt/kernel/Rollercoaster-Roll.3.3.zip`

## Integrasi yang disiapkan

- Mengganti kernel di `boot.img` menggunakan `magiskboot`, memeriksa checksum kernel, batas ukuran partisi boot 128 MiB, dan hasil round-trip. Metadata `vbmeta` yang ditemukan dipatch untuk menonaktifkan verifikasi/hash-tree.
- Mengemas ulang partisi EROFS dengan LZ4HC (`mkfs.erofs -zlz4hc,9`) agar sesuai dengan kernel yang dipilih. Metadata `super.img` divalidasi langsung mengikuti format AOSP liblp v10—termasuk checksum SHA-256 geometri, header, tabel, batas tabel, kapasitas perangkat, dan slot—untuk input raw/sparse serta hasil rebuild. Ini tidak mematikan pemeriksaan dan menghindari ketergantungan pada biner `lpdump` pihak ketiga. Ukuran `super.img` dan grup partisi dibatasi sesuai kapasitas joyeuse; pipeline menolak layout A/B.
- Mengintegrasikan PropsHook ke `framework.jar` dengan hook pada `Instrumentation`, tanpa menerapkan patch signature-bypass dari proyek port sumber.
- Menyiapkan modul KernelSU melalui `ksud`. Modul yang sudah ada dengan ID sama tidak ditimpa. Pengaktifan modul dan layanan root tetap perlu dicek pada perangkat.
- Menambal properti dan file fitur khusus `joyeuse`, GPS Indonesia, serta daftar aplikasi CN. Perubahan flag fitur tidak membuktikan bahwa NFC, face unlock, VoLTE/VoWiFi, atau fungsi hardware lain bekerja.
- Menggabungkan audio effects/codec Dolby dan spatializer dari sumber yang dipin, membangun DAX UI Bahasa Indonesia, serta menyusun APK Leica Camera v6.8 dari potongan sumber. Integrasi file tidak menjamin HAL, izin, codec, kamera, atau efek audio berfungsi di perangkat.
- Menyertakan Rollercoaster Support/overlay, tema MTZ, bootanimation, KernelSU Manager/WebUI, AdAway, HeliBoard, Insular, serta modul ReVanced yang diperiksa terhadap checksum dan payload MicroG.

## Verifikasi dan build

Push perubahan hanya menjalankan pemeriksaan sintaks dan rangkaian tes offline. Build ROM lengkap dijalankan secara manual melalui GitHub Actions (`workflow_dispatch`). Publikasi tidak berjalan otomatis: aktifkan `publish_release` hanya setelah build dan pemeriksaan hasil dinilai aman.

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
