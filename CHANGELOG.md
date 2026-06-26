# Changelog

All notable changes to this project are documented here.

## [Unreleased]

Initial implementation of the MemryX MX3 sysext for TrueNAS SCALE, adapted from
the sibling [coral-pcie-support](https://github.com/truenas-community-sysexts/coral-pcie-support)
and [hailo8-support](https://github.com/truenas-community-sysexts/hailo8-support)
CI/CD approach.

### Hardware bring-up fixes (r2–r4)

- **r2** — prerelease `install.sh` couldn't fetch `memryx-lib.sh` from
  `releases/latest` (a fresh repo has no Latest); now bundled in the `.raw` and
  extracted from a local image.
- **r3** — `mxa-manager` died with `Config file not found at
  /etc/memryx/mxa_manager.conf` (the SDK 2.1 binary reads that hardcoded path
  unconditionally; a sysext can't ship `/etc`). The unit now copies a bundled
  conf into `/etc/memryx/` via `ExecStartPre` on every start.
- **r4** — **firmware anti-rollback**: the SDK 2.1 runtime requires firmware
  cnt ≥ 6, but the `v2.1.0`-tag firmware we shipped was the stale cnt-5 image
  (cards failed with `accelerator has <garbage> chips`). Firmware now comes from
  a separate **`firmware_ref`** (`v2.2.0`, cnt ≥ 6); added `install.sh
  --update-firmware` with the bundled flash tools, gated to **bare metal** only
  (VFIO blocks flashing from a passthrough VM). Documented the full
  hardware-confirmed recipe: firmware ≥ 6 (bare-metal flash + power-cycle) +
  Frigate as a **privileged** Custom App (SYS_RAWIO insufficient) with
  `device: PCIe:0`.
- **Driver log-noise patch (build-time, no version change).** `build.yml` now
  applies `patches/*.patch` to the cloned `mx3_driver_pub` tree before compiling,
  via the idempotent `.github/scripts/apply-driver-patches.sh`. The first patch
  gates the un-guarded `fops_read`/`fops_write` `wait timeout N(s), retrying
  again` `pr_info` logs behind `#ifdef DEBUG` (matching every other diagnostic in
  that file), so an idle-but-open accelerator — Frigate holding the device
  between motion-gated detections — no longer floods the kernel log every ~10s on
  a perfectly healthy card. Upstreamed as `scyto/mx3_driver_pub` branch
  `gate-fops-timeout-debug-logs` (PR pending); the applier auto-skips the patch
  once the fix lands in the pinned `driver_ref`.

### Added

- **TrueNAS 26 beta preview channel.** A `truenas_preview` block in
  `tracked-versions.json` tracks the latest TrueNAS 26 beta (e.g. `26.0.0-BETA.2`).
  Because 26 betas are not in `scale-build` tags and ship no GITMANIFEST,
  `check-releases.yml` scrapes the browsable channel listing
  (`iso.sys.truenas.net/TrueNAS-26-BETA/`) for the highest `X.Y.Z-BETA.N`/`-RC.N`,
  ISO-gates it, and `build.yml` fetches the ISO via an `iso_url` override against a
  pinned runner. A MemryX SDK bump now dispatches **both** a stable (25.x) and a
  preview (26-beta) build; a TrueNAS-only bump on one channel builds just that
  channel. Preview builds publish as permanent pre-releases (label
  `preview-hardware-test`) and are never promoted to Latest, so stable installs are
  unaffected; `promote.yml` additionally refuses any `BETA`/`RC` tag.
- **Full Frigate-ready host stack in one sysext.** Ships the MX3 PCIe kernel
  module, the MemryX userspace runtime (`libmemx` / `mx_accl`), the `mxa-manager`
  daemon, firmware, udev rules, and the boot-time PREINIT activation — everything
  Frigate's `memryx` detector needs on the host.
- **Two-source build.** `build.yml` compiles `memx_cascade_plus_pcie.ko` from
  [memryx/mx3_driver_pub](https://github.com/memryx/mx3_driver_pub) (GPLv2) against
  the exact TrueNAS kernel headers, and pulls the redistributable userspace stack
  from MemryX's `developer.memryx.com/deb` packages pinned to the Frigate SDK.
- **`mxa-manager.service` daemon.** Persistent unit (adapted from MemryX's
  upstream) that runs `/usr/bin/mxa_manager`, waits for `/dev/memx0`, exposes
  `/run/mxa_manager`, and is ordered `Before=docker.service` so containers start
  after the socket exists.
- **`memryx-load.service`.** Oneshot module load, idempotent (guards on
  `/sys/module/memx_cascade_plus_pcie`), with restart-loop caps.
- **Frigate SDK cap.** `check-releases.yml` parses Frigate's
  `docker/memryx/user_installation.sh` for the pinned MemryX SDK (currently 2.1)
  and resolves the matching driver source tag — mirroring how the Hailo sysext
  caps at Frigate's HailoRT pin.
- **TrueNAS auto-tracking.** Daily check bumps the TrueNAS version/train and
  dispatches a kernel-matched build, same as the sibling sysexts.
- **`install.sh --check` / `--dry-run`.** Read-only probe (device node, module,
  daemon + socket, sysext merge, persistence, PREINIT registration, kernel match,
  PREINIT boot result) and a no-op validation mode.
- **Prerelease + hardware-test gate.** Auto-builds publish as prereleases and open
  a `hardware-test` issue; closing it as completed promotes the build to Latest
  (`promote.yml`).
- **Build-time smoke test.** Asserts required paths exist, key binaries are ELF,
  and the module vermagic matches the target kernel before publishing.

### Notes

- Open verification items for the first hardware build (exact deb layout, GLIBC
  compatibility, `mxa_manager` config, firmware flashing) are tracked in
  [docs/build-ci-notes.md](docs/build-ci-notes.md).
