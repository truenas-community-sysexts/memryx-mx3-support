# Troubleshooting

Start with the built-in probe — it checks every failure point and prints a hint
for each:

```bash
sudo ./install.sh --check
```

## `/dev/memx0` does not appear after a TrueNAS update

After a TrueNAS upgrade the running kernel changes, and the sysext logs an
insmod failure: `memx_cascade_plus_pcie` is compiled against an exact kernel
version, so a module built for the old kernel won't load on the new one.

This is **expected** on a TrueNAS upgrade, not a bug. To recover:

1. Check the running kernel: `uname -r`.
2. Check what the sysext shipped:
   `ls /usr/lib/modules/*/extra/memx_cascade_plus_pcie.ko`.
3. Install a `memryx.raw` release built for the new kernel. Releases are tagged
   `v<truenas>-memryx<sdk>-r<run>`; the release notes record the target kernel.
   Re-run `install.sh` — it auto-detects the new TrueNAS version and fetches the
   matching build.

If no matching release exists yet, the daily `check-releases` workflow will
produce one once the new TrueNAS version is published; or trigger `build.yml`
manually with the new `truenas_version`.

## `/dev/memx0` exists but Frigate can't use the MX3

The device node alone is not enough — the **`mxa-manager` daemon** must be
running and the container must see its socket.

1. `systemctl status mxa-manager` — must be `active (running)`.
   - If it's failing, check `journalctl -u mxa-manager -b`. The unit waits up to
     ~15s for `/dev/memx0`; if the card is absent it lands in `failed`.
   - **`[critical] Config file not found at /etc/memryx/mxa_manager.conf`** — the
     SDK 2.1 `mxa_manager` reads that hardcoded path unconditionally and a sysext
     can't ship `/etc`. Fixed in **r3+**, where an `ExecStartPre` copies the
     bundled conf into `/etc/memryx/` on every start. (r1/r2 hit this.) To fix an
     older install, reinstall an r3+ release, or create the file by hand:
     ```bash
     mkdir -p /etc/memryx
     printf '%s\n' 'LISTEN_ADDRESS="/run/mxa_manager/"' 'BASE_PORT=10000' 'LOG_LEVEL=low' 'HW_MONITOR_INTERVAL=500' \
       > /etc/memryx/mxa_manager.conf
     rm -rf /run/systemd/system/mxa-manager.service.d   # drop any earlier override
     systemctl daemon-reload && systemctl reset-failed mxa-manager && systemctl restart mxa-manager
     ```
2. `ls /run/mxa_manager` — the socket directory must exist (the daemon creates it).
3. The Frigate container must map **both** the device and the socket, and run
   privileged:
   ```yaml
   devices:
     - /dev/memx0
   volumes:
     - /run/mxa_manager:/run/mxa_manager
   privileged: true
   ```
4. The detector type must be `memryx` (not `edgetpu`).

## SDK / Frigate version mismatch

Frigate's stable release supports exactly one MemryX SDK (currently 2.1). If you
installed a build for a different SDK than your Frigate expects, the detector
won't initialise. Check `cat /mnt/<pool>/.config/memryx/.memryx-sdk-version`
against the SDK Frigate pins in
[`docker/memryx/user_installation.sh`](https://github.com/blakeblackshear/frigate/blob/dev/docker/memryx/user_installation.sh).
CI tracks this automatically, so the Latest release should match Frigate's stable.

## Firmware version mismatch

The MX3 M.2 boots firmware from its on-board QSPI flash. The sysext bundles the
firmware blobs but does **not** re-flash the card automatically (a hardware
write). If the driver reports a firmware/driver version mismatch (e.g. after a
large SDK jump), follow MemryX's
[firmware update guide](https://developer.memryx.com/tutorials/how_to/update_firmware.html)
using the bundled flash tool, or temporarily install MemryX's own `memx-drivers`
package which re-flashes at install. (An explicit `install.sh --update-firmware`
step is a planned addition — see [build-ci-notes.md](build-ci-notes.md).)

## Daemon didn't start on boot

The sysext-shipped `[Install] WantedBy=` symlinks are ignored by TrueNAS at boot;
activation is driven by the PREINIT script. Confirm it's registered and ran:

```bash
sudo midclt call initshutdownscript.query | python3 -m json.tool   # look for memryx-preinit
journalctl -b -t memryx-preinit                                    # this boot's log
```

A successful run ends with a `Done` line. If there's no entry, re-run
`install.sh` to re-register PREINIT.

## Secure Boot

The kernel module is unsigned. If Secure Boot is enabled, the module load is
rejected. Disable Secure Boot in firmware, or sign the module yourself.
