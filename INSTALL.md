# Installing Cynthology - step by step

Cynthology is installed in two parts: first a plain **Debian 13** (the operating system underneath),
then **one command** that turns it into a Cynthology NAS. Allow about 30 minutes.

## What you need

- A PC or virtual machine (VMware, Proxmox, VirtualBox, Hyper-V) with an **x86-64 (Intel/AMD) CPU**,
  **2 GB RAM** or more (4 GB recommended) and a network cable (Wi-Fi is not supported).
- A small **system disk** (16 GB or more - an SSD or USB stick is fine) for Debian + Cynthology.
- One or more **data disks** for your files. **They will be erased** when you create a storage pool.
- The Debian 13 installer: <https://www.debian.org/distrib/netinst> → **amd64** → `debian-13.x.x-amd64-netinst.iso`
  (on a real PC, write it to a USB stick with [balenaEtcher](https://etcher.balena.io/) or Rufus).

> **Tip:** connect only the system disk while installing Debian, and add the data disks afterwards.
> That way you can't install Debian onto the wrong disk.

---

## Step 1 - Create the virtual machine (VMware; skip on a real PC)

1. **File → New Virtual Machine → Typical**.
2. **Installer disc image file (iso)** → choose the Debian `netinst` ISO.
3. Guest operating system: **Linux**, version **Debian 12.x 64-bit** (or the newest Debian listed).
4. Name it, e.g. `Cynthology`.
5. Disk size **16 GB**, "Store virtual disk as a single file".
6. **Customize Hardware**: Memory **4 GB**, Processors **2**, Network adapter **Bridged** (so the NAS gets its
   own address on your home network, like a real NAS). Finish.

## Step 2 - Install Debian 13

Boot the machine from the ISO and choose **Graphical install**. Answer the screens like this:

| Screen | What to choose |
|---|---|
| Language / Location / Keyboard | Your own |
| Hostname | `nas` (or any name you like) |
| Domain name | leave empty |
| **Root password** | **leave both fields empty** → Continue. (Your own user then gets administrator rights.) |
| Full name, username, password | Your name, e.g. user `nasadmin`, and a strong password. **This is your Cynthology login.** |
| Time zone | Yours |
| Partitioning | **Guided - use entire disk** → pick the **16 GB system disk** → **All files in one partition** → **Finish partitioning** → **Yes** |
| Scan extra media | No |
| Mirror country / archive | Your country, `deb.debian.org`, proxy empty |
| Popularity contest | No |
| **Software selection** | **Untick** "Debian desktop environment" and "GNOME". **Tick only "SSH server" and "standard system utilities".** |
| Install GRUB boot loader | Yes → the system disk (`/dev/sda`) |
| Installation complete | Continue (the machine restarts) |

## Step 3 - Install Cynthology (one command)

1. At the black login screen, sign in with the **username and password** from step 2.
   (Or, from another computer: `ssh nasadmin@<address>` - the address is shown on the screen or by `ip -br a`.)
2. Type this command and press Enter (enter your password when asked).
   **If you set a root password** in step 2, first type `su -` and the root password, then run the command
   without `sudo`. Your user from step 2 is made the Cynthology administrator either way.

   ```bash
   wget -qO- https://raw.githubusercontent.com/RusTus/cynthology/main/get.sh | sudo bash
   ```

3. Wait 5-10 minutes. It ends with:

   ```
   Cynthology installed.  Open  https://192.168.1.50:5001  (or http://192.168.1.50:5000)
   ```

   Note that address. It is also shown on the NAS's screen from now on.

## Step 4 - Add the data disks

- **VMware:** VM → Settings → **Add… → Hard Disk** (e.g. four disks of 20, 20, 40, 40 GB). Then restart the VM
  (`sudo reboot`).
- **Real PC:** shut down (`sudo poweroff`), connect the drives, power on.

## Step 5 - First sign-in

1. On another computer, open the address from step 3 in a browser, e.g. `https://192.168.1.50:5001`.
2. The browser warns about the certificate - this is expected for a home NAS:
   Chrome/Edge: **Advanced → Proceed**; Firefox: **Advanced → Accept the Risk and Continue**; Safari: **Show Details → visit this website**.
3. Sign in with your **Debian username and password**.
4. The **setup wizard** asks for server name, time zone, secure connection (keep "Always use HTTPS" on) and storage.
   Choose **Create a storage pool now**.

## Step 6 - Create storage and your first shared folder

1. **Storage Manager → Create**: tick the data disks, choose **SHR** (recommended - it uses mixed disk sizes
   well and survives one disk failure), file system **Btrfs** → **Create**.
2. **Control Panel → Shared Folders → Create**: e.g. `family`.
3. Connect from your computers:
   - **Windows:** File Explorer → address bar `\\192.168.1.50\family` → sign in with your Cynthology user.
   - **Mac:** Finder → Go → Connect to Server → `smb://192.168.1.50/family`.

## Recommended next steps

- **Control Panel → Notifications**: enter an email account so the NAS tells you when a disk fails.
- **Snapshots**: give your shared folders a daily schedule (undo mistakes, Windows "Previous Versions").
- **Backup**: RAID is not a backup - back up to a USB drive, another NAS or the cloud.
- **Personal settings** (click your name, top right): turn on **2-step verification**.
- **Control Panel → Network**: give the NAS a fixed address, so it never changes.

## Updating

**Control Panel → Update → Update now** (or `sudo cynthology update` on the NAS). Settings and data are kept.

## Help

- Something not working? Run `sudo journalctl -u cynthology-ui -n 100` on the NAS and include the output when
  you report it (GitHub → Issues).
- Forgot your password? On the NAS screen: reboot, press `e` at the boot menu, add `init=/bin/bash` to the end of the
  line starting with `linux`, press Ctrl+X, then `mount -o remount,rw /`, `passwd <user>`, `exec /sbin/init`.
