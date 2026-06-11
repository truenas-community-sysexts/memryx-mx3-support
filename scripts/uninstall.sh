#!/usr/bin/env bash
# Uninstall the MemryX MX3 sysext. Thin alias for restore.sh, kept under
# this name because users searching for "uninstall" won't grep for
# "restore". restore.sh is still shipped in releases for backwards
# compatibility with old install instructions.
#
# Usage: curl -fsSL <release-url>/uninstall.sh | sudo bash
#    or: sudo ./uninstall.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# When piped through `curl | sudo bash`, $0 is /dev/stdin and there is no
# sibling restore.sh on disk. Detect that case and fetch restore.sh from
# the same release. Otherwise (run from a checked-out tree or extracted
# release tarball), exec the sibling directly.
if [ -f "${SCRIPT_DIR}/restore.sh" ]; then
    exec bash "${SCRIPT_DIR}/restore.sh" "$@"
fi

# Fallback: stdin path. Resolve the matching restore.sh from the latest
# release of the fork that shipped this script. MEMRYX_REPO is honored to
# match install.sh's --repo= override.
REPO="${MEMRYX_REPO:-truenas-community-sysexts/memryx-mx3-support}"
BASE_URL="https://github.com/${REPO}/releases/latest/download"
echo "uninstall.sh: fetching restore.sh + memryx-lib.sh from ${REPO}/releases/latest..." >&2
TMPDIR=$(mktemp -d /tmp/memryx-uninstall.XXXXXXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT
if ! curl -fsSL --max-time 60 "${BASE_URL}/restore.sh" -o "${TMPDIR}/restore.sh"; then
    echo "ERROR: failed to download restore.sh from ${REPO}/releases/latest" >&2
    exit 1
fi
if [ ! -s "${TMPDIR}/restore.sh" ]; then
    echo "ERROR: downloaded restore.sh is empty (${REPO}/releases/latest)" >&2
    exit 1
fi
# memryx-lib.sh is a shared library that restore.sh sources at startup.
# A download failure is not fatal here: restore.sh has its own fallback
# that re-fetches the lib if the sibling is missing.
curl -fsSL --max-time 30 "${BASE_URL}/memryx-lib.sh" -o "${TMPDIR}/memryx-lib.sh" 2>/dev/null || true
bash "${TMPDIR}/restore.sh" "$@"
exit $?
