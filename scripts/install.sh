#!/usr/bin/env bash
# Installs the pre-built memryx.raw sysext on a running TrueNAS system.
# All compilation happens on GitHub Actions; this script only downloads and
# places the pre-built memryx.raw file, then loads the MX3 PCIe kernel module
# and starts the mxa-manager device-management daemon.
#
# The sysext bundles everything (kernel module, libmemx/mx_accl runtime,
# mxa_manager daemon, firmware, udev) — no install-time download from MemryX
# is needed. Frigate's `memryx` detector connects to /run/mxa_manager (created
# by the daemon) and /dev/memx0.
#
# Usage: curl -fsSL <release-url>/install.sh | sudo bash
#    or: sudo ./install.sh [path-to-memryx.raw]
#    or: sudo ./install.sh --pool=fast
#    or: sudo ./install.sh --check          (probe an existing install)
#    or: sudo ./install.sh --dry-run        (validate without modifying)
# See --help for the full option list.

set -euo pipefail

# do_check: read-only probe of an existing install. Exits 0 if all checks
# pass (warnings allowed), 1 if any check fails. Used by --check.
do_check() {
    local pass=0 warn=0 fail=0
    local mark_ok="✓" mark_warn="⚠" mark_fail="✗"
    local -a status_lines=()
    local -a hint_lines=()

    record_pass() { status_lines+=("  ${mark_ok} $1"); pass=$((pass+1)); }
    record_warn() {
        status_lines+=("  ${mark_warn} $1"); warn=$((warn+1))
        [ -n "${2:-}" ] && hint_lines+=("    → $2")
    }
    record_fail() {
        status_lines+=("  ${mark_fail} $1"); fail=$((fail+1))
        [ -n "${2:-}" ] && hint_lines+=("    → $2")
    }

    echo "=== MemryX MX3 install status ==="
    echo ""

    # 1. PCIe device node
    if [ -e /dev/memx0 ]; then
        record_pass "Device /dev/memx0 present"
    else
        record_fail "Device /dev/memx0 not present" \
            "is the MemryX MX3 seated, and was the system rebooted after install?"
    fi

    # 2. Kernel module loaded
    if lsmod 2>/dev/null | awk '{print $1}' | grep -qx memx_cascade_plus_pcie; then
        record_pass "Kernel module memx_cascade_plus_pcie loaded"
    else
        record_fail "Kernel module memx_cascade_plus_pcie not loaded" \
            "re-run install.sh or manually insmod /usr/lib/modules/\$(uname -r)/extra/memx_cascade_plus_pcie.ko"
    fi

    # 2b. mxa-manager daemon active and its runtime socket dir present.
    # Frigate connects to /run/mxa_manager; without the daemon the MX3 is
    # visible at /dev/memx0 but applications can't use it.
    if systemctl is-active --quiet mxa-manager 2>/dev/null; then
        record_pass "mxa-manager daemon active"
    else
        record_fail "mxa-manager daemon not active" \
            "check 'systemctl status mxa-manager'; the daemon needs /dev/memx0 present"
    fi
    if [ -d /run/mxa_manager ]; then
        record_pass "Runtime socket dir /run/mxa_manager present"
    else
        record_warn "/run/mxa_manager not present" \
            "mxa-manager creates it on start; check the daemon status"
    fi

    # 2c. Firmware (informational). The anti-rollback counter isn't exposed in
    # verinfo, so we can't assert >= 6 here — but if the device is present we
    # surface its firmware commit and point at the fix for the tell-tale
    # "accelerator has <garbage> chips" failure (firmware too old).
    if [ -r /sys/memx0/verinfo ]; then
        local fw_commit
        fw_commit=$(grep -oE 'FW_CommitID=0x[0-9a-fA-F]+' /sys/memx0/verinfo 2>/dev/null | head -1)
        record_pass "Device firmware present (${fw_commit:-version unknown})"
        if systemd-detect-virt -q --vm 2>/dev/null; then
            hint_lines+=("    → firmware (anti-rollback cnt >= 6) must be flashed on the BARE-METAL host, not this VM")
        else
            hint_lines+=("    → if a detector reports 'accelerator has <garbage> chips', run: sudo ./install.sh --update-firmware")
        fi
    fi

    # 3. Activation symlink present and resolves to an image. It lives on tmpfs
    # (/run/extensions), so the PREINIT script recreates it on every boot; a
    # missing symlink is a warning, not a hard failure.
    if [ -L /run/extensions/memryx.raw ] && [ -f /run/extensions/memryx.raw ]; then
        record_pass "Activation symlink /run/extensions/memryx.raw resolves to an image"
    else
        record_warn "Activation symlink /run/extensions/memryx.raw missing or dangling" \
            "the PREINIT script recreates it on boot; reboot or re-run install.sh"
    fi

    # 4. Sysext merged into /usr
    if systemd-sysext list 2>/dev/null | awk '{print $1}' | grep -qx memryx; then
        record_pass "Sysext merged into /usr"
    else
        record_warn "Sysext not currently merged" \
            "the PREINIT script merges it on boot; check 'systemctl status systemd-sysext'"
    fi

    # 5. Persistent config dir (same resolver as install path)
    local persist_dir=""
    if resolve_persist_dir; then
        persist_dir="$PERSIST_DIR"
        record_pass "Persistent config at ${persist_dir}"
    else
        record_fail "No persistent config resolved" \
            "re-run install.sh with --pool=NAME or --persist-path=PATH"
    fi

    # 6. Backup memryx.raw on persistent pool
    if [ -n "$persist_dir" ] && [ -f "${persist_dir}/memryx.raw" ]; then
        record_pass "Backup ${persist_dir}/memryx.raw present"
    elif [ -n "$persist_dir" ]; then
        record_fail "Backup memryx.raw missing in ${persist_dir}" "re-run install.sh"
    fi

    # 7. PREINIT script on disk
    if [ -n "$persist_dir" ] && [ -x "${persist_dir}/memryx-preinit.sh" ]; then
        record_pass "PREINIT script ${persist_dir}/memryx-preinit.sh present and executable"
    elif [ -n "$persist_dir" ]; then
        record_fail "PREINIT script missing or not executable in ${persist_dir}" "re-run install.sh"
    fi

    # 8. PREINIT registered with TrueNAS middleware (read-only midclt query)
    if command -v midclt >/dev/null 2>&1; then
        local lookup script_when script_enabled
        lookup=$(memryx_init_script_lookup)
        case "$lookup" in
            error)
                record_warn "Could not query TrueNAS middleware" \
                    "run with sudo on TrueNAS SCALE"
                ;;
            "")
                record_fail "No init script registered for memryx" "re-run install.sh"
                ;;
            *)
                IFS='|' read -r _ script_when script_enabled <<<"$lookup"
                if [ "$script_when" = "PREINIT" ] && [ "$script_enabled" = "True" ]; then
                    record_pass "PREINIT script registered with TrueNAS middleware (PREINIT, enabled)"
                else
                    record_warn "Init script registered but not as enabled PREINIT" \
                        "re-run install.sh to fix"
                fi
                ;;
        esac
    else
        record_warn "midclt not available, skipping middleware check" \
            "this script must run on TrueNAS SCALE"
    fi

    # 9. Kernel module path matches running kernel
    local running_kver memx_ko
    running_kver=$(uname -r)
    memx_ko="/usr/lib/modules/${running_kver}/extra/memx_cascade_plus_pcie.ko"
    if [ -f "$memx_ko" ]; then
        record_pass "Kernel module path matches running kernel ${running_kver}"
    else
        record_fail "Missing memx_cascade_plus_pcie.ko for running kernel ${running_kver}" \
            "download a new memryx.raw release matching this kernel"
    fi

    # 10. PREINIT script result on last boot.
    # memryx-preinit.sh logs via `logger -t memryx-preinit`, so journalctl can
    # filter by tag. The script ends with a "Done" sentinel on success; any
    # ERROR: line in the same boot indicates a failure path was hit.
    if ! command -v journalctl >/dev/null 2>&1; then
        record_fail "journalctl not available, cannot read PREINIT result" \
            "this script must run on TrueNAS SCALE"
    else
        local preinit_log preinit_last
        preinit_log=$(journalctl -b -t memryx-preinit --no-pager -o cat 2>/dev/null || true)
        if [ -z "$preinit_log" ]; then
            record_warn "No memryx-preinit entries this boot" \
                "PREINIT may not be registered yet; reboot after install, or re-run install.sh"
        elif printf '%s' "$preinit_log" | grep -q '^ERROR:'; then
            preinit_last=$(printf '%s' "$preinit_log" | grep '^ERROR:' | head -1)
            record_fail "PREINIT logged an error this boot: ${preinit_last}" \
                "see full log: journalctl -b -t memryx-preinit"
        else
            preinit_last=$(printf '%s' "$preinit_log" | tail -1)
            if [ "$preinit_last" = "Done" ]; then
                record_pass "PREINIT completed successfully this boot"
            else
                record_warn "PREINIT ran but did not log the Done sentinel (last: ${preinit_last})" \
                    "review full log: journalctl -b -t memryx-preinit"
            fi
        fi
    fi

    printf '%s\n' "${status_lines[@]}"
    echo ""
    if [ "${#hint_lines[@]}" -gt 0 ]; then
        printf '%s\n' "${hint_lines[@]}"
        echo ""
    fi
    printf 'Summary: %d ok, %d warn, %d fail\n' "$pass" "$warn" "$fail"

    [ "$fail" -gt 0 ] && return 1
    return 0
}

