# Install Reference

How `get.sh` and `install.sh` work, their options, and what ends up on disk.
For the quick path, see the [README](../README.md#quick-start).

## Release artifacts

Each release ships:

- `memryx.raw` + `memryx.raw.sha256` — the sysext image and its checksum
- `install.sh`, `restore.sh`, `uninstall.sh`, `memryx-lib.sh` — the install-side
  scripts

Release tags encode the versions: `v<truenas>-memryx<sdk>-r<run>` (e.g.
`v25.10.4-memryx2.1-r12`).

## Which release gets installed

`get.sh` (on `main`, so it changes only through a reviewed PR) does the
picking, and `install.sh` applies the same rule when it is run on its own:

1. Read the TrueNAS version (`midclt call system.info`) and derive the
   **train**: the major version from 26 on (every 26.x release, betas
   included, is train 26), `major.minor` before that (`25.10`, `25.04`).
2. List every release and keep the **approved** ones. A release is approved
   for a train when its notes carry `<!-- verified-train: <train> -->`, which
   `promote.yml` writes when that train's hardware-test issue closes as
   completed; a full (non-prerelease) release with no marker at all was
   promoted before per-train sign-off and counts for every train.
3. Of those, take the newest built for this box's exact TrueNAS version
   (releases are tagged `v<truenas>-...`).

There is no fallback to an unverified build, on stable or on preview boxes.
With nothing approved, `get.sh` stops, lists the builds waiting for a
hardware test, and links the open hardware-test issues.

**Script-only modes** (`--check`, `--help`, `--uninstall`, or passing your own
`memryx.raw`) run a release's scripts without installing its image, so they
accept the newest approved release built for this box's **own train** when
this TrueNAS version has none. They never take a release built for another
train, grandfathered or not: another train's `--check` inspects a different
install layout and its `restore.sh` runs a different removal flow. Pin one
with `--release=TAG` to use it anyway.

## Install

Detect the TrueNAS version and train, download the approved build, verify the
checksum, and run that release's installer:

```bash
curl -fsSL https://raw.githubusercontent.com/truenas-community-sysexts/memryx-mx3-support/main/get.sh | sudo bash
```

Install one exact release (for example a prerelease under hardware test),
skipping the selection:

```bash
curl -fsSL https://raw.githubusercontent.com/truenas-community-sysexts/memryx-mx3-support/main/get.sh | sudo bash -s -- --release=<tag>
```

Install a specific local image:

```bash
curl -fSL https://github.com/truenas-community-sysexts/memryx-mx3-support/releases/download/<tag>/memryx.raw -o /tmp/memryx.raw
curl -fSL https://github.com/truenas-community-sysexts/memryx-mx3-support/releases/download/<tag>/install.sh | sudo bash -s -- /tmp/memryx.raw
```

## get.sh options

Everything after `bash -s --` goes to the release's `install.sh`, except the
three flags `get.sh` reads itself:

| Option | Effect |
| --- | --- |
| `--release=TAG` | Use that release, with no selection |
| `--repo=OWNER/NAME` | Point the selection and downloads at a fork (or `MEMRYX_REPO`) |
| `--uninstall` | Run the approved release's `uninstall.sh` instead of `install.sh` |

## install.sh options

| Option | Effect |
| --- | --- |
| `--pool=NAME` | ZFS pool for persistent config (`/mnt/NAME/.config/memryx`) |
| `--persist-path=PATH` | Exact persist dir; must be `/mnt/<pool>/.config/memryx` |
| `--repo=OWNER/NAME` | Download releases from a fork (or `MEMRYX_REPO` env var) |
| `--release=TAG` | Install that release (no selection); with a local image, record it as that release's image |
| `--check` | Read-only probe of an existing install; exits 1 on any failure |
| `--update-firmware` | Flash the bundled MX3 firmware (bare metal only) |
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
curl -fsSL https://raw.githubusercontent.com/truenas-community-sysexts/memryx-mx3-support/main/get.sh | sudo bash -s -- --uninstall
```

`get.sh --uninstall` downloads `uninstall.sh`, `restore.sh` and
`memryx-lib.sh` from the approved release and runs them together. Run
straight from a release, `uninstall.sh` and `restore.sh` apply the same
selection themselves for whatever they still have to fetch, and both take
`--release=TAG`.

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
