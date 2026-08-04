# nd45_hsm_dtsu666 -- Deployment Guide

Modbus bridge: Lumel ND45 (grid-tie meter, TCP) and Huawei SmartLogger (PV farm,
TCP) each re-served as a CHINT DTSU666 power meter over RS-485, so a Sigenergy
battery system can read them as its "Power Sensor". Two independent bridges,
two independent RS-485 buses, one process **per bridge**.

Written 2026-08-04 after cutting this host (`wgro1`) over from a stale,
single-bridge, pre-refactor install to the current `nd45_hsm_dtsu666` codebase.
Use this to set up a **second** device the same way.

This file lives in the repo (`docs/DEPLOY.md`) so it travels with a checkout;
on a deployed host it is reachable at
`/persistence/app/current_modbus_converter/docs/DEPLOY.md`.

Full architecture/config reference: `CLAUDE.md` in the repo root. This document
is only the *deploy* procedure plus two incidents worth knowing before you
touch the service.

---

## 0. Two things that will bite you if you skip them

### 0.1 A wildly swinging power reading is the plant, not the bridge

**Both "corruption" incidents originally written up here on 2026-08-04 were
wrong and have been retracted.** Later the same day, measurement showed the
symptom they chased -- `p_total` slamming between roughly +200 kW and -290 kW
within seconds, no read or decode error, not self-correcting -- is a
**closed-loop limit cycle**, period ~3.1-3.3 s: Sigenergy regulates against
the meter this bridge serves, its response returns through the grid
connection, and the loop hunts.

How it was settled, on this host:

- A second, fully independent reader (separate process, own TCP client, no
  output server -- exactly what `diag` does) polled the ND45 *while the
  service kept running* and saw the identical swing **sample for sample**.
- `u_l1` and `freq` stayed flat throughout (0.5 V / 0.02 Hz peak-to-peak)
  while `i_l1` moved 125 → 484 A. No decode fault does that.
- Stopping `nd45-dtsu666@nd45` collapsed the swing to **zero sign changes**
  within ~2 s (site parked at a steady +230 kW); restarting it brought the
  ~3.3 s cycle straight back, unchanged.

**Why the old A/B tests lied:** every arm of them changed whether, or how
steadily, Sigenergy was being fed -- so they measured the plant's reaction,
not the data path. Same trap with `diag`: it looks steady only because §3.2
tells you to run it with the service **stopped**, which puts Sigenergy in its
own safe mode and stops the regulation. That is a regulating plant vs. an idle
one, not two decoders compared like for like.

**Before blaming this software for an unstable reading, reproduce it with a
second independent reader while the service keeps running.** If both agree,
the meter really is seeing it.

No pymodbus 3.6.9 bug is implicated. Consequently the read-activity tracker
(`RtuActivity`/`WireActivity`) is **enabled again for `run`**, gated on
`config.prometheus.enabled` as it originally was, and `/metrics` reports the
Sigenergy read families normally. One bridge per systemd service is still the
rule -- for **fault isolation** (a crash, OOM or restart of one must not
silence the other), which was always its own good reason.

### 0.2 The unstable regulation itself is still open

The limit cycle is a real, unresolved site problem; the bridge just reports it
faithfully. Established so far:

- **The sign convention is correct as `sign: +1`** (measured 2026-08-04 by
  flipping `p_total`/`p_l1..l3` to `-1` in both output maps live). Unregulated
  the site sits at +230 kW; with `+1` the loop pulls the mean down to +71 kW
  (right direction, unstable); with `-1` it drove the grid *up* to +789 kW and
  pinned there, dead steady at 8.6 kW peak-to-peak. **That flat line is
  saturation, not stability** -- the controller pushing the error until it hits
  its power limit. Do not read "the oscillation stopped" as "fixed".
- So the fault is **loop gain/timing**, not sign.
- **The CT-ratio arrangement is correct and is not the cause** (confirmed by
  the site operator 2026-08-04). The `divide_by_ct` split -- classic
  `dtsu_target` (fc=3) divided by `dtsu.identity.ir_at`, the Sigen OEM map
  `dtsu_sigen_ext_target` (fc=4) not divided -- is copied from the original
  Sigen meter, and the values shown in the Sigenergy app match reality. A
  briefly-floated theory that Sigenergy double-applies its CT ratio to the ext
  map is therefore **wrong**; do not re-open it. The read tracker does confirm
  Sigenergy regulates on `fc=4, addr=5404, count=16` (`dtsu_sigen_ext_target`,
  p_total..q_l3, ~9.5 reads/s) and never reads `dtsu_target` at all -- useful
  to know when changing maps, but not a fault.
