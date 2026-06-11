#!/usr/bin/env bash
# TrueNAS PREINIT script: activates memryx.raw sysext on every boot.
# Runs before middleware starts, so the MemryX MX3 device is ready before
# app containers (e.g., Frigate) launch.
#
# Stored on persistent pool; registered via midclt during install.
# Idempotent: safe to run on every boot.

set -euo pipefail

log() {
    echo "[memryx-preinit] $*"
    logger -t memryx-preinit "$*" 2>/dev/null || true
}

# --- Find persistent config via glob ---
# nullglob: if no pool matches, the loop body never runs (instead of
# iterating once with the literal glob string). Localized via subshell-free
# save/restore so the rest of the script keeps default globbing.
PERSIST_DIR=""
PERSIST_DIRS=()
shopt -s nullglob
for d in /mnt/*/.config/memryx; do
    [ -d "$d" ] && PERSIST_DIRS+=("$d")
done
shopt -u nullglob

if [ ${#PERSIST_DIRS[@]} -eq 0 ]; then
    log "No persistent config found at /mnt/*/.config/memryx/, nothing to do"
    exit 0
fi
if [ ${#PERSIST_DIRS[@]} -gt 1 ]; then
    log "WARNING: memryx config found on ${#PERSIST_DIRS[@]} pools: ${PERSIST_DIRS[*]}"
    log "WARNING: using ${PERSIST_DIRS[0]} (alphabetically first). Remove duplicates to silence this warning."
fi
PERSIST_DIR="${PERSIST_DIRS[0]}"

# The persistent blob on the data pool is the sysext image itself; we point
# /run/extensions at it directly instead of copying it onto the boot pool.
# /usr is wiped on every TrueNAS update, so a boot-pool copy would not survive
# anyway, and writing to /usr means toggling its readonly ZFS property.
MEMRYX_RAW="${PERSIST_DIR}/memryx.raw"

# Read which repo this install came from (written by install.sh)
MEMRYX_REPO="truenas-community-sysexts/memryx-mx3-support"
if [ -f "${PERSIST_DIR}/.memryx-repo" ]; then
    MEMRYX_REPO=$(cat "${PERSIST_DIR}/.memryx-repo" 2>/dev/null) || MEMRYX_REPO="truenas-community-sysexts/memryx-mx3-support"
    [ -z "$MEMRYX_REPO" ] && MEMRYX_REPO="truenas-community-sysexts/memryx-mx3-support"
fi

if [ ! -f "$MEMRYX_RAW" ]; then
    log "No memryx.raw at ${MEMRYX_RAW}, nothing to do"
    exit 0
fi

# --- Activate sysext directly off the data pool ---
# /run/extensions is tmpfs (gone after reboot), so we recreate the symlink
# every boot. systemd-sysext loop-mounts the symlink target wherever it lives;
# loop_device_make_by_path() is filesystem-agnostic, so a ZFS data-pool path
# works the same as a boot-pool path.
log "Activating memryx sysext..."
mkdir -p /run/extensions
ln -sf "$MEMRYX_RAW" /run/extensions/memryx.raw
systemd-sysext refresh
ldconfig

# --- Check kernel version matches the module in the sysext ---
# memx_cascade_plus_pcie.ko must be present for the running kernel.
running_kver=$(uname -r)
MEMX_KO="/usr/lib/modules/${running_kver}/extra/memx_cascade_plus_pcie.ko"
if [ -f "$MEMX_KO" ]; then
    if [ -e /sys/module/memx_cascade_plus_pcie ]; then
        log "memx_cascade_plus_pcie already loaded, skipping insmod"
    else
        log "Loading memx_cascade_plus_pcie module..."
        insmod_rc=0
        insmod_err=$(insmod "$MEMX_KO" 2>&1) || insmod_rc=$?
        if [ "$insmod_rc" -ne 0 ]; then
            log "ERROR: insmod memx_cascade_plus_pcie failed (rc=${insmod_rc}): ${insmod_err:-no output from insmod}"
            log "ERROR: check 'dmesg | grep -i memx' for the kernel reason; a TrueNAS update can introduce a driver/kernel ABI mismatch"
            log "ERROR: if so, install a memryx.raw release matching ${running_kver} from https://github.com/${MEMRYX_REPO}/releases"
        fi
    fi
else
    SYSEXT_KVER=""
    for d in /usr/lib/modules/*/; do
        [ -d "$d" ] || continue
        name=${d%/}
        name=${name##*/}
        if [ "$name" != "$running_kver" ] && [ -f "${d}extra/memx_cascade_plus_pcie.ko" ]; then
            SYSEXT_KVER="$name"
            break
        fi
    done
    if [ -n "$SYSEXT_KVER" ]; then
        log "ERROR: Kernel version mismatch: running ${running_kver} but sysext has a module for ${SYSEXT_KVER}"
        log "ERROR: TrueNAS was likely updated. Download a new memryx.raw release matching ${running_kver}"
        log "ERROR: Visit https://github.com/${MEMRYX_REPO}/releases"
    else
        log "WARNING: memx_cascade_plus_pcie.ko not found at /usr/lib/modules/${running_kver}/extra/"
    fi
fi

# --- Reload udev rules from sysext so /dev/memx0 gets correct permissions ---
log "Reloading udev rules..."
udevadm control --reload-rules 2>/dev/null || true
if [ -e /dev/memx0 ]; then
    udevadm trigger --subsystem-match=memx 2>/dev/null || true
fi

# --- Start the mxa-manager daemon ---
# Frigate connects to /run/mxa_manager (created by the daemon). The sysext
# ships the unit auto-enabled (multi-user.target.wants), but PREINIT runs
# before systemd reaches multi-user.target, so we kick it explicitly once the
# sysext is merged and the module is loaded. mxa-manager.service itself waits
# (bounded) for /dev/memx0 and orders Before=docker.service, so containers
# start after the socket exists.
if [ -x /usr/bin/mxa_manager ]; then
    log "Starting mxa-manager daemon..."
    systemctl daemon-reload 2>/dev/null || true
    if ! systemctl restart mxa-manager 2>/dev/null; then
        log "WARNING: could not start mxa-manager (it will retry; check 'systemctl status mxa-manager')"
    fi
else
    log "WARNING: /usr/bin/mxa_manager not found in merged sysext; daemon not started"
fi

log "Done"
exit 0
