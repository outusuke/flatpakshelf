# src/

`flatpakshelf.py` in the repo root is built from these fragments. It stays a single file because
`--install` copies that one file to `~/.local/bin` and the systemd unit runs that copy.

Edit a fragment, then `python3 build.py`, then commit `src/` and `flatpakshelf.py` together.
`python3 build.py --check` fails if they disagree. `python3 build.py --map` shows which lines of
the built file come from which fragment.

## Layers

The number is the layer. A fragment may use names from its own layer and lower ones, never a
higher one; `build.py` fails otherwise. Fragments are not standalone modules: all imports live
in `00_header.py` and the built file is the only thing you run.

| Fragment | What is in it |
|---|---|
| `00_header.py` | shebang, licence line, docstring, imports, `gi.require_version` |
| `10_config.py` | constants, systemd unit templates |
| `20_install.py` | **writes outside the app:** `install_self`, `uninstall_self`, `BIN_PATH`, desktop entry |
| `30_flatpak_data.py` | read-only `flatpak` calls and their parsers, package and data-size caches |
| `40_model.py` | `Pkg`, `PkgModel`, `filter_rows`, small text helpers |
| `50_details.py` | info, permissions, metadata and release-notes parsing, `detail_text` |
| `60_operations.py` | **changes flatpak state:** `flatpak_op`, `Tx`, `run_transaction` (libflatpak, deletes app data), `run_ops` |
| `70_automatic.py` | **runs unattended:** systemd units and timer, GNOME Software pause, `auto_update` |
| `80_dialogs.py` | `HistoryWindow`, `VersionWindow`, `AutoUpdatesWindow` |
| `90_main_window.py` | `Win` |
| `99_app.py` | `App` and the command-line entry point |

To audit what can change the system, read `20`, `60` and `70`. Nothing below `80` draws a widget.

`tools/verify_split.py` compares the built file with an older revision (AST, lines, bytecode and
a side-by-side run with a fake `flatpak`). It is how the split was checked.
