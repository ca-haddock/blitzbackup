# blitzback

Package-aware backup with hardlink versioning for Arch Linux.

Knows which files belong to **pacman** or **yay (AUR)** packages, detects files modified since installation, saves a full package inventory, and backs everything up as space-efficient hardlink snapshots.

---

## Features

- **Package awareness** – runs `pacman -Qkk` to find files that differ from their installed package version; tags each file as `[official]` or `[AUR]`
- **Package inventory** – saves a full list of installed packages (official, AUR, flatpak) in every snapshot for easy reinstallation after a disaster
- **Unowned file detection** – scans `/etc` and `/usr/local` for files that belong to no package
- **Flatpak support** – automatically detects and backs up `~/.var/app/` user data
- **Hardlink versioning** – every run is a full snapshot; unchanged files are hardlinked from the previous snapshot, so only deltas use new disk space
- **Configurable** – TOML config file, environment variables, or CLI flags (CLI wins)
- **systemd integration** – generates a service + timer unit for daily 02:00 backups

---

## Requirements

| Dependency | Notes |
|---|---|
| Python ≥ 3.11 | uses stdlib `tomllib` |
| `rsync` | snapshot engine |
| `pacman` | Arch Linux package manager |
| `yay` | optional, AUR helper – auto-detected |
| `flatpak` | optional – auto-detected |

---

## Quick start

```bash
git clone https://github.com/ca-haddock/blitzbackup.git
cd blitzbackup

# run once as root
sudo python blitzback.py
```

Snapshots land in `/var/backups/blitzback/snapshots/` by default.

---

## Usage

```
usage: blitzback [-h] [--dir PATH] [--include DIRS] [--exclude PATTERNS]
                 [--no-pkg-check] [--makeconf] [--install-systemd [DIR]]

options:
  --dir PATH              backup root directory
  --include DIRS          colon-separated dirs to back up
  --exclude PATTERNS      colon-separated rsync exclude patterns
  --no-pkg-check          skip pacman/yay integrity check (faster)
  --makeconf              write blitzback.conf template and exit
  --install-systemd [DIR] write systemd service + timer and exit
                          (default dir: /etc/systemd/system)
```

### Examples

```bash
# default run
sudo python blitzback.py

# custom backup location
sudo python blitzback.py --dir /mnt/external/backup

# only back up /etc and /home, skip package check
sudo python blitzback.py --include /etc:/home --no-pkg-check

# via environment variable
BLITZBACK_DIR=/mnt/external sudo -E python blitzback.py
```

---

## Configuration

### Generate a config file

```bash
python blitzback.py --makeconf
# writes blitzback.conf in the current directory
# if blitzback.conf already exists, writes blitzback.conf.templ instead
```

### Config file format (TOML)

Config files are loaded in order – later files override earlier ones:

1. `/etc/blitzback.conf`
2. `~/.config/blitzback.conf`
3. `./blitzback.conf`

```toml
# blitzback.conf

# Backup root directory
dir = "/var/backups/blitzback"

# Directories to back up
include = [
    "/etc",
    "/home",
    "/root",
    "/srv",
    "/var/spool/cron",
]

# rsync exclude patterns
exclude = [
    ".cache",
    "*.cache",
    "node_modules",
    "__pycache__",
    ".git",
    ".Trash",
    ".cache/yay",
]

# Run pacman -Qkk integrity check
pkg_check = true
```

### Environment variables

All options can also be set via environment variables. These override config files.

| Variable | Description |
|---|---|
| `BLITZBACK_DIR` | backup root directory |
| `BLITZBACK_INCLUDE` | colon-separated directories |
| `BLITZBACK_EXCLUDE` | colon-separated rsync exclude patterns |
| `BLITZBACK_PKG_CHECK` | `true` / `false` |

---

## Backup layout

```
/var/backups/blitzback/
├── snapshots/
│   ├── 20260318-020000/
│   │   ├── packages/
│   │   │   ├── all.txt             ← all installed packages + versions
│   │   │   ├── official.txt        ← pacman repo packages only
│   │   │   ├── aur.txt             ← AUR packages only
│   │   │   └── flatpak.txt         ← flatpak apps (if present)
│   │   ├── pkg_modified/           ← package files modified since install
│   │   │   └── etc/ssh/sshd_config
│   │   ├── pkg_modified_files.txt  ← annotated list ([official]/[AUR])
│   │   ├── unowned_files.txt       ← files with no package owner (list only)
│   │   ├── etc/                    ← /etc backup
│   │   ├── home/                   ← /home backup
│   │   ├── root/                   ← /root backup
│   │   └── flatpak_homer/          ← flatpak user data (if present)
│   ├── 20260319-020000/            ← next snapshot (hardlinks for unchanged files)
│   └── latest -> 20260319-020000/  ← symlink to most recent snapshot
└── logs/
    ├── 20260318-020000.log
    └── 20260319-020000.log
```

`pkg_modified_files.txt` example:
```
[official]  /etc/ssh/sshd_config
[official]  /etc/locale.conf
[AUR]       /etc/freetube/freetube.conf
```

---

## systemd – daily backup at 02:00

```bash
# write unit files to /etc/systemd/system
sudo python blitzback.py --install-systemd

# activate
systemctl daemon-reload
systemctl enable --now blitzback.timer

# verify
systemctl list-timers blitzback.timer
journalctl -u blitzback.service -f
```

The generated `blitzback.timer` uses `Persistent=true`, so if the machine is off at 02:00 the backup runs automatically on the next boot.

To write units to a different directory (e.g. for review before installing):

```bash
python blitzback.py --install-systemd ./units
```

---

## Restoring files

### Single file

```bash
cp /var/backups/blitzback/snapshots/latest/etc/ssh/sshd_config /etc/ssh/sshd_config
```

### Restore a directory

```bash
rsync -aHAX /var/backups/blitzback/snapshots/latest/home/alice/ /home/alice/
```

### Restore from a specific snapshot

```bash
cp /var/backups/blitzback/snapshots/20260318-020000/etc/ssh/sshd_config /etc/ssh/sshd_config
```

### Reset a modified package file to its package original

```bash
# cleanest: reinstall the package
sudo pacman -S openssh

# or restore your customised version from the backup
cp /var/backups/blitzback/snapshots/latest/pkg_modified/etc/ssh/sshd_config /etc/ssh/sshd_config
```

### Reinstall all packages from a snapshot

```bash
# reinstall official packages
awk '{print $1}' /var/backups/blitzback/snapshots/latest/packages/official.txt \
  | sudo pacman -S -

# reinstall AUR packages
awk '{print $1}' /var/backups/blitzback/snapshots/latest/packages/aur.txt \
  | yay -S -
```

---

## Pruning old snapshots

Hardlinks keep disk usage low, but old snapshots should still be pruned eventually.

```bash
# delete snapshots older than 30 days
find /var/backups/blitzback/snapshots/ -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +

# keep only the 10 most recent snapshots
ls -dt /var/backups/blitzback/snapshots/[0-9]* | tail -n +11 | xargs rm -rf
```

---

## License

MIT