# do_update_firmware: flash the bundled cnt>=6 MX3 firmware to the card's QSPI.
# BARE METAL ONLY — VFIO passthrough silently swallows the QSPI writes, so we
# refuse inside a VM and print the host-side procedure. Requires the sysext to
# be installed+merged (the flash tool + firmware live in it). A full power-cycle
# is needed afterward; the card only reads firmware from QSPI at power-on.
do_update_firmware() {
    local fw="/usr/lib/firmware/cascade_4chips_flash.bin"
    local flash="/usr/lib/memryx/flash/pcieupdateflash"

    # 1. VM guard — flashing from a passthrough guest does not work.
    if command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt -q --vm; then
        local virt; virt=$(systemd-detect-virt 2>/dev/null || echo "vm")
        echo "ERROR: this TrueNAS is a VM (${virt}). MX3 firmware CANNOT be flashed from a" >&2
        echo "  passthrough guest — VFIO silently drops the QSPI writes. Flash on the" >&2
        echo "  bare-metal hypervisor host instead:" >&2
        echo "    1. shut down this VM" >&2
        echo "    2. on the host:  echo <BDF> > /sys/bus/pci/drivers/vfio-pci/unbind   # BDF from: lspci -d 1fe9:" >&2
        echo "    3. git clone https://github.com/memryx/mx3_driver_pub && cd mx3_driver_pub" >&2
        echo "       tools/flash_update_tool/bin/x86_64/pcieupdateflash -f firmware/cascade_4chips_flash.bin" >&2
        echo "    4. FULL power-cycle the host (a reboot is NOT enough) to load the new firmware" >&2
        return 1
    fi

    [ -e /dev/memx0 ] || { echo "ERROR: /dev/memx0 not present — is the MX3 seated and the sysext merged?" >&2; return 1; }
    [ -x "$flash" ]   || { echo "ERROR: flash tool not found at ${flash} — install the sysext first." >&2; return 1; }
    [ -f "$fw" ]      || { echo "ERROR: firmware not found at ${fw} — install the sysext first." >&2; return 1; }

    echo "=== MX3 firmware update (bare metal) ==="
    echo "Current device firmware:"
    sed 's/^/  /' /sys/memx0/verinfo 2>/dev/null || echo "  (could not read /sys/memx0/verinfo)"
    echo ""
    echo "WARNING: this writes the card's QSPI flash. Do not interrupt it."
    if [ "$FORCE" != "1" ]; then
        if { : </dev/tty; } 2>/dev/null; then
            printf 'Flash %s now? [y/N] ' "$fw"
            read -r ans </dev/tty || ans=""
            case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted."; return 1 ;; esac
        else
            echo "ERROR: no terminal to confirm; re-run with --force to flash non-interactively." >&2
            return 1
        fi
    fi

    # Release the device: stop the daemon, unload the module (the flasher does
    # direct PCIe access and needs the driver out of the way).
    echo "Stopping mxa-manager and unloading the driver..."
    systemctl stop mxa-manager 2>/dev/null || true
    if lsmod 2>/dev/null | awk '{print $1}' | grep -qx memx_cascade_plus_pcie; then
        rmmod memx_cascade_plus_pcie || {
            echo "ERROR: could not unload memx_cascade_plus_pcie (in use?). Stop any consumers (e.g. docker stop frigate) and retry." >&2
            return 1
        }
    fi

    echo "Flashing..."
    if ! "$flash" -f "$fw"; then
        echo "ERROR: firmware flash failed." >&2
        return 1
    fi

    echo ""
    echo "=== Firmware flash complete ==="
    echo "A FULL POWER-CYCLE is now required (power off, then on — a soft reboot is"
    echo "NOT enough; the card only loads firmware from QSPI at power-on)."
    echo "After power-on, verify: cat /sys/memx0/verinfo"
    return 0
}

