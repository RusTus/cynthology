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
| 28 | Expand LUN | SAN Manager → LUN → Expand (bigger size) → vSphere: rescan adapter → datastore → Increase Datastore Capacity | Datastore grows; VMs keep running (LUNs from v0.20/0.21: unmount first, one time) |
| 29 | Firewall | Control Panel → Firewall → On, default Deny, + Allow everything from my network → Apply | Still connected; from another subnet the NAS is unreachable |
| 30 | Auto-block | Sign in with a wrong password 10× from a phone on mobile data / another PC | That address is blocked (Firewall → Blocked now → Unblock) |
| 31 | DDNS + Let's Encrypt | External Access → + Add (e.g. DuckDNS) · router: forward port 80 → NAS · Security → Get certificate | Name resolves to your public IP; browser trusts https://name:5001 |
| 32 | Size limit | Shared Folders → Limit → 1 GB; copy 2 GB of videos into it | Copy stops at ~1 GB ("disk full") |
| 33 | User homes | Users & Groups → User home → on; as a normal user open \\NAS\home | Own private folder; can't open other users' homes |
| 34 | Encrypted folder | Shared Folders → Create → Encrypt (save the recovery key) → copy files → Lock → Unlock with the recovery key | Locked: folder gone from the network; unlocked: files back |
| 35 | Task Scheduler | Control Panel → Task Scheduler → + Create → S.M.A.R.T. test (short) → Run now → Results | Test started on every drive; Storage Manager → Drives shows the result |
| 36 | Permissions | Shared Folders → Permissions → only your user read/write → try another user over SMB | Other user is refused |
| 37 | Domain (if you have AD) | Domain/LDAP → Join → Permissions: @DOMAIN\\Domain Users → sign in on Windows with a domain account | Domain user opens the share; DOMAIN\\admin signs in to the web interface |
| 38 | VPN | Control Panel → VPN Server → On → + Add device → scan the QR code with the WireGuard app; router: forward UDP 51820; phone on mobile data | Phone opens https://<NAS LAN IP>:5001 and the shares |
| 39 | Log Center | Shared Folders → Edit → Log file access → open/delete a file over SMB; sign in once with a wrong password → Log Center | File access shows the open/delete with your user and PC address; Sign-ins shows the failed attempt |
| 40 | Security update | After updating, as a non-admin user: File Station / SMB → the `docker` folder | Not visible / access denied (admins only now) |
| 41 | WebDAV | File Services → WebDAV → Enable (HTTPS 5006) → Mac: Finder → Connect to Server → https://NAS:5006/ (Windows: needs Let's Encrypt or HTTP 5005) | Your shared folders appear; a user without write permission can't save into a read-only folder |
| 42 | SFTP for users | File Services → Let users connect with SFTP → FileZilla/WinSCP to the NAS, port 22, as a normal user | Files work as over SMB; ssh in a terminal says "sftp connections only" |
| 43 | rsync server | File Services → Enable the rsync server → Set password for a user → `rsync -av folder/ rsync://USER@NAS/SHARE/folder/` | Files arrive, owned by that user |
| 44 | Phone notifications | Notifications → + Add service → ntfy (keep the suggested topic) → subscribe to that topic in the ntfy app → Save & send test | Test arrives on the phone; pulling a drive later sends a critical alert |
| 45 | Resource Monitor history | Leave the NAS on for a few hours → Resource Monitor → History → 24 hours; copy a big file meanwhile | Network/disk show the copy at the right time; hovering shows the values |
| 46 | Sharing links | File Station → right-click a folder → Share link… → password, 7 days → Create → open the link on your phone (or a private window) | Password asked; files listed; Download all (zip) works; deleting the link makes it stop working |
| 19 | Synology alternatives | Package Center → Synology alternatives → MariaDB → Install, then phpMyAdmin → Install → Open (server: NAS IP, user root, password from Info) | phpMyAdmin logs in |

Logs if something breaks: `sudo journalctl -u cynthology-ui -n 100`
