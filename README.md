# Steam Game Icons for Omarchy

![Steam game icons displayed in the Omarchy launcher](assets/steam-game-icons.png)

Repairs Steam game icons in the app launcher. It reacts when Steam shortcuts appear or change in Omarchy’s launcher. Existing
Steam entries are checked once when enabled or at session startup. There is no
periodic scan. No bar widget, API key, or network access.

## Install

Enabling this plugin authorizes it to update Icon fields in your user Steam
shortcuts and save local artwork and undo records as described below.

Uses Python and ImageMagick, both included with standard Omarchy. No extra
packages are required.

```sh
omarchy plugin add https://github.com/14brussell/Steam-Game-Icons --enable --yes
```

For a manual installation, copy this directory to
`~/.config/omarchy/plugins/io.github.14brussell.steam-icons`, then run:

```sh
omarchy plugin validate ~/.config/omarchy/plugins/io.github.14brussell.steam-icons
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.14brussell.steam-icons
```

## Behavior

- Repairs existing user `.desktop` shortcuts with Steam `rungameid` or `run` URLs.
- Replaces generic Steam icons, absent icons, broken absolute icon paths, and
  missing `steam_icon_<appid>` icons. Preserves custom named and working icons.
- Uses installed Steam icons or square hash-named artwork in Steam's library
  cache. Supports native Steam, legacy cache filenames, and Flatpak Steam.
- Copies icons into `$XDG_DATA_HOME/icons/omarchy-steam-icons` as PNGs, so clearing
  Steam's cache won't break repaired shortcuts.
- Saves original Icon fields in `$XDG_STATE_HOME/omarchy-steam-icons/changes.json`.
- Leaves games without cached artwork alone. It retries when the shortcut changes,
  at the next session startup, or when you request a manual scan. Artwork arriving
  by itself does not trigger a scan.

This repairs launcher shortcuts; it does not create shortcuts for an entire
Steam library. Use Steam's Create Desktop Shortcut option for missing games.
It does not fetch artwork, change Steam itself, or replace working custom icons.

## Preview and undo

```sh
/usr/bin/python3 -I steam_icons.py --dry-run
omarchy-shell steam-game-icons status
omarchy-shell steam-game-icons sync
omarchy plugin disable io.github.14brussell.steam-icons
/usr/bin/python3 -I steam_icons.py --restore
```

Run commands from the plugin directory. Undo changes only Icon fields still
pointing to this plugin's artwork, preserving other shortcut edits. Disable
before undoing to prevent the next scan from applying repairs again. Removing
or disabling the plugin leaves repaired icons working; undo first if desired.

## Filesystem safety and scan limits

The service runs `/usr/bin/python3 -I` and `/usr/bin/magick`. Python ignores
ambient Python import settings; image decoding uses a minimal environment and
an isolated temporary working directory.

Data, state, and shortcut directories must be absolute, owned by your user, and
not writable by other users. Their ancestors must be owned by your user or root
and not writable by others (root-owned sticky ancestors such as `/tmp` are
allowed). Symlinks in mutation paths, symlinked files, hardlinked files, and
non-regular files are rejected. Standard Steam cache aliases can be resolved
for reading; the resulting directory chain is validated before use.

The worker retains directory descriptors, uses a non-truncating, nonblocking
lock, and checks directory and file identities immediately before replacement.
If a shortcut changes while artwork is being prepared, the repair stops without
overwriting that edit. Restore uses the same checks. These checks do not sandbox
other processes running as your user.

Each scan is limited to 8,192 discovered directory entries, 1,024 shortcuts,
eight levels of icon subdirectories, and 128 MiB of input. Individual desktop
files are limited to 256 KiB, the undo ledger to 1 MiB / 2,048 records, and image
inputs to 16 MiB. Each game has at most 64 artwork candidates; a scan performs at
most 64 decoder calls and checks a 60-second deadline during discovery, reads,
and decoding. Each decoder has a maximum 10-second timeout, 5 seconds of CPU,
512 MiB of address space, and 8 MiB per output file. Reaching a limit stops the
scan; completed repairs and their undo records remain valid.

Installed icon discovery uses your data directory's `icons`, `~/.icons`, and
the system `hicolor` directories under `/usr/local/share/icons` and
`/usr/share/icons`. Custom `XDG_DATA_DIRS` search paths are not traversed.
Failures are reported in the shell log; the service status shows the exit code.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Remove

To keep the repaired icons, remove the plugin directly:

```sh
omarchy plugin remove io.github.14brussell.steam-icons --yes
```

To restore the original icons, disable the plugin and run `--restore` as shown
above before removing it. Saved artwork and undo records remain in your data
and state directories so removing the plugin does not break repaired shortcuts.

## License

Plugin code is licensed under the [MIT License](LICENSE). Game artwork visible
in the screenshots belongs to its respective owners and is not covered by the
code license.
