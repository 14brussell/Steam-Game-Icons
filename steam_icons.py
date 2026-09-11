#!/usr/bin/python3 -I
"""Repair existing Steam shortcuts; use only local artwork and preserve custom icons."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
import time

# -I excludes both ambient Python paths and the script directory. Load only the
# bundled helper by its explicit path, without restoring either search path.
_helper_spec = importlib.util.spec_from_file_location("steam_icons_safe_fs", Path(__file__).with_name("safe_fs.py"))
_helper = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(_helper)
Budget, Directory = _helper.Budget, _helper.Directory
LimitExceeded, UnsafePath = _helper.LimitExceeded, _helper.UnsafePath
walk_files = _helper.walk_files

MAGICK = "/usr/bin/magick"
DESKTOP_LIMIT = 256 * 1024
LEDGER_LIMIT = 1024 * 1024
ARTWORK_LIMIT = 16 * 1024 * 1024
MAX_SHORTCUTS = 1024
MAX_LEDGER_ENTRIES = 2048


def image_limits():
    # Bound decoder output (including errors), CPU, and address space even for
    # malformed inputs. This worker is single-threaded, so preexec_fn is safe.
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)


def image_command(arguments, data, budget):
    budget.take("decodes")
    # A minimal environment also excludes user ImageMagick configuration paths,
    # module paths, preload settings, and shell-startup variables.
    env = {"PATH": "/usr/bin", "HOME": "/nonexistent", "LANG": "C",
           "MAGICK_THREAD_LIMIT": "1"}
    with tempfile.TemporaryDirectory(prefix="steam-icons-decode-") as temporary:
        env["MAGICK_TEMPORARY_PATH"] = temporary
        env["XDG_CONFIG_HOME"] = temporary
        with tempfile.TemporaryFile(dir=temporary) as output, tempfile.TemporaryFile(dir=temporary) as errors:
            subprocess.run([MAGICK, *arguments], input=data, stdout=output, stderr=errors,
                           cwd=temporary, env=env, preexec_fn=image_limits, check=True,
                           timeout=min(10, max(0.01, budget.deadline - time.monotonic())))
            output.seek(0)
            return output.read(8 * 1024 * 1024)


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
        self.data = Path(os.environ.get("XDG_DATA_HOME") or self.home / ".local/share")
        self.state = Path(os.environ.get("XDG_STATE_HOME") or self.home / ".local/state") / "omarchy-steam-icons"
        self.icons = self.data / "icons/omarchy-steam-icons"
        self.roots = [self.data / "Steam", self.home / ".steam/steam", self.home / ".steam/root",
                      self.home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
        # Do not accept an unbounded or attacker-selected recursive search list.
        self.icon_dirs = [self.data / "icons", self.home / ".icons",
                          Path("/usr/local/share/icons/hicolor"), Path("/usr/share/icons/hicolor")]
        self.budget = Budget()
        self.installed_icons = None

    def known_icons(self):
        self.installed_icons = {}
        for folder in self.icon_dirs:
            for path in walk_files(folder, self.budget, recursive=True):
                if re.fullmatch(r"steam_icon_[0-9]{1,20}\.(png|svg|xpm)", path.name):
                    with Directory(path.parent, owned=False) as directory:
                        directory.snapshot(path.name, owned=False)
                    self.installed_icons.setdefault(path.stem, []).append(path)
        return set(self.installed_icons)

    def needs_icon(self, icon, known):
        if not icon or icon in {"steam", "com.valvesoftware.Steam"}:
            return True
        if icon.startswith("/"):
            return not Path(icon).is_file()
        if icon.startswith("steam_icon_"):
            return icon not in known
        return False

    def artwork(self, appid):
        if not re.fullmatch(r"[0-9]{1,20}", appid):
            return None
        if self.installed_icons is None:
            self.known_icons()
        candidates = []
        visited = set()
        for root in self.roots:
            # Steam commonly uses ~/.steam/root and ~/.steam/steam symlinks.
            # Resolve read-only cache roots, then validate every resulting
            # component with no-follow opens; mutation paths never resolve links.
            root = root.resolve()
            if root in visited:
                continue
            visited.add(root)
            cache = root / "appcache/librarycache"
            for folder, pattern in [(cache / appid, r"[0-9a-f]{40}\.(jpg|png)"),
                                    (cache, rf"{appid}_[0-9a-f]{{40}}\.(jpg|png)")]:
                for path in walk_files(folder, self.budget):
                    if re.fullmatch(pattern, path.name):
                        candidates.append(path)
                        if len(candidates) > 64:
                            raise LimitExceeded("more than 64 artwork candidates")
        candidates.extend(p for p in self.installed_icons.get(f"steam_icon_{appid}", []) if p.suffix == ".png")
        if len(candidates) > 64:
            raise LimitExceeded("more than 64 artwork candidates")
        best = None
        for path in sorted(set(candidates)):
            try:
                with Directory(path.parent, owned=False) as directory:
                    data, _ = directory.read(path.name, ARTWORK_LIMIT, self.budget, owned=False)
                coder = "PNG:-[0]" if path.suffix == ".png" else "JPEG:-[0]"
                result = image_command(["identify", "-limit", "memory", "64MiB", "-limit", "map", "64MiB",
                                        "-limit", "disk", "0", "-ping", "-format", "%w %h", coder], data, self.budget)
                width, height = map(int, result.split())
                if width == height and 16 <= width <= 1024 and (best is None or width > best[0]):
                    converted = image_command(["-limit", "memory", "64MiB", "-limit", "map", "64MiB",
                                               "-limit", "disk", "0", coder, "-strip", "PNG32:-"], data, self.budget)
                    if converted.startswith(b"\x89PNG\r\n\x1a\n"):
                        best = (width, converted)
            except (LimitExceeded, UnsafePath):
                raise
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        return best[1] if best else None

    def run(self, dry_run=False, restore=False):
        self.budget = Budget()
        self.installed_icons = None
        with ExitStack() as stack:
            # Missing roots are opened/created one no-follow component at a time.
            try:
                data = stack.enter_context(Directory(self.data, create=not dry_run))
            except FileNotFoundError:
                return
            try:
                apps = stack.enter_context(Directory(data.path / "applications"))
            except FileNotFoundError:
                return
            try:
                state = stack.enter_context(Directory(self.state, create=not dry_run))
            except FileNotFoundError:
                state = None
            verify_lock = stack.enter_context(state.lock()) if not dry_run else lambda: None
            ledger = {}
            ledger_identity = state.snapshot("changes.json") if state else None
            if ledger_identity is not None:
                raw, ledger_identity = state.read("changes.json", LEDGER_LIMIT, self.budget,
                                                 expected=ledger_identity)
                ledger = json.loads(raw)
                if not isinstance(ledger, dict) or len(ledger) > MAX_LEDGER_ENTRIES:
                    raise UnsafePath("invalid or oversized undo ledger")
                for key, saved in ledger.items():
                    if (not isinstance(key, str) or not isinstance(saved, dict)
                            or set(saved) != {"original", "installed"}
                            or not isinstance(saved["installed"], str)
                            or not (saved["original"] is None or isinstance(saved["original"], str))
                            or any("\n" in value or "\r" in value for value in saved.values() if isinstance(value, str))):
                        raise UnsafePath("invalid undo ledger entry")
            known = set() if restore else self.known_icons()
            shortcuts = []
            with os.scandir(apps.fd) as entries:
                for item in entries:
                    self.budget.take("entries")
                    if item.name.endswith(".desktop") and not item.is_symlink():
                        discovered = apps.snapshot(item.name)
                        if discovered is None:
                            continue
                        shortcuts.append((item.name, discovered))
                        if len(shortcuts) > MAX_SHORTCUTS:
                            raise LimitExceeded("more than 1024 desktop shortcuts")
            icons = None
            for name, discovered in sorted(shortcuts):
                path = apps.path / name
                try:
                    raw, original_identity = apps.read(name, DESKTOP_LIMIT, self.budget, expected=discovered)
                    text = raw.decode("utf-8")
                    parsed = entry(text)
                    if not parsed:
                        continue
                    fields = parsed[2]
                    key = str(path)
                    if restore:
                        saved = ledger.get(key)
                        if not saved or fields.get("Icon") != saved["installed"]:
                            continue
                        updated = set_icon(text, saved["original"]).encode()
                        print(f"Restore: {name}")
                        if not dry_run:
                            state.unchanged("changes.json", ledger_identity)
                            apps.write(name, updated, original_identity, before_replace=verify_lock)
                            del ledger[key]
                            state.write("changes.json", json.dumps(ledger, indent=2).encode(), ledger_identity,
                                        before_replace=verify_lock)
                            ledger_identity = state.snapshot("changes.json")
                        continue
                    game = re.search(r'steam://(?:rungameid|run)/([0-9]{1,20})(?=$|[\s/"])', fields.get("Exec", ""))
                    if not game or fields.get("Hidden") == "true" or not self.needs_icon(fields.get("Icon"), known):
                        continue
                    appid = game[1]
                    artwork = self.artwork(appid)
                    if not artwork:
                        print(f"Waiting for cached icon: {name}")
                        continue
                    icon = str(self.icons / f"{appid}.png")
                    print(f"Repair: {name}")
                    if not dry_run:
                        apps.unchanged(name, original_identity)
                        if icons is None:
                            icons = stack.enter_context(Directory(self.icons, create=True))
                        icons.write(f"{appid}.png", artwork, icons.snapshot(f"{appid}.png"), before_replace=verify_lock)
                        # Record undo information before changing the shortcut.
                        # Preserve the first original when repairing deleted artwork.
                        if key not in ledger or fields.get("Icon") != ledger[key]["installed"]:
                            ledger[key] = {"original": fields.get("Icon"), "installed": icon}
                        encoded = json.dumps(ledger, indent=2).encode()
                        if len(ledger) > MAX_LEDGER_ENTRIES or len(encoded) > LEDGER_LIMIT:
                            raise LimitExceeded("undo ledger limit exceeded")
                        state.write("changes.json", encoded, ledger_identity, before_replace=verify_lock)
                        ledger_identity = state.snapshot("changes.json")
                        def verify_repair():
                            verify_lock()
                            state.unchanged("changes.json", ledger_identity)
                            icons.verify()
                        apps.write(name, set_icon(text, icon).encode(), original_identity, before_replace=verify_repair)
                except (OSError, UnicodeError) as error:
                    print(f"Skipped {name}: {error}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        Repair().run(dry_run=args.dry_run, restore=args.restore)
    except (OSError, ValueError, RecursionError) as error:
        print(f"Steam Game Icons: {error}", file=sys.stderr)
        sys.exit(1)
