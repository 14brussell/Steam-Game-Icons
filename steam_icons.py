#!/usr/bin/env python3
"""Repair existing Steam shortcuts; use only local artwork and preserve custom icons."""
import argparse
from contextlib import nullcontext
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".steam-icons-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def entry(text):
    """Read only the main group, leaving desktop action icons untouched."""
    match = re.search(r"(?m)^\[Desktop Entry\]\s*\n", text)
    if not match:
        return None
    end = re.search(r"(?m)^\[", text[match.end():])
    stop = match.end() + end.start() if end else len(text)
    fields = dict(re.findall(r"(?m)^([A-Za-z]+)=(.*)$", text[match.end():stop]))
    return match.end(), stop, fields


def set_icon(text, icon):
    start, stop, _ = entry(text)
    group = text[start:stop]
    group = re.sub(r"(?m)^Icon=.*\n?", "", group)
    if icon is not None:
        group = f"Icon={icon}\n" + group
    return text[:start] + group + text[stop:]


class Repair:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.data = Path(os.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        self.state = Path(os.environ.get("XDG_STATE_HOME", self.home / ".local/state")) / "omarchy-steam-icons"
        self.icons = self.data / "icons/omarchy-steam-icons"
        self.roots = list(dict.fromkeys(p.resolve() for p in [
            self.data / "Steam", self.home / ".steam/steam", self.home / ".steam/root",
            self.home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ] if p.is_dir()))
        data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
        self.icon_dirs = [self.data / "icons", self.home / ".icons"] + [Path(p) / "icons" for p in data_dirs if p]

    def known_icons(self):
        result = set()
        for folder in self.icon_dirs:
            if folder.is_dir():
                result.update(p.stem for p in folder.rglob("steam_icon_*.*") if p.is_file())
        return result

    def needs_icon(self, icon, known):
        if not icon or icon in {"steam", "com.valvesoftware.Steam"}:
            return True
        if icon.startswith("/"):
            return not Path(icon).is_file()
        if icon.startswith("steam_icon_"):
            return icon not in known
        return False

    def artwork(self, appid):
        candidates = []
        for root in self.roots:
            cache = root / "appcache/librarycache"
            # Steam's app-specific hash-named files are icons; exclude banners/logos.
            candidates.extend(p for p in (cache / appid).glob("*") if re.fullmatch(r"[0-9a-f]{40}\.(jpg|png)", p.name))
            candidates.extend(p for p in cache.glob(f"{appid}_*") if re.fullmatch(rf"{appid}_[0-9a-f]{{40}}\.(jpg|png)", p.name))
        for folder in self.icon_dirs:
            candidates.extend(folder.glob(f"**/steam_icon_{appid}.png"))
        ranked = []
        for path in sorted(set(candidates)):
            try:
                # Force the expected decoder and use stdin, so filenames never
                # become ImageMagick options, pseudo-images, or frame selectors.
                with path.open("rb") as stream:
                    data = stream.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    continue
                coder = "PNG:-" if path.suffix == ".png" else "JPEG:-"
                result = subprocess.run(
                    ["magick", "identify", "-ping", "-format", "%w %h", coder],
                    input=data, capture_output=True, timeout=10, check=True)
                width, height = map(int, result.stdout.split())
                if width == height and 16 <= width <= 1024:
                    ranked.append((width, coder, data))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        for _, coder, data in sorted(ranked, key=lambda item: item[0], reverse=True):
            try:
                result = subprocess.run(
                    ["magick", "-limit", "memory", "64MiB", "-limit", "map", "64MiB",
                     "-limit", "disk", "0", coder, "-strip", "PNG32:-"],
                    input=data, capture_output=True, timeout=10, check=True)
                if result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                    return result.stdout
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    def run(self, dry_run=False, restore=False):
        if not dry_run:
            self.state.mkdir(parents=True, exist_ok=True)
        with (nullcontext() if dry_run else (self.state / "lock").open("w")) as lock:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            ledger_path = self.state / "changes.json"
            ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
            known = self.known_icons()
            for path in sorted((self.data / "applications").glob("*.desktop")):
                if path.is_symlink():
                    continue
                try:
                    text = path.read_text()
                    parsed = entry(text)
                    if not parsed:
                        continue
                    fields = parsed[2]
                    key = str(path)
                    if restore:
                        saved = ledger.get(key)
                        if saved and fields.get("Icon") == saved["installed"]:
                            print(f"Restore: {path.name}")
                            if not dry_run:
                                atomic_write(path, set_icon(text, saved["original"]).encode())
                                del ledger[key]
                                atomic_write(ledger_path, json.dumps(ledger, indent=2).encode())
                        continue
                    game = re.search(r"steam://(?:rungameid|run)/([0-9]+)(?=$|[\s/\"])", fields.get("Exec", ""))
                    if not game or fields.get("Hidden") == "true" or not self.needs_icon(fields.get("Icon"), known):
                        continue
                    appid = game[1]
                    artwork = self.artwork(appid)
                    if not artwork:
                        print(f"Waiting for cached icon: {path.name}")
                        continue
                    icon = str(self.icons / f"{appid}.png")
                    print(f"Repair: {path.name}")
                    if not dry_run:
                        atomic_write(Path(icon), artwork)
                        # Record undo information before changing the shortcut.
                        ledger[key] = {"original": fields.get("Icon"), "installed": icon}
                        atomic_write(ledger_path, json.dumps(ledger, indent=2).encode())
                        atomic_write(path, set_icon(text, icon).encode())
                except (OSError, UnicodeError) as error:
                    print(f"Skipped {path.name}: {error}", file=__import__("sys").stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    Repair().run(dry_run=args.dry_run, restore=args.restore)
