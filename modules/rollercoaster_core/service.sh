#!/system/bin/sh
# Rollercoaster Hypos 4.0.1.7 - Late Boot Optimizer (joyeuse)
MODDIR=${0%/*}

while [ "$(getprop sys.boot_completed)" != "1" ]; do
    sleep 3
done
sleep 5

# 1. ZRAM 4GB LZ4 + VM tuning for Snapdragon 720G (6/8 GB RAM)
if [ -b /dev/block/zram0 ]; then
    CUR_SIZE=$(cat /sys/block/zram0/disksize 2>/dev/null || echo 0)
    if [ "$CUR_SIZE" -lt 4000000000 ]; then
        swapoff /dev/block/zram0 2>/dev/null
        echo 1 > /sys/block/zram0/reset 2>/dev/null
        echo lz4 > /sys/block/zram0/comp_algorithm 2>/dev/null
        echo 4294967296 > /sys/block/zram0/disksize 2>/dev/null
        mkswap /dev/block/zram0 >/dev/null 2>&1
        swapon /dev/block/zram0 -p 32758 >/dev/null 2>&1
    fi
fi
echo 100 > /proc/sys/vm/swappiness 2>/dev/null
echo 0 > /proc/sys/vm/page-cluster 2>/dev/null
echo 20 > /proc/sys/vm/dirty_ratio 2>/dev/null
echo 10 > /proc/sys/vm/dirty_background_ratio 2>/dev/null

# 2. Keep Google Play services and real-time notification settings available.
# Installer verification and confirmation prompts are intentionally left unchanged.
settings put system google_core_control 1 2>/dev/null
settings put system status_bar_show_notification_icon 1 2>/dev/null

for ovl in \
    id.rollercoaster.overlay.settings \
    id.rollercoaster.overlay.systemui \
    id.rollercoaster.overlay.thememanager; do
    cmd overlay enable "$ovl" >/dev/null 2>&1
done

for pkg in \
    com.google.android.gms \
    com.google.android.gsf \
    com.android.vending \
    com.whatsapp \
    com.whatsapp.w4b \
    org.telegram.messenger \
    org.telegram.messenger.web \
    com.google.android.gm \
    com.dolby.daxappui; do
    if pm path "$pkg" >/dev/null 2>&1; then
        dumpsys deviceidle whitelist +"$pkg" >/dev/null 2>&1
        cmd appops set "$pkg" RUN_IN_BACKGROUND allow >/dev/null 2>&1
        cmd appops set "$pkg" RUN_ANY_IN_BACKGROUND allow >/dev/null 2>&1
    fi
done

# 3. Disable Xiaomi built-in app ad recommendations settings flags
settings put system personalized_ad_enabled 0 2>/dev/null
settings put global passport_ad_status off 2>/dev/null

# 4. Seed offline iOS Liquid Glass .mtz into /sdcard/Download/Themes on first boot
if [ -f /system/rollercoaster/themes/Rollercoaster_iOS_Pro.mtz ]; then
    for _ in 1 2 3 4 5; do
        if [ -d /sdcard/Download ]; then
            mkdir -p /sdcard/Download/Themes 2>/dev/null
            if [ ! -f /sdcard/Download/Themes/Rollercoaster_iOS_Pro.mtz ]; then
                cp -f /system/rollercoaster/themes/Rollercoaster_iOS_Pro.mtz /sdcard/Download/Themes/Rollercoaster_iOS_Pro.mtz 2>/dev/null
            fi
            break
        fi
        sleep 3
    done
fi

# 5. Xiaomi Theme Manager custom/MTZ anti-revert persistence
if [ -d /data/system/theme_magic ]; then
    rm -rf /data/system/theme_magic/customized_icons_revert 2>/dev/null
fi

# 6. Auto-detach YouTube ReVanced & YouTube Music ReVanced from Play Store auto-updates
PS_DB="/data/data/com.android.vending/databases/library.db"
if [ -f "$PS_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$PS_DB" "DELETE FROM ownership WHERE doc_id IN ('com.google.android.youtube', 'com.google.android.apps.youtube.music');" 2>/dev/null
fi
