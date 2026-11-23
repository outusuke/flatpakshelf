# Flatshelf

A Synaptic-style Flatpak manager. GTK4, libadwaita, Everything goes through the `flatpak` command line.

## Run

```
python3 flatshelf.py
```

Requires `python3-gobject`, GTK 4, libadwaita 1.5+ and `flatpak`.

## Install

```
python3 flatpakshelf.py --install
```

Copies the script to `~/.local/bin/flatpakshelf` and adds a menu entry. `--uninstall` removes both.

## License

GPL-3.0-or-later
