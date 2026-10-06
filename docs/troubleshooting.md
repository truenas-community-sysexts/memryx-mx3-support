# Troubleshooting

Start with the built-in probe — it checks every failure point and prints a hint
for each:

```bash
sudo ./install.sh --check
```

## `/dev/memx0` does not appear after a TrueNAS update

When a TrueNAS update changes the kernel, the boot-time PREINIT script logs
the mismatch and the module does not load:

```
[memryx-preinit] ERROR: Kernel version mismatch: running <new-kver> but sysext has a module for <old-kver>
[memryx-preinit] ERROR: TrueNAS was likely updated. Download a new memryx.raw release matching <new-kver>
[memryx-preinit] ERROR: Visit https://github.com/<repo>/releases
```

`memx_cascade_plus_pcie` is compiled against an exact kernel version, so a
module built for the old kernel won't load on the new one. This is
**expected** on a TrueNAS upgrade that moves the kernel, not a bug. Many
point releases keep the kernel, and on those the installed build keeps
working. To recover:

1. Check the running kernel: `uname -r`.
2. Check what the sysext shipped:
   `ls /usr/lib/modules/*/extra/memx_cascade_plus_pcie.ko`.
3. Re-run the installer. It reads the running kernel and installs the
   approved build for it, whichever TrueNAS version that build was made
   for. Releases are tagged by kernel (`k<kernel>-memryx<sdk>-r<run>`, e.g.
   `k6.12.105-...` for kernel `6.12.105-production+truenas`); older ones use
   `v<truenas>-...` tags, and either way the release notes record the exact
   `Target kernel`:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/truenas-community-sysexts/memryx-mx3-support/main/get.sh | sudo bash
   ```

If no build for the new kernel exists yet, the daily `check-releases`
workflow produces one once the new TrueNAS version is published; or trigger
`build.yml` manually with the new `truenas_version`. If a build exists but is
still waiting for its hardware test, see the next section.

## No approved build for your kernel yet

The installer only installs a build that a hardware test approved for your
TrueNAS train (see [Which release gets installed](install.md#which-release-gets-installed)).
A new build is a pre-release with an open hardware-test issue until someone
verifies it on real hardware, so for a while the installer stops with:

```
No approved stable release found for kernel <kver> (TrueNAS <version>).
Only a release approved for TrueNAS train <train> is installed: a hardware test on
that train signed it off, or it was promoted before per-train sign-off.
A build for kernel <kver> exists but is a prerelease awaiting hardware-test
promotion; re-run this installer once it is promoted.
Builds waiting for a hardware test:
  <tag> (prerelease)
Open hardware-test issues for these builds, waiting for a tester:
  #<n> <issue title>
  <issue url>
```

The issue is the one step left: once a tester closes it as completed, the
same one-liner installs the build. If you have the hardware, the issue body
lists the exact test steps; testing it yourself and reporting back is the
fastest way to get it approved. Installing it before then is possible by hand
(the issue's steps, or `get.sh` with `--release=<tag>`), but it is untested
on your train.

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
   **`privileged: true`** (not just `cap_add: SYS_RAWIO` — confirmed insufficient).
   The TrueNAS **catalog** app can't be privileged, so run Frigate as a **Custom
   App** (compose):
   ```yaml
   devices:
     - /dev/memx0
   volumes:
     - /run/mxa_manager:/run/mxa_manager
   privileged: true
   ```
4. The detector type must be `memryx` (not `edgetpu`), with `device: PCIe:0`.

## SDK / Frigate version mismatch

Frigate's stable release supports exactly one MemryX SDK (currently 2.1). If you
installed a build for a different SDK than your Frigate expects, the detector
won't initialise. Check `cat /mnt/<pool>/.config/memryx/.memryx-sdk-version`
against the SDK Frigate pins in
[`docker/memryx/user_installation.sh`](https://github.com/blakeblackshear/frigate/blob/dev/docker/memryx/user_installation.sh).
CI tracks this automatically, so the Latest release should match Frigate's stable.

## `Init DFP Runner failed` / `accelerator has <garbage> chips`

This is the most common MX3+Frigate failure and has **two independent causes** —
work through both. The tell-tale line is e.g.
`Input DFP was compiled for 4 chips, but the connected accelerator has 301989888
chips`, or `Driver required firmware anti_rollback cnt >= 6`.

**Isolate host vs container first.** Run the bundled benchmark on the host:
```bash
acclBench -H                              # device info: should list 4 chips
acclBench -d /path/to/yolo_nas_s.dfp      # full init: should benchmark, no errors
```

- **`acclBench -d` fails on the host with `anti_rollback cnt >= 6`** → it's
  [firmware](#firmware) (cause 1). The card's firmware is too old.
- **`acclBench` works on the host but Frigate still reports `301989888 chips`** →
  it's container privilege (cause 2). Frigate's detector `mmap`s the chip's BAR
  memory, which a non-privileged container can't do. Run Frigate as a **privileged
  Custom App** (`privileged: true` — `cap_add: SYS_RAWIO` is **not** enough). The
  TrueNAS catalog app can't be privileged; see the [README](../README.md#using-with-frigate).

## Firmware

The MX3 boots firmware from its on-board QSPI flash. The SDK 2.1 runtime requires
the firmware's **anti-rollback counter ≥ 6**; older cards fail with the chip-count
error above. Check the device:
```bash
cat /sys/memx0/verinfo            # FW_CommitID = current firmware rev
```

The sysext bundles a cnt ≥ 6 image plus the flash tools. To update **on bare-metal
TrueNAS**:
```bash
sudo ./install.sh --update-firmware       # flashes, then tells you to power-cycle
```
Then **fully power off and on** — the card only re-reads QSPI firmware at power-on;
a soft reboot is not enough.

**On a hypervisor (PCIe passthrough), you cannot flash from inside the TrueNAS
VM.** VFIO silently swallows the QSPI writes — the flash tool reports success but
`verinfo`'s `FW_CommitID` never changes. `--update-firmware` detects the VM and
refuses with instructions. Flash from the **hypervisor host**:
```bash
# VM shut down. On the host:
echo <BDF> > /sys/bus/pci/drivers/vfio-pci/unbind      # BDF from: lspci -d 1fe9:
git clone --depth 1 https://github.com/memryx/mx3_driver_pub && cd mx3_driver_pub
tools/flash_update_tool/bin/x86_64/pcieupdateflash -f firmware/cascade_4chips_flash.bin
# then FULLY power-cycle the physical host (not a reboot)
```

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
