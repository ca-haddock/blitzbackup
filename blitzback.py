#!/usr/bin/env python3
"""
blitzback – package-aware backup with hardlink versioning

Configuration files (TOML, searched in order):
  /etc/blitzback.conf
  ~/.config/blitzback.conf
  ./blitzback.conf

Environment variables override config file values:
  BLITZBACK_DIR        backup root directory
  BLITZBACK_INCLUDE    colon-separated dirs to back up
  BLITZBACK_EXCLUDE    colon-separated rsync exclude patterns
  BLITZBACK_PKG_CHECK  true/false – run pacman integrity check
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path


# ── Config template ───────────────────────────────────────────────────────────

CONF_TEMPLATE = """\
# blitzback configuration file
# https://toml.io/en/

# Backup root directory.
# All snapshots and logs are stored here.
dir = "/var/backups/blitzback"

# Directories to back up (full backup, hardlink-versioned).
include = [
    "/etc",
    "/home",
    "/root",
    "/srv",
    "/var/spool/cron",
]

# rsync exclude patterns.
# Matches are skipped in every backed-up directory.
exclude = [
    ".cache",
    "*.cache",
    "node_modules",
    "__pycache__",
    ".git",
    ".Trash",
    ".cache/yay",   # yay AUR build cache – not worth backing up
]

# Run pacman -Qkk to find package files modified since installation.
# Disable to speed things up if you only care about home/etc.
pkg_check = true
"""


def write_conf(target: Path) -> None:
    """Write the config template; use .templ suffix if target already exists."""
    dest = target if not target.exists() else target.with_suffix(".conf.templ")
    dest.write_text(CONF_TEMPLATE)
    print(f"Config written to: {dest}")


# ── systemd units ─────────────────────────────────────────────────────────────

def _script_path() -> str:
    """Absolute path to this script."""
    return str(Path(__file__).resolve())


SERVICE_TEMPLATE = """\
[Unit]
Description=blitzback – package-aware backup
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python {script}
StandardOutput=journal
StandardError=journal
"""

TIMER_TEMPLATE = """\
[Unit]
Description=blitzback daily backup at 02:00

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""


def install_systemd(unit_dir: Path) -> None:
    """Write blitzback.service and blitzback.timer to unit_dir."""
    if os.getuid() != 0:
        print("WARNING: not running as root – writing unit files anyway, but 'systemctl' calls will fail.")

    unit_dir.mkdir(parents=True, exist_ok=True)
    service_file = unit_dir / "blitzback.service"
    timer_file   = unit_dir / "blitzback.timer"

    service_file.write_text(SERVICE_TEMPLATE.format(script=_script_path()))
    print(f"Service written : {service_file}")

    timer_file.write_text(TIMER_TEMPLATE)
    print(f"Timer written   : {timer_file}")

    print()
    print("Enable and start with:")
    print("  systemctl daemon-reload")
    print("  systemctl enable --now blitzback.timer")
    print()
    print("Check status:")
    print("  systemctl list-timers blitzback.timer")
    print("  journalctl -u blitzback.service -f")


# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULTS = {
    "dir":       "/var/backups/blitzback",
    "include":   ["/etc", "/home", "/root", "/srv", "/var/spool/cron"],
    "exclude":   [".cache", "*.cache", "node_modules", "__pycache__", ".git", ".Trash", ".cache/yay"],
    "pkg_check": True,
}


def load_config() -> dict:
    cfg: dict = dict(_DEFAULTS)

    for path in [
        Path("/etc/blitzback.conf"),
        Path.home() / ".config" / "blitzback.conf",
        Path("blitzback.conf"),
    ]:
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            cfg.update(data)

    def env(key, fallback):
        return os.environ.get(f"BLITZBACK_{key.upper()}", fallback)

    raw_dir       = env("DIR",       cfg["dir"])
    raw_include   = env("INCLUDE",   None)
    raw_exclude   = env("EXCLUDE",   None)
    raw_pkg_check = env("PKG_CHECK", None)

    return {
        "dir":       raw_dir,
        "include":   raw_include.split(":") if raw_include else cfg["include"],
        "exclude":   raw_exclude.split(":") if raw_exclude else cfg["exclude"],
        "pkg_check": (raw_pkg_check.lower() == "true") if raw_pkg_check else cfg["pkg_check"],
    }


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = "[%(asctime)s] %(levelname)s %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )
    return logging.getLogger("blitzback")


