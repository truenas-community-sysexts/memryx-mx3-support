#!/usr/bin/env bash
# Restores the original state by removing memryx.raw sysext.
# Run this to completely remove the MemryX MX3 driver extension.

set -euo pipefail

# --- Parse CLI arguments ---
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --help)
            echo "Usage: sudo ./restore.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --force    Proceed even if the module is in use (requires reboot afterward)"
            echo "  --help     Show this help"
            exit 0
            ;;
        *)
            echo "ERROR: unknown option: $arg (see --help)" >&2
            exit 2
            ;;
    esac
done

# Source shared library (provides memryx_init_script_lookup).
# Try the sibling file first (checkout or extracted release); fall back to
# downloading from the release for backwards compat with old uninstall.sh
# callers that don't fetch memryx-lib.sh alongside restore.sh.
_source_memryx_lib() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || dir=""
    if [ -n "$dir" ] && [ -f "${dir}/memryx-lib.sh" ]; then
        # shellcheck source=scripts/memryx-lib.sh
        source "${dir}/memryx-lib.sh"
        return 0
    fi
    local tmp repo
    repo="${MEMRYX_REPO:-truenas-community-sysexts/memryx-mx3-support}"
    tmp=$(mktemp /tmp/memryx-lib.XXXXXXXXXX)
    if curl -fsSL --max-time 30 \
           "https://github.com/${repo}/releases/latest/download/memryx-lib.sh" \
           -o "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
        # shellcheck source=scripts/memryx-lib.sh
        source "$tmp"
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp"
    return 1
}
_source_memryx_lib || {
    echo "ERROR: Could not load memryx-lib.sh (not found locally, download failed)." >&2
    echo "  Run from the release directory, or ensure network access to GitHub." >&2
    exit 1
}

echo "=== Removing MemryX MX3 sysext ==="

# Stop the mxa-manager daemon first: it opens /dev/memx0, so the module's
# refcount stays > 0 (and the .ko can't be unloaded) while it runs. Also mask
# nothing — the unit lives in the sysext, which we drop below, so it simply
# disappears once unmerged.
if systemctl list-unit-files 2>/dev/null | awk '{print $1}' | grep -qx 'mxa-manager.service'; then
    echo "Stopping mxa-manager daemon..."
    systemctl stop mxa-manager 2>/dev/null || echo "WARNING: could not stop mxa-manager (may already be stopped)"
fi

# Pre-check: refuse to proceed if the module is still in use unless --force is
# given. A held module means the backing .ko will be pulled out from under
# running consumers, leaving a half-state that confuses on next operation.
NEEDS_REBOOT=0
mod="memx_cascade_plus_pcie"
if lsmod 2>/dev/null | awk '{print $1}' | grep -qx "$mod"; then
    REFCNT=$(lsmod | awk -v m="$mod" '$1 == m {print $3}')
    if [ "${REFCNT:-0}" -gt 0 ]; then
        echo "${mod} is currently in use (refcount: ${REFCNT})."
        if command -v fuser >/dev/null 2>&1; then
            PIDS=$(fuser /dev/memx* 2>/dev/null | tr -s ' ') || true
            if [ -n "$PIDS" ]; then
                echo "  PIDs using /dev/memx*: ${PIDS}"
                for pid in $PIDS; do
                    PNAME=$(ps -p "$pid" -o comm= 2>/dev/null) || PNAME="(unknown)"
                    echo "    PID ${pid}: ${PNAME}"
                done
            fi
        fi
        if [ "$FORCE" = "0" ]; then
            echo ""
            echo "ERROR: Refusing to remove sysext while ${mod} is in use." >&2
            echo "  Stop the consuming process first (e.g. docker stop frigate)," >&2
            echo "  then re-run this script." >&2
            echo "  Or pass --force to remove anyway (reboot required afterward)." >&2
            exit 1
        fi
        echo ""
        echo "WARNING: --force given. Proceeding despite active consumers."
        echo "  The module will remain loaded in memory until reboot."
        echo "  A REBOOT IS REQUIRED to cleanly unload ${mod}."
        NEEDS_REBOOT=1
    fi

    if [ "$NEEDS_REBOOT" = "0" ]; then
        echo "Unloading ${mod} module..."
        if ! rmmod "$mod"; then
            echo "WARNING: rmmod ${mod} failed unexpectedly."
            if [ "$FORCE" = "0" ]; then
                echo "ERROR: Cannot unload module. Pass --force to continue anyway." >&2
                exit 1
            fi
            echo "  Continuing due to --force. A REBOOT IS REQUIRED."
            NEEDS_REBOOT=1
        fi
    fi
fi

# Remove the memryx sysext symlink and unmerge to drop the /usr overlay. Plain
# `systemd-sysext refresh` would re-merge any other active sysexts (e.g. the
# NVIDIA sysext on TrueNAS SCALE) instead of dropping ours cleanly, so unmerge
# everything first and re-merge the survivors below. The memryx.raw image itself
# lives on the data pool and is removed with the persistent config further down.
echo "Removing memryx sysext..."
rm -f /run/extensions/memryx.raw
systemd-sysext unmerge 2>/dev/null || true

# Re-merge any remaining sysexts (e.g. NVIDIA) that were deactivated by
# the earlier `systemd-sysext unmerge`. Without this, co-installed sysexts
# stay unmerged until the next reboot.
if ls /run/extensions/*.raw >/dev/null 2>&1; then
    echo "Re-merging remaining sysexts..."
    systemd-sysext refresh 2>/dev/null || echo "WARNING: Failed to re-merge remaining sysexts"
    ldconfig 2>/dev/null || true
fi

echo ""
echo "=== Restore complete ==="

# --- Clean up persistence ---
echo ""
echo "=== Cleaning up persistence ==="

# Deregister init script (preinit or legacy postinit). Treat midclt errors
# as "not found": there's nothing safe to do if we can't query, and a stale
# entry the user can clean up manually beats a half-finished restore.
INIT_LOOKUP=$(memryx_init_script_lookup)
if [ "$INIT_LOOKUP" = "error" ]; then
    echo "WARNING: Could not query TrueNAS middleware, skipping init script deregistration"
    INIT_ID=""
else
    INIT_ID="${INIT_LOOKUP%%|*}"
fi

if [ -n "$INIT_ID" ]; then
    midclt call initshutdownscript.delete "$INIT_ID" 2>/dev/null \
        && echo "Init script deregistered (id: ${INIT_ID})" \
        || echo "WARNING: Failed to deregister init script"
elif [ "$INIT_LOOKUP" != "error" ]; then
    echo "No init script found to deregister"
fi

# Remove persistent config
for d in /mnt/*/.config/memryx; do
    if [ -d "$d" ]; then
        echo "Removing persistent config: $d"
        rm -rf "$d"
    fi
done

echo "Persistence cleanup complete"

if [ "$NEEDS_REBOOT" = "1" ]; then
    echo ""
    echo "============================================================"
    echo "  WARNING: memx_cascade_plus_pcie was still in use when removed."
    echo "  The module remains loaded in memory but its backing file"
    echo "  is gone. A REBOOT IS REQUIRED to fully clean up."
    echo "  Stop any MemryX consumers (e.g. Frigate) before rebooting."
    echo "============================================================"
fi
