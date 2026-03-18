# blitzback – HOWTO

Package-aware backup with hardlink versioning for Arch Linux.

---

## Requirements

```
python >= 3.11
rsync
pacman          (Arch Linux)
yay             (optional, AUR helper – auto-detected)
flatpak         (optional – auto-detected)
```

---

## Quick start

```bash
# Run as root for full file access
sudo python blitzback.py
```

That's it for the first run. Backup is written to `/var/backups/blitzback/`.

---

## What the script does

### 1. Package manager detection

blitzback auto-detects which package managers are available and logs them:

```
Package manager(s) detected: pacman + yay
```

### 2. Pacman / yay – find modified package files

```
pacman -Qkk
```

Verifies every installed file (including AUR packages installed via yay) against
the mtree checksum stored in the package database. Files that differ from the
original (e.g. a tweaked config in `/etc/`) are backed up separately under
`pkg_modified/`, preserving the full path. Each file is tagged as `[official]`
or `[AUR]` in the list.

Example: `/etc/ssh/sshd_config` was edited after installation
→ saved as `<snapshot>/pkg_modified/etc/ssh/sshd_config`
→ listed as `[official]  /etc/ssh/sshd_config` in `pkg_modified_files.txt`

### 3. Package inventory

Every snapshot contains a `packages/` directory with four files:

| File | Contents |
|---|---|
| `all.txt` | all installed packages with version (`pacman -Q`) |
| `official.txt` | packages from official repos only |
| `aur.txt` | AUR / foreign packages only (`pacman -Qm`) |
| `flatpak.txt` | flatpak apps with version + origin (if flatpak is installed) |

### 4. Unowned files

Scans `/etc` and `/usr/local` for files that don't belong to any installed
package. These are **not** backed up automatically, but listed in
`unowned_files.txt` inside the snapshot for manual review.

### 5. Flatpak – user data

If `flatpak` is installed, app user-data is backed up automatically:

```
~/.var/app/         → flatpak_<username>/
/root/.var/app/     → flatpak_root/         (root only)
```

The flatpak binaries themselves (`/var/lib/flatpak/`) are skipped –
they can be reinstalled at any time.

### 6. Configured directories

All directories listed in `include` are backed up in full
(default: `/etc /home /root /srv /var/spool/cron`).

### 7. Hardlink versioning

Every run creates a new snapshot. Unchanged files are stored as hardlinks
pointing to the previous snapshot – they use no additional disk space.

```
snapshots/
  20260318-083000/   ← full snapshot (hardlinks where possible)
  20260318-190000/   ← only changed files occupy new space
  latest             ← symlink to the most recent snapshot
```

---

## Configuration

### Generate a config file

```bash
python blitzback.py --makeconf
```

Writes `blitzback.conf` in the current directory (TOML format).
If `blitzback.conf` already exists, the template is written to
`blitzback.conf.templ` instead.

### Option A – Environment variables

```bash
export BLITZBACK_DIR=/mnt/backup/blitzback
export BLITZBACK_INCLUDE=/etc:/home:/root:/opt
export BLITZBACK_EXCLUDE=.cache:node_modules:.git:*.tmp
export BLITZBACK_PKG_CHECK=true

sudo -E python blitzback.py
```

(`sudo -E` passes the environment variables through to root)

### Option B – Config file (TOML)

`/etc/blitzback.conf` (system-wide) or `~/.config/blitzback.conf` (per user)
or `./blitzback.conf` (local):

```toml
[blitzback]
dir     = "/mnt/backup/blitzback"
include = ["/etc", "/home", "/root", "/opt", "/var/spool/cron"]
exclude = [".cache", "*.cache", "node_modules", "__pycache__", ".git", ".Trash", ".cache/yay"]
pkg_check = true
```

Environment variables override config file values.

### All options

| Option      | Env variable          | Default                                        | Description                           |
|-------------|-----------------------|------------------------------------------------|---------------------------------------|
| `dir`       | `BLITZBACK_DIR`       | `/var/backups/blitzback`                       | Backup root directory                 |
| `include`   | `BLITZBACK_INCLUDE`   | `/etc:/home:/root:/srv:/var/spool/cron`        | Colon-separated dirs to back up       |
| `exclude`   | `BLITZBACK_EXCLUDE`   | `.cache:*.cache:node_modules:__pycache__:.git` | rsync exclude patterns (colon-separated) |
| `pkg_check` | `BLITZBACK_PKG_CHECK` | `true`                                         | Enable/disable pacman integrity check |

