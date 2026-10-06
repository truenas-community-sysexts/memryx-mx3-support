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

Daily cron + manual dispatch. Three independent checks; any firing triggers build
dispatches. Every build publishes as a prerelease and installs nowhere until a
hardware test on its train signs it off. A **MemryX SDK bump builds
both** the stable (25.x) and preview (26-beta) targets so each driver release ships
both; a TrueNAS-only bump on one channel builds just that channel.

- **TrueNAS stable half**: identical to the sibling sysexts — newest stable
  `scale-build` tag, train resolved from `download.truenas.com`, gated on the ISO
  being published. Bumps `truenas.version` / `truenas.train`. A new version
  whose kernel an existing build already covers is not rebuilt (see
  [Kernel-keyed builds](#kernel-keyed-builds)).
- **TrueNAS preview half**: tracks the latest TrueNAS beta or RC
  (`truenas_preview.version`, e.g. `27.0.0-RC.1`; TrueNAS 26 was renamed 27 at its
  first RC). Previews are not in `scale-build` tags and ship no GITMANIFEST. They
  are published on `iso.sys.truenas.net` in one directory per channel, and the
  directory moves with the release (`TrueNAS-26-BETA/`, then `TrueNAS-27-RC/`), so
  `.github/scripts/latest-preview.py` reads the server's root index (every
  channel) plus the tracked `truenas_preview.channel_url` listing, picks the
  highest `X.Y.Z-BETA.N` / `-RC.N`, gates on the ISO being uploaded, and bumps
  `truenas_preview.version` and `channel_url` together. The runner is pinned (`truenas_preview.runner`,
  `ubuntu-24.04`) since there is no GITMANIFEST to resolve from, and `build.yml`
  fetches the ISO via an `iso_url` override. Preview builds publish as
  pre-releases (label `preview-hardware-test`) and become full releases for
  their own train when their hardware test passes.
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
  "truenas_preview": {
    "version": "27.0.0-RC.1",       // latest TrueNAS beta/RC; see latest-preview.py
    "train": "Halfmoon",
    "runner": "ubuntu-24.04",       // pinned: previews ship no GITMANIFEST to resolve from
    "channel_url": "https://iso.sys.truenas.net/TrueNAS-27-RC/"  // moves with the release
  },
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

- **Tag:** `k<kernel>-memryx<sdk>-r<run_number>` (e.g. `k6.12.105-memryx2.1-r17`,
  the kernel cut at its first `-`), titled `Kernel <kernel> (TrueNAS <version>) -
  MemryX SDK <sdk> (r<run>)`. Releases from before kernel-keyed builds keep their
  `v<truenas>-memryx<sdk>-r<run>` tags. The `-r<run>` suffix is monotonic per
  workflow, so every dispatch gets a unique tag even on same-commit retries
  (GitHub immutable-release tag-burn), and it orders builds of both tag schemes
  for the Latest decision. The release notes keep the `for TrueNAS SCALE
  <version> (<train>)` header and the `Target kernel` row:
  `tests/test_notes_contract.py` renders the notes and feeds them to every
  parser.
- **Every** build publishes as a **prerelease** and opens one hardware-test
  issue: `hardware-test` for a stable target, `preview-hardware-test` for a
  TrueNAS beta/RC one. There is no override that publishes straight to Latest:
  a full release with no `verified-train` marker counts as approved for every
  train (the grandfather rule), so it would reach every box untested.
- Closing that issue as **completed** runs
  [`promote.yml`](../.github/workflows/promote.yml), which appends
  `<!-- verified-train: <train> -->` to the release notes. The train comes from
  the TrueNAS version in the notes header: the major from 26 on, `major.minor`
  before that. The build, stable or preview, is promoted in the same update
  (prerelease flag cleared, changelog appended from the previous full release
  of the same channel), and takes Latest when it is the newest signed-off build
  on any train by build order (the `-r<N>` run number). A stable box still never
  installs a preview build.
- `get.sh` and `install.sh` install a build on a train only when its notes
  carry that train's marker, or when it is a full release with no marker at
  all, and only on the kernel it was built for. Nothing else is ever
  installed, on stable or preview boxes. GitHub's "Latest" flag selects
  nothing for `get.sh`; only the older `releases/latest/download/install.sh`
  one-liner runs the Latest release's installer.

## Kernel-keyed builds

A sysext's kernel module binds to the exact kernel string (vermagic must
equal `uname -r`), and TrueNAS point releases usually reuse the previous
release's kernel. The pipeline is keyed accordingly:

