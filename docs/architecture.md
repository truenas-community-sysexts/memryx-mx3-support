# Architecture

Deep technical reference for the MemryX MX3 sysext: what it ships, how it's
built, and the constraints that shape it. For day-to-day install/uninstall, see
[install.md](install.md). For CI specifics and open verification items, see
[build-ci-notes.md](build-ci-notes.md).

## The daemon model (why this differs from coral/hailo)

Most accelerator sysexts (Coral, Hailo) only need to put a kernel module and a
device node on the host; the consuming container links the vendor's userspace
library itself and talks straight to `/dev/<device>`.

The MX3 is different. Its software stack is **daemon-mediated**:

```
  Frigate container                       TrueNAS host
  ┌────────────────────┐                 ┌───────────────────────────────┐
  │ frigate (memryx     │   unix socket  │ mxa-manager (daemon)          │
  │ detector, mx_accl)  │◀──────────────▶│  /run/mxa_manager             │
  │                     │                 │      │                        │
  │  /dev/memx0 ────────┼─────────────────┼──────┘ memx_cascade_plus_pcie │
  └────────────────────┘   device map     │        (kernel module)        │
                                           │        /dev/memx0  ← udev     │
                                           └───────────────────────────────┘
```

Frigate's `memryx` detector connects to **`mxa-manager`** over the
`/run/mxa_manager` socket (and also needs `/dev/memx0` mapped, privileged). If the
daemon is not running on the host, the MX3 is visible but unusable.

Consequence: the sysext must ship and run the full host stack, not just a module:

1. **`memx_cascade_plus_pcie.ko`** — the PCIe kernel module
2. **`libmemx*` / `libmx*`** — the userspace C API + `mx_accl` C++ runtime
3. **`mxa_manager`** — the device-management daemon, run as a persistent service
4. **firmware**, **udev rules**

## What gets built where

The kernel module is the only kernel-version-coupled artifact, so it is compiled
from source in CI against the exact TrueNAS kernel headers (the coral/hailo
pattern). Everything else in the stack is kernel-independent userspace.