# if_real: run a command unless --dry-run is set, in which case print what
# would have been run. For redirections and heredocs, gate the entire block
# manually with `if [ "$DRY_RUN" = "1" ]; then ... else ... fi` since the
# shell evaluates redirections before the command runs.
if_real() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '[dry-run] would: %s\n' "$*"
    else
        "$@"
    fi
}

# resolve_persist_dir: determine where persistent config lives.
# Priority: --persist-path > --pool > existing config dir > only-data-pool
#         > interactive prompt (multi-pool) > error (no tty + ambiguous)
# Sets PERSIST_DIR on success; prints to stderr and returns 1 on failure.
resolve_persist_dir() {
    PERSIST_DIR=""
    local d p
    local -a existing=() pools=() choices=()
    local header n i

    if [ -n "${PERSIST_PATH:-}" ]; then
        PERSIST_DIR="$PERSIST_PATH"
        return 0
    fi
    if [ -n "${POOL_NAME:-}" ]; then
        PERSIST_DIR="/mnt/${POOL_NAME}/.config/memryx"
        return 0
    fi

    shopt -s nullglob
    for d in /mnt/*/.config/memryx; do
        [ -d "$d" ] && existing+=("$d")
    done
    shopt -u nullglob

    if [ "${#existing[@]}" -eq 1 ]; then
        PERSIST_DIR="${existing[0]}"
        echo "Re-using existing config: $PERSIST_DIR"
        return 0
    fi

    while IFS= read -r p; do
        [ -n "$p" ] && [ "$p" != "boot-pool" ] && pools+=("$p")
    done < <(zpool list -H -o name 2>/dev/null)

    if [ "${#existing[@]}" -eq 0 ] && [ "${#pools[@]}" -eq 0 ]; then
        echo "ERROR: No ZFS pool found (excluding boot-pool). Cannot set up persistence." >&2
        echo "  Re-run with --pool=<name> or --persist-path=/mnt/<pool>/<path>" >&2
        return 1
    fi

    if [ "${#existing[@]}" -eq 0 ] && [ "${#pools[@]}" -eq 1 ]; then
        PERSIST_DIR="/mnt/${pools[0]}/.config/memryx"
        echo "Auto-selected pool: ${pools[0]} → $PERSIST_DIR"
        return 0
    fi

    if [ "${#existing[@]}" -gt 1 ]; then
        header="Found existing memryx configs on multiple pools:"
        choices=("${existing[@]}")
    else
        header="Multiple data pools available (no existing config):"
        for p in "${pools[@]}"; do
            choices+=("/mnt/${p}/.config/memryx")
        done
    fi

    if ! { : </dev/tty; } 2>/dev/null; then
        echo "ERROR: $header" >&2
        echo "  No controlling terminal. Pass --pool=<name> or --persist-path=<path>." >&2
        return 1
    fi

    echo "$header"
    for i in "${!choices[@]}"; do
        echo "  [$((i+1))] ${choices[$i]}"
    done
    while true; do
        printf 'Pick one (1-%d): ' "${#choices[@]}"
        read -r n </dev/tty || return 1
        if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#choices[@]}" ]; then
            PERSIST_DIR="${choices[$((n-1))]}"
            echo "Selected: $PERSIST_DIR"
            return 0
        fi
        echo "  Invalid. Enter 1-${#choices[@]}."
    done
}

# REPO can be overridden via --repo=OWNER/NAME or MEMRYX_REPO env var.
REPO="${MEMRYX_REPO:-truenas-community-sysexts/memryx-mx3-support}"
# MEMRYX_RAW (the sysext image we activate) lives on the data pool and is set
# to "${PERSIST_DIR}/memryx.raw" once the persistent pool is resolved below.
MEMRYX_RAW=""

# --- Parse CLI arguments ---
LOCAL_RAW=""
POOL_NAME=""
PERSIST_PATH=""
CHECK_MODE=0
DRY_RUN=0
UPDATE_FW_MODE=0
FORCE=0

for arg in "$@"; do
    case "$arg" in
        --repo=*)
            REPO="${arg#*=}"
            [ -n "$REPO" ] || { echo "ERROR: --repo= requires a non-empty value (e.g., --repo=owner/name)" >&2; exit 2; }
            ;;
        --pool=*)
            POOL_NAME="${arg#*=}"
            [ -n "$POOL_NAME" ] || { echo "ERROR: --pool= requires a non-empty value" >&2; exit 2; }
            ;;
        --persist-path=*)
            PERSIST_PATH="${arg#*=}"
            [ -n "$PERSIST_PATH" ] || { echo "ERROR: --persist-path= requires a non-empty value" >&2; exit 2; }
            ;;
        --check) CHECK_MODE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --update-firmware) UPDATE_FW_MODE=1 ;;
        --force) FORCE=1 ;;
        --help)
            echo "Usage: sudo ./install.sh [OPTIONS] [path-to-memryx.raw]"
            echo ""
            echo "Options:"
            echo "  --repo=OWNER/NAME             GitHub repo to download release from (default: truenas-community-sysexts/memryx-mx3-support)"
            echo "                                Can also be set via MEMRYX_REPO env var."
            echo "  --pool=NAME                   ZFS pool for persistent config (e.g., fast)"
            echo "  --persist-path=PATH           Exact path for persistent config"
            echo "  --check                       Probe an existing install (read-only) and report status"
            echo "  --update-firmware             Flash the bundled MX3 firmware (BARE METAL ONLY; needs the sysext installed)"
            echo "  --force                       With --update-firmware: flash without the interactive prompt"
            echo "  --dry-run                     Validate everything (downloads, checksums, network) without modifying the system"
            echo "  --help                        Show this help"
            echo ""
            echo "Examples:"
            echo "  sudo ./install.sh --pool=fast"
            echo "  sudo ./install.sh --check"
            echo "  sudo ./install.sh --update-firmware"
            echo "  sudo ./install.sh --dry-run"
            echo "  sudo ./install.sh /tmp/memryx-input.raw"
            echo "  curl -fsSL <url>/install.sh | sudo bash"
            exit 0
            ;;
        *)
            # A `curl | sudo bash` user who typos `--pol=fast` or `/tmp/typ.raw`
            # silently gets auto-detect / a release download. They think their
            # flag took effect when it didn't. Refuse rather than guess.
            if [ -f "$arg" ]; then
                LOCAL_RAW="$arg"
            elif [[ "$arg" == -* ]]; then
                echo "ERROR: unknown option: $arg (see --help)" >&2
                exit 2
            else
                echo "ERROR: positional argument is not an existing file: $arg" >&2
                echo "  Pass --help for usage." >&2
                exit 2
            fi
            ;;
    esac
done

if [ "$CHECK_MODE" = "1" ] && [ "$DRY_RUN" = "1" ]; then
    echo "ERROR: --check and --dry-run are mutually exclusive" >&2
    exit 2
fi

# Every mode past --help touches privileged state: zfs readonly toggles,
# writes under /usr, midclt, insmod. Fail fast with a clear message rather
# than partway through after a download and unsquash.
if [ "$(id -u 2>/dev/null)" != "0" ]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
fi

# --update-firmware is standalone (operates on an already-installed sysext);
# it doesn't need the lib, pool resolution, or a release download.
if [ "$UPDATE_FW_MODE" = "1" ]; then
    do_update_firmware
    exit $?
fi

# Persistence only works if --persist-path is the exact location the
# boot-time PREINIT script scans: /mnt/<pool>/.config/memryx, a single pool
# component under /mnt. memryx-preinit.sh re-derives the dir by globbing
# /mnt/*/.config/memryx and reads nothing else, so any other path silently
# breaks persistence after the next reboot or TrueNAS update:
#   - tmpfs (/tmp, /run): backup and script gone on the next reboot
#   - an OS dir (/usr, /etc, /var, /data, /): wiped on the next update
#   - a real pool but wrong/deeper subdir (/mnt/tank/foo): the glob misses it
# Anchored regex (not a case glob, whose * would span /) enforces a single
# pool component. --pool resolves to this shape automatically.
if [ -n "$PERSIST_PATH" ]; then
    PERSIST_PATH_REAL=$(realpath -m "$PERSIST_PATH" 2>/dev/null || echo "$PERSIST_PATH")
    if [[ ! "$PERSIST_PATH_REAL" =~ ^/mnt/[^/]+/\.config/memryx/?$ ]]; then
        echo "ERROR: --persist-path must be /mnt/<pool>/.config/memryx (got: ${PERSIST_PATH})" >&2
        echo "  The boot-time PREINIT script only scans /mnt/*/.config/memryx for the backup," >&2
        echo "  so any other location silently breaks persistence after a reboot or update." >&2
        echo "  Pass --pool=<name> instead (it resolves to /mnt/<name>/.config/memryx)." >&2
        exit 2
    fi
fi

# Source shared library (provides memryx_init_script_lookup).
# Resolution order: sibling file (checkout / extracted release dir) > the
# bundled copy inside a local .raw > download from the promoted "latest"
# release. The .raw fallback is what makes the piped form
#   curl .../install.sh | sudo bash -s -- /path/to/memryx.raw
# work for a PRE-RELEASE under hardware test, which has no "latest" asset yet.
_source_memryx_lib() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || dir=""
    if [ -n "$dir" ] && [ -f "${dir}/memryx-lib.sh" ]; then
        # shellcheck source=scripts/memryx-lib.sh
        source "${dir}/memryx-lib.sh"
        return 0
    fi
    # Bundled inside a local .raw at usr/lib/memryx/memryx-lib.sh. LOCAL_RAW is
    # set during arg parsing above, before this runs.
    if [ -n "${LOCAL_RAW:-}" ] && [ -f "$LOCAL_RAW" ] && command -v unsquashfs >/dev/null 2>&1; then
        local libtmp
        libtmp=$(mktemp -d /tmp/memryx-lib-extract.XXXXXXXXXX)
        if unsquashfs -q -d "${libtmp}/x" "$LOCAL_RAW" usr/lib/memryx/memryx-lib.sh >/dev/null 2>&1 \
           && [ -f "${libtmp}/x/usr/lib/memryx/memryx-lib.sh" ]; then
            # shellcheck source=scripts/memryx-lib.sh
            source "${libtmp}/x/usr/lib/memryx/memryx-lib.sh"
            rm -rf "$libtmp"
            return 0
        fi
        rm -rf "$libtmp"
    fi
    local tmp
    tmp=$(mktemp /tmp/memryx-lib.XXXXXXXXXX)
    if curl -fsSL --max-time 30 \
           "https://github.com/${REPO}/releases/latest/download/memryx-lib.sh" \
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
    echo "ERROR: Could not load memryx-lib.sh (no sibling file, none bundled in the .raw, download failed)." >&2
    echo "  Download it alongside install.sh from the same release and re-run, e.g.:" >&2
    echo "    curl -fsSL <release-url>/memryx-lib.sh -o memryx-lib.sh" >&2
    exit 1
}