- `check-releases.yml` resolves a new stable version's kernel from its
  `rootfs.mtree` manifest. If a release for that kernel (with the current
  MemryX SDK) already exists and is served on the new version's train (a full
  release with that train's `verified-train` marker, or a grandfathered one),
  no build is dispatched; `tracked-versions.json` still updates and the
  installer serves the new version by kernel match. If the only coverage is a
  build still awaiting its hardware test on that train, no duplicate build is
  dispatched either, but the tracked version holds until that build is
  approved (so the kernel keeps a rebuild path if a failed build is deleted).
  Preview (BETA/RC) builds and builds from another train never count as
  coverage ([`check-kernel-coverage.py`](../.github/scripts/check-kernel-coverage.py)).
- Coverage only counts while the Latest release is kernel-keyed (its tag
  starts with `k`). The older one-liner runs the `install.sh` attached to
  Latest, and until the first k-tagged build is promoted that is the
  pre-migration installer, which matches exact TrueNAS versions only; a
  skipped build would leave the new version with nothing it can serve. So
  until then, and whenever the Latest lookup fails, every new stable version
  builds as before. (`get.sh` does not depend on Latest: it runs the approved
  release's own installer on that release's image.)
- A new kernel, an SDK bump, or an unresolvable kernel always builds.
- `install.sh` (and `get.sh`, `uninstall.sh` and `restore.sh`, which carry the
  same selection code) matches `uname -r` against a release's `Target kernel`
  row, falling back to the short kernel in a k-tag when a body lost its row
  (the coverage gate's rule), and only from the box's own train: the
  userspace (`libmemx`, `mx_accl`, `mxa_manager`) is staged on a runner
  matched to that train's base system. It then checks the image's own
  `usr/lib/modules/<kernel>` before installing. The `memryx.kver` asset
  carries the same kernel string for external tooling; the installers do not
  read it (older immutable releases predate it).
- The README supported versions table is kernel-keyed too: `.github/kernel-map.json`
  (version to kernel, from each release's `rootfs.mtree`, refreshed by
  `gen-kernel-map.py`) plus the releases give one row per kernel with the
  range of versions it covers.
- Hardware-test sign-off is per build, which now means per kernel and train:
  one verification covers every TrueNAS version of that train sharing that
  kernel.

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

4. **Firmware anti-rollback — resolved (r4), confirmed on hardware.** The SDK 2.1
   runtime requires firmware **anti-rollback cnt ≥ 6**; cards with an older
   counter fail with `accelerator has <garbage> chips` (e.g. `301989888`). The
   firmware in the `driver_ref` (`v2.1.0`) tag is the OLD cnt-5 image, so we now
   source firmware from a separate **`firmware_ref`** (`v2.2.0`, the cnt ≥ 6
   image) — kept independent of the SDK-matched `driver_ref` (the kernel module
   still builds from `driver_ref`). r4 also bundles the prebuilt GPLv2+ flash
   tools (`pcieupdateflash` etc.) under `/usr/lib/memryx/flash/` and adds
   `install.sh --update-firmware`. **Flashing only works on bare metal** —
   `--update-firmware` calls `systemd-detect-virt` and refuses inside a VM,
   because VFIO passthrough silently swallows the QSPI write (confirmed on a
   Proxmox host: the in-guest flash reported OK but `verinfo` never changed;
   flashing from the bare-metal host worked). A full power-cycle is required to
   load new firmware.

## Hardware-confirmed requirements (the full working recipe)

End-to-end validated on bare-metal-flashed hardware + TrueNAS-in-Proxmox:
1. Kernel module + `mxa-manager` daemon (with `/etc/memryx/mxa_manager.conf`
   materialized by the unit — r3).
2. Firmware anti-rollback ≥ 6, flashed on **bare metal** + full power-cycle (r4).
3. Frigate as a **privileged** Custom App (`privileged: true`; `cap_add:
   SYS_RAWIO` confirmed **insufficient** — the detector `mmap`s the BAR memory)
   with `device: PCIe:0`, `/dev/memx0`, and the `/run/mxa_manager` socket.
   The TrueNAS catalog app can't be privileged.

## Lint

[`lint.yml`](../.github/workflows/lint.yml) runs `shellcheck --severity=warning`
over `get.sh`, `scripts/*.sh` + `.github/scripts/*.sh`, validates
`tracked-versions.json` shape, runs `actionlint` (with the same shellcheck
severity) over the workflows, byte-compiles `.github/scripts/*.py` and runs the
unit tests in `tests/`.
