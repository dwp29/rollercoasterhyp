#!/system/bin/sh
# Rollercoaster Hypos 4.0.1.7 - Early Boot Bootloader & VerifiedBoot Stealth Spoofer
MODDIR=${0%/*}

resetprop_if_exists() {
    PROP_NAME="$1"
    PROP_VAL="$2"
    if command -v resetprop >/dev/null 2>&1; then
        resetprop -n "$PROP_NAME" "$PROP_VAL" 2>/dev/null
    fi
}

# Spoof green / locked Verified Boot state for Play Integrity & Banking apps
resetprop_if_exists ro.boot.vbmeta.device_state locked
resetprop_if_exists ro.boot.verifiedbootstate green
resetprop_if_exists ro.boot.flash.locked 1
resetprop_if_exists ro.boot.veritymode enforcing
resetprop_if_exists ro.boot.warranty_bit 0
resetprop_if_exists ro.warranty_bit 0
resetprop_if_exists vendor.boot.vbmeta.device_state locked
resetprop_if_exists vendor.boot.verifiedbootstate green
resetprop_if_exists sys.oem_unlock_allowed 0
resetprop_if_exists ro.debuggable 0
resetprop_if_exists ro.secure 1
resetprop_if_exists ro.build.type user
resetprop_if_exists ro.build.tags release-keys

# Ensure TrickyStore target package list includes GMS, Play Store & common verifiers
mkdir -p /data/adb/tricky_store 2>/dev/null
if [ ! -f /data/adb/tricky_store/target.txt ]; then
    cat << 'EOF' > /data/adb/tricky_store/target.txt
com.google.android.gms!
com.android.vending!
io.github.vvb2060.keyattestation
io.github.vvb2060.mahoshojo
icu.nullptr.nativetest
EOF
    chmod 0644 /data/adb/tricky_store/target.txt
fi

# Configure SuSFS kernel stealth paths if ksu_susfs binary is available
for SUS_BIN in /data/adb/ksu/bin/ksu_susfs /system/bin/ksu_susfs; do
    if [ -x "$SUS_BIN" ]; then
        "$SUS_BIN" add_sus_path /system/addon.d >/dev/null 2>&1
        "$SUS_BIN" add_sus_path /sdcard/TWRP >/dev/null 2>&1
        "$SUS_BIN" add_sus_path /sdcard/OrangeFox >/dev/null 2>&1
        break
    fi
done
