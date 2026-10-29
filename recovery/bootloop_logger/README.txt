Rollercoaster Bootloop Log Collector 1.0 — joyeuse

TUJUAN
Mengumpulkan log yang masih tersedia setelah ROM gagal boot/bootloop. ZIP ini
hanya berjalan dari custom recovery dan tidak memasang kernel/ROM.

CARA PAKAI
1. Simpan ZIP ini di penyimpanan yang bisa diakses recovery, SD card, atau USB OTG.
2. Boot ke TWRP/OrangeFox setelah bootloop. Jangan wipe data/cache sebelum
   pengumpulan selesai. Jika recovery meminta dekripsi data, buka kunci data
   terlebih dahulu agar tombstone/ANR/DropBox dapat dibaca.
3. Pilih Install ZIP, pilih Rollercoaster-Bootloop-Logger-1.0-joyeuse.zip, lalu
   konfirmasi flash. Jika recovery menawarkan verifikasi signature, nonaktifkan;
   ZIP ini tidak ditandatangani dengan kunci recovery perangkat.
4. Hasil biasanya tersimpan di internal storage/BootloopLogs/bootloop-... .
   Salin folder itu ke komputer melalui MTP atau ADB, misalnya:
       adb pull /data/media/0/BootloopLogs/bootloop-... .
5. Jika tidak ada penyimpanan persisten yang bisa ditulis, hasil sementara berada
   di /tmp/BootloopLogs/bootloop-... . Salin segera sebelum keluar dari recovery:
       adb pull /tmp/BootloopLogs/bootloop-... .

APA YANG DIKUMPULKAN
- pstore/ramoops dan /proc/last_kmsg jika tersedia;
- tombstone, ANR, sebagian DropBox, serta logd yang masih dapat dibaca dari /data;
- log recovery (termasuk last_log/last_install jika tersedia);
- konteks recovery saat ini: cmdline, bootconfig, mounts, dmesg, getprop, dan
  logcat recovery jika perintahnya tersedia.

BATASAN PENTING
Logcat Android biasa dan dmesg yang hanya berada di RAM akan hilang saat reboot.
ZIP ini tidak dapat merekonstruksi log tersebut. Bukti kernel dari boot gagal
hanya dapat dikumpulkan jika kernel/perangkat menyimpan pstore/ramoops atau
/proc/last_kmsg. Jika /data terenkripsi dan belum dibuka, data tombstone/ANR/
DropBox tidak dapat dibaca.

ZIP ini tidak mengubah boot, system, vendor, super, metadata, atau userdata;
ZIP hanya menulis folder hasil log ke penyimpanan yang tersedia. Log dapat memuat
informasi privat—tinjau dulu sebelum dibagikan. Folder dump modem/SoC yang besar
hanya dicatat namanya, tidak disalin.

Gunakan TWRP/OrangeFox yang kompatibel dengan perangkat. ZIP ini belum diuji
pada recovery fisik joyeuse; tidak ada klaim bahwa semua recovery menerimanya.