if [ "$CHECK_MODE" = "1" ]; then
    do_check
    exit $?
fi

WORK_DIR=$(mktemp -d /tmp/memryx-install.XXXXXXXXXX)

cleanup() {
    [ -n "${WORK_DIR:-}" ] && rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

# If a local path is provided, use it; otherwise download from GitHub releases
if [ -n "$LOCAL_RAW" ]; then
    # Reject input path == staging path: cp would refuse with "are the same
    # file" and the EXIT trap would then rm -rf the work dir, deleting the
    # user's input. Detect and refuse rather than risk data loss.
    LOCAL_REAL=$(realpath "$LOCAL_RAW" 2>/dev/null || echo "$LOCAL_RAW")
    STAGE_REAL=$(realpath -m "${WORK_DIR}/memryx.raw" 2>/dev/null || echo "${WORK_DIR}/memryx.raw")
    if [ "$LOCAL_REAL" = "$STAGE_REAL" ]; then
        echo "ERROR: input file collides with the installer's staging path." >&2
        echo "  Move or copy it to a different path and re-run." >&2
        exit 2
    fi
    echo "Using local memryx.raw: $LOCAL_RAW"
    cp "$LOCAL_RAW" "${WORK_DIR}/memryx.raw"
else
    # Detect TrueNAS version
    VERSION=$(midclt call system.info | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin)['version'])
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
") || { echo "ERROR: Failed to detect TrueNAS version"; exit 1; }
    [ -z "$VERSION" ] && { echo "ERROR: TrueNAS version is empty"; exit 1; }
    echo "Detected TrueNAS version: ${VERSION}"

    # Find matching release
    echo "Searching for matching release..."
    export VERSION
    RELEASE_TAG=$(curl -sS --max-time 30 "https://api.github.com/repos/${REPO}/releases?per_page=100" \
        | python3 -c "
import sys, json, os
try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    print('Failed to parse GitHub API response', file=sys.stderr)
    sys.exit(1)
if isinstance(data, dict) and 'message' in data:
    msg = data['message']
    if 'rate limit' in msg.lower():
        print('GitHub API rate limit exceeded (60 requests/hour for unauthenticated calls).', file=sys.stderr)
        print('Wait a few minutes and try again.', file=sys.stderr)
    else:
        print(f'GitHub API error: {msg}', file=sys.stderr)
    sys.exit(1)
version = os.environ['VERSION']
prefix = f'v{version}-'
# Exclude prereleases: an unverified build is published as a prerelease until a
# human closes its hardware-test issue (promoting it to Latest). Installing one
# would bypass that gate.
matches = [r for r in data if r.get('tag_name', '').startswith(prefix) and not r.get('prerelease')]
if not matches:
    print(f'No release found for TrueNAS version {version}', file=sys.stderr)
    tags = [r.get('tag_name', '?') for r in data]
    if tags:
        print('Available releases:', file=sys.stderr)
        for t in tags:
            print(f'  {t}', file=sys.stderr)
    sys.exit(1)
matches.sort(key=lambda r: r.get('published_at') or r.get('created_at') or '', reverse=True)
print(matches[0]['tag_name'], end='')
") || { echo "ERROR: Failed to query GitHub releases"; exit 1; }

    echo "Found release: ${RELEASE_TAG}"

    # Extract the MemryX SDK version from the tag for informational purposes.
    # Tags look like: v25.10.4-memryx2.1-r1
    MEMRYX_SDK_VERSION=$(echo "$RELEASE_TAG" | sed -n 's/.*memryx\([0-9][0-9.]*[0-9]\).*/\1/p')
    if [ -z "$MEMRYX_SDK_VERSION" ]; then
        echo "ERROR: Could not parse MemryX SDK version from release tag '${RELEASE_TAG}'." >&2
        echo "  Expected format: v<truenas>-memryx<sdk>-r<run>" >&2
        exit 1
    fi
    echo "MemryX SDK version: ${MEMRYX_SDK_VERSION}"

    # Download memryx.raw and checksum
    BASE_URL="https://github.com/${REPO}/releases/download/${RELEASE_TAG}"
    echo "Downloading memryx.raw..."
    curl -fSL --max-time 600 "${BASE_URL}/memryx.raw" -o "${WORK_DIR}/memryx.raw" || { echo "ERROR: Failed to download memryx.raw"; exit 1; }
    curl -fSL --max-time 600 "${BASE_URL}/memryx.raw.sha256" -o "${WORK_DIR}/memryx.raw.sha256" || { echo "ERROR: Failed to download checksum"; exit 1; }

    # Validate downloads are non-empty
    [ -s "${WORK_DIR}/memryx.raw" ] || { echo "ERROR: memryx.raw is empty"; exit 1; }
    [ -s "${WORK_DIR}/memryx.raw.sha256" ] || { echo "ERROR: checksum file is empty"; exit 1; }

    # Verify checksum
    echo "Verifying checksum..."
    if ! (cd "$WORK_DIR" && sha256sum -c memryx.raw.sha256); then
        echo "ERROR: Checksum verification failed!"
        exit 1
    fi
    echo "Checksum OK"
fi

# --- Extract PREINIT script from sysext ---
# The sysext bundles memryx-preinit.sh at usr/lib/memryx/memryx-preinit.sh.
# Extract it via unsquashfs (read-only). No repacking is needed: the firmware
# blobs are already inside the sysext at /usr/lib/firmware, so unlike the Hailo
# installer there's nothing to fetch or inject at install time.
echo ""
echo "=== Extracting PREINIT script from memryx.raw ==="

if ! command -v unsquashfs &>/dev/null; then
    echo "ERROR: unsquashfs not found, cannot extract PREINIT script from sysext"
    echo "  Install squashfs-tools: apt-get install squashfs-tools"
    exit 1
fi

unsquashfs -q -d "${WORK_DIR}/memryx-sysext-unpack" "${WORK_DIR}/memryx.raw" usr/lib/memryx/memryx-preinit.sh

BUNDLED_PREINIT="${WORK_DIR}/memryx-sysext-unpack/usr/lib/memryx/memryx-preinit.sh"
if [ ! -f "$BUNDLED_PREINIT" ]; then
    echo "ERROR: memryx-preinit.sh not found in sysext at /usr/lib/memryx/memryx-preinit.sh" >&2
    echo "  This memryx.raw was built before the preinit script was bundled in." >&2
    echo "  Re-fetch a current release: https://github.com/${REPO}/releases/latest" >&2
    exit 1
fi
cp "$BUNDLED_PREINIT" "${WORK_DIR}/memryx-preinit.sh"
chmod +x "${WORK_DIR}/memryx-preinit.sh"
rm -rf "${WORK_DIR}/memryx-sysext-unpack"
echo "PREINIT script extracted"

echo ""
echo "=== Installing memryx.raw ==="

# --- Detect persistent storage pool ---
# The sysext image lives only on the data pool; /run/extensions points at it
# directly. Resolve the pool first so the blob is in place before we activate.
if ! resolve_persist_dir; then
    echo "ERROR: No persistent storage pool found; cannot install." >&2
    exit 1
fi
echo "Persistent config directory: ${PERSIST_DIR}"
if_real mkdir -p "$PERSIST_DIR"
MEMRYX_RAW="${PERSIST_DIR}/memryx.raw"

# Write the sysext image to the data pool. This is the single copy we activate
# and the one that survives reboots and TrueNAS updates (no boot-pool copy).
echo "Installing memryx.raw to ${MEMRYX_RAW}..."
if_real cp "${WORK_DIR}/memryx.raw" "${MEMRYX_RAW}"

# Remove memryx from sysext before modifying. If nothing is currently merged,
# unmerge exits non-zero with "No extensions found" on stderr, which is fine.
# A real failure (overlay held open by another process) must not be swallowed.
echo "Removing old memryx sysext symlink..."
if_real rm -f /run/extensions/memryx.raw
if [ "$DRY_RUN" != "1" ]; then
    UNMERGE_ERR=$(systemd-sysext unmerge 2>&1) || {
        if printf '%s' "$UNMERGE_ERR" | grep -qi "no extensions"; then
            true  # nothing was merged, harmless
        else
            echo "ERROR: systemd-sysext unmerge failed: ${UNMERGE_ERR}" >&2
            echo "  Another process may be holding the overlay open." >&2
            exit 1
        fi
    }
else
    echo "[dry-run] would: systemd-sysext unmerge"
fi

# Activate sysext via symlink + refresh (TrueNAS middleware pattern).
# systemd-sysext loop-mounts the symlink target wherever it lives, so pointing
# at the ZFS data-pool path works the same as a boot-pool path would. ldconfig
# after refresh so the new libmemx/mx_accl sonames merged under /usr/lib are
# resolvable by the daemon and by container loaders.
echo "Activating memryx sysext..."
if_real mkdir -p /run/extensions
if_real ln -sf "${MEMRYX_RAW}" /run/extensions/memryx.raw
if_real systemd-sysext refresh
if_real ldconfig

# Load the kernel module (use insmod directly: /lib/modules is read-only on
# TrueNAS so depmod can't update module deps, and modprobe can't find modules
# without it).
echo "Loading MemryX MX3 kernel module..."
MEMX_KO="/usr/lib/modules/$(uname -r)/extra/memx_cascade_plus_pcie.ko"
if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would: insmod ${MEMX_KO} (if present and not already loaded)"
elif [ -f "$MEMX_KO" ]; then
    if [ -e /sys/module/memx_cascade_plus_pcie ]; then
        echo "memx_cascade_plus_pcie already loaded, skipping insmod"
    else
        insmod "$MEMX_KO" || echo "WARNING: insmod memx_cascade_plus_pcie failed (device may not be present)"
    fi
else
    echo "WARNING: memx_cascade_plus_pcie.ko not found at ${MEMX_KO}"
fi

# Reload udev rules from sysext so /dev/memx0 gets correct permissions
echo "Reloading udev rules..."
if_real udevadm control --reload-rules 2>/dev/null || true
if [ -e /dev/memx0 ]; then
    if_real udevadm trigger --subsystem-match=memx 2>/dev/null || true
fi

# Start the mxa-manager daemon. The sysext ships the unit with a
# multi-user.target.wants symlink so it auto-starts on boot; here we bring it
# up immediately for this session (systemd already sees the unit after the
# sysext refresh above). A failure here is non-fatal to the install — the unit
# retries on boot — but we surface it.
echo "Starting mxa-manager daemon..."
if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would: systemctl daemon-reload && systemctl restart mxa-manager"
else
    systemctl daemon-reload 2>/dev/null || true
    if ! systemctl restart mxa-manager 2>/dev/null; then
        echo "WARNING: could not start mxa-manager now (it will retry on boot once /dev/memx0 is present)."
        echo "  Check: systemctl status mxa-manager"
    fi
fi

echo ""
echo "=== Installation complete ==="
echo ""

# Verify
if [ -e /dev/memx0 ]; then
    echo "Device /dev/memx0 detected!"
else
    echo "Device /dev/memx0 not found."
    echo "  - Ensure a MemryX MX3 M.2 card is installed"
    echo "  - Try rebooting the system"
fi

# ==========================================================================
# Persistence setup: survives reboots and TrueNAS updates
# ==========================================================================

echo ""
echo "=== Setting up persistence ==="

# The sysext image (${PERSIST_DIR}/memryx.raw) and its activation symlink were
# put in place during install above. Here we record metadata and register the
# boot-time PREINIT script that re-creates the symlink after each reboot.

# Save MemryX SDK version for reference
if [ -n "${MEMRYX_SDK_VERSION:-}" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] would: write \$MEMRYX_SDK_VERSION (${MEMRYX_SDK_VERSION}) to ${PERSIST_DIR}/.memryx-sdk-version"
    else
        printf '%s' "$MEMRYX_SDK_VERSION" > "${PERSIST_DIR}/.memryx-sdk-version"
    fi
fi

# Save source repo so the boot-time PREINIT script can point users at the right
# releases page when a kernel mismatch is detected.
if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would: write \$REPO (${REPO}) to ${PERSIST_DIR}/.memryx-repo"
else
    printf '%s' "$REPO" > "${PERSIST_DIR}/.memryx-repo"
fi

# --- Install PREINIT script to persistent storage ---
# Source is ${WORK_DIR}/memryx-preinit.sh, extracted from the unsquashed
# sysext earlier. Bundling the script in the sysext means the memryx.raw
# release artifact is self-contained.
echo "Installing PREINIT script..."

# Clean up old postinit script if present
if_real rm -f "${PERSIST_DIR}/memryx-postinit.sh"

if_real cp "${WORK_DIR}/memryx-preinit.sh" "${PERSIST_DIR}/memryx-preinit.sh"
if_real chmod +x "${PERSIST_DIR}/memryx-preinit.sh"

# --- Register PREINIT script via midclt ---
PREINIT_SCRIPT="${PERSIST_DIR}/memryx-preinit.sh"
echo "Registering PREINIT script..."

# Find any existing memryx init script (postinit or preinit). A midclt
# lookup error is NOT the same as not-found: midclt records aren't keyed
# by command, so falling through to create on a transient query failure
# can produce a duplicate registration that restore.sh's first-match
# cleanup won't fully undo. Refuse rather than guess.
EXISTING_LOOKUP=$(memryx_init_script_lookup)
if [ "$EXISTING_LOOKUP" = "error" ]; then
    echo "ERROR: Could not query TrueNAS middleware to check for existing init scripts." >&2
    echo "  Refusing to register without a clean lookup, risks duplicate PREINIT entries." >&2
    echo "  Run 'midclt call initshutdownscript.query' to confirm middleware health, then re-run." >&2
    exit 1
fi
EXISTING_ID="${EXISTING_LOOKUP%%|*}"

# Build the payload via python3 -> json.dumps so PREINIT_SCRIPT is escaped
# correctly even if the path ever grows characters that are special to JSON.
PREINIT_PAYLOAD=$(PREINIT_SCRIPT="$PREINIT_SCRIPT" python3 -c '
import json, os
print(json.dumps({
    "type": "COMMAND",
    "command": os.environ["PREINIT_SCRIPT"],
    "when": "PREINIT",
    "enabled": True,
    "timeout": 30,
    "comment": "Activate MemryX MX3 sysext before apps start",
}))
')

if [ -n "$EXISTING_ID" ]; then
    echo "MemryX init script already registered (id: ${EXISTING_ID}), updating to PREINIT..."
    if ! if_real midclt call initshutdownscript.update "$EXISTING_ID" "$PREINIT_PAYLOAD"; then
        echo "ERROR: Failed to update init script (id: ${EXISTING_ID})." >&2
        echo "ERROR: Without a registered PREINIT script the sysext will NOT survive a reboot." >&2
        echo "ERROR: Check 'midclt call initshutdownscript.query' and re-run the installer." >&2
        exit 1
    fi
else
    if ! if_real midclt call initshutdownscript.create "$PREINIT_PAYLOAD"; then
        echo "ERROR: Failed to register PREINIT script via midclt." >&2
        echo "ERROR: Without a registered PREINIT script the sysext will NOT survive a reboot." >&2
        echo "ERROR: Check that the TrueNAS middleware is reachable (midclt call core.ping) and re-run." >&2
        exit 1
    fi
    echo "PREINIT script registered"
fi

echo ""
echo "=== Persistence setup complete ==="
echo ""
echo "Persistent config: ${PERSIST_DIR}/"
echo "  memryx.raw                - sysext backup"
echo "  .memryx-sdk-version       - MemryX SDK version (informational)"
echo "  memryx-preinit.sh         - runs before apps start (registered as PREINIT)"
echo ""
echo "The MemryX MX3 driver + mxa-manager daemon will survive TrueNAS updates and reboots."

# ==========================================================================
# Final verification: did the stack actually come up?
# ==========================================================================
# A silently-failed daemon (missing config, device init, etc.) is the failure
# mode that's most expensive to discover later — apps just can't reach the MX3.
# `systemctl restart` returning 0 only means the start was accepted; a
# Type=simple daemon can crash a moment later (and Restart=on-failure will then
# cycle it). So settle briefly, then check is-active + the socket dir and
# report clearly. NOT an EXIT trap: this must run only on the successful path,
# not after the many `exit 1` error branches above.
if [ "$DRY_RUN" != "1" ]; then
    echo ""
    echo "=== Verifying the MX3 stack ==="
    # Wait for the daemon to settle (its ExecStartPre waits for /dev/memx0,
    # then it binds the socket). Poll up to ~10s.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        systemctl is-active --quiet mxa-manager 2>/dev/null && [ -d /run/mxa_manager ] && break
        sleep 1
    done

    verify_fail=0
    if [ -e /dev/memx0 ]; then
        echo "  ✓ device /dev/memx0 present"
    else
        echo "  ⚠ device /dev/memx0 not present — reboot, or confirm the card is seated / passed through"
    fi
    if systemctl is-active --quiet mxa-manager 2>/dev/null; then
        echo "  ✓ mxa-manager daemon active"
    else
        echo "  ✗ mxa-manager daemon NOT active"
        echo "      diagnose: systemctl status mxa-manager ; journalctl -u mxa-manager -b --no-pager | tail"
        verify_fail=1
    fi
    if [ -d /run/mxa_manager ]; then
        echo "  ✓ /run/mxa_manager socket dir present (mount this into your Frigate container)"
    else
        echo "  ✗ /run/mxa_manager missing (the daemon creates it on start)"
        verify_fail=1
    fi

    if [ "$verify_fail" -eq 0 ]; then
        echo ""
        echo "Stack is up. For Frigate: run it PRIVILEGED with device /dev/memx0,"
        echo "volume /run/mxa_manager, and detector 'device: PCIe:0'. If a detector"
        echo "later reports 'accelerator has <garbage> chips', the card firmware is"
        echo "too old — run: sudo ./install.sh --update-firmware  (bare metal only)."
    else
        echo ""
        echo "WARNING: the MX3 stack did not fully come up (see the ✗ lines above)."
        echo "  It will retry on boot. Re-run 'sudo ./install.sh --check' for a full probe."
    fi
fi

if [ "$DRY_RUN" = "1" ]; then
    echo ""
    echo "=== Dry-run complete ==="
    echo "No changes were made to the system."
    echo ""
    echo "Would have installed:"
    echo "  Sysext image:      ${MEMRYX_RAW}"
    echo "  Persistent dir:    ${PERSIST_DIR}"
    [ -n "${MEMRYX_SDK_VERSION:-}" ] && echo "  MemryX SDK:        ${MEMRYX_SDK_VERSION}"
    [ -n "${RELEASE_TAG:-}" ] && echo "  Release tag:       ${RELEASE_TAG}"
fi
