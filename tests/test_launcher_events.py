"""Integration test using Quickshell's real desktop-entry notifications."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(shutil.which('qs'), 'Quickshell is required')
class LauncherEventsTest(unittest.TestCase):
    def test_only_steam_entry_changes_launch_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = root / 'data/applications'
            apps.mkdir(parents=True)
            runtime = root / 'runtime'
            runtime.mkdir(mode=0o700)
            plugin = root / 'plugin'
            plugin.mkdir()
            shutil.copy2(Path(__file__).resolve().parents[1] / 'SteamIcons.qml', plugin)
            (plugin / 'shell.qml').write_text('import Quickshell\nShellRoot { SteamIcons {} }\n')
            scans = root / 'scans'
            fake_bin = root / 'fake-bin'
            fake_bin.mkdir()
            shadowed = root / 'shadow-executed'
            fake_python = fake_bin / 'python3'
            fake_python.write_text('#!/bin/sh\n/usr/bin/touch ' + str(shadowed) + '\nexit 99\n')
            fake_python.chmod(0o755)
            (fake_bin / 'sitecustomize.py').write_text('open(' + repr(str(shadowed)) + ', "w").close()')
            (plugin / 'steam_icons.py').write_text(
                'from pathlib import Path\np=Path(' + repr(str(scans)) + ')\n'
                'with p.open("a") as f: f.write("scan\\n")\n')
            env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QPA_PLATFORMTHEME='basic',
                       PATH=str(fake_bin) + ':/usr/bin', PYTHONPATH=str(fake_bin), PYTHONHOME=str(fake_bin),
                       XDG_DATA_HOME=str(root / 'data'), XDG_DATA_DIRS=str(root / 'system'),
                       XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'),
                       XDG_RUNTIME_DIR=str(runtime))
            def count():
                return len(scans.read_text().splitlines()) if scans.exists() else 0
            def wait_count(expected):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and count() < expected:
                    time.sleep(.05)
                self.assertEqual(count(), expected, log.read_text())
            def shortcut(name, command, icon='steam'):
                path = apps / (name + '.desktop')
                temporary_path = path.with_suffix('.tmp')
                temporary_path.write_text('[Desktop Entry]\nType=Application\nName=' + name
                                + '\nExec=' + command + '\nIcon=' + icon + '\n')
                temporary_path.replace(path)
                return path
            log = root / 'qs.log'
            with log.open('w') as output:
                process = subprocess.Popen(['qs', '-p', str(plugin / 'shell.qml')],
                                           env=env, stdout=output, stderr=output)
                try:
                    time.sleep(1)
                    self.assertIsNone(process.poll(), log.read_text())
                    self.assertEqual(count(), 0)
                    shortcut('Other', 'true')
                    time.sleep(1)
                    self.assertEqual(count(), 0)
                    game = shortcut('Game', 'steam steam://rungameid/123')
                    wait_count(1)
                    shortcut('Game', 'steam steam://rungameid/123', '/tmp/icons/omarchy-steam-icons/123.png')
                    time.sleep(1)
                    self.assertEqual(count(), 1, 'Own icon update must not launch another worker')
                    shortcut('Game', 'steam steam://rungameid/123')
                    wait_count(2)
                    game.unlink()
                    time.sleep(1)
                    self.assertEqual(count(), 2, 'Removal must not launch a worker')
                    self.assertFalse(shadowed.exists(), 'Service must ignore ambient Python executables and modules')
                finally:
                    process.terminate()
                    process.wait(timeout=5)
