#!/usr/bin/env bash
# Apply local patches to the cloned mx3_driver_pub tree before building.
#
# Usage:
#   apply-driver-patches.sh <mx3-driver-checkout> <patches-dir>
#
# Every *.patch in <patches-dir> (lexical order) is applied idempotently
# against the git checkout at <mx3-driver-checkout>:
#
#   - if it already reverse-applies  -> the change is present upstream for
#                                       this driver ref; skip it
#   - else if it forward-applies     -> apply it
#   - else                           -> fail loud
#
# The loud failure is the point: we pin the driver to a fixed mx3_driver_pub
# ref (driver_ref in tracked-versions.json) and these patches carry fixes the
# pinned ref lacks. If the upstream source shape drifts so a patch no longer
# applies, that is a signal to re-review the patch against the new ref BEFORE
# shipping a driver that silently misses the fix.
#
# Run locally against a checkout:
#   git clone https://github.com/memryx/mx3_driver_pub /tmp/d && \
#     git -C /tmp/d checkout v2.1.0
#   .github/scripts/apply-driver-patches.sh /tmp/d "$PWD/patches"
set -euo pipefail

SRC_DIR="${1:?usage: apply-driver-patches.sh <mx3-driver-checkout> <patches-dir>}"
PATCH_DIR="${2:?usage: apply-driver-patches.sh <mx3-driver-checkout> <patches-dir>}"

if [ ! -d "${SRC_DIR}/.git" ]; then
  echo "::error title=driver-patch::${SRC_DIR} is not a git checkout (git apply needs one)" >&2
  exit 1
fi
if [ ! -d "$PATCH_DIR" ]; then
  echo "No patches directory at ${PATCH_DIR} — nothing to apply"
  exit 0
fi

shopt -s nullglob
patches=("${PATCH_DIR}"/*.patch)
if [ "${#patches[@]}" -eq 0 ]; then
  echo "No *.patch files in ${PATCH_DIR} — nothing to apply"
  exit 0
fi

cd "$SRC_DIR"
applied=0
for patch in "${patches[@]}"; do
  name="$(basename "$patch")"
  if git apply --reverse --check "$patch" >/dev/null 2>&1; then
    echo "= ${name}: already present in this driver ref — skipping"
    continue
  fi
  if git apply --check "$patch" >/dev/null 2>&1; then
    git apply "$patch"
    echo "+ ${name}: applied"
    applied=$((applied + 1))
  else
    echo "::error title=driver-patch::${name} does not apply to the mx3_driver_pub checkout and is not already present. The upstream source shape changed — re-review the patch against this driver ref before building." >&2
    exit 1
  fi
done

echo "Driver patches: ${applied} applied, $(( ${#patches[@]} - applied )) skipped/present"