---

## Backup directory layout

```
/var/backups/blitzback/
├── snapshots/
│   ├── 20260318-020000/
│   │   ├── packages/
│   │   │   ├── all.txt             ← all installed packages + versions
│   │   │   ├── official.txt        ← pacman repo packages only
│   │   │   ├── aur.txt             ← AUR packages only
│   │   │   └── flatpak.txt         ← flatpak apps (if present)
│   │   ├── pkg_modified/           ← package files that differ from originals
│   │   │   └── etc/
│   │   │       └── ssh/
│   │   │           └── sshd_config
│   │   ├── pkg_modified_files.txt  ← annotated list ([official]/[AUR])
│   │   ├── unowned_files.txt       ← files with no package owner (list only)
│   │   ├── etc/                    ← /etc backup
│   │   ├── home/                   ← /home backup
│   │   ├── root/                   ← /root backup
│   │   └── flatpak_homer/          ← flatpak user data (if present)
│   ├── 20260318-190000/            ← next snapshot (hardlinks where unchanged)
│   └── latest -> 20260318-190000/  ← symlink to latest snapshot
└── logs/
    ├── 20260318-020000.log
    └── 20260318-190000.log
```

---

## Automation with systemd

Generate and install unit files with a single command:

```bash
sudo python blitzback.py --install-systemd
```

This writes the exact script path into `ExecStart` automatically.

### Generated service: `/etc/systemd/system/blitzback.service`

```ini
[Unit]
Description=blitzback – package-aware backup
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python /path/to/blitzback.py
StandardOutput=journal
StandardError=journal
```

### Generated timer: `/etc/systemd/system/blitzback.timer`

```ini
[Unit]
Description=blitzback daily backup at 02:00

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` means the backup runs on the next boot if the machine was
off at 02:00.

```bash
systemctl daemon-reload
systemctl enable --now blitzback.timer

# Check status
systemctl list-timers blitzback.timer
journalctl -u blitzback.service -f
```

---

## Restoring files

### Single file

```bash
# From the latest snapshot
cp /var/backups/blitzback/snapshots/latest/etc/ssh/sshd_config /etc/ssh/sshd_config

# From a specific snapshot
cp /var/backups/blitzback/snapshots/20260318-020000/etc/ssh/sshd_config /etc/ssh/sshd_config
```

### Restore a directory

```bash
rsync -aHAX /var/backups/blitzback/snapshots/latest/home/homer/ /home/homer/
```

### Reset a modified package file to its original

```bash
# Reinstall the package (cleanest option)
sudo pacman -S openssh

# Or restore your customised version from the backup
cp /var/backups/blitzback/snapshots/latest/pkg_modified/etc/ssh/sshd_config /etc/ssh/sshd_config
```

### Reinstall all packages after a disaster

```bash
SNAP=/var/backups/blitzback/snapshots/latest

# Official packages
awk '{print $1}' "$SNAP/packages/official.txt" | sudo pacman -S -

# AUR packages
awk '{print $1}' "$SNAP/packages/aur.txt" | yay -S -

# Flatpak apps
awk '{print $1}' "$SNAP/packages/flatpak.txt" | xargs flatpak install -y
```

---

## Cleaning up old snapshots

Hardlinks save space, but old snapshots should still be pruned eventually:

```bash
# Delete all snapshots older than 30 days
find /var/backups/blitzback/snapshots/ -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +

# Keep only the 10 most recent snapshots
ls -dt /var/backups/blitzback/snapshots/[0-9]* | tail -n +11 | xargs rm -rf
```

---

## Tips

**Backup to an external drive:**
```bash
BLITZBACK_DIR=/run/media/homer/backup/blitzback sudo -E python blitzback.py
```

**Back up only modified package files** (skip /home etc.):
```bash
BLITZBACK_INCLUDE="" sudo -E python blitzback.py
```

**Disable the package check** (faster, e.g. for a /home-only backup):
```bash
BLITZBACK_PKG_CHECK=false sudo -E python blitzback.py
```

**Review unowned files** after a backup run:
```bash
cat /var/backups/blitzback/snapshots/latest/unowned_files.txt
```

**Check what packages were installed at a given snapshot:**
```bash
cat /var/backups/blitzback/snapshots/20260318-020000/packages/all.txt
```
