# Testing Cynthology (v0.14.0-alpha)

Start from a **clean, minimal Debian 13** VM (no desktop; "SSH server" + "standard utilities"; root password
left empty so your user gets sudo). Give the VM **4+ extra virtual disks** (mixed sizes, e.g. 20/20/40/40 GB)
and, optionally, a **second network card**.

```bash
wget -qO- https://raw.githubusercontent.com/RusTus/cynthology/main/get.sh | sudo bash
```

Open `https://<vm-ip>:5001` (accept the self-signed certificate) and sign in with your Debian user.
Already installed an older version? Run the same command again - it upgrades in place.

## Checklist (report anything odd - a screenshot or the error text is enough)

| # | Area | Try this | Expected |
|---|------|----------|----------|
| 1 | Setup wizard | First sign-in | Server name, time zone, HTTPS, storage steps; ends on https://…:5001 |
| 2 | Storage | Storage Manager → Create → SHR with all extra disks | Pool "healthy", capacity as previewed |
| 3 | Hot spare | Add one more disk, Storage Pools → Add hot spare | "Hot spare: sdX" shown |
| 4 | Shares | Control Panel → Shared Folders → create `family` | Visible from Windows/macOS as `\\<vm-ip>\family` |
| 5 | File Station | Upload by drag & drop, rename, delete (goes to #recycle) | Works; non-admin users only see their folders |
| 6 | Snapshots | Snapshots → Take snapshot; on Windows: file → Properties → Previous Versions | Snapshot listed / version restorable |
| 7 | Backup | Backup → task to a folder on another pool (or USB) → Back up now → Restore a file | Succeeded; file restored |
| 8 | Package Center | Install Gotify (SynoCommunity) → Open | Gotify web page |
| 9 | Containers | Container Manager → Settings → Move Docker data; Registry → nginx → Download → Run (port 8080) | Page on :8080 |
| 10 | USB | VMware: VM → Removable Devices → your stick → Connect | `usbshare1` appears; Eject works |
| 11 | Network | Use the 2nd NIC: give it a fixed IP → Apply | Applied & confirmed automatically |
| 12 | Network rollback (optional) | Give the NIC you are connected through a fixed IP in a *wrong* subnet → Apply (keep the VMware console open just in case) | NAS unreachable, then back on its old address ~2 min later; alert "Network change rolled back" |
| 13 | 2-step | Your name → Personal settings → Turn on 2-step verification; sign out/in | Code required |
| 14 | Notifications | Control Panel → Notifications → SMTP + Send test email | Mail arrives |
| 15 | Time Machine | File Services → Time Machine on; on a Mac: Time Machine → Add Backup Disk | NAS listed; backup starts |
| 16 | UPS | Only if you have a USB UPS: Control Panel → UPS → Detect | Battery status shown |
| 17 | Drive failure (VM) | VMware: remove one pool disk while running | Pool degraded → hot spare rebuilds; email |
| 18 | Web Station | Main menu → Web Station → Install; then copy an `index.php` (`<?php phpinfo();`) into the `web` share and open `http://<NAS>/` | PHP info page |
| 20 | Reinstall with existing drives | Reinstall Debian + Cynthology on the system disk only, keep the pool drives → Storage Manager → Overview → Import (or Erase drives) | Pool and its shared folders are back with their files (or drives become free for a new pool) |
| 21 | File Station | Right-click a folder → Download (zip); Action → Move to…; search box | Zip downloads; item moved; results show their folder |
| 22 | SSD cache | VMware: add 2 small disks → Storage Manager → pool → Create SSD cache (read-only, mirrored) → copy files → reboot | Pool healthy after reboot, hit rate shown; Remove SSD cache frees the disks |
| 23 | VLAN | Control Panel → Network → + Add VLAN (e.g. 10 on your 2nd port) → Apply | Interface `<port>.10` appears (needs a VLAN-capable switch/vSwitch to get an address) |
| 24 | Language | Your name → Personal settings → Language → Deutsch | Menus, buttons and headings in German |
| 25 | iSCSI + VAAI (ESXi) | SAN Manager → LUN → Create (thin, new target) → vSphere: iSCSI adapter → Dynamic Discovery → NAS:3260 → rescan → New Datastore (VMFS) | Datastore created; Hardware Acceleration = Supported |
| 26 | LUN snapshots | SAN Manager → LUN → Snapshots → Take snapshot; change a file in a VM; Clone as new LUN → map it, mount in ESXi (resignature) | Clone shows the old state |
| 27 | LUN replication | Snapshots → Replication → + Replicate → choose "LUN …" → Sync now (twice) | Second run "changes only"; on the other NAS: Receive replicas → Use as LUN |
| 19 | Synology alternatives | Package Center → Synology alternatives → MariaDB → Install, then phpMyAdmin → Install → Open (server: NAS IP, user root, password from Info) | phpMyAdmin logs in |

Logs if something breaks: `sudo journalctl -u cynthology-ui -n 100`
