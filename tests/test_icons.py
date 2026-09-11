import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import subprocess


def make_image(path, size=(32, 32)):
    subprocess.run(["magick", "-size", f"{size[0]}x{size[1]}", "xc:red", str(path)], check=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from steam_icons import Repair, entry


class IconsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_DATA_DIRS": str(self.home / "system"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        cache = self.home / ".local/share/Steam/appcache/librarycache/123"
        cache.mkdir(parents=True)
        make_image(cache / ("a" * 40 + ".jpg"))
        self.repair = Repair(self.home)
        self.repair.icon_dirs = [self.repair.data / "icons", self.home / ".icons", self.home / "system/icons"]
        apps = self.repair.data / "applications"
        apps.mkdir()
        self.path = apps / "Game.desktop"
        self.original = '[Desktop Entry]\nName=Game\nExec=steam steam://rungameid/123\nIcon=steam\nType=Application\n\n[Desktop Action other]\nIcon=other\nExec=other\n'
        self.path.write_text(self.original)

    def test_repair_idempotence_and_restore_preserve_edits(self):
        self.repair.run()
        repaired = self.path.read_text()
        self.assertTrue(Path(entry(repaired)[2]["Icon"]).is_file())
        self.assertIn('[Desktop Action other]\nIcon=other', repaired)
        before = self.path.stat().st_mtime_ns
        self.repair.run()
        self.assertEqual(before, self.path.stat().st_mtime_ns)
        self.path.write_text(repaired.replace('Name=Game', 'Name=Renamed'))
        self.repair.run(restore=True)
        self.assertEqual(entry(self.path.read_text())[2]["Icon"], "steam")
        self.assertIn('Name=Renamed', self.path.read_text())

    def test_preserve_custom_and_existing_game_icons(self):
        for icon in ['my-custom-icon', 'steam_icon_123']:
            folder = self.repair.data / "icons/hicolor/32x32/apps"
            folder.mkdir(parents=True, exist_ok=True)
            make_image(folder / 'steam_icon_123.png')
            text = self.original.replace('Icon=steam\n', f'Icon={icon}\n')
            self.path.write_text(text)
            self.repair.run()
            self.assertEqual(text, self.path.read_text())

    def test_dry_run_missing_icon_and_later_install(self):
        self.repair.run(dry_run=True)
        self.assertEqual(self.original, self.path.read_text())
        self.path.write_text(self.original.replace('Icon=steam\n', ''))
        self.repair.run()
        self.repair.run(restore=True)
        self.assertNotIn('Icon', entry(self.path.read_text())[2])
        later = self.path.with_name('Later.desktop')
        later.write_text(self.original.replace('Icon=steam', 'Icon=steam_icon_123'))
        self.repair.run()
        self.assertTrue(entry(later.read_text())[2]['Icon'].startswith('/'))

    def test_no_artwork_and_non_steam_unchanged(self):
        for text in [self.original.replace('/123', '/456'), self.original.replace('steam://rungameid/123', 'https://example.com')]:
            self.path.write_text(text)
            self.repair.run()
            self.assertEqual(text, self.path.read_text())

    def test_artwork_selects_largest_square_and_skips_invalid(self):
        cache = self.repair.roots[0] / 'appcache/librarycache/123'
        make_image(cache / ('b' * 40 + '.png'), (64, 64))
        make_image(cache / ('c' * 40 + '.png'), (128, 64))
        (cache / ('d' * 40 + '.png')).write_bytes(b'not an image')
        result = self.repair.artwork('123')
        self.assertTrue(result.startswith(b'\x89PNG\r\n\x1a\n'))
        dimensions = subprocess.run(['magick', 'identify', '-format', '%w %h', 'PNG:-'],
                                    input=result, capture_output=True, check=True).stdout
        self.assertEqual(dimensions, b'64 64')

    def test_restore_preserves_user_icon(self):
        self.repair.run()
        text = self.path.read_text()
        self.path.write_text(text.replace(entry(text)[2]['Icon'], 'custom'))
        self.repair.run(restore=True)
        self.assertEqual(entry(self.path.read_text())[2]['Icon'], 'custom')


if __name__ == '__main__':
    unittest.main()
