# Cynthology (alpha)

An open-source NAS operating system for any x86-64 box (BYOB), built around a desktop-style
web UI with a Synology DSM-like workflow, but with original code and artwork and only open-source
components. It runs on Debian 13.

> **Alpha:** a proof of concept to check direction. Don't put the only copy of any data on it.

## Install

**New to this? Follow the step-by-step guide: [INSTALL.md](INSTALL.md).**

**Requirement: a clean, minimal Debian 13 (trixie) x86-64 install.** This works on any PC,
server, or VM (VMware, Proxmox, Hyper-V, VirtualBox).

1. Install Debian 13 from the [netinst ISO](https://www.debian.org/distrib/netinst) onto your **boot disk only**.
   - At "Software selection", untick the desktop environment and tick only **SSH server**
     and **standard system utilities**.
   - Leave the root password empty, so the user you create gets `sudo`. That user
     becomes the Cynthology administrator.
   - Leave the data drives untouched. Cynthology's Storage Manager sets them up.
2. Log in on the console (or over SSH) and run:
   ```bash
   wget -qO- https://raw.githubusercontent.com/RusTus/cynthology/main/get.sh | sudo bash
   ```
3. Open **`https://<box-ip>:5001`** (or `http://<box-ip>:5000`) and sign in with your Debian user.
   The browser warns once about the self-signed certificate, which is expected. A setup wizard
   then walks you through server name, time zone, HTTPS, and storage.

**Updating:** Control Panel → Update → **Update now**, or `sudo cynthology update` on the NAS. Settings, shares and packages are kept.

<details><summary>Other options</summary>

- A specific release: `curl -fsSL https://raw.githubusercontent.com/RusTus/cynthology/main/get.sh | sudo CYN_VERSION=v0.1.1-alpha bash`
- The development code from `main`: `... | sudo CYN_CHANNEL=main bash`
- Offline / manual: download `cynthology-install.tar.gz` from
  [Releases](https://github.com/RusTus/cynthology/releases), then
  `tar xzf cynthology-install.tar.gz && cd cynthology && sudo bash install.sh`
- **All-in-one installer ISO** (Debian 13 + Cynthology in one go, BIOS + UEFI):
  [cynthology-amd64.iso](https://github.com/RusTus/cynthology/releases/latest/download/cynthology-amd64.iso)
  ([sha256](https://github.com/RusTus/cynthology/releases/latest/download/cynthology-amd64.iso.sha256)).
  Boot it, choose **Install Cynthology**, and pick your boot disk when asked where to install.
  It needs internet during install.

</details>

## What works so far

| Area | Status |
|---|---|
| Web desktop: login, taskbar, main menu, draggable windows, widgets | ✅ |
| **Network settings**: DHCP/static IPv4, MTU (jumbo frames), DNS, **link aggregation** (adaptive load balancing, active-backup, LACP); every change is rolled back automatically unless confirmed from the browser within 90 s | ✅ tested incl. failover and a deliberate lock-out |
| **2-step verification** (authenticator app TOTP + backup codes), optional requirement for all admins, sign-in lockout after 5 failed attempts | ✅ |
| HTTPS on 5001 (self-signed or your own certificate), optional HTTP→HTTPS redirect | ✅ |
| First-run setup wizard (server name, time zone/NTP, HTTPS, storage) | ✅ |
| VM guest tools installed automatically (VMware, KVM/Proxmox, Hyper-V) | ✅ |
| Storage Manager: drives, SMART, pools with **Basic / RAID 0 / 1 / 5 / 6 / 10 / SHR / SHR-2**, Btrfs or ext4 | ✅ SHR verified on a real Debian 13 kernel: create, reboot, drive failure, repair, expand |
| SHR expand (add drive, RAID1 tier → RAID5 automatically) / repair (replace failed drive), Btrfs scrub | ✅ |
| **Hot spare**: standby drive per pool; the kernel rebuilds onto it automatically when a drive fails (all SHR tiers) | ✅ verified on a real kernel incl. reboot |
| Shared folders (Btrfs subvolumes), SMB + NFS export, recycle bin | ✅ |
| Snapshots: on demand or hourly/daily/weekly with retention; **browse & restore files** (to original place or as a copy), restore a whole folder (with safety snapshot); Windows **Previous Versions** | ✅ |
| **Snapshot replication** to another Cynthology NAS (btrfs send/receive over SSH, incremental), one-click failover of a replica to a writable shared folder | ✅ |
| **External USB drives**: auto-mount on plug-in as shared folders `usbshare1…` (exFAT, NTFS, FAT32, ext4, Btrfs, XFS; HFS+ read-only), safe eject (refused while in use), format, never offered to Storage Manager; used by Backup | ✅ tested with hot-plugged USB in a VM |
| File Station: browse, upload (drag & drop), download, new folder, rename, copy/move, delete to recycle bin, per-user share permissions | ✅ |
| **Time Machine** for Macs over SMB (Bonjour-discovered, optional size limit); Apple SMB extensions on all shares | ✅ config, streams and Bonjour verified; ⚠️ not yet tried from a real Mac |
| Users & groups (Linux + Samba passwords in sync), admin = `cynthology-admin`/`sudo` group | ✅ |
| Services: SMB, NFS, SSH, WS-Discovery, Bonjour, Docker, rsync | ✅ |
| Resource Monitor (CPU, RAM, network, disk) | ✅ |
| **Backup** (restic): encrypted, versioned backups of shared folders to a USB drive, another NAS/server (SFTP), S3-compatible cloud (Backblaze B2, Wasabi, AWS, MinIO) or a restic REST server; schedules, retention, integrity checks, browse & restore any version, email on failure | ✅ |
| **UPS** (NUT): USB, SNMP or network UPS (incl. a Synology UPS server); safe shutdown after N minutes on battery or at low battery; turn UPS off afterwards; act as UPS server for other devices (Synology-compatible); instant power-failure emails | ✅ |
| Health monitoring + **email alerts** (degraded/rebuilding pool, SMART failure, volume >90% full, certificate expiry) | ✅ |
| **Package Center**: Synology `.spk` packages from SynoCommunity and other sources, Manual Install, install wizards, start/stop/uninstall, **updates** (with automatic rollback if an update fails), **automatic dependency install**, optional nightly auto-update | ✅ tested with real SynoCommunity packages (Gotify, Transmission, Subliminal + Python 3.14) |
| **SSD cache** (LVM dm-cache): read-only (1-4 SSDs, mirrored by default or striped) or read-write (2 mirrored SSDs); hit rate, usage, safe removal (write-back flushed first) | ✅ tested: create, reboot, SSD failure, removal |
| **File Station**: folder tree, back/forward, path bar, search, list/icon view, sortable columns, Create/Upload/Action menus, right-click menu, shortcuts, zip download of folders, Copy/Move to, folder upload | ✅ |
| **Languages**: English, Deutsch, Français, Español, Nederlands (menus, buttons, headings; Personal settings → Language) | partial - help texts still English |
| **Import existing pools** after a reinstall or when moving drives to a new box (arrays re-assembled, volume mounted, shared folders restored), or **erase** drives that hold old data | ✅ tested: SHR with hot spare, reinstall, reboot |
| **Synology alternatives** (Package Center): open-source replacements for DSM's own apps in one place (Photos → PhotoPrism, Drive → Nextcloud, Surveillance Station → Frigate, Active Backup → UrBackup, Office → ONLYOFFICE, MariaDB/phpMyAdmin, DNS → Pi-hole/AdGuard, …), installed as SynoCommunity packages, one-click containers or built-in apps | ✅ |
| **Web Station**: nginx + PHP 8.4 (FPM, common extensions) installed on demand; websites from the `web` shared folder, name- or port-based virtual hosts, HTTPS with the NAS certificate, reverse proxy, PHP settings | ✅ tested on Debian 13 |
| **Container Manager**: containers (start/stop/logs/details, live CPU & memory), images (pull, Docker Hub search, run wizard with ports, shared-folder mappings, env, restart policy), **Compose projects** in the `docker` shared folder (create/edit/start/stop/update/logs), Docker data on a storage pool | ✅ |

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


SSD cache · i18n.
