"""Regression coverage for marketplace issue omacom/omarchy-plugin-marketplace#6210."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_icons
import steam_icons
from steam_icons import Budget, Directory, LimitExceeded, UnsafePath, entry, walk_files


class SecurityTest(unittest.TestCase):
    def setUp(self):
        test_icons.IconsTest.setUp(self)

    def assert_unchanged(self):
        self.assertEqual(self.path.read_text(), self.original)

    def test_reject_relative_and_symlinked_xdg_roots(self):
        for path in [Path("relative"), self.home / "alias"]:
            if path.is_absolute():
                path.symlink_to(self.repair.data, target_is_directory=True)
            with self.subTest(path=path), patch.object(self.repair, "data", path):
                with self.assertRaises((UnsafePath, OSError)):
                    self.repair.run()
                self.assert_unchanged()

    def test_reject_unowned_and_writable_directories(self):
        with self.assertRaises(UnsafePath):
            Directory("/usr")
        self.repair.data.chmod(0o777)
        with self.assertRaises(UnsafePath):
            self.repair.run()
        self.assert_unchanged()

    def test_reject_symlinked_state_and_output_directories(self):
        outside = self.home / "outside"
        outside.mkdir()
        for target in [self.repair.state, self.repair.icons]:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(outside, target_is_directory=True)
            with self.subTest(target=target), self.assertRaises((UnsafePath, OSError)):
                self.repair.run()
            self.assert_unchanged()
            self.assertEqual(list(outside.iterdir()), [])
            target.unlink()

    def test_lock_never_truncates_and_rejects_links(self):
        self.repair.state.mkdir(parents=True)
        lock = self.repair.state / "lock"
        victim = self.home / "victim"
        victim.write_text("keep me")
        for hardlink in [False, True]:
            if hardlink:
                os.link(victim, lock)
            else:
                lock.symlink_to(victim)
            with self.subTest(hardlink=hardlink), self.assertRaises((UnsafePath, OSError)):
                self.repair.run()
            self.assertEqual(victim.read_text(), "keep me")
            self.assert_unchanged()
            lock.unlink()
        lock.write_text("existing lock contents")
        with Directory(self.repair.state) as directory, directory.lock():
            self.assertEqual(lock.read_text(), "existing lock contents")

    def test_contended_lock_returns_without_waiting(self):
        with Directory(self.repair.state, create=True) as directory, directory.lock():
            with self.assertRaises(BlockingIOError):
                self.repair.run()
        self.assert_unchanged()

    def test_ledger_symlink_fifo_and_oversize_rejected(self):
        self.repair.state.mkdir(parents=True)
        ledger = self.repair.state / "changes.json"
        victim = self.home / "victim"
        victim.write_text("{}")
        ledger.symlink_to(victim)
        with self.assertRaises(UnsafePath):
            self.repair.run()
        ledger.unlink()
        os.mkfifo(ledger)
        with self.assertRaises(UnsafePath):
            self.repair.run()
        ledger.unlink()
        ledger.write_bytes(b" " * (steam_icons.LEDGER_LIMIT + 1))
        with self.assertRaises(LimitExceeded):
            self.repair.run()
        self.assertEqual(victim.read_text(), "{}")
        self.assert_unchanged()

    def test_invalid_and_excessive_ledger_records_rejected(self):
        self.repair.state.mkdir(parents=True)
        ledger = self.repair.state / "changes.json"
        for value in [[], {"bad": {}}, {"bad": {"installed": "icon", "original": "x\nExec=bad"}},
                      {str(n): {} for n in range(steam_icons.MAX_LEDGER_ENTRIES + 1)}]:
            ledger.write_text(json.dumps(value))
            with self.subTest(value_type=type(value)), self.assertRaises(UnsafePath):
                self.repair.run()
            self.assert_unchanged()

    def test_desktop_read_limit(self):
        oversized = self.original + "#" * steam_icons.DESKTOP_LIMIT
        self.path.write_text(oversized)
        with self.assertRaises(LimitExceeded):
            self.repair.run()
        self.assertEqual(self.path.read_text(), oversized)

    def test_shortcut_cardinality_limit(self):
        self.path.with_name("Other.desktop").write_text(self.original)
        with patch.object(steam_icons, "MAX_SHORTCUTS", 1), self.assertRaises(LimitExceeded):
            self.repair.run()
        self.assert_unchanged()

    def test_discovery_entry_and_depth_limits(self):
        budget = Budget()
        budget.entries = 0
        with self.assertRaises(LimitExceeded):
            list(walk_files(self.path.parent, budget))
        folder = self.home / "deep"
        folder.joinpath(*(["child"] * 10)).mkdir(parents=True)
        with self.assertRaises(LimitExceeded):
            list(walk_files(folder, Budget(), recursive=True))

    def test_symlinked_icon_tree_is_not_traversed(self):
        folder = self.repair.data / "icons"
        folder.mkdir()
        (folder / "loop").symlink_to(folder, target_is_directory=True)
        self.assertEqual(list(walk_files(folder, Budget(), recursive=True)), [])

    def test_artwork_input_candidate_decode_and_total_byte_limits(self):
        cache = self.repair.roots[0] / "appcache/librarycache/123"
        image = next(cache.iterdir())
        with patch.object(steam_icons, "ARTWORK_LIMIT", 1), self.assertRaises(LimitExceeded):
            self.repair.artwork("123")
        self.repair.budget = Budget()
        self.repair.budget.bytes = 1
        with self.assertRaises(LimitExceeded):
            self.repair.artwork("123")
        self.repair.budget = Budget()
        self.repair.budget.decodes = 0
        with self.assertRaises(LimitExceeded):
            self.repair.artwork("123")
        self.repair.budget = Budget()
        self.repair.budget.deadline = 0
        with self.assertRaises(LimitExceeded):
            self.repair.artwork("123")
        self.repair.budget = Budget()
        for number in range(65):
            (cache / f"{number:040x}.png").write_bytes(b"invalid")
        with self.assertRaises(LimitExceeded):
            self.repair.artwork("123")
        self.assertTrue(image.exists())
        self.assert_unchanged()

    def test_desktop_replaced_or_edited_during_artwork_is_preserved(self):
        for replacement in [False, True]:
            self.path.write_text(self.original)
            artwork = self.repair.artwork
            edited = self.original.replace("Name=Game", "Name=User edit")
            def change(appid):
                data = artwork(appid)
                if replacement:
                    self.path.unlink()
                self.path.write_text(edited)
                return data
            with patch.object(self.repair, "artwork", side_effect=change), self.assertRaises(UnsafePath):
                self.repair.run()
            self.assertEqual(self.path.read_text(), edited)

    def test_discovered_file_identity_checked_before_read(self):
        with Directory(self.path.parent) as directory:
            original = directory.snapshot(self.path.name)
            self.path.unlink()
            self.path.write_text("replacement")
            with self.assertRaises(UnsafePath):
                directory.read(self.path.name, 1024, Budget(), expected=original)

    def test_final_target_and_parent_identity_checks(self):
        with Directory(self.path.parent) as directory:
            original = directory.snapshot(self.path.name)
            with self.assertRaises(UnsafePath):
                directory.write(self.path.name, b"plugin change", original,
                                before_replace=lambda: self.path.write_text("user change"))
            self.assertEqual(self.path.read_text(), "user change")
            original = directory.snapshot(self.path.name)
            moved = self.path.parent.with_name("old-applications")
            def move_parent():
                self.path.parent.rename(moved)
                self.path.parent.mkdir()
                self.path.write_text("replacement directory")
            with self.assertRaises(UnsafePath):
                directory.write(self.path.name, b"plugin change", original, before_replace=move_parent)
            self.assertEqual(self.path.read_text(), "replacement directory")
            self.assertEqual((moved / self.path.name).read_text(), "user change")
            self.assertFalse(list(moved.glob(".steam-icons-*")))

    def test_output_file_symlink_and_hardlink_rejected(self):
        self.repair.icons.mkdir(parents=True)
        victim = self.home / "victim"
        victim.write_text("preserve")
        icon = self.repair.icons / "123.png"
        for hardlink in [False, True]:
            if hardlink:
                os.link(victim, icon)
            else:
                icon.symlink_to(victim)
            with self.assertRaises(UnsafePath):
                self.repair.run()
            self.assertEqual(victim.read_text(), "preserve")
            self.assert_unchanged()
            icon.unlink()

    def test_state_changed_during_repair_is_not_overwritten(self):
        self.repair.state.mkdir(parents=True)
        ledger = self.repair.state / "changes.json"
        ledger.write_text("{}")
        artwork = self.repair.artwork
        def change(appid):
            data = artwork(appid)
            ledger.write_text('{"user": "edit"}')
            return data
        with patch.object(self.repair, "artwork", side_effect=change), self.assertRaises(UnsafePath):
            self.repair.run()
        self.assertEqual(ledger.read_text(), '{"user": "edit"}')
        self.assert_unchanged()

    def test_restore_checks_identity_before_replacement(self):
        self.repair.run()
        original_write = Directory.write
        def change(directory, name, data, expected, **kwargs):
            if name == self.path.name:
                self.path.write_text("user edit during restore")
            return original_write(directory, name, data, expected, **kwargs)
        with patch.object(Directory, "write", new=change), self.assertRaises(UnsafePath):
            self.repair.run(restore=True)
        self.assertEqual(self.path.read_text(), "user edit during restore")

    def test_repair_missing_output_preserves_original_undo(self):
        self.repair.run()
        Path(entry(self.path.read_text())[2]["Icon"]).unlink()
        self.repair.run()
        self.repair.run(restore=True)
        self.assertEqual(entry(self.path.read_text())[2]["Icon"], "steam")

    def test_isolated_cli_ignores_shadowed_executables_and_python_modules(self):
        fake = self.home / "fake-bin"
        fake.mkdir()
        marker = self.home / "shadow-executed"
        for name in ["python3", "magick"]:
            executable = fake / name
            executable.write_text(f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 99\n")
            executable.chmod(0o755)
        (fake / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').close()")
        env = dict(os.environ, PATH=str(fake), PYTHONPATH=str(fake), PYTHONHOME=str(fake),
                   MAGICK_CONFIGURE_PATH=str(fake), MAGICK_CODER_MODULE_PATH=str(fake))
        result = subprocess.run(["/usr/bin/python3", "-I", str(Path(steam_icons.__file__)), "--dry-run"],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Repair: Game.desktop", result.stdout)
        self.assertFalse(marker.exists())
        self.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
