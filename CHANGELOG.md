# Changelog

All notable changes to this project are documented here.

## [Unreleased]

Initial implementation of the MemryX MX3 sysext for TrueNAS SCALE, adapted from
the sibling [coral-pcie-support](https://github.com/truenas-community-sysexts/coral-pcie-support)
and [hailo8-support](https://github.com/truenas-community-sysexts/hailo8-support)
CI/CD approach.

### Added

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
