# Flatshelf

A Synaptic-style Flatpak manager. GTK4, libadwaita, Everything goes through the `flatpak` command line.

## Run

```
python3 flatpakshelf.py
```

Requires `python3-gobject`, GTK 4, libadwaita 1.5+ and `flatpak`.

## Install

```
python3 flatpakshelf.py --install
```

Copies the script to `~/.local/bin/flatpakshelf` and adds a menu entry. `--uninstall` removes both.

## Automatic updates

Menu, Automatic updates. Flatpakshelf writes a systemd user timer (`flatpakshelf-update.timer`) in `~/.config/systemd/user` that runs `flatpak update` daily or weekly, either download only or download and install. It runs while you are logged in. The window also shows GNOME Software's automatic download switch.

Menu, Recent changes lists installs, updates and removals from `flatpak history`, which reads the systemd journal.

Pause updates stops updates for one installed package with `flatpak mask`; Resume updates removes the mask. Change version lists older commits from `flatpak remote-info --log` and installs the one you pick with `flatpak update --commit`, and can pause updates so it stays.

## License

GPL-3.0-or-later
