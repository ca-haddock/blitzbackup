# blitzback – HOWTO

Package-aware backup with hardlink versioning for Arch Linux.

---

## Requirements

```
python >= 3.10
rsync
pacman          (Arch Linux)
flatpak         (optional, auto-detected)
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

### 1. Pacman – find modified package files

```
pacman -Qkk
```

Verifies every installed file against the mtree checksum stored in the package
database. Files that differ from the original (e.g. a tweaked config in `/etc/`)
are backed up separately under `pkg_modified/`, preserving the full path.

Example: `/etc/ssh/sshd_config` was edited after installation
→ saved as `<snapshot>/pkg_modified/etc/ssh/sshd_config`

### 2. Unowned files

Scans `/etc` and `/usr/local` for files that don't belong to any installed
package. These are **not** backed up automatically, but listed in
`unowned_files.txt` inside the snapshot for manual review.

### 3. Flatpak – user data

If `flatpak` is installed, app user-data is backed up automatically:

```
~/.var/app/         → flatpak_<username>/
/root/.var/app/     → flatpak_root/         (root only)
```

The flatpak binaries themselves (`/var/lib/flatpak/`) are skipped –
they can be reinstalled at any time.

### 4. Configured directories

All directories listed in `include` are backed up in full
(default: `/etc /home /root /srv /var/spool/cron`).

### 5. Hardlink versioning

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

### Option A – Environment variables

```bash
export BLITZBACK_DIR=/mnt/backup/blitzback
export BLITZBACK_INCLUDE=/etc:/home:/root:/opt
export BLITZBACK_EXCLUDE=.cache:node_modules:.git:*.tmp
export BLITZBACK_PKG_CHECK=true

sudo -E python blitzback.py
```

(`sudo -E` passes the environment variables through to root)

### Option B – Config file

`/etc/blitzback.conf` (system-wide) or `~/.config/blitzback.conf` (per user):

```ini
[blitzback]
dir     = /mnt/backup/blitzback
include = /etc:/home:/root:/opt:/var/spool/cron
exclude = .cache:*.cache:node_modules:__pycache__:.git:.Trash:*.tmp
pkg_check = true
```

Environment variables override config file values.

### All options

| Option      | Env variable          | Default                                        | Description                          |
|-------------|-----------------------|------------------------------------------------|--------------------------------------|
| `dir`       | `BLITZBACK_DIR`       | `/var/backups/blitzback`                       | Backup root directory                |
| `include`   | `BLITZBACK_INCLUDE`   | `/etc:/home:/root:/srv:/var/spool/cron`        | Colon-separated dirs to back up      |
| `exclude`   | `BLITZBACK_EXCLUDE`   | `.cache:*.cache:node_modules:__pycache__:.git` | rsync exclude patterns (colon-separated) |
| `pkg_check` | `BLITZBACK_PKG_CHECK` | `true`                                         | Enable/disable pacman integrity check |

---

## Backup directory layout

```
/var/backups/blitzback/
├── snapshots/
│   ├── 20260318-083000/
│   │   ├── pkg_modified/          ← package files that differ from originals
│   │   │   └── etc/
│   │   │       └── ssh/
│   │   │           └── sshd_config
│   │   ├── pkg_modified_files.txt ← list of all modified package files
│   │   ├── unowned_files.txt      ← files with no package owner (list only)
│   │   ├── etc/                   ← /etc backup
│   │   ├── home/                  ← /home backup
│   │   ├── root/                  ← /root backup
│   │   └── flatpak_homer/         ← flatpak user data (if present)
│   ├── 20260318-190000/           ← next snapshot (hardlinks where unchanged)
│   └── latest -> 20260318-190000/ ← symlink to latest snapshot
└── logs/
    ├── 20260318-083000.log
    └── 20260318-190000.log
```

---

## Automation with systemd

### Service unit: `/etc/systemd/system/blitzback.service`

```ini
[Unit]
Description=blitzback backup
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python /opt/blitzback/blitzback.py
```

### Timer unit: `/etc/systemd/system/blitzback.timer`

```ini
[Unit]
Description=blitzback daily backup

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
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
cp /var/backups/blitzback/snapshots/20260318-083000/etc/ssh/sshd_config /etc/ssh/sshd_config
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
