BIN_PATH = os.path.expanduser("~/.local/bin/flatpakshelf")
DESKTOP_PATH = os.path.join(GLib.get_user_data_dir(), "applications", f"{APP_ID}.desktop")
DESKTOP_ENTRY = f"""[Desktop Entry]
Type=Application
Name=Flatpakshelf
Comment=Synaptic-style Flatpak manager
Exec={BIN_PATH}
Icon=system-software-install
Terminal=false
Categories=System;PackageManager;GTK;
StartupWMClass={APP_ID}
"""


def install_self():
    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DESKTOP_PATH), exist_ok=True)
    try:
        shutil.copyfile(os.path.abspath(__file__), BIN_PATH)
    except shutil.SameFileError:
        pass
    os.chmod(BIN_PATH, 0o755)
    with open(DESKTOP_PATH, "w") as f:
        f.write(DESKTOP_ENTRY)
    print(f"Installed {BIN_PATH} and {DESKTOP_PATH}")


def uninstall_self():
    for path in (BIN_PATH, DESKTOP_PATH):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    print("Removed menu entry and binary")
