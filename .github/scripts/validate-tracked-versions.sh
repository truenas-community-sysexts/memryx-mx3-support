#!/usr/bin/env bash
# Validate that .github/tracked-versions.json has the shape the rest of the
# CI machinery (check-releases.yml, build.yml) assumes.
#
# Run locally:
#   .github/scripts/validate-tracked-versions.sh
# Exits non-zero with a `::error::` annotation on any shape violation.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FILE="${REPO_ROOT}/.github/tracked-versions.json"

if [ ! -f "$FILE" ]; then
  echo "::error title=tracked-versions::file not found: ${FILE}" >&2
  exit 1
fi

python3 - "$FILE" <<'PY'
import json
import re
import sys

path = sys.argv[1]

def fail(msg):
    print(f"::error title=tracked-versions::{msg}", file=sys.stderr)
    sys.exit(1)

try:
    with open(path) as f:
        data = json.load(f)
except json.JSONDecodeError as e:
    fail(f"invalid JSON in {path}: {e}")

if not isinstance(data, dict):
    fail("top-level value must be an object")

# Match the shape check-releases.yml's tag parser will accept: 2-or-more
# numeric parts. Today's TrueNAS tags are 3- or 4-part (25.10.3, 25.10.3.1)
# but a future train (e.g. TS-26.0) could legitimately be 2-part. Capping at
# 5 parts so a runaway tag still trips the gate.
ver_re = re.compile(r"^\d+(\.\d+){1,4}$")
# Train names are capitalized words (Goldeye, Fangtooth, etc.)
train_re = re.compile(r"^[A-Z][a-zA-Z]+$")

truenas = data.get("truenas")
if not isinstance(truenas, dict):
    fail("'truenas' key missing or not an object")

tn_version = truenas.get("version")
if not isinstance(tn_version, str) or not ver_re.match(tn_version):
    fail(f"'truenas.version' missing or malformed (got {tn_version!r}); expected X.Y[.Z[.W[.V]]]")

tn_train = truenas.get("train")
if not isinstance(tn_train, str) or not train_re.match(tn_train):
    fail(f"'truenas.train' missing or malformed (got {tn_train!r}); expected capitalized word (e.g. Goldeye)")

# MemryX SDK is a major.minor string (e.g. "2.1") — the value Frigate pins
# (memx-drivers=2.1.*). The build resolves the concrete patch from the apt
# pool at build time.
sdk_re = re.compile(r"^\d+\.\d+$")
# Driver-source tag in memryx/mx3_driver_pub (e.g. v2.1.0). The kernel module
# is compiled from this tag; it pairs with the SDK major.minor above.
driver_ref_re = re.compile(r"^v?\d+\.\d+(\.\d+)?$")

memryx = data.get("memryx")
if not isinstance(memryx, dict):
    fail("'memryx' key missing or not an object")

mx_sdk = memryx.get("sdk")
if not isinstance(mx_sdk, str) or not sdk_re.match(mx_sdk):
    fail(f"'memryx.sdk' missing or malformed (got {mx_sdk!r}); expected X.Y (e.g. 2.1)")

mx_ref = memryx.get("driver_ref")
if not isinstance(mx_ref, str) or not driver_ref_re.match(mx_ref):
    fail(f"'memryx.driver_ref' missing or malformed (got {mx_ref!r}); expected a mx3_driver_pub tag like v2.1.0")

# Firmware is sourced from a separate ref: the v2.1.0-tag firmware predates the
# anti-rollback cnt>=6 bump the SDK 2.1.x runtime requires, so we ship the
# newer firmware (e.g. v2.2.0) while the kernel module still builds from
# driver_ref. Same tag-shape as driver_ref.
mx_fw_ref = memryx.get("firmware_ref")
if not isinstance(mx_fw_ref, str) or not driver_ref_re.match(mx_fw_ref):
    fail(f"'memryx.firmware_ref' missing or malformed (got {mx_fw_ref!r}); expected a mx3_driver_pub tag like v2.2.0")

mx_repo = memryx.get("driver_repo")
if not isinstance(mx_repo, str) or not mx_repo.strip():
    fail(f"'memryx.driver_repo' missing or empty (got {mx_repo!r})")
if "/" not in mx_repo:
    fail(f"'memryx.driver_repo' must be owner/name format (got {mx_repo!r})")

mx_channel = memryx.get("apt_channel")
if not isinstance(mx_channel, str) or not mx_channel.strip():
    fail(f"'memryx.apt_channel' missing or empty (got {mx_channel!r})")
# developer.memryx.com/deb publishes 'stable' and 'early_access' channels.
if mx_channel not in ("stable", "early_access"):
    fail(f"'memryx.apt_channel' must be 'stable' or 'early_access' (got {mx_channel!r})")

print(f"tracked-versions OK: TrueNAS {tn_version} ({tn_train}), MemryX SDK {mx_sdk} "
      f"(driver {mx_ref}, firmware {mx_fw_ref} from {mx_repo}, apt channel {mx_channel})")
PY
