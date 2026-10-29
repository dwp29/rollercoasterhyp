#!/sbin/sh
# Best-effort, read-only collection of persistent Android and current recovery logs.
# RCBL_ROOT and RCBL_OUTPUT_DIR are test hooks; normal recovery use leaves them unset.

OUTFD="$1"
ROOT="${RCBL_ROOT:-/}"
case "$ROOT" in
    /) ROOT_PREFIX="" ;;
    *) ROOT_PREFIX="${ROOT%/}" ;;
esac
umask 022

root_path() {
    case "$1" in
        /*) printf '%s%s' "$ROOT_PREFIX" "$1" ;;
        *)  printf '%s/%s' "$ROOT_PREFIX" "$1" ;;
    esac
}

ui_print() {
    message="$1"
    case "$OUTFD" in
        ''|*[!0-9]*) printf '%s\n' "$message"; return ;;
    esac
    if [ -e "/proc/self/fd/$OUTFD" ] && printf 'ui_print %s\nui_print\n' "$message" > "/proc/self/fd/$OUTFD" 2>/dev/null; then
        return
    fi
    printf '%s\n' "$message"
}

PROC_MOUNTS="$(root_path /proc/mounts)"
STAMP="$(date '+%Y%m%d-%H%M%S' 2>/dev/null)"
[ -n "$STAMP" ] || STAMP="unknown-time"
BASE_DIR=""
TEMPORARY_OUTPUT=0

mount_fs() {
    mountpoint="$1"
    [ -r "$PROC_MOUNTS" ] || return 0
    awk -v point="$mountpoint" '$2 == point { fs = $3 } END { print fs }' "$PROC_MOUNTS" 2>/dev/null
}

is_persistent_fs() {
    case "$1" in
        ''|tmpfs|rootfs|ramfs|proc|sysfs|devtmpfs|debugfs|tracefs)
            return 1
            ;;
        *)
            return 0
            ;;
    esac
}

probe_storage() {
    mountpoint="$1"
    storage_path="$2"
    fs_type="$(mount_fs "$mountpoint")"
    is_persistent_fs "$fs_type" || return 1

    candidate="$(root_path "$storage_path")/BootloopLogs"
    mkdir -p "$candidate" 2>/dev/null || return 1
    probe="$candidate/.rcbl-write-test.$$"
    : > "$probe" 2>/dev/null || return 1
    rm -f "$probe" 2>/dev/null
    BASE_DIR="$candidate"
    return 0
}

if [ -n "${RCBL_OUTPUT_DIR:-}" ]; then
    OUT_DIR="$RCBL_OUTPUT_DIR"
    mkdir -p "$OUT_DIR" 2>/dev/null || {
        ui_print "ERROR: Could not create the requested output directory."
        exit 1
    }
else
    # Prefer decrypted internal shared storage, then removable/USB storage.
    if probe_storage /data /data/media/0; then
        :
    elif probe_storage /sdcard /sdcard; then
        :
    else
        for mountpoint in /external_sd /external_sdcard /sdcard1 /usb-otg /usb_otg; do
            if probe_storage "$mountpoint" "$mountpoint"; then
                break
            fi
        done
    fi

    if [ -z "$BASE_DIR" ] && [ -r "$PROC_MOUNTS" ]; then
        for mountpoint in $(awk '$2 ~ /^\/storage\// || $2 ~ /^\/mnt\/media_rw\// { print $2 }' "$PROC_MOUNTS" 2>/dev/null); do
            if probe_storage "$mountpoint" "$mountpoint"; then
                break
            fi
        done
    fi

    if [ -n "$BASE_DIR" ]; then
        OUT_DIR="$BASE_DIR/bootloop-$STAMP-$$"
    else
        # Recovery RAM is only a last resort. Pull this directory with ADB before rebooting.
        OUT_DIR="$(root_path /tmp)/BootloopLogs/bootloop-$STAMP-$$"
        TEMPORARY_OUTPUT=1
        mkdir -p "$OUT_DIR" 2>/dev/null || {
            ui_print "ERROR: No writable storage was found, including recovery /tmp."
            exit 1
        }
    fi
fi

chmod 0755 "$OUT_DIR" 2>/dev/null
WARNINGS="$OUT_DIR/collection-warnings.txt"
COPIED=0

copy_file() {
    source_path="$(root_path "$1")"
    relative_path="$2"
    [ -r "$source_path" ] || return 1
    destination="$OUT_DIR/$relative_path"
    destination_dir="${destination%/*}"
    mkdir -p "$destination_dir" 2>/dev/null || return 1
    if cp "$source_path" "$destination" 2>> "$WARNINGS"; then
        chmod 0644 "$destination" 2>/dev/null
        COPIED=$((COPIED + 1))
        return 0
    fi
    return 1
}

copy_tree() {
    source_path="$(root_path "$1")"
    relative_path="$2"
    [ -d "$source_path" ] && [ -r "$source_path" ] || return 1
    destination="$OUT_DIR/$relative_path"
    mkdir -p "$destination" 2>/dev/null || return 1
    if cp -R "$source_path"/. "$destination"/ 2>> "$WARNINGS"; then
        COPIED=$((COPIED + 1))
        return 0
    fi
    return 1
}

copy_recent_entries() {
    source_path="$(root_path "$1")"
    relative_path="$2"
    [ -d "$source_path" ] && [ -r "$source_path" ] || return 1
    destination="$OUT_DIR/$relative_path"
    mkdir -p "$destination" 2>/dev/null || return 1
    if command -v head >/dev/null 2>&1; then
        entries="$(ls -1t "$source_path" 2>/dev/null | head -n 40)"
    else
        entries="$(ls -1 "$source_path" 2>/dev/null)"
    fi
    for entry in $entries; do
        case "$entry" in
            *[!A-Za-z0-9_.-]*) continue ;;
        esac
        [ -e "$source_path/$entry" ] || continue
        if cp -R "$source_path/$entry" "$destination"/ 2>> "$WARNINGS"; then
            COPIED=$((COPIED + 1))
        fi
    done
    return 0
}

# Persistent kernel panic/console records, if the kernel and device expose them.
copy_tree /sys/fs/pstore kernel/pstore
copy_file /proc/last_kmsg kernel/last_kmsg
copy_file /sys/fs/pstore/console-ramoops kernel/console-ramoops
copy_file /sys/fs/pstore/dmesg-ramoops kernel/dmesg-ramoops

# Recovery logs from the current recovery session and prior recovery installs.
copy_file /tmp/recovery.log recovery/recovery.log
copy_file /cache/recovery/last_log recovery/last_log
copy_file /cache/recovery/log recovery/log
copy_file /cache/recovery/last_install recovery/last_install
copy_file /cache/recovery/last_kmsg recovery/last_kmsg

# Crash artifacts that may have been persisted by the failed Android boot.
copy_tree /data/tombstones android/tombstones
copy_tree /data/vendor/tombstones android/vendor_tombstones
copy_tree /data/anr android/anr
copy_recent_entries /data/system/dropbox android/dropbox_recent
copy_tree /data/misc/logd android/logd_persistent

# Current recovery context only; these are not represented as logs from the failed boot.
copy_file /proc/cmdline recovery_context/cmdline.txt
copy_file /proc/bootconfig recovery_context/bootconfig.txt
copy_file /proc/partitions recovery_context/partitions.txt
copy_file /proc/mounts recovery_context/mounts.txt
copy_file /proc/meminfo recovery_context/meminfo.txt
copy_file /proc/uptime recovery_context/uptime.txt
copy_file /proc/version recovery_context/kernel_version.txt

if command -v getprop >/dev/null 2>&1; then
    properties="$OUT_DIR/recovery_context/properties.txt"
    mkdir -p "${properties%/*}" 2>/dev/null
    {
        for property in \
            ro.product.device ro.product.vendor.device ro.build.product \
            ro.build.fingerprint ro.build.version.release ro.build.version.sdk \
            ro.hardware ro.board.platform ro.boot.slot_suffix \
            ro.boot.dynamic_partitions ro.boot.verifiedbootstate \
            ro.boot.veritymode ro.boot.selinux ro.boot.bootreason \
            ro.bootmode ro.crypto.state ro.crypto.type; do
            printf '%s=' "$property"
            getprop "$property" 2>/dev/null
        done
    } > "$properties" 2>> "$WARNINGS"
    chmod 0644 "$properties" 2>/dev/null
    COPIED=$((COPIED + 1))
else
    printf 'getprop command is unavailable in this recovery.\n' > "$OUT_DIR/recovery_context/properties.txt"
fi

if command -v dmesg >/dev/null 2>&1; then
    dmesg > "$OUT_DIR/recovery_context/current_recovery_dmesg.txt" 2>> "$WARNINGS"
    [ -s "$OUT_DIR/recovery_context/current_recovery_dmesg.txt" ] && COPIED=$((COPIED + 1))
fi

if command -v logcat >/dev/null 2>&1; then
    logcat -b all -d -v threadtime > "$OUT_DIR/recovery_context/current_recovery_logcat.txt" 2>> "$WARNINGS"
    [ -s "$OUT_DIR/recovery_context/current_recovery_logcat.txt" ] && COPIED=$((COPIED + 1))
fi

# Inventory potentially useful large modem/SoC dumps without copying them wholesale.
mkdir -p "$OUT_DIR/index" 2>/dev/null
inventory="$OUT_DIR/index/vendor-dump-inventory.txt"
: > "$inventory"
for item in /data/vendor/ramdump /data/vendor/ssrdump /data/vendor/ssr /data/vendor/log; do
    source_path="$(root_path "$item")"
    if [ -d "$source_path" ]; then
        printf '\n[%s]\n' "$item" >> "$inventory"
        ls -la "$source_path" >> "$inventory" 2>&1
    else
        printf '\n[%s] unavailable\n' "$item" >> "$inventory"
    fi
done

cat > "$OUT_DIR/README.txt" <<'EOF'
Rollercoaster Bootloop Log Collector 1.0

This package copies only readable diagnostic records and recovery context.
It does not flash or modify boot, system, vendor, super, metadata, or userdata.

Files under kernel/ (pstore or last_kmsg), android/ (tombstones, ANR, DropBox),
and recovery/ may contain useful persistent evidence. Files under
recovery_context/ describe the recovery session, not necessarily the failed
Android boot.

Important limitation: ordinary Android logcat and RAM-only kernel dmesg are
lost on reboot unless the system/kernel persisted them. Previous-boot kernel
logs are available only if this device/kernel exposed pstore/ramoops or
/proc/last_kmsg. An encrypted/unmounted /data partition will not be collected.

Logs can contain private data such as app/process names, paths, or device
properties. Review them before sharing publicly. Large modem/SoC dump folders
are listed in index/vendor-dump-inventory.txt but are not copied.
EOF

{
    printf 'collector_version=1.0\n'
    printf 'collection_time=%s\n' "$STAMP"
    printf 'output_directory=%s\n' "$OUT_DIR"
    printf 'output_is_temporary=%s\n' "$TEMPORARY_OUTPUT"
    printf 'files_or_directories_copied=%s\n' "$COPIED"
    printf 'data_mount_filesystem=%s\n' "$(mount_fs /data)"
    printf 'note=Android logcat and RAM-only dmesg from a previous boot are not recoverable unless persisted.\n'
} > "$OUT_DIR/collection-info.txt"

if [ ! -s "$WARNINGS" ]; then
    rm -f "$WARNINGS" 2>/dev/null
fi

if [ "$TEMPORARY_OUTPUT" -eq 1 ]; then
    ui_print "No writable persistent storage was detected; logs are temporarily in: $OUT_DIR"
    ui_print "Keep recovery running and copy them with: adb pull $OUT_DIR"
else
    ui_print "Logs saved to: $OUT_DIR"
    ui_print "Copy the folder with MTP or: adb pull $OUT_DIR"
fi

if [ -d "$(root_path /sys/fs/pstore)" ] && [ -n "$(ls -A "$(root_path /sys/fs/pstore)" 2>/dev/null)" ]; then
    ui_print "Persistent pstore/ramoops records were found if readable; inspect kernel/pstore/."
else
    ui_print "No pstore records were visible. Prior-boot kernel logs may not be available on this kernel/recovery."
fi
ui_print "Collection complete. Review private data before sharing these logs."
exit 0