# ── Pacman / yay ─────────────────────────────────────────────────────────────

def detect_pkg_managers(log: logging.Logger) -> tuple[bool, bool]:
    """Return (has_pacman, has_yay)."""
    has_pacman = bool(shutil.which("pacman"))
    has_yay    = bool(shutil.which("yay"))
    managers   = " + ".join(filter(None, [
        "pacman" if has_pacman else "",
        "yay"    if has_yay    else "",
    ]))
    if managers:
        log.info("Package manager(s) detected: %s", managers)
    # yay installs AUR packages via pacman – they share the same DB,
    # so pacman -Qkk / pacman -Qo already cover AUR packages.
    return has_pacman, has_yay


def _aur_packages() -> set[str]:
    """Return set of package names installed from AUR (foreign to sync DBs)."""
    r = subprocess.run(["pacman", "-Qmq"], capture_output=True, text=True)
    return set(r.stdout.splitlines())


def _pkg_of_file(path: str) -> str | None:
    """Return package name owning path, or None."""
    r = subprocess.run(["pacman", "-Qqo", path], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def pacman_modified_files(log: logging.Logger, has_yay: bool) -> list[tuple[Path, str]]:
    """Run pacman -Qkk and return (path, tag) pairs for modified files.

    tag is '[AUR]' for yay/AUR packages, '[official]' for repo packages.
    """
    log.info("Checking package file integrity (pacman -Qkk) …")
    result = subprocess.run(["pacman", "-Qkk"], capture_output=True, text=True)

    aur_pkgs = _aur_packages() if has_yay else set()

    modified: list[tuple[Path, str]] = []
    for line in result.stdout.splitlines():
        # format: "warning: <pkg>: /path/to/file (reason)"
        if not (line.startswith("warning:") and ": /" in line):
            continue
        try:
            # extract package name and path
            rest      = line[len("warning: "):]
            pkg_name  = rest.split(":")[0].strip()
            path_part = line.split(": /", 1)[1]
            path_str  = "/" + path_part.split(" ")[0].rstrip(")")
            p = Path(path_str)
            if not p.exists():
                continue
            tag = "[AUR]" if pkg_name in aur_pkgs else "[official]"
            modified.append((p, tag))
        except (IndexError, ValueError):
            pass

    aur_count      = sum(1 for _, t in modified if t == "[AUR]")
    official_count = len(modified) - aur_count
    log.info(
        "  → %d modified file(s): %d official, %d AUR",
        len(modified), official_count, aur_count,
    )
    return modified


def pacman_unowned_files(log: logging.Logger, scan_dirs: list[str]) -> list[Path]:
    """Find files in scan_dirs not owned by any pacman/yay package."""
    log.info("Scanning for unowned files in: %s", ", ".join(scan_dirs))
    unowned = []
    for scan_dir in scan_dirs:
        p = Path(scan_dir)
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            r = subprocess.run(["pacman", "-Qo", str(f)], capture_output=True)
            if r.returncode != 0:
                unowned.append(f)

    log.info("  → %d unowned file(s) found", len(unowned))
    return unowned


# ── Flatpak ───────────────────────────────────────────────────────────────────

def flatpak_user_data_dirs(log: logging.Logger) -> list[tuple[str, Path]]:
    """Return (label, path) pairs for flatpak user-data directories."""
    if not shutil.which("flatpak"):
        return []

    log.info("Flatpak detected")
    dirs = []

    if os.getuid() == 0:
        # root: collect for every user
        for homedir in Path("/home").iterdir():
            d = homedir / ".var" / "app"
            if d.is_dir():
                dirs.append((f"flatpak_{homedir.name}", d))
        root_d = Path("/root/.var/app")
        if root_d.is_dir():
            dirs.append(("flatpak_root", root_d))
    else:
        d = Path.home() / ".var" / "app"
        if d.is_dir():
            dirs.append((f"flatpak_{Path.home().name}", d))

    return dirs


# ── rsync snapshot ────────────────────────────────────────────────────────────

def rsync_snapshot(
    src: Path | str,
    dest: Path,
    latest_counterpart: Path | None,
    excludes: list[str],
    log: logging.Logger,
    files_from: Path | None = None,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)

    cmd = ["rsync", "-aHAX", "--delete"]

    for pat in excludes:
        cmd += [f"--exclude={pat}"]

    if latest_counterpart and latest_counterpart.is_dir():
        cmd += [f"--link-dest={latest_counterpart.resolve()}"]

    if files_from:
        cmd += [f"--files-from={files_from}", "--no-delete"]

    cmd += [str(src), str(dest) + "/"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 24):  # 24 = vanished files, acceptable
        log.warning("rsync exited %d for '%s':\n%s", result.returncode, src, result.stderr[:500])


# ── Package lists ─────────────────────────────────────────────────────────────

def save_package_lists(snapshot_dir: Path, has_yay: bool, log: logging.Logger) -> None:
    """Write installed package lists to snapshot_dir/packages/."""
    pkg_dir = snapshot_dir / "packages"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # All installed packages with version: "name version"
    r = subprocess.run(["pacman", "-Q"], capture_output=True, text=True)
    all_pkgs = sorted(r.stdout.splitlines())

    # Foreign (AUR) packages
    r_aur = subprocess.run(["pacman", "-Qm"], capture_output=True, text=True)
    aur_pkgs = sorted(r_aur.stdout.splitlines())
    aur_names = {line.split()[0] for line in aur_pkgs if line.strip()}

    # Official = all minus AUR
    official_pkgs = [l for l in all_pkgs if l.split()[0] not in aur_names]

    (pkg_dir / "all.txt").write_text("\n".join(all_pkgs) + "\n")
    (pkg_dir / "official.txt").write_text("\n".join(official_pkgs) + "\n")
    (pkg_dir / "aur.txt").write_text("\n".join(aur_pkgs) + "\n")

    # Flatpak apps (system + user) if available
    if has_yay or shutil.which("flatpak"):
        pass  # flatpak list handled below

    if shutil.which("flatpak"):
        r_fp = subprocess.run(
            ["flatpak", "list", "--columns=application,version,origin"],
            capture_output=True, text=True,
        )
        (pkg_dir / "flatpak.txt").write_text(r_fp.stdout)

    log.info(
        "Package lists → packages/  (%d official, %d AUR%s)",
        len(official_pkgs),
        len(aur_pkgs),
        f", {len(r_fp.stdout.splitlines())} flatpak" if shutil.which("flatpak") else "",
    )


# ── Backup orchestration ──────────────────────────────────────────────────────

def do_backup(
    snapshot_dir: Path,
    latest_link: Path,
    cfg: dict,
    modified_files: list[tuple[Path, str]],
    unowned_files: list[Path],
    flatpak_dirs: list[tuple[str, Path]],
    has_pacman: bool,
    has_yay: bool,
    log: logging.Logger,
) -> None:

    def latest_of(name: str) -> Path | None:
        p = latest_link / name
        return p if p.is_dir() else None

    # 0. Package lists
    if has_pacman:
        save_package_lists(snapshot_dir, has_yay, log)

    # 1. Modified package files – back up preserving full path
    if modified_files:
        log.info("Backing up %d modified package file(s) → pkg_modified/", len(modified_files))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            # paths relative to / for rsync --files-from
            f.writelines(str(p).lstrip("/") + "\n" for p, _ in modified_files)
            rel_list = Path(f.name)
        try:
            rsync_snapshot(
                src="/",
                dest=snapshot_dir / "pkg_modified",
                latest_counterpart=latest_of("pkg_modified"),
                excludes=[],
                log=log,
                files_from=rel_list,
            )
        finally:
            rel_list.unlink(missing_ok=True)

        # save annotated list for reference
        (snapshot_dir / "pkg_modified_files.txt").write_text(
            "\n".join(f"{tag}  {p}" for p, tag in modified_files) + "\n"
        )

    # 2. Log unowned files (don't auto-backup – could be anything)
    if unowned_files:
        unowned_log = snapshot_dir / "unowned_files.txt"
        unowned_log.write_text("\n".join(str(p) for p in unowned_files) + "\n")
        log.info("Unowned file list → %s", unowned_log)

    # 3. Configured include directories
    for raw_dir in cfg["include"]:
        src = Path(raw_dir)
        if not src.is_dir():
            continue
        dest_name = str(src).lstrip("/").replace("/", "_")
        log.info("Backing up %s → %s/", src, dest_name)
        rsync_snapshot(
            src=Path(str(src) + "/"),
            dest=snapshot_dir / dest_name,
            latest_counterpart=latest_of(dest_name),
            excludes=cfg["exclude"],
            log=log,
        )

    # 4. Flatpak user data
    for label, fdir in flatpak_dirs:
        log.info("Backing up flatpak user data (%s) → %s/", fdir, label)
        rsync_snapshot(
            src=Path(str(fdir) + "/"),
            dest=snapshot_dir / label,
            latest_counterpart=latest_of(label),
            excludes=cfg["exclude"],
            log=log,
        )


# ── Hardlink-based size calculation ──────────────────────────────────────────

def snapshot_size(path: Path) -> str:
    """du -sh equivalent."""
    result = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True)
    return result.stdout.split()[0] if result.returncode == 0 else "?"


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="blitzback",
        description="Package-aware backup with hardlink versioning for Arch Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration files (INI, section [blitzback]):
  /etc/blitzback.conf
  ~/.config/blitzback.conf