| Artifact | Build path | Why |
| --- | --- | --- |
| `memx_cascade_plus_pcie.ko` | Compiled in the CI runner from [mx3_driver_pub](https://github.com/memryx/mx3_driver_pub) (GPLv2) against the TrueNAS headers | Must match the exact running kernel's vermagic |
| `libmemx*`, `mx_accl`, `mxa_manager`, firmware, flash tools | Pulled from MemryX's redistributable apt packages, unpacked into the sysext | `libmemx.so` (the userspace C API) is **not** in the public source mirror; `memx-accl`/`mxa-manager` build-depend on the `memx-drivers` package for it. The official debs are the Frigate-validated binaries. |

This split is the one meaningful departure from the source-only coral/hailo
build. See [build-ci-notes.md](build-ci-notes.md) for the exact steps and the open
items that need confirmation on the first real hardware run.

## Build pipeline (`build.yml`)

```
┌──────────────────────────────────────────────────────────────┐
│ resolve:  read tracked-versions.json → TrueNAS ver/train,     │
│           MemryX SDK + driver_ref + apt_channel, runner       │
├──────────────────────────────────────────────────────────────┤
│ build (on the GLIBC-matched runner):                          │
│  1. Download + verify the TrueNAS ISO                         │
│  2. Extract kernel headers from the nested rootfs.squashfs    │
│  3. Detect REAL_KVER + the kernel's GCC major version         │
│  4. Clone mx3_driver_pub@driver_ref, build the .ko            │
│  5. Add developer.memryx.com/deb, resolve + download          │
│     memx-drivers / memx-accl / mxa-manager at <sdk>.*,        │
│     unpack the userspace files                                │
│  6. Assemble the sysext tree (module + runtime + daemon +     │
│     firmware + units + udev + preinit)                        │
│  7. mksquashfs → memryx.raw (zstd, -all-root)                 │
│  8. Smoke-test the squashfs contents (paths + ELF + vermagic) │
├──────────────────────────────────────────────────────────────┤
│ release:  draft → prerelease (gate) or Latest (override),     │
│           open a hardware-test issue for auto-builds          │
└──────────────────────────────────────────────────────────────┘
```

The runner image is resolved by [`.github/scripts/resolve-runner.sh`](../.github/scripts/resolve-runner.sh)
from the TrueNAS Debian base (bookworm → ubuntu-22.04, trixie → ubuntu-24.04) so
the compiled module and any runner-built binaries don't out-run the rootfs GLIBC.

## Sysext layout

```
memryx.raw  (squashfs, ID=_any)
└── usr/
    ├── lib/
    │   ├── extension-release.d/extension-release.memryx   (ID=_any)
    │   ├── modules/<REAL_KVER>/extra/memx_cascade_plus_pcie.ko
    │   ├── x86_64-linux-gnu/libmemx*.so* , libmx*.so*
    │   ├── firmware/cascade*.bin
    │   ├── systemd/system/memryx-load.service
    │   ├── systemd/system/mxa-manager.service
    │   ├── systemd/system/multi-user.target.wants/{memryx-load,mxa-manager}.service
    │   ├── udev/rules.d/51-memryx-udev.rules
    │   └── memryx/memryx-preinit.sh           (bundled for install.sh)
    └── bin/
        ├── mxa_manager
        └── (acclBench / flash tool, if present in the SDK)
```

`<REAL_KVER>` is the actual kernel release string (e.g.
`6.12.33-production+truenas`), not the header package name. On TrueNAS `/lib` →
`/usr/lib`, so after the sysext merges, the module is at
`/lib/modules/<REAL_KVER>/extra/` and the firmware at `/lib/firmware/` where the
driver's `request_firmware()` path looks.

## Boot / runtime services

Two units, mirroring MemryX's own split (driver package vs. manager package):

- **`memryx-load.service`** — `Type=oneshot`, insmods `memx_cascade_plus_pcie.ko`
  (guarded by `[ -e /sys/module/... ]` so it no-ops if PREINIT already loaded it),
  ordered `Before=docker.service mxa-manager.service`.
- **`mxa-manager.service`** — `Type=simple`, runs `/usr/bin/mxa_manager`, waits
  (bounded) for `/dev/memx0`, creates `/run/mxa_manager` (0777), ordered
  `After=memryx-load.service` and `Before=docker.service`. Adapted from MemryX's
  upstream `debian_manager/mxa-manager.service`; it runs as `root` rather than the
  upstream dedicated `mxa-manager` sysuser, because a sysext-shipped `sysusers.d`
  entry isn't reliably created before the PREINIT merge on TrueNAS. `ExecStart`
  passes the config as flags (`--addr /run/mxa_manager/ --port 10000 --log low
  --interval 500`) rather than relying on `/etc/memryx/mxa_manager.conf`: the
  daemon hardcodes that path and exits if it's absent, but a sysext can't ship
  `/etc`, and `main_linux.cpp` skips the conf entirely when given CLI args.

Both are ordered **`Before=docker.service`** for the same reason as the sibling
sysexts (`hailo-load`, `coral-load`, `nvidia-mig-setup`): TrueNAS apps run as
docker `restart=unless-stopped` containers, so a Frigate container that starts
before the daemon socket exists would fail to reach the accelerator.

Because TrueNAS silently ignores sysext-shipped `[Install] WantedBy=` symlinks at
boot, activation is driven by a **PREINIT** `initshutdownscript` entry that runs
[`memryx-preinit.sh`](../scripts/memryx-preinit.sh): it recreates the
`/run/extensions/memryx.raw` symlink, `systemd-sysext refresh`, `ldconfig`,
insmods the module, and `systemctl restart mxa-manager`. See
[truenas-sysext-notes.md](truenas-sysext-notes.md).

## Persistence

```
/mnt/<pool>/.config/memryx/
├── memryx.raw                - the sysext image (activated directly off the pool)
├── .memryx-sdk-version       - MemryX SDK version (informational)
├── .memryx-repo              - source repo for error output (informational)
└── memryx-preinit.sh         - runs before apps start (registered as PREINIT)
```

The `.raw` lives on the data pool, never under `/usr` (which a TrueNAS update
wipes). The PREINIT script re-activates it on every boot.

## Licensing

| Component | License | Redistributable? |
| --- | --- | --- |
| `memx_cascade_plus_pcie.ko` (from `kdriver`) | GPLv2 | Yes — built from source |
| `libmemx.so` (userspace C API, from `memx-drivers`) | GPLv2 | Yes |
| `mx_accl` runtime, `mxa_manager` (from MxAccl) | MPL-2.0 | Yes |
| flash tool (`tools/flash_update_tool`) | GPLv2+ | Yes |
| firmware (`cascade*.bin`) | MemryX firmware license | Yes — "free to use and redistribute exact copies"; source not available |
| first-party scripts/units/workflows | MIT | — |

Every bundled artifact is redistributable, which is why the full stack can ship in
the release rather than being downloaded on the user's host at install time.
