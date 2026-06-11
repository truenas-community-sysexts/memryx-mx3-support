# Build / CI notes

Reasoning behind non-obvious CI decisions that aren't self-evident from the
workflow YAML, plus the open items that need confirmation on the first real
hardware build. Living document; update when a decision changes.

## What CI builds

`memryx.raw` — a sysext (`ID=_any`) containing the MX3 PCIe kernel module, the
MemryX userspace runtime + `mxa-manager` daemon, the firmware, the two systemd
units, the udev rule, and the bundled PREINIT script. The build runs on a
GLIBC-matched Ubuntu runner (~10–20 min including the ISO download).

Steps (see [`build.yml`](../.github/workflows/build.yml)):

1. Download + checksum-verify the TrueNAS ISO; extract kernel headers from the
   nested `rootfs.squashfs`; detect `REAL_KVER` and the kernel's GCC major.
2. Clone [`mx3_driver_pub`](https://github.com/memryx/mx3_driver_pub) at the
   tracked `driver_ref` and compile `memx_cascade_plus_pcie.ko` against those
   exact headers with the kernel-matching GCC.
3. Add `developer.memryx.com/deb <channel> main`, resolve the concrete patch
   version of `memx-drivers` / `memx-accl` / `mxa-manager` matching the tracked
   SDK (`<sdk>.*`, mirroring Frigate's pin), `apt-get download` them, and
   `dpkg-deb -x` the userspace files.
4. Assemble the sysext tree, `mksquashfs`, and smoke-test the result (required
   paths present, key binaries are ELF, module vermagic references `REAL_KVER`).

## The source-vs-deb split

The kernel module is compiled from source (it must match the running kernel). The
userspace stack is taken from MemryX's official redistributable `.deb` packages,
**not** compiled, because:

- `libmemx.so` (the userspace C API) is not in the public source mirror; the
  `memx-accl` / `mxa-manager` source build-depends on the `memx-drivers` package
  for it.
- The debs are the exact binaries Frigate's release is validated against.
- Every deb's payload is redistributable (GPLv2 / MPL-2.0 / redistributable
  firmware), so shipping it in the release is fine.

The DKMS module *source* shipped inside `memx-drivers` is ignored — we build the
`.ko` ourselves so it targets the TrueNAS kernel, not the install host's.

## Version tracking ([`check-releases.yml`](../.github/workflows/check-releases.yml))

Daily cron + manual dispatch. Two independent checks; either firing triggers one
build dispatch with `mark_latest=false` (prerelease gate).

- **TrueNAS half**: identical to the sibling sysexts — newest stable `scale-build`
  tag, train resolved from `download.truenas.com`, gated on the ISO being
  published. Bumps `truenas.version` / `truenas.train`.
- **MemryX half**: parses
  [Frigate's `docker/memryx/user_installation.sh`](https://github.com/blakeblackshear/frigate/blob/dev/docker/memryx/user_installation.sh)
  for the `memx-drivers=<sdk>.*` pin (currently `2.1`). Frigate explicitly
  supports one SDK at a time, so its pin **is** the target — the same cap-at-the-
  consumer logic the Hailo sysext uses. When the pin moves, the workflow resolves
  the matching `mx3_driver_pub` source tag (e.g. SDK `2.1` → `v2.1.0`) and bumps
  `memryx.sdk` + `memryx.driver_ref`.

`tracked-versions.json` shape:

```json
{
  "truenas": { "version": "25.10.4", "train": "Goldeye" },
  "memryx": {
    "sdk": "2.1",                       // major.minor; userspace debs pinned <sdk>.*
    "driver_ref": "v2.1.0",             // mx3_driver_pub tag the .ko is built from
    "driver_repo": "memryx/mx3_driver_pub",
    "apt_channel": "stable"             // stable | early_access
  }
}
```

Validated by [`validate-tracked-versions.sh`](../.github/scripts/validate-tracked-versions.sh)
in `lint.yml`.

## Release tagging + promotion

- **Tag:** `v<truenas>-memryx<sdk>-r<run_number>` (e.g. `v25.10.4-memryx2.1-r12`).
  The `-r<run>` suffix is monotonic per workflow, so every dispatch gets a unique
  tag even on same-commit retries (GitHub immutable-release tag-burn).
- Auto-builds publish as a **prerelease** and open a `hardware-test` issue. They
  are not served by `releases/latest` (or `install.sh`) until a human verifies on
  hardware and closes the issue as completed — [`promote.yml`](../.github/workflows/promote.yml)
  then flips it to Latest and appends a changelog.
- `mark_latest=true` (manual dispatch) skips the gate and publishes straight to
  Latest.

## Verification status

The first end-to-end build (`v25.10.4-memryx2.1-r1`, kernel
`6.12.91-production+truenas`) ran green, which already confirms the build-time
items below. The remaining ones are runtime/hardware concerns the build can't
exercise; the prerelease + hardware-test gate exists to catch them before users
do, and every assembly assumption fails loud in `build.yml` rather than shipping
a broken sysext.

**Confirmed by the first build:**

1. **Deb file layout.** The apt pool served `memx-drivers 2.1.1-1.1`,
   `memx-accl 2.1.2-1`, `mxa-manager 2.1.1-1`; the assembly globs picked up
   `libmemx.so(.2.1.1)`, `libmx_accl.so(.2)`, `usr/bin/mxa_manager` (+ bonus
   `acclBench`) and all four `cascade*.bin` firmware blobs, and the smoke-test
   passed (paths present, binaries ELF, module vermagic matches the kernel). If a
   future SDK renames/relocates these the build fails loudly — adjust the globs then.

2. **GLIBC compatibility — confirmed on hardware (r1).** The module loaded
   (`/dev/memx0` + `/dev/memx0_feature` created) and `ldd /usr/bin/mxa_manager`
   resolved every userspace lib (`libmemx.so`, `libmx_accl.so.2`, …) against the
   TrueNAS rootfs after the sysext merge + `ldconfig`. No GLIBC issue.

3. **`mxa_manager` config — resolved (r3).** `mxa_manager` hardcodes
   `/etc/memryx/mxa_manager.conf` (no `--config` flag) and the **SDK 2.1 binary
   reads it unconditionally**, exiting `critical` if it's missing — which a sysext
   can't satisfy directly (no `/etc`). The `argc`-based "skip the conf if given
   CLI flags" branch in `main_linux.cpp` only exists in newer MxAccl (≥2.2); the
   r2 attempt to pass flags was confirmed on hardware to **not** help on 2.1.
   The working fix: bundle the conf in the sysext at
   `/usr/lib/memryx/mxa_manager.conf` (taken from the `mxa-manager` deb, with a
   built-in default fallback) and have an `ExecStartPre` copy it to
   `/etc/memryx/mxa_manager.conf` on every start. `/etc` is writable on TrueNAS,
   and the copy is recreated each start so it needn't persist. r1/r2 failed with
   "Config file not found"; r3+ is fixed. (The optional `/etc/memryx/power.conf`
   read by `dfp_executor.cpp` is guarded by an `exists()` check, so its absence is
   harmless.)

**Still to verify on hardware:**

4. **Firmware path / flashing.** The M.2 is a QSPI-flash-boot board, so the
   kernel only `request_firmware()`s `cascade.bin` for host-load boards. We bundle
   all `cascade*.bin` to `/usr/lib/firmware`. On-board QSPI re-flashing (via the
   GPL flash tool) is **not** done automatically — it's a hardware write. If a
   driver/firmware version mismatch shows up, expose an explicit
   `install.sh --update-firmware` step.

## Lint

[`lint.yml`](../.github/workflows/lint.yml) runs `shellcheck --severity=warning`
over `scripts/*.sh` + `.github/scripts/*.sh`, validates `tracked-versions.json`
shape, and runs `actionlint` (with the same shellcheck severity) over the
workflows.
