# Install Reference

How `install.sh` works, its options, and what ends up on disk. For the quick
path, see the [README](../README.md#quick-start).

## Release artifacts

Each release ships:

- `memryx.raw` + `memryx.raw.sha256` — the sysext image and its checksum
- `install.sh`, `restore.sh`, `uninstall.sh`, `memryx-lib.sh` — the install-side
  scripts

Release tags encode the versions: `v<truenas>-memryx<sdk>-r<run>` (e.g.
`v25.10.4-memryx2.1-r12`).

## Install

Auto-detect TrueNAS version, download the matching (non-prerelease) build, install:

```bash
curl -fsSL https://github.com/truenas-community-sysexts/memryx-mx3-support/releases/latest/download/install.sh | sudo bash
```

Install a specific local image (e.g. a prerelease under hardware test):

```bash
curl -fSL https://github.com/truenas-community-sysexts/memryx-mx3-support/releases/download/<tag>/memryx.raw -o /tmp/memryx.raw
curl -fSL https://github.com/truenas-community-sysexts/memryx-mx3-support/releases/download/<tag>/install.sh | sudo bash -s -- /tmp/memryx.raw
```

## Options

| Option | Effect |
| --- | --- |
| `--pool=NAME` | ZFS pool for persistent config (`/mnt/NAME/.config/memryx`) |
| `--persist-path=PATH` | Exact persist dir; must be `/mnt/<pool>/.config/memryx` |
| `--repo=OWNER/NAME` | Download releases from a fork (or `MEMRYX_REPO` env var) |
| `--check` | Read-only probe of an existing install; exits 1 on any failure |
| `--dry-run` | Validate downloads/checksums/network without modifying the system |
| `--help` | Usage |

When no pool is given, the script reuses an existing `/mnt/*/.config/memryx`,
auto-selects the only data pool, or prompts (interactive) / errors (non-interactive,
ambiguous).

**`--check`** probes: `/dev/memx0` present, `memx_cascade_plus_pcie` loaded,
`mxa-manager` active + `/run/mxa_manager` present, sysext merged, persistent
config + backup, PREINIT script + middleware registration, kernel-version match,
and the PREINIT boot result. Each failure includes a one-line hint.

## What install does

1. **Resolves the persistent pool** and copies `memryx.raw` to
   `/mnt/<pool>/.config/memryx/memryx.raw` (the single activated copy — never
   under `/usr`, which a TrueNAS update wipes).
2. **Activates the sysext**: symlinks `/run/extensions/memryx.raw` → the pool
   copy, `systemd-sysext refresh`, `ldconfig` (so the new `libmemx`/`mx_accl`
   sonames resolve).
3. **Loads the kernel module** via `insmod` (`/lib/modules` is read-only, so
   `modprobe`/`depmod` can't be used) and reloads udev for `/dev/memx0`.
4. **Starts the `mxa-manager` daemon** (`systemctl restart mxa-manager`), which
   creates `/run/mxa_manager`.
5. **Registers a PREINIT script** via `midclt initshutdownscript.create` so the
   sysext + module + daemon come back on every boot, before apps start.

## Persistent layout

```
/mnt/<pool>/.config/memryx/
├── memryx.raw                ← sysext image (activated directly off the pool)
├── .memryx-sdk-version       ← MemryX SDK version (informational)
├── .memryx-repo              ← source repo for error output (informational)
└── memryx-preinit.sh         ← runs before apps start (registered as PREINIT)
```

## Uninstall

```bash
curl -fsSL https://github.com/truenas-community-sysexts/memryx-mx3-support/releases/latest/download/uninstall.sh | sudo bash
```

`uninstall.sh` is a thin alias for `restore.sh`, which: stops `mxa-manager`,
unloads `memx_cascade_plus_pcie` (refusing if it's in use unless `--force`),
unmerges the sysext (re-merging any co-installed sysexts like the NVIDIA one),
deregisters the PREINIT script, and removes the persistent config. `--force`
proceeds even if the module is held, but then a reboot is required.

## Device permissions

The sysext ships `51-memryx-udev.rules` setting `/dev/memx*` to mode `0666`
(world read/write), mirroring MemryX's in-tree rule, so non-root Docker
containers (Frigate) can open the device. On a single-user TrueNAS box this is
fine; for a locked-down alternative use `GROUP="video", MODE="0660"` and add your
container user to the `video` group.
