# TrueNAS Sysext Notes

Practical knowledge about building and shipping systemd-sysext packages on TrueNAS. Gathered from this project and from other community sysext projects.

## Boot-time unit activation

Sysext-shipped systemd units with `[Install] WantedBy=multi-user.target` are silently skipped at boot on TrueNAS. No journal entries, no errors. The reliable pattern is a `midclt initshutdownscript` entry with `type=COMMAND` and `when=PREINIT`, which runs a script that calls `systemctl start <unit>` explicitly.

## PREINIT vs POSTINIT

PREINIT runs after ZFS pools are mounted but before the middleware starts apps. POSTINIT runs after the middleware is up, by which time app containers may already be starting. For device drivers that apps depend on, PREINIT is the only safe option.

## Read-only filesystem constraints

| Path | Writable? | Notes |
|------|-----------|-------|
| `/usr` | No (ZFS readonly) | Can be temporarily unlocked via `zfs set readonly=off` |
| `/lib` | No (separate ZFS dataset) | Symlink to `/usr/lib` but on its own readonly dataset |
| `/lib/modules` | No | Part of the `/lib` dataset |
| `/lib/firmware` | No | Part of the `/lib` dataset |
| `/run/extensions` | Yes (tmpfs) | Where sysext symlinks go, cleared on reboot |
| `/mnt/<pool>` | Yes | ZFS data pools, persistent across updates |

Because of this:
- `depmod` cannot write to `/lib/modules`, so `modprobe` won't find out-of-tree modules. Use `insmod` with an absolute path instead.
- You do not need to write the `.raw` under `/usr` at all. Keep it on a data pool and symlink `/run/extensions/` straight at it (see below), which avoids the `zfs set readonly=off/on` dance entirely.

## Sysext activation path

TrueNAS does not use the standard `systemd-sysext merge` path (`/var/lib/extensions/`). The working pattern is:

1. Keep `<name>.raw` on a ZFS data pool, e.g. `/mnt/<pool>/.config/<name>/<name>.raw`
2. Symlink into `/run/extensions/<name>.raw`
3. `systemd-sysext refresh`
4. `ldconfig`

`systemd-sysext` loop-mounts whatever the symlink resolves to. `loop_device_make_by_path()` is filesystem-agnostic, so the image can live on the data pool just as well as on the boot pool; there is no need to copy it under `/usr/` first. The `/run/extensions/` symlink is on tmpfs and disappears on every reboot, which is why the PREINIT script must recreate it.

This only applies to **additive** sysexts (ones that add new files to `/usr`). A sysext that replaces stock TrueNAS files is a different problem and is out of scope here.

## TrueNAS updates wipe /usr

Any sysext placed in `/usr/` is lost when TrueNAS updates (the rootfs is replaced). Keeping the `.raw` on a ZFS data pool (`/mnt/<pool>/`) sidesteps this: the image is never on `/usr` in the first place, so an update cannot wipe it. A PREINIT script recreates the `/run/extensions/` symlink and refreshes on each boot.

## `Type=oneshot RemainAfterExit=yes` gotcha

`systemctl start` on a unit with `Type=oneshot` and `RemainAfterExit=yes` is a no-op once the unit is already active. If the unit needs to re-execute (e.g. after config changes), use `systemctl restart` instead.

## Live driver swap

Replacing a sysext that contains kernel modules at runtime leaves the previous driver's kernel modules in memory. The new modules only load after a reboot. Install scripts should warn the user when a reboot is required.
