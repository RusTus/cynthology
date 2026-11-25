# Cynthology (alpha)

An open-source NAS operating system for any x86-64 box (BYOB), built around a desktop-style
web UI with a Synology DSM-like workflow, but with original code and artwork and only open-source
components. It runs on Debian 13.

> **Alpha:** a proof of concept to check direction. Don't put the only copy of any data on it.

## ⬇️ Download (current release)

| File | What it is |
|---|---|
| **[cynthology-amd64.iso](https://github.com/RusTus/cynthology/releases/latest/download/cynthology-amd64.iso)** · [sha256](https://github.com/RusTus/cynthology/releases/latest/download/cynthology-amd64.iso.sha256) | Bootable installer for USB stick or VM (BIOS + UEFI, ~760 MB) |
| **[cynthology-install.tar.gz](https://github.com/RusTus/cynthology/releases/latest/download/cynthology-install.tar.gz)** · [sha256](https://github.com/RusTus/cynthology/releases/latest/download/cynthology-install.tar.gz.sha256) | Install onto an existing Debian 13 system |

Release notes and older versions: **[Releases](https://github.com/RusTus/cynthology/releases)**

## Install from the ISO (recommended)

1. Write the ISO to a USB stick (balenaEtcher, Rufus in **DD mode**, or `dd`), or attach it to a VM.
2. Boot it and choose **Install Cynthology**. The box needs a wired internet connection during install.
3. The installer asks for language, network, hostname, a **user and password** (that user becomes
   the administrator), and **which disk to install the system on**. Pick your small boot
   SSD/USB disk and leave the data drives alone. Storage Manager sets those up later.
4. After the reboot, the console shows the address: open `http://<box-ip>:5000` and sign in.

## Install on an existing Debian 13 box

```bash
curl -LO https://github.com/RusTus/cynthology/releases/latest/download/cynthology-install.tar.gz
tar xzf cynthology-install.tar.gz && cd cynthology && sudo bash install.sh
```

Leave the data drives empty: Storage Manager wipes the drives you select when it creates a pool.

## What works so far

| Area | Status |
|---|---|
| Web desktop: login, taskbar, main menu, draggable windows, widgets | ✅ |
| Storage Manager: drives, SMART, pools with **Basic / RAID 0 / 1 / 5 / 6 / 10 / SHR / SHR-2**, Btrfs or ext4 | ✅ UI + planner (unit-tested); ⚠️ mdadm/LVM steps not yet run on real hardware |
| SHR expand (add drive) / repair (replace failed drive), Btrfs scrub | ⚠️ first version, untested on hardware |
| Shared folders (Btrfs subvolumes), SMB + NFS export, recycle bin, snapshots | ✅ |
| Users & groups (Linux + Samba passwords in sync), admin = `cynthology-admin`/`sudo` group | ✅ |
| Services: SMB, NFS, SSH, WS-Discovery, Bonjour, Docker, rsync | ✅ |
| Resource Monitor (CPU, RAM, network, disk) | ✅ |
| **Package Center**: Synology `.spk` packages from SynoCommunity and other sources, Manual Install, install wizards, start/stop/uninstall | ✅ tested with real SynoCommunity packages (Gotify, Transmission) |
| Containers tab (Docker Compose one-click apps) | ✅ basic |

## How SHR works here

`backend/cyn/raidplan.py` cuts mixed-size drives into tiers at each distinct drive size. Each
tier becomes its own mdadm array (RAID 5/6 when enough drives reach it, otherwise a mirror), and
LVM joins the arrays into one volume, which is then formatted Btrfs. Example: 4+4+8+8 TB gives
16 TB usable with SHR, 12 TB with RAID 5.

## How .spk compatibility works

`backend/cyn/spk.py` reads the public SPK format (INFO, package.tgz, scripts, conf, wizard)
and reproduces the DSM 7 environment on Debian: `/var/packages/<pkg>` with
`target/var/home/tmp/etc` on `/volume1/@app*`, a dedicated user per `conf/privilege`,
`SYNOPKG_*` variables, `usr-local-linker`, `data-share` (shared folder + ACL), static and
scripted (`install_uifile.sh`) wizards, and a systemd unit per package. Small shims for
`synopkg`, `synoshare` and friends are in `rootfs/usr/lib/cynthology/syno-shims`.

It cannot run Synology's own closed packages (Synology Photos, Active Backup and so on),
or anything that depends on them (WebStation, Synology's PHP/Apache). Those show as
"Not compatible". Packages built for ARM are refused.

## Develop without a NAS

```bash
pip install fastapi uvicorn psutil pytest
cd backend
CYN_DEV=1 uvicorn cynthology.main:app --reload --port 5000   # log in as admin / admin, drives are mocked
python -m pytest tests
```

## Making a release

Push a tag like `v0.1.2-alpha` (or run **Actions → Release ISO** by hand). GitHub Actions
builds the ISO by remastering the official Debian netinst (`image/build-iso.sh`) and publishes
it, so the Download links above always point at the newest release.

## Roadmap

HTTPS + first-run wizard · File Station · snapshot schedules and
replication · Hyper-Backup-style backups (restic/borg) · UPS · notifications/email ·
package upgrades and dependency installs · hot-spare and SSD cache · 2FA · i18n.
