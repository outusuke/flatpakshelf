BIN_PATH = os.path.expanduser("~/.local/bin/flatpakshelf")
DESKTOP_PATH = os.path.join(GLib.get_user_data_dir(), "applications", f"{APP_ID}.desktop")
DBUS_SERVICE_PATH = os.path.join(GLib.get_user_data_dir(), "dbus-1", "services", f"{APP_ID}.service")
DBUS_SERVICE = f"""[D-BUS Service]
Name={APP_ID}
Exec={BIN_PATH} --gapplication-service
"""
DESKTOP_ENTRY = f"""[Desktop Entry]
Type=Application
Name=Flatpakshelf
Comment=Synaptic-style Flatpak manager
Exec={BIN_PATH} %f
Icon=system-software-install
Terminal=false
DBusActivatable=true
MimeType=application/vnd.flatpak.repo;
Categories=System;PackageManager;GTK;
StartupWMClass={APP_ID}
"""


def install_self():
    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DESKTOP_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DBUS_SERVICE_PATH), exist_ok=True)
    try:
        shutil.copyfile(os.path.abspath(__file__), BIN_PATH)
    except shutil.SameFileError:
        pass
    os.chmod(BIN_PATH, 0o755)
    with open(DESKTOP_PATH, "w") as f:
        f.write(DESKTOP_ENTRY)
    # lets a notification click start the app when it is not running
    with open(DBUS_SERVICE_PATH, "w") as f:
        f.write(DBUS_SERVICE)
    print(f"Installed {BIN_PATH}, {DESKTOP_PATH} and {DBUS_SERVICE_PATH}")


def uninstall_self():
    for path in (BIN_PATH, DESKTOP_PATH, DBUS_SERVICE_PATH):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    print("Removed menu entry and binary")
