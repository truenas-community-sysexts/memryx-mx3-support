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

## Open verification items (confirm on first hardware run)

These are best-effort and intentionally fail-loud in `build.yml`; the
prerelease + hardware-test gate exists precisely to catch them before users do.

1. **Exact deb file layout.** The assembly step globs for `libmemx*`/`libmx*`
   shared objects and `usr/bin/mxa_manager`. If MemryX renames or relocates these
   in a future SDK, the build fails loudly rather than shipping a broken sysext —
   adjust the globs then.
2. **GLIBC compatibility.** The userspace debs are MemryX-built (Ubuntu/Debian
   baseline). They are assumed ≤ the TrueNAS rootfs GLIBC. If a binary fails to
   load on the target, pin the runner/SDK accordingly or rebuild `mx_accl` from
   the MPL source on the matched runner.
3. **`mxa_manager` config.** Upstream installs `/etc/memryx/mxa_manager.conf`, but
   a sysext cannot ship `/etc`. The unit currently runs the daemon with its
   built-in defaults. If it needs the conf, drop it to `/etc/memryx/` from
   `install.sh`.
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
