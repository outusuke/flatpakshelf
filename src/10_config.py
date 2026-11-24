APP_ID = "io.github.outusuke.Flatpakshelf"
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "flatpakshelf")
CACHE_FILE = os.path.join(CACHE_DIR, "packages.json")
DATA_CACHE = os.path.join(CACHE_DIR, "userdata.json")
STATE_FILE = os.path.join(CACHE_DIR, "autoupdate.json")
MEDIA_DIR = os.path.join(CACHE_DIR, "media")
MEDIA_MAX_BYTES = 12_000_000
SHOT_LIMIT = 8
SHOT_WIDTH = 800
SHOT_HEIGHT = 160
ICON_PX = 28
FLATPAK_ROOTS = {"user": os.path.join(GLib.get_user_data_dir(), "flatpak"),
                 "system": "/var/lib/flatpak"}
# Plain tab-separated output, no headers or fancy formatting.
FLATPAK_ENV = {**os.environ, "FLATPAK_FANCY_OUTPUT": "0"}
SCOPES = ("--user", "--system")
LIST_COLS = ("ref", "application", "name", "version", "branch", "arch", "origin",
             "installation", "description", "size")
REMOTE_COLS = ("ref", "application", "name", "version", "branch", "arch", "origin",
               "description", "installed-size")
SIZE_UNITS = {"bytes": 1, "byte": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
MODES = ["All", "Installed", "Not installed", "Applications", "Runtimes", "Upgradable"]
UNIT_DIR = os.path.join(GLib.get_user_config_dir(), "systemd", "user")
AUTO_TIMER = "flatpakshelf-update.timer"
AUTO_SERVICE = "flatpakshelf-update.service"
AUTO_MODE_LABELS = ["Download only", "Download and install"]
AUTO_MODE_HINTS = ["Downloads updates, you install them from here",
                   "Installs updates without asking"]
HISTORY_COLS = ("time", "change", "application", "branch", "installation", "remote")
HISTORY_DAYS = (7, 30, 90)
HISTORY_LABELS = ["Last 7 days", "Last 30 days", "Last 90 days"]
HISTORY_LIMIT = 300
AUTO_FREQS = {"daily": "Daily", "weekly": "Weekly"}
GS_NOTE = "Separate from the timer above."
GS_BOTH = "Both are on and will update the same apps. Turn one off."
SERVICE_UNIT = """[Unit]
Description=Update Flatpaks (Flatpakshelf)

[Service]
Type=oneshot
ExecStart={exe} --auto-update{extra}
"""
TIMER_UNIT = """[Unit]
Description=Update Flatpaks on a schedule (Flatpakshelf)

[Timer]
OnCalendar={freq}
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
"""