- What that leaves: since the magnitude, the sign and the CT scaling are all
  confirmed right, the remaining suspects are on the **timing/tuning** side --
  Sigenergy's own regulation period and dead band, and the phase lag this
  bridge adds versus a directly-wired DTSU666 (`source.poll_interval_s` plus
  RTU serving latency at 9600 baud). Not yet investigated.

### 0.3 A crash-loop leaves the bridge dead until a human intervenes

`nd45-dtsu666@.service` sets `Restart=always` with `RestartSec=2` but no
start-limit override, so systemd's defaults apply (`StartLimitBurst=5` per
`StartLimitIntervalSec=10s`). Any genuine crash-loop therefore trips the limit
within ten seconds, systemd gives up permanently, and the unit sits in
`failed` -- **Power Sensor dead until someone runs `systemctl reset-failed`**.
This defeats the assumption in the watchdog design that systemd is the
backstop. Observed for real on this host 2026-08-04. Recovery:

```bash
sudo systemctl reset-failed nd45-dtsu666@nd45
sudo systemctl start nd45-dtsu666@nd45
```

Fixed 2026-08-04: the unit now sets `StartLimitIntervalSec=0` (in `[Unit]`) and
`RestartSec=5`, so systemd retries forever instead of latching. Note the
directive belongs in `[Unit]`, **not** `[Service]` -- systemd moved it in v229
and silently ignores it in the wrong section, which looks like it worked until
you run `systemctl show -p StartLimitIntervalUSec`.

### 0.4 If `/` is an overlayroot, systemd changes evaporate on reboot

This host mounts `/` as an overlay with the writable layer **on tmpfs**:

```
/dev/mmcblk0p2 on /media/root-ro   type ext4  (ro)
tmpfs-root     on /media/root-rw   type tmpfs (rw)
overlayroot    on /                type overlay (lowerdir=/media/root-ro, upperdir=/media/root-rw/overlay)
```

Everything written to `/etc` since boot lives in RAM. `/persistence` is a
separate real partition (`/dev/mmcblk0p3`), so the checkout and its config are
safe -- but **`systemctl enable`, unit files copied into
`/etc/systemd/system/`, and `systemctl disable` of an old unit are all lost on
the next reboot.** Check before trusting any of §3.3:

```bash
mount | grep -E 'overlayroot|root-ro'          # is / an overlay at all?
diff /media/root-ro/etc/systemd/system/nd45-dtsu666@.service \
     /etc/systemd/system/nd45-dtsu666@.service  # on-disk vs live
ls /media/root-ro/etc/systemd/system/multi-user.target.wants/ | grep nd45
```

Found for real on this host 2026-08-04, hours after the cutover: the
`nd45-dtsu666@.service` template and both enable symlinks existed **only** in
tmpfs, while the disk still carried the retired single-process
`nd45-dtsu666.service` from July, still enabled, still pointing at the stale
pre-refactor checkout at `/persistence/app/nd45_dtsu666`. A reboot would have
silently reverted the site to a bridge with no SmartLogger support and without
the over-range fix.

To persist, write through `overlayroot-chroot`, which remounts the real root
read-write and chroots into it:

```bash
sudo overlayroot-chroot sh -s <<'EOF'
W=/etc/systemd/system/multi-user.target.wants
cp /path/to/nd45-dtsu666@.service /etc/systemd/system/    # see note below
chmod 644 /etc/systemd/system/nd45-dtsu666@.service
ln -sfn /etc/systemd/system/nd45-dtsu666@.service $W/nd45-dtsu666@nd45.service
ln -sfn /etc/systemd/system/nd45-dtsu666@.service $W/nd45-dtsu666@smartlogger.service
EOF
```

`/persistence` is **not** mounted inside that chroot, so a `cp` from the
checkout will not work -- feed the unit's contents in through the heredoc, or
stage it somewhere under `/` first. Afterwards verify all three copies agree
(repo, `/etc/systemd/system/`, `/media/root-ro/etc/systemd/system/`) with
`md5sum`, then `systemctl daemon-reload`.

