# 🎢 Rolleracoaster Hypos 4.0.1.7 — Automated Custom ROM Builder (`joyeuse`)

Pipeline otomatis berbasis GitHub Actions untuk membangun, mengoptimalkan, dan mempublikasikan ROM **Rolleracoaster Hypos 4.0.1.7** (berbasis **HyperOS 4.0.17.0 / Android 17**) secara khusus untuk **Xiaomi Redmi Note 9 Pro (`joyeuse` / `miatoll`)**.

- **Nama Versi ROM:** `Rolleracoaster Hypos 4.0.1.7`
- **Perangkat Target:** Redmi Note 9 Pro (`joyeuse`)
- **Pembuat / Maintainer:** **Graziemille**
- **Dukungan Telegram:** [**@Grazie_milles**](https://t.me/Grazie_milles)

---

## ✨ Arsitektur & Fitur Utama

1. **Integrasi Kernel `Rollercoaster-Roll.3.3` (`Linux 4.14.370`)**
   - Patching offline otomatis `boot.img` (partisi 128 MB `miatoll`) menggunakan `magiskboot` dengan kernel `Rollercoaster-Roll.3.3.zip` (`prebuilt/kernel/`), mempertahankan DTB asli ROM bila kompatibel.
   - Terintegrasi **KernelSU-Next v3.4.0 (`33294`)**, **SuSFS v2.3.0**, **NoMount VFS**, dan **KSUWebUI**.
   - Seluruh partisi **EROFS** dikemas ulang menggunakan **`mkfs.erofs -zlz4hc,9`** (tanpa LZMA) agar 100% kompatibel dengan konfigurasi kernel `Roll.3.3`.

2. **Root Stealth, SuSFS & Play Store Integrity**
   - Auto-provisioning `/data/adb/ksud` beserta modul `rollercoaster_core`, `susfs4ksu`, `PlayIntegrityFork`, `TrickyStore`, dan `PropsHook` saat boot pertama.
   - Spoofing status Bootloader Locked (`green` / `locked`) aktif otomatis.

3. **Optimasi Hardware Spesifik `joyeuse`**
   - **2D Face Unlock** & **Side-Mounted Fingerprint** (`is_suppot_finger_fod=false`, `is_SideFingerprint=true`).
   - **NFC (`NQNfcNci` + HCE/UICC/eSE)** khusus Redmi Note 9 Pro (`joyeuse`), **30W Mi Turbo Charge**, **Jack Audio 3.5mm**, & **IR Blaster**.
   - Unlock **VoLTE / VoWiFi Operator Indonesia** + konfigurasi GPS cepat `id.pool.ntp.org`.

4. **Multimedia, Tampilan & Aplikasi Bawaan**
   - **Dolby Atmos (Cruecy + Lunaris UI Bahasa Indonesia)** + **Google Spatial Audio** anti-konflik EQ.
   - **Xiaomi Leica Camera v6.8** untuk sensor 64MP `joyeuse`.
   - **Bootanimation Custom 60 FPS (`1080x2400`, `ZIP_STORED`)**: Huruf **`Rollercoaster`** jatuh dari atas, menyatu dengan **`OS`**, dan berkilau penuh warna dengan efek **Neon Glow**.
   - Halaman **Tentang Ponsel -> Support** menampilkan pembuat **Graziemille** beserta foto profil & ikon Telegram `@Grazie_milles`.
   - **Bahasa Indonesia (`id-ID`)** default, **Aplikasi Tema Xiaomi Global**, serta **Tema iOS Liquid Glass (`Rollercoaster_iOS_Pro.mtz`)** tersedia offline.
   - **Debloat CN & Ad-Blocker Xiaomi**, **Google Play Ready**, **YouTube ReVanced & YT Music ReVanced** (terintegrasi KSU/NoMount tanpa MicroG), **AdAway**, dan **Sandbox AOSP (Work Profile)** via **Insular**.

5. **Distribusi Ganda Otomatis**
   - **GoFile:** Mengunggah 1 file ZIP penuh siap flash.
   - **GitHub Releases:** Mengunggah file terpecah `< 2 GB` (`.001`, `.002`, dst.) beserta `SHA256SUMS.txt` dan `build_manifest.json`.
