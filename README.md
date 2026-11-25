# CairnOS (alpha)

An open-source NAS operating system for any x86-64 box (BYOB), built around a desktop-style
web UI with a Synology DSM-like workflow, but with original code and artwork and only open-source
components. It runs on Debian.

> **Alpha 0.1:** a proof of concept to check direction. Don't put the only copy of any data on it.

## Install (alpha)

1. Install **Debian 13 "trixie"** (or 12) x86-64 on the boot drive: the *netinst* ISO, with
   only "SSH server" and "standard system utilities" selected. Create a user and give it sudo
   (leave the root password empty in the installer; that user then gets sudo automatically).
2. Copy `cairnos-0.1.0-alpha.tar.gz` to the box, then:
   ```bash
   tar xzf cairnos-0.1.0-alpha.tar.gz
   cd cairnos
   sudo bash install.sh
   ```
3. Open `http://<box-ip>:5000` and sign in with that Debian user and password.

Leave the data drives empty: Storage Manager wipes the drives you select when it creates a pool.

## What works in 0.1

| Area | Status |
|---|---|
| Web desktop: login, taskbar, main menu, draggable windows, widgets | ✅ |
| Storage Manager: drives, SMART, pools with **Basic / RAID 0 / 1 / 5 / 6 / 10 / SHR / SHR-2**, Btrfs or ext4 | ✅ UI + planner (unit-tested); ⚠️ mdadm/LVM steps not yet run on real hardware |
| SHR expand (add drive) / repair (replace failed drive), Btrfs scrub | ⚠️ first version, untested on hardware |
| Shared folders (Btrfs subvolumes), SMB + NFS export, recycle bin, snapshots | ✅ |
| Users & groups (Linux + Samba passwords in sync), admin = `cairn-admin`/`sudo` group | ✅ |
| Services: SMB, NFS, SSH, WS-Discovery, Bonjour, Docker, rsync | ✅ |
| Resource Monitor (CPU, RAM, network, disk) | ✅ |
| **Package Center**: Synology `.spk` packages from SynoCommunity and other sources, Manual Install, install wizards, start/stop/uninstall | ✅ tested with real SynoCommunity packages (Gotify, Transmission) |
| Containers tab (Docker Compose one-click apps) | ✅ basic |

## How SHR works here

`backend/cairn/raidplan.py` cuts mixed-size drives into tiers at each distinct drive size. Each
tier becomes its own mdadm array (RAID 5/6 when enough drives reach it, otherwise a mirror), and
LVM joins the arrays into one volume, which is then formatted Btrfs. Example: 4+4+8+8 TB gives
16 TB usable with SHR, 12 TB with RAID 5.

## How .spk compatibility works

`backend/cairn/spk.py` reads the public SPK format (INFO, package.tgz, scripts, conf, wizard)
and reproduces the DSM 7 environment on Debian: `/var/packages/<pkg>` with
`target/var/home/tmp/etc` on `/volume1/@app*`, a dedicated user per `conf/privilege`,
`SYNOPKG_*` variables, `usr-local-linker`, `data-share` (shared folder + ACL), static and
scripted (`install_uifile.sh`) wizards, and a systemd unit per package. Small shims for
`synopkg`, `synoshare` and friends are in `rootfs/usr/lib/cairn/syno-shims`.

It cannot run Synology's own closed packages (Synology Photos, Active Backup and so on),
or anything that depends on them (WebStation, Synology's PHP/Apache). Those show as
"Not compatible". Packages built for ARM are refused.

## Develop without a NAS

```bash
pip install fastapi uvicorn psutil pytest
cd backend
CAIRN_DEV=1 uvicorn cairn.main:app --reload --port 5000   # log in as admin / admin, drives are mocked
python -m pytest tests
```

## Roadmap

Bootable installer ISO · HTTPS + first-run wizard · File Station · snapshot schedules and
replication · Hyper-Backup-style backups (restic/borg) · UPS · notifications/email ·
package upgrades and dependency installs · hot-spare and SSD cache · 2FA · i18n.