---

## 1. Layout convention

```
/persistence/app/<version-dir>/          the actual checked-out code, e.g.
                                          nd45_hsm_dtsu666 (a git checkout)
/persistence/app/current_modbus_converter  symlink -> the live version-dir
/etc/systemd/system/nd45-dtsu666@.service  WorkingDirectory/ExecStart point at
                                          the symlink, never at a version-dir
                                          directly
/persistence/backup/systemd/             old/replaced unit files, kept for
                                          rollback reference
```

The indirection through `current_modbus_converter` is the whole point: an
upgrade is "check out the new version next to the old one, repoint the
symlink, restart" -- the systemd units never change. See §5.

---

## 2. Prerequisites

- Debian/Ubuntu-family host with systemd, Python 3.10-3.12 (3.13 also verified
  working on this host), and the target UARTs enabled (Raspberry Pi: the
  extra `/dev/ttyAMAn` devices need the relevant `dtoverlay=uart...` lines in
  `/boot/firmware/config.txt` and a reboot -- do this first if the ports
  aren't already present).
- A service account in the `dialout` group (this host uses `ems_admin`; adjust
  `User=`/`SupplementaryGroups=` in the unit file if your convention differs).
- Network reachability to both sources (ND45 and SmartLogger, both Modbus TCP,
  default port 502) from this host.
- The two RS-485 buses wired to Sigenergy, each on its own UART, each
  terminated per the DTSU666/RS-485 wiring notes in `CLAUDE.md`/`README.md`.

## 3. Install

```bash
# 1. Get the code onto the box, under its own version directory.
sudo mkdir -p /persistence/app
cd /persistence/app
git clone <your-remote-url> nd45_hsm_dtsu666    # or: rsync a known-good checkout
sudo chown -R ems_admin:ems_admin nd45_hsm_dtsu666

# 2. Point the stable symlink at it.
sudo ln -s /persistence/app/nd45_hsm_dtsu666 /persistence/app/current_modbus_converter

# 3. Build the venv INSIDE the version dir (not the symlink -- doesn't matter
#    which you use once it exists, but build through a path that still works
#    if the symlink later moves).
cd /persistence/app/current_modbus_converter
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### 3.1 Configure for this site

Edit **on the version directory** (`config/` is not shared between versions
unless you deliberately keep it that way -- see §5 note):

- `config/config.json` -- per bridge: `source.host`/`port`/`unit_id`, the
  RS-485 `dtsu.rtu.port` (which `/dev/ttyAMAn`), `dtsu.slave_id`,
  `dtsu.identity.ir_at` (CT ratio), `safety.max_data_age_s` vs
  `source.poll_interval_s` (config load rejects `max_age < 2x interval`).
  **These are all site-specific. Do not copy this file from another device
  without re-checking every field against that site's wiring and meters.**
- `config/registers.json` -- only if this site's register map genuinely
  differs (new firmware, different meter model). Normally unchanged between
  sites; if you do edit `huawei_*` sections, re-run
  `scripts/gen_smartlogger_map_doc.py` (a test enforces this).

### 3.2 Verify the source(s) BEFORE serving anything

One bridge at a time, nothing else running against that source:

```bash
.venv/bin/python -m nd45_dtsu666 --bridge nd45 diag --interval 0.2        # match source.poll_interval_s
.venv/bin/python -m nd45_dtsu666 --bridge smartlogger diag --interval 1.0
```

Watch the "spread over N samples" table at the bottom for implausible
peak-to-peak / sign flips. See `README.md` "Diagnostics" section for how to
read it. Do this with no systemd unit for this bridge running yet (a second
concurrent master on the same source is itself a source of noise -- see
`README.md`).

### 3.3 Install the systemd units

```bash
sudo cp /persistence/app/current_modbus_converter/systemd/nd45-dtsu666@.service \
        /etc/systemd/system/nd45-dtsu666@.service
sudo systemctl daemon-reload

# bridge names must match config/config.json's bridges[].name
sudo systemctl enable --now nd45-dtsu666@nd45
sudo systemctl enable --now nd45-dtsu666@smartlogger
```

The unit file as checked in already points at
`/persistence/app/current_modbus_converter` -- if this device uses a different
base path, edit the unit (WorkingDirectory, both `--config`/`--registers`
paths, ExecStart's venv path, `ConditionPathExists`) before copying it in, and
update this doc's §1 to match.

**If `/` is an overlayroot on this device, none of the three commands above
survive a reboot** -- neither the copied unit file nor either `enable`. Do
§0.4 as well, and verify there before considering this step done.

### 3.4 Verify

```bash
systemctl status nd45-dtsu666@nd45 nd45-dtsu666@smartlogger
journalctl -u nd45-dtsu666@nd45 -u nd45-dtsu666@smartlogger -f
```

Expect one "DTSU server started (transport=rtu, data fresh...)" per bridge and
no repeated WARNING/ERROR lines after startup. Confirm exactly one process
holds each RS-485 device:

```bash
ps aux | grep nd45_dtsu666
```

Since the Prometheus/`monitor` read-activity check is currently unreliable
(see §0.2), confirm Sigenergy is actually receiving data by checking
**Sigenergy's own UI** for the Power Sensor reading, not this bridge's tools.

---

## 4. Known-good reference config (this host, `wgro1`)

For sanity-checking a second device's config against a working one -- **do
not copy these values**, only the shape:

| | ND45 bridge | SmartLogger bridge |
|---|---|---|
| bridge name | `nd45` | `smartlogger` |
| source type | `nd45` | `huawei` |
| poll interval | 0.2 s | 1.0 s |
| `max_data_age_s` | 3.0 s | 30.0 s |
| RS-485 port | `/dev/ttyAMA2` | `/dev/ttyAMA4` |
| `slave_id` | 10 | 10 (different bus, fine to repeat) |
| `metrics_port` | 8081 | 8082 |

Note `CLAUDE.md` currently documents the SmartLogger port as `/dev/ttyAMA3`;
on this host it is actually `/dev/ttyAMA4` (confirmed against the physical
wiring 2026-08-04) -- that doc line is stale and should be fixed, but check
your own device's physical wiring either way rather than trusting either
source.

---

## 5. Upgrading in place

```bash
cd /persistence/app
git clone <remote-url> nd45_hsm_dtsu666_<date-or-tag>
cd nd45_hsm_dtsu666_<date-or-tag>
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
# copy this device's site-specific config across -- it is NOT part of the
# checkout's identity, don't take the new version's example config
cp /persistence/app/current_modbus_converter/config/config.json config/
cp /persistence/app/current_modbus_converter/config/registers.json config/

sudo ln -sfn /persistence/app/nd45_hsm_dtsu666_<date-or-tag> \
             /persistence/app/current_modbus_converter
sudo systemctl restart nd45-dtsu666@nd45 nd45-dtsu666@smartlogger
```

Verify per §3.4, then optionally remove the old version directory once
confident. If `systemd/nd45-dtsu666@.service` changed between versions,
re-copy and `daemon-reload` before restarting.

### Rollback

```bash
sudo ln -sfn /persistence/app/<previous-version-dir> \
             /persistence/app/current_modbus_converter
sudo systemctl restart nd45-dtsu666@nd45 nd45-dtsu666@smartlogger
```

---

## 6. This host's cutover, for reference

- Was running a stale, pre-refactor, single-process copy at
  `/persistence/app/nd45_dtsu666` (no "hsm") via a non-templated
  `/etc/systemd/system/nd45-dtsu666.service`, last updated 2026-07-07 --
  predates the SmartLogger bridge and a real over-range-detection bugfix.
  That unit is now removed from systemd (backed up at
  `/persistence/backup/systemd/nd45-dtsu666.service.old-single-process`); the
  stale directory itself was left in place, untouched, unreferenced by
  anything -- safe to archive/delete once this cutover has proven itself.
- `/persistence/app/current_modbus_converter` now points at
  `/persistence/app/nd45_hsm_dtsu666`.
- Both bridges run as `nd45-dtsu666@nd45` / `nd45-dtsu666@smartlogger`.
- The two incidents in §0 were found and worked around during this cutover;
  the workaround for §0.2 (disabling the read-activity tracker in `build_bridge`)
  is an uncommitted-at-time-of-writing change on top of the git history --
  confirm `git log`/`git status` in the repo before assuming a fresh clone has
  it.