Environment variables override config file values:
  BLITZBACK_DIR        backup root directory
  BLITZBACK_INCLUDE    colon-separated dirs to back up
  BLITZBACK_EXCLUDE    colon-separated rsync exclude patterns
  BLITZBACK_PKG_CHECK  true/false – run pacman integrity check

Examples:
  sudo python blitzback.py
  sudo python blitzback.py --dir /mnt/backup
  sudo python blitzback.py --include /etc:/home --no-pkg-check
  BLITZBACK_DIR=/mnt/disk sudo -E python blitzback.py
        """,
    )
    p.add_argument(
        "--dir",
        metavar="PATH",
        help="backup root directory (default: /var/backups/blitzback)",
    )
    p.add_argument(
        "--include",
        metavar="DIRS",
        help="colon-separated directories to back up (default: /etc:/home:/root:/srv:/var/spool/cron)",
    )
    p.add_argument(
        "--exclude",
        metavar="PATTERNS",
        help="colon-separated rsync exclude patterns (default: .cache:*.cache:node_modules:__pycache__:.git:.Trash)",
    )
    p.add_argument(
        "--no-pkg-check",
        action="store_true",
        help="skip pacman/yay file integrity check (faster)",
    )
    p.add_argument(
        "--makeconf",
        action="store_true",
        help="write a config template to blitzback.conf (blitzback.conf.templ if it already exists) and exit",
    )
    p.add_argument(
        "--install-systemd",
        metavar="DIR",
        nargs="?",
        const="/etc/systemd/system",
        help="write blitzback.service + blitzback.timer to DIR (default: /etc/systemd/system) and exit",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.makeconf:
        write_conf(Path("blitzback.conf"))
        return

    if args.install_systemd is not None:
        install_systemd(Path(args.install_systemd))
        return

    cfg = load_config()

    # CLI arguments override config/env
    if args.dir:
        cfg["dir"] = args.dir
    if args.include:
        cfg["include"] = args.include.split(":")
    if args.exclude:
        cfg["exclude"] = args.exclude.split(":")
    if args.no_pkg_check:
        cfg["pkg_check"] = False

    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")

    backup_root  = Path(cfg["dir"])
    snapshot_dir = backup_root / "snapshots" / date_str
    latest_link  = backup_root / "snapshots" / "latest"
    log_file     = backup_root / "logs" / f"{date_str}.log"

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(log_file)

    if os.getuid() != 0:
        log.warning("Not running as root – some files may be skipped.")

    log.info("blitzback starting – %s", date_str)
    log.info("Backup dir : %s", backup_root)

    modified_files: list[tuple[Path, str]] = []
    unowned_files:  list[Path] = []

    has_pacman, has_yay = detect_pkg_managers(log)

    if cfg["pkg_check"] and has_pacman:
        modified_files = pacman_modified_files(log, has_yay)
        unowned_files  = pacman_unowned_files(log, ["/etc", "/usr/local"])

    flatpak_dirs = flatpak_user_data_dirs(log)

    do_backup(snapshot_dir, latest_link, cfg, modified_files, unowned_files, flatpak_dirs, has_pacman, has_yay, log)

    # Update 'latest' symlink
    tmp_link = latest_link.parent / f".latest_tmp_{os.getpid()}"
    tmp_link.symlink_to(snapshot_dir)
    tmp_link.rename(latest_link)
    log.info("latest → %s", snapshot_dir)

    # Summary
    log.info("──────────────────────────────────────────────────")
    log.info("Snapshot : %s", snapshot_dir)
    log.info("Size     : %s (hardlinks counted once)", snapshot_size(snapshot_dir))
    if modified_files:
        log.info("Modified package files:")
        for p, tag in modified_files:
            log.info("  %s  %s", tag, p)
    log.info("Log      : %s", log_file)
    log.info("Done.")


if __name__ == "__main__":
    main()
