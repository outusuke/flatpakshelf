#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Flatpakshelf: a Synaptic-style Flatpak manager (GTK4 + libadwaita).

Dependencies:
    sudo dnf install python3-gobject gtk4 libadwaita flatpak

Run:
    python3 flatpakshelf.py

Everything goes through the flatpak CLI.
"""
import gzip
import hashlib
import json
import operator
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import xml.etree.ElementTree as ET
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatchcase

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Flatpak", "1.0")
from gi.repository import Adw, Flatpak, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402


APP_ID = "io.github.outusuke.Flatpakshelf"
VERSION = "0.1.0"
REPO_URL = "https://github.com/outusuke/flatpakshelf"
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "flatpakshelf")
CACHE_FILE = os.path.join(CACHE_DIR, "packages.json")
DATA_CACHE = os.path.join(CACHE_DIR, "userdata.json")
STATE_FILE = os.path.join(CACHE_DIR, "autoupdate.json")
MEDIA_DIR = os.path.join(CACHE_DIR, "media")
MEDIA_MAX_BYTES = 12_000_000
SHOT_LIMIT = 8
SEARCH_MODES = ("Name, ID & description", "Name only", "Application ID", "Word start",
                "Exact name or ID")
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
FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
ALL_REMOTES = "All remotes"
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
MimeType=application/vnd.flatpak.repo;application/vnd.flatpak.ref;
Categories=System;PackageManager;GTK;
StartupWMClass={APP_ID}
"""


def refresh_mime_cache():
    """File managers only offer an app for a MimeType after this cache is rebuilt."""
    try:
        subprocess.run(["update-desktop-database", os.path.dirname(DESKTOP_PATH)],
                       capture_output=True)
    except OSError:
        pass


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
    refresh_mime_cache()
    print(f"Installed {BIN_PATH}, {DESKTOP_PATH} and {DBUS_SERVICE_PATH}")


def uninstall_self():
    for path in (BIN_PATH, DESKTOP_PATH, DBUS_SERVICE_PATH):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    refresh_mime_cache()
    print("Removed menu entry and binary")


# ---------------------------------------------------------------- data layer
def flatpak(*args, answer=None):
    """(returncode, stdout) of one flatpak call; (1, "") if flatpak is missing."""
    try:
        r = subprocess.run(["flatpak", *args], capture_output=True, text=True,
                           env=FLATPAK_ENV, input=answer)
    except OSError:
        return 1, ""
    return r.returncode, r.stdout


def parse_rows(out, ncols):
    """Tab-separated rows; anything without an app id in column 2 (a header) is dropped."""
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == ncols and "." in parts[1]:
            rows.append(parts)
    return rows


def size_bytes(text):
    num, _, unit = text.replace("\xa0", " ").partition(" ")
    try:
        return float(num.replace(",", ".")) * SIZE_UNITS.get(unit.strip(), 0)
    except ValueError:
        return 0


def user_data_sizes(found):
    """Fill `found` with {app id: bytes} for each folder in ~/.var/app, as each is measured."""
    root = os.path.expanduser("~/.var/app")
    try:
        dirs = [e.path for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return

    def one(path):
        try:
            out = subprocess.run(["du", "-sb", "--", path], capture_output=True,
                                 text=True).stdout
        except OSError:
            return
        num = out.partition("\t")[0]  # du exits non-zero on unreadable files but still reports
        if num.isdigit():
            found[os.path.basename(path)] = int(num)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, dirs))


def leftover_data_dirs(installed_ids, root=None):
    """App ids with a folder in ~/.var/app but no installed package."""
    root = root or os.path.expanduser("~/.var/app")
    try:
        return sorted(e.name for e in os.scandir(root)
                      if e.is_dir(follow_symlinks=False) and "." in e.name
                      and e.name not in installed_ids)
    except OSError:
        return []


def read_data_cache():
    try:
        with open(DATA_CACHE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_data_cache(sizes):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(DATA_CACHE, "w") as f:
            json.dump(sizes, f)
    except OSError:
        pass


def list_installed():
    _rc, out = flatpak("list", "--columns=" + ",".join(LIST_COLS))
    return parse_rows(out, len(LIST_COLS))


def list_remote(scope, cached):
    """Everything the configured remotes of one scope offer."""
    args = ["remote-ls", scope, "--columns=" + ",".join(REMOTE_COLS)]
    if cached:  # local metadata only; fall back to a normal call if that gives nothing
        rc, out = flatpak(*args, "--cached")
        rows = parse_rows(out, len(REMOTE_COLS))
        if rc == 0 and rows:
            return rows
    _rc, out = flatpak(*args)
    return parse_rows(out, len(REMOTE_COLS))


def mask_patterns(scope):
    """Patterns `flatpak mask` holds for one scope option such as --user."""
    _rc, out = flatpak("mask", scope)
    return [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]


def is_masked(patterns, app_id, arch, branch):
    """Port of flatpak's mask matching: id[/arch[/branch]], empty or missing parts match anything."""
    for pat in patterns:
        pat = pat.split("/", 1)[1] if pat.startswith(("app/", "runtime/")) else pat
        segs = (pat.split("/") + ["", ""])[:3]
        if all(not s or fnmatchcase(v, s) for s, v in zip(segs, (app_id, arch, branch))):
            return True
    return False


def parse_versions(out):
    """[(commit, subject, date)] from the History block of `flatpak remote-info --log`."""
    rows = []
    for key, val in re.findall(r"^\s*(Commit|Subject|Date):[ \t]*(.*)$", out.partition("History:")[2], re.M):
        if key == "Commit":
            rows.append([val.strip(), "", ""])
        elif rows:
            rows[-1][1 if key == "Subject" else 2] = val.strip()
    return [tuple(r) for r in rows]


def parse_remotes(out, scope):
    """[(scope, name, title, url, enabled)] from `flatpak remotes --columns=name,title,url,options`."""
    rows = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split("\t")]
        if len(parts) >= 4 and parts[0] and parts[3] != "Options":
            title = "" if parts[1] == "-" else parts[1]
            rows.append((scope, parts[0], title, parts[2], "disabled" not in parts[3].split(",")))
    return rows


def list_remotes():
    rows = []
    for scope in ("user", "system"):
        _rc, out = flatpak("remotes", "--" + scope, "--show-disabled",
                           "--columns=name,title,url,options")
        rows += parse_remotes(out, scope)
    return rows


def valid_remote_location(text):
    """A URL with a scheme, or an existing .flatpakrepo file."""
    return bool(re.match(r"[a-z][a-z0-9+.-]*://\S+$", text)) or (
        text.endswith(".flatpakrepo") and os.path.isfile(text))


def parse_flatpakref(text):
    """Name, Title, Branch, Url and IsRuntime of a .flatpakref, or None if it is not one."""
    if "[Flatpak Ref]" not in text:
        return None
    info = {k: v.strip() for k, v in re.findall(r"^(Name|Title|Branch|Url|IsRuntime)=(.*)$", text, re.M)}
    return info if info.get("Name") else None


def default_remote_name(location):
    """flathub for https://dl.flathub.org/repo/..., the file stem for a local file."""
    host = re.match(r"[a-z][a-z0-9+.-]*://(?:[^/@]*@)?([^/:]+)", location)
    if host:
        labels = host.group(1).split(".")
        guess = labels[-2] if len(labels) > 1 else labels[0]
    else:
        guess = os.path.basename(location).removesuffix(".flatpakrepo")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", guess)


def enabled_remote_names(scope):
    """Names of the enabled remotes of one scope option such as --user."""
    _rc, out = flatpak("remotes", scope, "--columns=name,title,url,options")
    return [r[1] for r in parse_remotes(out, scope) if r[4]]


def list_updates(scope):
    """(id, arch, branch) of every installed ref in one scope that has an update."""
    found = set()
    for remote in enabled_remote_names(scope):  # one broken remote makes remote-ls fail for the whole scope
        _rc, out = flatpak("remote-ls", scope, remote, "--updates",
                           "--columns=ref,application,branch,arch,origin")
        found |= {(i, a, b) for _ref, i, b, a, _o in parse_rows(out, 5)}
    masks = mask_patterns(scope)  # remote-ls ignores masks, flatpak update skips them
    return {t for t in found if not is_masked(masks, *t)}


def _kind(ref):
    return "runtime" if ref.startswith("runtime/") else "app"


def collect_raw(cached=True):
    """[[kind, id, name, version, branch, arch, origin, scope, summary, installed(0/1/2), size]]
    sorted by name. Installed refs come from `flatpak list`, the rest from `remote-ls`."""
    with ThreadPoolExecutor(max_workers=7) as ex:  # the calls are independent
        f_inst = ex.submit(list_installed)
        f_user = ex.submit(list_remote, SCOPES[0], cached)
        f_sys = ex.submit(list_remote, SCOPES[1], cached)
        f_upd = {s[2:]: ex.submit(list_updates, s) for s in SCOPES}
        f_mask = {s[2:]: ex.submit(mask_patterns, s) for s in SCOPES}
        inst, avail = f_inst.result(), f_user.result() + f_sys.result()
        upd = {s: f.result() for s, f in f_upd.items()}
        masks = {s: f.result() for s, f in f_mask.items()}
    pk = {}
    for ref, i, n, v, b, a, o, sc, s, sz in inst:
        # 1 installed, 2 installed with an update pending, 3 installed with updates paused
        flag = 3 if is_masked(masks.get(sc, ()), i, a, b) else 2 if (i, a, b) in upd.get(sc, ()) else 1
        pk[(i, a, b, sc)] = [_kind(ref), i, n, v, b, a, o, sc, s, flag, sz]
    have = {k[:3] for k in pk}
    for ref, i, n, v, b, a, o, s, sz in avail:
        if (i, a, b) not in have:  # first remote wins; installed refs already listed
            have.add((i, a, b))
            pk[(i, a, b, "")] = [_kind(ref), i, n, v, b, a, o, "", s, 0, sz]
    if not pk:
        raise RuntimeError("flatpak returned no packages (no remotes configured?)")
    return sorted(pk.values(), key=lambda d: ((d[2] or d[1]).lower(), d[1], d[4], d[7]))


def read_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) and data and len(data[0]) == 11 else None
    except Exception:
        return None


def write_cache(raw):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, separators=(",", ":"))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def scope_args(scope):
    """flatpak scope option for an installation id from `flatpak list`."""
    if scope in ("user", "system"):
        return ["--" + scope]
    return [f"--installation={scope}"] if scope else []


def list_history(days):
    """Newest first: [time, change, app id, branch, installation, remote]."""
    _rc, out = flatpak("history", "--reverse", f"--since={days}days",
                       "--columns=" + ",".join(HISTORY_COLS))
    rows = (line.split("\t") for line in out.splitlines())
    return [r for r in rows if len(r) == len(HISTORY_COLS) and "." in r[2]]


def media_path(url):
    ext = os.path.splitext(url.partition("?")[0])[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        ext = ".png"
    return os.path.join(MEDIA_DIR, hashlib.sha1(url.encode()).hexdigest() + ext)


def fetch_media(url):
    if not url.startswith(("https://", "http://")):
        return ""
    path = media_path(url)
    if os.path.exists(path):
        return path
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "flatpakshelf"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(MEDIA_MAX_BYTES + 1)
        if not data or len(data) > MEDIA_MAX_BYTES:
            return ""
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    except Exception:  # a missing picture is not worth an error
        return ""


class Pkg(GObject.Object):
    """Plain Python attributes on purpose: GObject properties cost a C<->Python round
    trip per access, which is painful with thousands of rows."""
    __gtype_name__ = "FlatpakshelfPkg"

    def __init__(self, d):
        super().__init__()
        (self.kind, self.id, name, self.version, self.branch, self.arch, self.origin,
         self.scope, self.summary, inst, self.size) = d
        self.size_bytes = size_bytes(self.size)
        self.data, self.data_bytes = "", 0
        self.icon = None  # "" means no icon, None means not looked up yet
        self.name = name or self.id
        self.installed = bool(inst)
        self.upgradable = inst == 2
        self.paused = inst == 3
        self.status = ("upgradable" if self.upgradable else "paused" if self.paused
                       else "installed" if self.installed else "")
        self.ref = f"{self.kind}/{self.id}/{self.arch}/{self.branch}"
        self.hay = f"{self.name} {self.id} {self.summary}".lower()


def names_summary(pkgs, limit=8):
    names = [p.name for p in pkgs]
    text = "\n".join(names[:limit])
    return text + (f"\n… and {len(names) - limit} more" if len(names) > limit else "")


def plural(n, word="package"):
    return f"{n} {word}" + ("" if n == 1 else "s")


def search_rows(rows, query, how):
    if how == 1:
        return [p for p in rows if query in p.name.lower()]
    if how == 2:
        return [p for p in rows if query in p.id.lower()]
    if how == 3:
        word = re.compile(r"(?<![a-z0-9])" + re.escape(query))
        return [p for p in rows if word.search(p.hay)]
    if how == 4:
        return [p for p in rows if query in (p.name.lower(), p.id.lower())]
    return [p for p in rows if query in p.hay]


def filter_rows(rows, mode, query, how=0, origin=""):
    """Mode and search filter in plain Python. `query` must be lower case."""
    if mode == 1:
        rows = [p for p in rows if p.installed]
    elif mode == 2:
        rows = [p for p in rows if not p.installed]
    elif mode == 3:
        rows = [p for p in rows if p.kind == "app"]
    elif mode == 4:
        rows = [p for p in rows if p.kind == "runtime"]
    elif mode == 5:
        rows = [p for p in rows if p.upgradable]
    if origin:
        rows = [p for p in rows if p.origin == origin]
    if query:
        rows = search_rows(rows, query, how)
    return rows


class PkgModel(GObject.Object, Gio.ListModel):
    """Minimal Python list model. Filtering and sorting happen in plain Python lists
    instead of Gtk filter/sort models calling back into Python per row."""

    def __init__(self):
        super().__init__()
        self.rows = []

    def do_get_item_type(self):
        return Pkg.__gtype__

    def do_get_n_items(self):
        return len(self.rows)

    def do_get_item(self, position):
        return self.rows[position] if 0 <= position < len(self.rows) else None

    def set_rows(self, rows):
        removed = len(self.rows)
        self.rows = rows
        self.items_changed(0, removed, len(rows))


TABS = ("Info", "Permissions", "Metadata", "Changes", "Screenshots")
TAB_EMPTY = ("No information available.", "No special permissions declared.",
             "No metadata available.", "No update notes available.",
             "No screenshots available.")
SHOT_TAB = 4  # widgets, not text
PERM_SECTIONS = ("Context", "Session Bus Policy", "System Bus Policy", "Environment")


def permissions_from_metadata(text):
    """Keep only the sandbox permission groups of a metadata file."""
    out, keep = [], False
    for line in text.splitlines():
        if line.startswith("["):
            name = line.strip("[]")
            keep = name in PERM_SECTIONS or name.startswith("Policy ")
        if keep:
            out.append(line)
    return "\n".join(out)


PERM_GROUPS = {"shared": "Shared with the host", "sockets": "Sockets", "devices": "Devices",
               "features": "Features", "filesystems": "Files", "persistent": "Persistent folders",
               "unset-environment": "Unset variables"}
PERM_LABELS = {
    "network": "Network", "ipc": "Inter-process communication",
    "x11": "X11 windowing system", "fallback-x11": "X11 (only if no Wayland)",
    "wayland": "Wayland windowing system", "pulseaudio": "Sound",
    "session-bus": "Full session bus access", "system-bus": "Full system bus access",
    "ssh-auth": "SSH agent", "pcsc": "Smart cards", "cups": "Printing",
    "gpg-agent": "GPG agent", "dri": "GPU acceleration", "all": "All devices",
    "kvm": "Virtualization (KVM)", "shm": "Shared memory", "input": "Input devices",
    "usb": "USB devices", "bluetooth": "Bluetooth", "devel": "Debugging (ptrace)",
    "multiarch": "32-bit code", "canbus": "CAN bus", "host": "All files",
    "host-os": "Host system files", "host-etc": "Host /etc", "home": "Home folder",
}
PERM_RISKY = {"x11", "session-bus", "system-bus", "ssh-auth", "all", "devel", "usb",
              "host", "host-os", "host-etc", "home"}
BUS_POLICY = {"talk": "Can talk to", "own": "Owns the name", "see": "Can see", "none": "Blocked"}
FS_MODES = {"ro": "Read only", "rw": "Read and write", "create": "Read and write, created if missing"}
# Context keys that are plain on/off switches: (flag to grant, flag to revoke) for `flatpak override`
PERM_FLAGS = {"shared": ("share", "unshare"), "sockets": ("socket", "nosocket"),
              "devices": ("device", "nodevice"), "features": ("allow", "disallow")}
OVERRIDES_MARK = "#overrides"  # separates the app's permissions from the user's overrides
INFO_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.*)$")


def parse_info(text):
    """Split `flatpak info` output into (title, [(key, value)]); None if it isn't that shape."""
    lines = text.splitlines()
    title = ""
    if len(lines) > 1 and lines[0].strip() and not lines[1].strip():
        title, lines = lines[0].strip(), lines[2:]
    rows = [m.groups() for m in map(INFO_LINE.match, lines) if m]
    return (title, rows) if len(rows) >= 2 else None


def perm_item(key, item):
    """(title, subtitle, risky, switch) for one entry of a Context permission list; switch is
    (key, name, granted) for entries that can be turned on or off, else None."""
    if key != "filesystems":
        base = item.lstrip("!")
        granted = base == item
        ctl = (key, base, granted) if key in PERM_FLAGS else None
        return PERM_LABELS.get(base, base), "" if granted else "Disabled", granted and base in PERM_RISKY, ctl
    denied = item.startswith("!")
    base, _, mode = item.lstrip("!").partition(":")
    title = PERM_LABELS.get(base)
    if title is None:
        title = base[4:].replace("-", " ").capitalize() + " folder" if base.startswith("xdg-") else base
    if denied:
        return title, "Denied", False, None
    return title, FS_MODES.get(mode, "Read and write"), base in PERM_RISKY and mode != "ro", None


def parse_keyfile(text):
    """{group title: [(title, subtitle, risky, switch)]} from a permissions keyfile."""
    groups, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section and "=" in line:
            key, _, val = line.partition("=")
            if section == "Context":
                title = PERM_GROUPS.get(key, key.capitalize())
                rows = [perm_item(key, i) for i in val.split(";") if i]
            elif section.endswith("Bus Policy"):
                title = section.replace(" Policy", "")
                rows = [(key, BUS_POLICY.get(val, val), val == "own", None)]
            else:
                title = "Environment variables" if section == "Environment" else section
                rows = [(key, val, False, None)]
            groups.setdefault(title, []).extend(rows)
    return groups


def parse_permissions(text):
    """[(group title, [(title, subtitle, risky, switch, overridden)])] from permissions text that may
    end with the user's overrides after OVERRIDES_MARK; None if there is nothing to show."""
    base, _, over = text.partition(OVERRIDES_MARK)
    groups = {t: {r[0]: r + (False,) for r in rows} for t, rows in parse_keyfile(base).items()}
    for title, rows in parse_keyfile(over).items():
        merged = groups.setdefault(title, {})
        for r in rows:  # an override replaces the app's own entry of the same name
            merged[r[0]] = r + (True,)
    return [(t, list(rows.values())) for t, rows in groups.items() if rows] or None


CHANGE_HEAD = re.compile(r"^\S+\s{2}\d{4}-\d{2}-\d{2}$")


def parse_metadata(text):
    """[(section, [(key, value)])] from a metadata keyfile; None for status text."""
    groups = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            groups.append((line[1:-1], []))
        elif groups and "=" in line:
            key, _, val = line.partition("=")
            groups[-1][1].append((key, val))
    return [g for g in groups if g[1]] or None


def parse_changes(text):
    """[(heading, body)] from release notes or a `remote-info --log` history; None otherwise."""
    if "History:" in text:
        entries = [(subject or commit[:12], f"{date}\n{commit[:12]}".strip())
                   for commit, subject, date in parse_versions(text)]
        return entries or None
    entries = []
    for block in text.split("\n\n"):
        head, _, body = block.partition("\n")
        if not CHANGE_HEAD.match(head):
            return None
        entries.append((head, "\n".join(ln.strip() for ln in body.splitlines())))
    return entries


def appstream_files(p):
    scopes = [p.scope] if p.installed else ["user", "system"]
    return [os.path.join(FLATPAK_ROOTS.get(sc, FLATPAK_ROOTS["system"]), "appstream", p.origin,
                         p.arch, "active", "appstream.xml.gz") for sc in scopes]


def icon_file(p, sizes=(64, 128)):
    """Cached AppStream icon, else one the installed app exports."""
    found = [os.path.join(os.path.dirname(x), "icons", f"{s}x{s}", f"{p.id}.png")
             for x in appstream_files(p) for s in sizes]
    if p.installed:
        root = FLATPAK_ROOTS.get(p.scope, FLATPAK_ROOTS["system"])
        icons = os.path.join(root, "exports", "share", "icons", "hicolor")
        found += [os.path.join(icons, f"{s}x{s}", "apps", f"{p.id}.png") for s in (*sizes, 256, 48)]
        found.append(os.path.join(icons, "scalable", "apps", f"{p.id}.svg"))
    return next((f for f in found if os.path.isfile(f)), "")


_TEXTURES = {}


def texture_from(path):
    if path not in _TEXTURES:
        if len(_TEXTURES) > 600:
            _TEXTURES.clear()
        try:
            _TEXTURES[path] = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            _TEXTURES[path] = None
    return _TEXTURES[path]


def pkg_icon(p):
    if p.icon is None:
        p.icon = icon_file(p)
    return texture_from(p.icon) if p.icon else None


XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def as_int(text):
    return int(text) if text and text.isdigit() else 0


def pick_image(images):
    """Biggest thumbnail up to SHOT_WIDTH, else the smallest larger one, else the source."""
    thumbs = sorted((w, u) for t, w, u in images if t == "thumbnail" and w)
    fit = [t for t in thumbs if t[0] <= SHOT_WIDTH]
    if fit:
        return fit[-1][1]
    if thumbs:
        return thumbs[0][1]
    return next((u for t, _w, u in images if t == "source"), images[0][2] if images else "")


def screenshot_list(p, limit=SHOT_LIMIT):
    for path in appstream_files(p):
        try:
            with gzip.open(path, "rb") as f:
                for _ev, el in ET.iterparse(f):
                    if el.tag != "component":
                        continue
                    cid = (el.findtext("id") or "").strip()
                    if cid.removesuffix(".desktop") != p.id:
                        el.clear()
                        continue
                    shots = []
                    for shot in el.iterfind("screenshots/screenshot"):
                        caption = next((" ".join("".join(c.itertext()).split())
                                        for c in shot.iterfind("caption") if not c.get(XML_LANG)), "")
                        images = [(im.get("type", "source"), as_int(im.get("width")),
                                   (im.text or "").strip())
                                  for im in shot.iterfind("image") if not im.get(XML_LANG)]
                        url = pick_image([i for i in images if i[2].startswith(("https://", "http://"))])
                        if url:
                            shots.append((caption, url))
                    return shots[:limit]
        except (OSError, ET.ParseError, EOFError):
            continue
    return []


def release_notes(p, limit=6):
    """Release notes of an app from the AppStream data of its remote, newest first."""
    for path in appstream_files(p):
        try:
            with gzip.open(path, "rb") as f:
                for _ev, el in ET.iterparse(f):
                    if el.tag != "component":
                        continue
                    cid = (el.findtext("id") or "").strip()
                    if cid.removesuffix(".desktop") != p.id:
                        el.clear()  # keeps memory flat on a catalog with thousands of apps
                        continue
                    blocks = []
                    for rel in list(el.iterfind("releases/release"))[:limit]:
                        head = f"{rel.get('version', '?')}  {rel.get('date', '')[:10]}".strip()
                        lines = []
                        for node in rel.iterfind("description//*"):
                            text = " ".join("".join(node.itertext()).split())
                            if node.tag == "li" and text:
                                lines.append(f"  • {text}")
                            elif node.tag == "p" and text:
                                lines.append(f"  {text}")
                        blocks.append("\n".join([head, *lines]))
                    return "\n\n".join(blocks)
        except (OSError, ET.ParseError, EOFError):
            continue
    return ""


def commit_log(p):
    scope = scope_args(p.scope) if p.installed else []
    _rc, out = flatpak("remote-info", "--log", *scope, p.origin, p.ref)
    return out


def detail_text(p, idx):
    """Text for one tab of the details pane (0 info, 1 permissions, 2 metadata, 3 changes)."""
    if idx == 3:
        text = release_notes(p) if p.kind == "app" else ""
        return (text or commit_log(p)).strip() or TAB_EMPTY[idx]
    if p.installed:
        flags = ([], ["--show-permissions"], ["--show-metadata"])[idx]
        _rc, text = flatpak("info", *scope_args(p.scope), f"--arch={p.arch}", *flags,
                            p.id, p.branch)
        if idx == 1:  # what the user changed, e.g. with Flatseal
            _rc, over = flatpak("override", "--show", "--user", p.id)
            if over.strip():
                text = f"{text.strip()}\n{OVERRIDES_MARK}\n{over}"
    else:  # not installed: ask the remote (local metadata first, then the network)
        flags = [] if idx == 0 else ["--show-metadata"]
        text = ""
        for extra in (["--cached"], []):
            rc, out = flatpak("remote-info", *flags, *extra, p.origin, p.ref)
            if rc == 0 and out.strip():
                text = out
                break
        if idx == 1:
            text = permissions_from_metadata(text)
    return text.strip() or TAB_EMPTY[idx]


def parse_unused(out):
    """[(id, branch)] from the table `flatpak uninstall --unused --assumeno` prints."""
    return [m.groups() for line in out.splitlines()
            if (m := re.match(r"\s*\d+\.\s+(\S+\.\S+)\s+(\S+)", line))]


def flatpak_op(*args):
    """(returncode, message) of one flatpak call that changes something."""
    try:
        r = subprocess.run(["flatpak", *args], capture_output=True, text=True,
                           env=FLATPAK_ENV)
    except OSError as e:
        return 1, str(e)
    return r.returncode, (r.stderr or r.stdout).strip()


def remote_names(scope):
    """Names of the enabled remotes of one installation ('user' or 'system')."""
    return set(enabled_remote_names("--" + scope))


Tx = namedtuple("Tx", "action scope refs origin commit delete_data",
                defaults=(None, None, False))


def install_commands(pkgs, scope):
    """One install per origin remote."""
    by_origin = {}
    for p in pkgs:
        by_origin.setdefault(p.origin, []).append(p.ref)
    return [Tx("install", scope, refs, origin) for origin, refs in by_origin.items()]


def uninstall_commands(pkgs, delete_data):
    """One uninstall per installation, so a ref present in both is unambiguous."""
    by_scope = {}
    for p in pkgs:
        by_scope.setdefault(p.scope, []).append(p.ref)
    return [Tx("uninstall", scope, refs, delete_data=delete_data)
            for scope, refs in by_scope.items()]


def update_commands(pkgs):
    by_scope = {}
    for p in pkgs:
        by_scope.setdefault(p.scope, []).append(p.ref)
    return [Tx("update", scope, refs) for scope, refs in by_scope.items()]


def launch_command(p):
    return ["flatpak", "run", f"--branch={p.branch}", *scope_args(p.scope), p.id]


def data_dir(p):
    return os.path.expanduser(f"~/.var/app/{p.id}")


def installation(scope):
    if scope == "user":
        return Flatpak.Installation.new_user(None)
    if scope == "system":
        return Flatpak.Installation.new_system(None)
    for inst in Flatpak.get_system_installations(None):
        if inst.get_id() == scope:
            return inst
    raise RuntimeError(f"Unknown installation: {scope}")


def run_transaction(tx, report):
    """(returncode, message) of a Tx run through libflatpak; report(fraction, text) as it goes."""
    verbs = {Flatpak.TransactionOperationType.INSTALL: "Installing",
             Flatpak.TransactionOperationType.UPDATE: "Updating",
             Flatpak.TransactionOperationType.UNINSTALL: "Removing"}
    state = {"done": 0, "total": 0}

    def ready(t):
        state["total"] = len(t.get_operations())
        return True

    def new_operation(_t, op, progress):
        label = (f"{verbs.get(op.get_operation_type(), 'Working on')} "
                 f"{op.get_ref().split('/')[1]} ({state['done'] + 1}/{state['total']})")

        def changed(prog):
            report((state["done"] + prog.get_progress() / 100) / state["total"], label)

        progress.set_update_frequency(100)
        progress.connect("changed", changed)
        changed(progress)

    report(None, "Preparing")
    try:
        t = Flatpak.Transaction.new_for_installation(installation(tx.scope), None)
        t.set_no_interaction(True)
        t.connect("ready", ready)
        t.connect("new-operation", new_operation)
        t.connect("operation-done", lambda *_: state.update(done=state["done"] + 1))
        for ref in tx.refs:
            if tx.action == "install":
                t.add_install(tx.origin, ref, None)
            elif tx.action == "update":
                t.add_update(ref, None, tx.commit)
            else:
                t.add_uninstall(ref)
        t.run(None)
    except (GLib.Error, RuntimeError) as e:
        return 1, getattr(e, "message", None) or str(e)
    if tx.delete_data:
        for ref in tx.refs:
            if ref.startswith("app/"):
                shutil.rmtree(os.path.expanduser(f"~/.var/app/{ref.split('/')[1]}"),
                              ignore_errors=True)
    return 0, ""


def run_ops(cmds, on_progress=None):
    """Run the steps in order, stopping at the first failure: (ok, last message line).

    A step is a Tx or the arguments of a flatpak call; on_progress(fraction or None, text)
    gets the overall progress, with None while there is nothing to measure.
    """
    for n, step in enumerate(cmds):
        def report(frac, text, n=n):
            if on_progress:
                on_progress(None if frac is None else (n + frac) / len(cmds), text)

        if isinstance(step, Tx):
            rc, msg = run_transaction(step, report)
        else:
            report(None, "Working")
            rc, msg = flatpak_op(*step)
        if rc:
            lines = msg.splitlines()
            return False, lines[-1] if lines else f"flatpak exited with {rc}"
    return True, ""


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True).stdout.strip()


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def automatic_settings():
    exec_line = re.search(r"^ExecStart=(.*)$", read_text(os.path.join(UNIT_DIR, AUTO_SERVICE)), re.M)
    cal = re.search(r"^OnCalendar=(.*?)\s*$", read_text(os.path.join(UNIT_DIR, AUTO_TIMER)), re.M)
    mode = 1 if exec_line and "--no-deploy" not in exec_line.group(1) else 0
    return {"mode": mode, "freq": cal.group(1) if cal else "daily"}


def timer_state():
    """(systemctl usable, enabled, next run) for the user timer."""
    try:
        enabled = systemctl("is-enabled", AUTO_TIMER) == "enabled"
        nxt = systemctl("show", AUTO_TIMER, "-p", "NextElapseUSecRealtime", "--value")
    except OSError:
        return False, False, ""
    return True, enabled, nxt


def gnome_software_settings():
    """GNOME Software's settings, or None when it isn't installed (a missing schema would abort)."""
    src = Gio.SettingsSchemaSource.get_default()
    schema = src and src.lookup("org.gnome.software", True)
    if schema and schema.has_key("download-updates"):
        return Gio.Settings.new_full(schema, None, None)
    return None


GS_SERVICE = "gnome-software.service"


def gs_paused():
    try:
        r = subprocess.run(["systemctl", "--user", "is-enabled", GS_SERVICE],
                           capture_output=True, text=True)
    except OSError:
        return False
    return r.stdout.strip() == "masked"


def set_gs_paused(on):
    """Mask or unmask GNOME Software's background service; returns an error message or ""."""
    verb = ["mask", "--now"] if on else ["unmask"]
    try:
        r = subprocess.run(["systemctl", "--user", *verb, GS_SERVICE],
                           capture_output=True, text=True)
    except OSError as e:
        return str(e)
    return "" if r.returncode == 0 else (r.stderr or r.stdout).strip() or f"systemctl exited with {r.returncode}"


def write_units(mode, freq):
    os.makedirs(UNIT_DIR, exist_ok=True)
    # --no-deploy only pulls the new commits; the app's Update button then deploys them
    extra = " --no-deploy" if mode == 0 else ""
    for name, text in ((AUTO_SERVICE, SERVICE_UNIT.format(exe=BIN_PATH, extra=extra)),
                       (AUTO_TIMER, TIMER_UNIT.format(freq=freq))):
        with open(os.path.join(UNIT_DIR, name), "w") as f:
            f.write(text)


def notify(title, body, action="app.open"):
    app = Gio.Application(application_id=APP_ID)
    app.register(None)
    note = Gio.Notification.new(title)
    note.set_body(body)
    note.set_default_action(action)
    app.send_notification("auto-update", note)
    conn = app.get_dbus_connection()
    if conn:
        conn.flush_sync(None)  # we exit right after, so push the message out first


def update_line(i, before, after):
    old, new = before.get(i), after.get(i)
    row = new or old
    ov, nv = old[3] if old else "", new[3] if new else ""
    name = row[2] if row and row[2] else i
    return f"{name} {ov} \u2192 {nv}" if ov and nv and ov != nv else name


GAMEMODE = "com.feralinteractive.GameMode"


def gamemode_active():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(GAMEMODE, "/com/feralinteractive/GameMode",
                              "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (GAMEMODE, "ClientCount")), None,
                              Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
    except GLib.Error:
        return False
    return reply.unpack()[0] > 0


def update_blocker():
    """Why automatic updates should wait, or "". Same checks as GNOME Software, plus GameMode."""
    if Gio.NetworkMonitor.get_default().get_network_metered():
        return "the connection is metered"
    monitor = Gio.PowerProfileMonitor.dup_default()
    if monitor and monitor.get_power_saver_enabled():
        return "power saver is on"
    if gamemode_active():
        return "GameMode is active"
    return ""


def retry_later():
    """Run the update service again in 30 minutes, unless the timer gets switched off."""
    start = f"systemctl --user is-enabled --quiet {AUTO_TIMER} && " \
            f"systemctl --user --no-block start {AUTO_SERVICE}"
    subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--on-active=30min",
                    "--unit=flatpakshelf-retry", "sh", "-c", start], capture_output=True)


def auto_update(download_only):
    why = update_blocker()
    if why:
        print(f"Automatic update skipped: {why}", file=sys.stderr)
        retry_later()
        return 0
    before = {r[1]: r for r in list_installed()}
    pending = set().union(*(list_updates(s) for s in SCOPES))
    if not pending:
        return 0
    rc, msg = flatpak_op("update", "--noninteractive", "-y", *(["--no-deploy"] if download_only else []))
    if rc:
        notify("Flatpak update failed", (msg.splitlines() or [f"flatpak exited with {rc}"])[-1])
        return 1
    ids = sorted({i for i, _a, _b in pending})
    after = before
    if download_only:
        key = [f"{i}/{a}/{b}" for i, a, b in sorted(pending)]
        try:
            seen = json.loads(read_text(STATE_FILE))
        except ValueError:
            seen = None
        if seen == key:
            return 0
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump(key, f)
        except OSError:
            pass
    else:
        after = {r[1]: r for r in list_installed()}
    lines = [update_line(i, before, after) for i in ids]
    if len(lines) > 8:
        lines[8:] = [f"\u2026 and {len(lines) - 8} more"]
    if download_only:
        notify(f"{plural(len(ids), 'Flatpak update')} ready",
               "\n".join(lines + ["Open Flatpakshelf to install them"]))
    else:
        notify(f"{plural(len(ids), 'Flatpak')} updated", "\n".join(lines), "app.changes")
    return 0


def save_automatic(now, loaded, freq):
    """Write the units and switch the timer as needed; returns an error message or ""."""
    sched = (now["mode"], now["freq"]) != (loaded["mode"], loaded["freq"])
    toggled = now["enabled"] != loaded["enabled"]
    steps = []
    try:
        if sched or (now["enabled"] and toggled):
            write_units(now["mode"], freq)
            steps.append(["daemon-reload"])
        if now["enabled"] and (sched or toggled):
            steps += [["enable", AUTO_TIMER], ["restart", AUTO_TIMER]]
        elif toggled:
            steps.append(["disable", "--now", AUTO_TIMER])
        for step in steps:
            r = subprocess.run(["systemctl", "--user", *step], capture_output=True, text=True)
            if r.returncode:
                return (r.stderr or r.stdout).strip() or f"systemctl exited with {r.returncode}"
    except OSError as e:
        return str(e)
    return ""


class HistoryWindow(Adw.Window):
    def __init__(self, parent, days=1):
        super().__init__(transient_for=parent, modal=True, title="Recent changes",
                         default_width=640, default_height=560)
        self.gen = 0
        hb = Adw.HeaderBar()
        self.range = Gtk.DropDown.new_from_strings(HISTORY_LABELS)
        self.range.set_selected(days)
        self.range.connect("notify::selected", lambda *_: self.load())
        hb.pack_start(self.range)
        self.box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                               margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
                               valign=Gtk.Align.START)
        self.empty = Gtk.Label(css_classes=["dim-label"], margin_top=48)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.empty)
        body.append(self.box)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(Gtk.ScrolledWindow(vexpand=True, child=body))
        self.set_content(tb)
        self.load()

    def load(self):
        self.gen += 1
        gen, days = self.gen, HISTORY_DAYS[self.range.get_selected()]
        self.empty.set_label("Loading\u2026")

        def work():
            GLib.idle_add(self.show, list_history(days), gen)

        threading.Thread(target=work, daemon=True).start()

    def show(self, rows, gen):
        if gen != self.gen:
            return False
        self.box.remove_all()
        self.empty.set_label("" if rows else "No changes in this period")
        for time, change, app, branch, scope, origin in rows[:HISTORY_LIMIT]:
            detail = " \u00b7 ".join(x for x in (time, change, branch, scope, origin) if x)
            self.box.append(Adw.ActionRow(title=GLib.markup_escape_text(app),
                                          subtitle=GLib.markup_escape_text(detail)))
        if len(rows) > HISTORY_LIMIT:
            self.box.append(Adw.ActionRow(title=f"\u2026 and {len(rows) - HISTORY_LIMIT} older entries"))
        return False


class VersionWindow(Adw.Window):
    """Install an older commit of one package, optionally pausing its updates afterwards."""
    LIMIT = 30

    def __init__(self, parent, p):
        super().__init__(transient_for=parent, modal=True, title=f"Change version of {p.name}",
                         default_width=560, default_height=520)
        self.parent_win, self.p = parent, p
        self.keep = Adw.SwitchRow(title="Keep this version", active=True,
                                  subtitle="Pause updates afterwards, otherwise the next update undoes it")
        top = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        top.append(self.keep)
        self.empty = Gtk.Label(label="Loading\u2026", css_classes=["dim-label"], margin_top=24)
        self.box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                               valign=Gtk.Align.START)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                       margin_bottom=12, margin_start=12, margin_end=12)
        for w in (top, self.empty, self.box):
            body.append(w)
        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        tb.set_content(Gtk.ScrolledWindow(vexpand=True, child=body))
        self.set_content(tb)
        threading.Thread(target=lambda: GLib.idle_add(self.show, parse_versions(commit_log(p))),
                         daemon=True).start()

    def show(self, rows):
        self.empty.set_label("" if rows else "No older versions found")
        for commit, subject, date in rows[:self.LIMIT]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(subject or "(no subject)"),
                                subtitle=GLib.markup_escape_text(f"{date} \u00b7 {commit[:12]}"))
            btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b, c=commit: self.pick(c))
            row.add_suffix(btn)
            self.box.append(row)
        return False

    def pick(self, commit):
        p = self.p
        cmds = [Tx("update", p.scope, [p.ref], commit=commit)]
        if self.keep.get_active() and not p.paused:
            cmds.append(["mask", *scope_args(p.scope), p.id])  # after the update: masked refs are skipped
        self.close()
        self.parent_win.run_batch(cmds, f"{p.name} set to {commit[:12]}")


class AutoUpdatesWindow(Adw.Window):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Automatic updates",
                         default_width=560, default_height=520)
        self.parent_win = parent
        have, enabled, nxt = timer_state()
        cfg = automatic_settings()
        self.gs = gnome_software_settings()
        self.freqs = list(AUTO_FREQS) + [v for v in [cfg["freq"]] if v not in AUTO_FREQS]
        self.loaded = {"enabled": enabled, "mode": cfg["mode"],
                       "gnome": bool(self.gs and self.gs.get_boolean("download-updates")),
                       "gs_paused": bool(self.gs and gs_paused()),
                       "freq": self.freqs.index(cfg["freq"])}

        hb = Adw.HeaderBar()
        self.apply_btn = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        self.apply_btn.connect("clicked", lambda *_: self.apply())
        hb.pack_end(self.apply_btn)

        page = Adw.PreferencesPage()
        if not have:
            grp = Adw.PreferencesGroup()
            grp.add(Adw.ActionRow(title="systemd is not available", subtitle="systemctl not found"))
            page.add(grp)

        self.sw = Adw.SwitchRow(title="Run automatically", subtitle=AUTO_TIMER, active=enabled)
        self.sw.connect("notify::active", lambda *_: self.refresh())
        when = nxt if enabled and nxt not in ("", "n/a") else "Not scheduled"
        sched = Adw.PreferencesGroup(
            title="Schedule",
            description=f"Runs while you are logged in. Units are saved to {UNIT_DIR}")
        sched.add(self.sw)
        self.freq_row = self.combo("Check", [AUTO_FREQS.get(v, f"{v} (current config)")
                                             for v in self.freqs], self.loaded["freq"])
        sched.add(self.freq_row)
        sched.add(Adw.ActionRow(title="Next run", subtitle=GLib.markup_escape_text(when)))

        self.mode_row = self.combo("Mode", AUTO_MODE_LABELS, self.loaded["mode"])
        what = Adw.PreferencesGroup(title="Updates")
        what.add(self.mode_row)

        for grp in (sched, what):
            grp.set_sensitive(have)
            page.add(grp)
        if self.gs:
            self.gs_row = Adw.SwitchRow(title="Download updates automatically",
                                        subtitle="GNOME Software", active=self.loaded["gnome"])
            self.gs_row.connect("notify::active", lambda *_: self.refresh())
            self.gs_group = Adw.PreferencesGroup(title="GNOME Software", description=GS_NOTE)
            self.gs_group.add(self.gs_row)
            self.gs_pause = Adw.SwitchRow(title="Pause background activity",
                                          subtitle="Stops its background refresh and its own notifications",
                                          active=self.loaded["gs_paused"])
            self.gs_pause.connect("notify::active", lambda *_: self.refresh())
            self.gs_group.add(self.gs_pause)
            page.add(self.gs_group)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(page)
        self.set_content(tb)
        self.refresh()

    def combo(self, title, labels, selected):
        row = Adw.ComboRow(title=title)
        row.set_model(Gtk.StringList.new(labels))
        row.set_selected(selected)
        row.connect("notify::selected", lambda *_: self.refresh())
        return row

    def current(self):
        return {"enabled": self.sw.get_active(), "mode": self.mode_row.get_selected(),
                "freq": self.freq_row.get_selected(),
                "gnome": bool(self.gs and self.gs_row.get_active()),
                "gs_paused": bool(self.gs and self.gs_pause.get_active())}

    def refresh(self):
        now = self.current()
        self.mode_row.set_subtitle(AUTO_MODE_HINTS[now["mode"]])
        if self.gs:
            self.gs_group.set_description(GS_BOTH if now["enabled"] and now["gnome"] else GS_NOTE)
        self.apply_btn.set_sensitive(now != self.loaded)

    def apply(self):
        now = self.current()
        if self.gs and now["gnome"] != self.loaded["gnome"]:
            self.gs.set_boolean("download-updates", now["gnome"])
        if self.gs and now["gs_paused"] != self.loaded["gs_paused"]:
            err = set_gs_paused(now["gs_paused"])
            if err:
                self.close()
                self.parent_win.toast(f"Failed: {err}")
                return
        loaded, freq, win = self.loaded, self.freqs[now["freq"]], self.parent_win
        self.close()
        if now["enabled"] and not os.path.exists(BIN_PATH):
            try:
                install_self()
            except OSError as e:
                win.toast(f"Failed: {e}")
                return
            win.refresh_entry_label()

        def work():
            err = save_automatic(now, loaded, freq)
            GLib.idle_add(win.toast, f"Failed: {err}" if err else "Automatic updates saved")

        threading.Thread(target=work, daemon=True).start()


class RemotesWindow(Adw.Window):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Remotes",
                         default_width=640, default_height=520)
        self.parent_win, self.changed = parent, False
        self.placeholder = Adw.StatusPage(title="Loading\u2026", css_classes=["compact"])
        self.box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                               margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
                               valign=Gtk.Align.START)
        self.box.set_placeholder(self.placeholder)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a remote")
        add.connect("clicked", lambda *_: self.ask_add())
        self.flathub_btn = Gtk.Button(label="Add Flathub", visible=False)
        self.flathub_btn.connect("clicked", lambda *_: self.ask_flathub())
        hb = Adw.HeaderBar()
        hb.pack_start(add)
        hb.pack_start(self.flathub_btn)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(Gtk.ScrolledWindow(vexpand=True, child=self.box))
        self.set_content(tb)
        self.connect("close-request", self._closing)
        self.load()

    def _closing(self, *_):
        if self.changed:
            self.parent_win.reload()
        return False

    def load(self):
        threading.Thread(target=lambda: GLib.idle_add(self.show, list_remotes()),
                         daemon=True).start()

    def show(self, rows):
        self.box.remove_all()
        self.placeholder.set_title("No remotes configured")
        self.flathub_btn.set_visible(not any(r[1] == "flathub" for r in rows))
        for scope, name, title, url, enabled in rows:
            row = Adw.SwitchRow(title=name, active=enabled, use_markup=False,
                                subtitle=" \u00b7 ".join(x for x in (title, scope, url) if x))
            row.connect("notify::active", lambda r, _p, s=scope, n=name: self.run(
                ("remote-modify", "--enable" if r.get_active() else "--disable", "--" + s, n),
                f"Could not change {n}"))
            trash = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                               css_classes=["flat"], tooltip_text="Remove remote")
            trash.connect("clicked", lambda _b, s=scope, n=name: self.ask_remove(s, n))
            row.add_suffix(trash)
            self.box.append(row)
        return False

    def run(self, args, failure):
        self.set_sensitive(False)

        def work():
            rc, msg = flatpak_op(*args)
            GLib.idle_add(self.done, rc, msg, failure)

        threading.Thread(target=work, daemon=True).start()

    def done(self, rc, msg, failure):
        self.set_sensitive(True)
        if rc:
            dlg = Adw.AlertDialog(heading=failure, body=msg or "flatpak reported an error.")
            dlg.add_response("ok", "OK")
            dlg.present(self)
        else:
            self.changed = True
        self.load()
        return False

    def ask_remove(self, scope, name):
        used = [p for p in self.parent_win.all if p.installed and p.origin == name and p.scope == scope]
        body = ("Nothing is installed from it." if not used else
                f"{plural(len(used))} installed from it will be removed too:\n{names_summary(used)}")
        dlg = Adw.AlertDialog(heading=f"Remove {name}?", body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", f"Remove with {plural(len(used))}" if used else "Remove")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "remove" and self.run(
            ("remote-delete", "--" + scope, *(["--force"] if used else []), name),
            f"Could not remove {name}"))
        dlg.present(self)

    def ask_flathub(self):
        drop = Gtk.DropDown.new_from_strings(["For this user only",
                                              "System-wide (asks for authorization)"])
        dlg = Adw.AlertDialog(heading="Add Flathub?", body="The main Flatpak app store.")
        dlg.set_extra_child(drop)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("add", "Add")
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("add")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "add" and self.run(
            ("remote-add", "--" + ("user", "system")[drop.get_selected()], "flathub", FLATHUB_REPO),
            "Could not add Flathub"))
        dlg.present(self)

    def ask_add(self, location=""):
        name = Adw.EntryRow(title="Name")
        where = Adw.EntryRow(title="URL or .flatpakrepo file")
        scope = Adw.ComboRow(title="Installation", subtitle="System asks for your password",
                             model=Gtk.StringList.new(["User", "System"]))
        form = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for w in (where, name, scope):
            form.append(w)
        hint = Gtk.Label(xalign=0, wrap=True, css_classes=["caption", "error"], visible=False,
                         label="Use a URL such as https://\u2026 or an existing .flatpakrepo file")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(form)
        box.append(hint)
        dlg = Adw.AlertDialog(heading="Add a remote")
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("add", "Add")
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_response_enabled("add", False)
        dlg.set_close_response("cancel")

        guessed = [""]

        def check(*_):
            loc = where.get_text().strip()
            ok = valid_remote_location(loc)
            bad = bool(loc) and not ok
            hint.set_visible(bad)
            (where.add_css_class if bad else where.remove_css_class)("error")
            dlg.set_response_enabled("add", bool(ok and name.get_text().strip()))

        def fill_name(*_):
            guess = default_remote_name(where.get_text().strip())
            if guess and name.get_text().strip() in ("", guessed[0]):  # leave a name the user typed
                guessed[0] = guess
                name.set_text(guess)
            check()

        def picked(fd, res):
            try:
                path = fd.open_finish(res).get_path()
            except GLib.Error:
                return
            where.set_text(path)

        def choose(*_):
            only = Gtk.FileFilter(name="Flatpak repositories")
            only.add_pattern("*.flatpakrepo")
            Gtk.FileDialog(default_filter=only).open(self, None, picked)

        pick = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                          css_classes=["flat"], tooltip_text="Choose a .flatpakrepo file")
        pick.connect("clicked", choose)
        where.add_suffix(pick)

        def respond(_d, r):
            if r == "add":
                self.run(("remote-add", "--" + ("user", "system")[scope.get_selected()],
                          name.get_text().strip(), where.get_text().strip()),
                         "Could not add the remote")

        name.connect("changed", check)
        where.connect("changed", fill_name)
        dlg.connect("response", respond)
        where.set_text(location)
        dlg.present(self)


# ---------------------------------------------------------------- details pane
class DetailsMixin:
    """Details tabs, permissions and screenshots for Win."""

    def _start_info(self, p, token):
        self.info_timer = 0
        self.load_tab(p, token, self.notebook.get_current_page())
        return False

    def on_tab_switch(self, _nb, _page, num):
        if self.cur is not None:
            self.info_token += 1
            self.load_tab(self.cur, self.info_token, num)

    def load_tab(self, p, token, idx):
        key = (p.ref, p.scope, p.origin, p.installed, idx)
        if key in self.detail_cache:
            self.show_tab(idx, self.detail_cache[key])
            return
        self.show_tab(idx, "Loading…")

        def work():
            try:
                if idx == SHOT_TAB:
                    text = (screenshot_list(p) if p.kind == "app" else []) or TAB_EMPTY[idx]
                else:
                    text = detail_text(p, idx)
            except Exception as e:
                text = f"Error: {e}"
            GLib.idle_add(self.set_detail, token, idx, key, text)

        threading.Thread(target=work, daemon=True).start()

    def set_detail(self, token, idx, key, text):
        if len(self.detail_cache) > 400:
            self.detail_cache.clear()
        self.detail_cache[key] = text
        if token == self.info_token:
            self.show_tab(idx, text)
        return False

    def detail_row(self, title, sub="", warn=False, prop=False, changed=False, switch=None, p=None):
        row = Adw.ActionRow(title=title, subtitle=sub, use_markup=False, title_lines=0,
                            subtitle_lines=0, subtitle_selectable=True)
        if prop:  # small dimmed key above a larger value
            row.add_css_class("property")
        if changed:
            tag = Gtk.Label(label="Overridden", valign=Gtk.Align.CENTER,
                            tooltip_text="Changed by a user override")
            tag.add_css_class("accent")
            tag.add_css_class("caption")
            row.add_suffix(tag)
        if switch and p:
            key, name, granted = switch
            sw = Gtk.Switch(active=granted, valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda w, _p: self.set_permission(w, p, key, name))
            row.add_suffix(sw)
        if warn:
            icon = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text="Broad access")
            icon.add_css_class("warning")
            row.add_suffix(icon)
        return row

    def set_permission(self, sw, p, key, name):
        flag = PERM_FLAGS[key][0 if sw.get_active() else 1]
        sw.set_sensitive(False)
        self.run_override(p, f"--{flag}={name}")

    def reset_overrides(self, p):
        dlg = Adw.AlertDialog(heading="Reset permission changes?",
                              body=f"Remove every permission change you made for {p.name}.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("reset", "Reset")
        dlg.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "reset" and self.run_override(p, "--reset"))
        dlg.present(self)

    def run_override(self, p, flag):
        def work():
            rc, _out = flatpak("override", "--user", flag, p.id)
            GLib.idle_add(self.override_done, p, rc)

        threading.Thread(target=work, daemon=True).start()

    def override_done(self, p, rc):
        if rc:
            self.toast("Could not change the permission")
        for k in [k for k in self.detail_cache if k[0] == p.ref and k[4] == 1]:
            del self.detail_cache[k]
        if self.cur is not None and self.cur.ref == p.ref:  # reload so the rows match reality
            self.info_token += 1
            self.load_tab(p, self.info_token, 1)
        return False

    def info_icon(self, p):
        path = icon_file(p, (128, 64)) if p else ""
        tex = texture_from(path) if path else None
        return Gtk.Image(paintable=tex, pixel_size=64, valign=Gtk.Align.START) if tex else None

    def show_shots(self, data):
        view = self.tab_views[SHOT_TAB]
        while child := view.get_first_child():
            view.remove(child)
        self.shot_gen += 1
        if isinstance(data, str):
            note = Gtk.Label(label=data, wrap=True, xalign=0, selectable=True)
            note.add_css_class("dim-label")
            view.append(note)
            return
        strip = Gtk.Box(spacing=12)
        view.append(strip)
        for caption, url in data:
            slot = Gtk.Button(halign=Gtk.Align.START, valign=Gtk.Align.START)
            slot.add_css_class("flat")
            if caption:
                slot.set_tooltip_text(caption)
            hold = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            hold.set_size_request(SHOT_HEIGHT * 16 // 9, SHOT_HEIGHT)
            hold.append(Gtk.Spinner(spinning=True, hexpand=True, vexpand=True,
                                    halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                    width_request=24, height_request=24))
            slot.set_child(hold)
            strip.append(slot)
            if os.path.exists(media_path(url)):
                self.shot_ready(self.shot_gen, slot, caption, media_path(url))
            else:
                threading.Thread(target=self._shot_work, args=(self.shot_gen, slot, caption, url),
                                 daemon=True).start()

    def _shot_work(self, gen, slot, caption, url):
        GLib.idle_add(self.shot_ready, gen, slot, caption, fetch_media(url))

    def shot_ready(self, gen, slot, caption, path):
        if gen != self.shot_gen:  # selection changed meanwhile
            return False
        tex = texture_from(path) if path else None
        if tex is None:
            if path:  # not an image: drop it so a later look retries
                _TEXTURES.pop(path, None)
                try:
                    os.remove(path)
                except OSError:
                    pass
            hold = Gtk.Image(icon_name="image-missing-symbolic", pixel_size=48,
                             tooltip_text="Could not load this screenshot")
            hold.set_size_request(SHOT_HEIGHT * 16 // 9, SHOT_HEIGHT)
            slot.set_child(hold)
            slot.set_sensitive(False)
            return False
        pic = Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        pic.set_size_request(max(1, round(tex.get_width() * SHOT_HEIGHT / tex.get_height())),
                             SHOT_HEIGHT)
        slot.set_child(pic)
        slot.connect("clicked", lambda *_: self.open_shot(path, caption))
        return False

    def open_shot(self, path, caption):
        tex = texture_from(path)
        if tex is None:
            return
        name = self.cur.name if self.cur else "Screenshot"
        dlg = Adw.Dialog(title=f"{name}: {caption}" if caption else name,
                         content_width=min(tex.get_width(), 1100),
                         content_height=min(tex.get_height() + 48, 800))
        body = Adw.ToolbarView()
        body.add_top_bar(Adw.HeaderBar())
        body.set_content(Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.CONTAIN,
                                     can_shrink=True))
        dlg.set_child(body)
        dlg.present(self)

    def show_tab(self, idx, text):
        view = self.tab_views[idx]
        if idx == SHOT_TAB:
            self.show_shots(text)
            return
        while child := view.get_first_child():
            view.remove(child)
        parsed = (parse_info, parse_permissions, parse_metadata, parse_changes)[idx](text)
        p = self.cur
        editable = p if p and p.installed and p.kind == "app" else None
        if parsed is None:  # status message such as "Loading…" or "No special permissions"
            note = Gtk.Label(label=text, wrap=True, xalign=0, selectable=True)
            note.add_css_class("dim-label")
            view.append(note)
        elif idx == 0:
            title, rows = parsed
            icon = self.info_icon(p)
            if title or icon:
                top = Gtk.Box(spacing=12)
                if icon:
                    top.append(icon)
                if title:
                    head = Gtk.Label(label=title, wrap=True, xalign=0, selectable=True,
                                     valign=Gtk.Align.CENTER)
                    head.add_css_class("title-4")
                    top.append(head)
                view.append(top)
            group = Adw.PreferencesGroup()
            for key, value in rows:
                group.add(self.detail_row(key, value, prop=True))
            view.append(group)
        elif idx == 2:
            for title, rows in parsed:
                group = Adw.PreferencesGroup(title=title)
                for key, value in rows:
                    group.add(self.detail_row(key, value, prop=True))
                view.append(group)
        elif idx == 3:
            group = Adw.PreferencesGroup()
            for head, body in parsed:
                group.add(self.detail_row(head, body))
            view.append(group)
        else:
            for title, rows in parsed:
                group = Adw.PreferencesGroup(title=title)
                for t, sub, warn, ctl, changed in rows:
                    group.add(self.detail_row(t, sub, warn, changed=changed, switch=ctl, p=editable))
                view.append(group)
            if editable and any(r[4] for _t, rows in parsed for r in rows):
                reset = Gtk.Button(label="Reset overrides", halign=Gtk.Align.START,
                                   tooltip_text="Remove all your permission changes for this app")
                reset.connect("clicked", lambda *_: self.reset_overrides(editable))
                view.append(reset)


# ---------------------------------------------------------------- main window
class Win(DetailsMixin, Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Flatpakshelf",
                         default_width=1150, default_height=720)
        self.model = PkgModel()
        self.all = []          # every package, kept sorted
        self.query = ""
        self.gen = 0           # load generation, so stale loads are ignored
        self.sort_prop, self.sort_desc = "name", False
        self.col_props = {}
        self.cur = None        # package shown in the details pane
        self.info_token = 0
        self.info_timer = 0
        self.shot_gen = 0      # drops late downloads of a previous selection
        self.detail_cache = {}
        self.busy = False      # an install/uninstall is running
        self.n_upg = 0
        self.data_sizes = {}
        self.scan_id = 0
        self.scanning = False

        hb = Adw.HeaderBar()
        self.mode = Gtk.DropDown.new_from_strings(MODES)
        self.mode.set_selected(MODES.index("Installed"))
        self.mode.connect("notify::selected", lambda *_: self.refilter())
        hb.pack_start(self.mode)
        self.origin = Gtk.DropDown.new_from_strings([ALL_REMOTES])
        self.origin.set_visible(False)
        self.origin.connect("notify::selected", lambda *_: self.on_origin())
        self.origin_name, self.syncing_origin = "", False
        hb.pack_start(self.origin)
        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                     tooltip_text="Reload package list from the remotes")
        self.reload_btn.connect("clicked", lambda *_: self.reload())
        hb.pack_start(self.reload_btn)
        self.spin = Gtk.Spinner()
        hb.pack_start(self.spin)
        menu = Gio.Menu()
        menu.append("Remove unused runtimes", "win.remove-unused")
        menu.append("Remove leftover app data", "win.remove-leftovers")
        menu.append("Repair installation", "win.repair")
        menu.append("Automatic updates", "win.auto-updates")
        menu.append("Remotes", "win.remotes")
        menu.append("Recent changes", "win.history")
        self.app_menu = menu
        menu.append(self.entry_label(), "win.toggle-menu-entry")
        menu.append("About Flatpakshelf", "win.about")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        act = Gio.SimpleAction.new("remove-unused", None)
        act.connect("activate", lambda *_: self.remove_unused())
        self.add_action(act)
        act = Gio.SimpleAction.new("remove-leftovers", None)
        act.connect("activate", lambda *_: self.remove_leftovers())
        self.add_action(act)
        act = Gio.SimpleAction.new("repair", None)
        act.connect("activate", lambda *_: self.ask_repair())
        self.add_action(act)
        act = Gio.SimpleAction.new("history", None)
        act.connect("activate", lambda *_: HistoryWindow(self).present())
        self.add_action(act)
        act = Gio.SimpleAction.new("auto-updates", None)
        act.connect("activate", lambda *_: AutoUpdatesWindow(self).present())
        self.add_action(act)
        act = Gio.SimpleAction.new("remotes", None)
        act.connect("activate", lambda *_: RemotesWindow(self).present())
        self.add_action(act)
        act = Gio.SimpleAction.new("about", None)
        act.connect("activate", lambda *_: self.show_about())
        self.add_action(act)
        act = Gio.SimpleAction.new("toggle-menu-entry", None)
        act.connect("activate", lambda *_: self.toggle_menu_entry())
        self.add_action(act)
        self.btn_remove = Gtk.Button(label="Uninstall", sensitive=False,
                                     tooltip_text="Uninstall the selected installed packages")
        self.btn_remove.add_css_class("destructive-action")
        self.btn_remove.connect("clicked", lambda *_: self.uninstall_selected())
        self.btn_install = Gtk.Button(label="Install", sensitive=False,
                                      tooltip_text="Install the selected packages")
        self.btn_install.add_css_class("suggested-action")
        self.btn_install.connect("clicked", lambda *_: self.install_selected())
        self.btn_launch = Gtk.Button(label="Launch", sensitive=False,
                                     tooltip_text="Run the selected application")
        self.btn_launch.connect("clicked", lambda *_: self.launch_selected())
        self.btn_folder = Gtk.Button(label="Data folder", sensitive=False,
                                     tooltip_text="Open the selected app's folder in ~/.var/app")
        self.btn_folder.connect("clicked", lambda *_: self.open_data_folder())
        self.btn_update = Gtk.Button(label="Update", sensitive=False,
                                     tooltip_text="Update the selected packages")
        self.btn_update.connect("clicked", lambda *_: self.update_selected())
        self.btn_pause = Gtk.Button(label="Pause updates", sensitive=False,
                                    tooltip_text="Stop updating the selected package (flatpak mask)")
        self.btn_pause.connect("clicked", lambda *_: self.toggle_pause())
        self.btn_version = Gtk.Button(label="Change version\u2026", sensitive=False,
                                      tooltip_text="Install an older version of the selected package")
        self.btn_version.connect("clicked", lambda *_: self.change_version())
        self.search = Gtk.SearchEntry(placeholder_text=f"Search \u00b7 {SEARCH_MODES[0]}",
                                      width_request=420)
        self.search.connect("search-changed", self.on_search)
        self.search.get_delegate().set_margin_end(32)  # room for the mode button
        mode_menu = Gio.Menu()
        for i, label in enumerate(SEARCH_MODES):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("win.search-mode", GLib.Variant.new_string(str(i)))
            mode_menu.append_item(item)
        mode_btn = Gtk.MenuButton(icon_name="pan-down-symbolic", menu_model=mode_menu,
                                  tooltip_text="What the search matches", halign=Gtk.Align.END,
                                  valign=Gtk.Align.CENTER, margin_end=32,
                                  css_classes=["flat", "circular"])
        overlay = Gtk.Overlay(child=self.search)
        overlay.add_overlay(mode_btn)
        hb.set_title_widget(overlay)
        self.search_idx = 0
        mode_act = Gio.SimpleAction.new_stateful("search-mode", GLib.VariantType.new("s"),
                                                 GLib.Variant.new_string("0"))
        mode_act.connect("activate", self.on_search_mode)
        self.add_action(mode_act)

        self.view = Gtk.ColumnView(show_row_separators=True)
        self.add_col("Status", "status", width=100)
        name_col = self.add_col("Name", "name", width=270, icon=True)
        self.add_col("Application ID", "id", width=280)
        self.add_col("Version", "version", width=120)
        self.add_col("Size", "size", width=90)
        self.add_col("User data", "data", width=100)
        self.add_col("Branch", "branch", width=90)
        self.add_col("Origin", "origin", width=100)
        self.add_col("Scope", "scope", width=80)
        self.add_col("Summary", "summary", expand=True)
        self.selection = Gtk.MultiSelection(model=self.model)
        self.selection.connect("selection-changed", self.on_select)
        self.view.set_model(self.selection)
        # Header clicks only update the view's sorter; we sort our own list when it changes.
        self.view.get_sorter().connect("changed", self.on_sort_changed)

        self.tab_views = []
        self.notebook = Gtk.Notebook(vexpand=True)
        for title in TABS:
            tv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                         margin_bottom=12, margin_start=12, margin_end=12)
            self.tab_views.append(tv)
            self.notebook.append_page(
                Gtk.ScrolledWindow(vexpand=True, min_content_height=170, child=tv),
                Gtk.Label(label=title))
        self.notebook.connect("switch-page", self.on_tab_switch)
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, position=400,
                          start_child=Gtk.ScrolledWindow(vexpand=True, child=self.view),
                          end_child=self.notebook, shrink_start_child=False,
                          shrink_end_child=False)

        self.count = Gtk.Label(label="", xalign=1, hexpand=True)
        self.progress = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER,
                                        width_request=260, visible=False)
        self.prog = (None, "")
        self.prog_timer = 0
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12,
                      margin_end=12)
        for w in (self.btn_install, self.btn_update, self.btn_launch, self.btn_folder,
                  self.btn_pause, self.btn_version, self.btn_remove, self.progress, self.count):
            bar.append(w)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(paned)
        body.append(bar)
        tb = Adw.ToolbarView()
        tb.add_top_bar(hb)
        tb.set_content(body)
        self.overlay = Adw.ToastOverlay(child=tb)
        self.set_content(self.overlay)
        self.view.sort_by_column(name_col, Gtk.SortType.ASCENDING)  # last: needs self.count

    def add_col(self, title, prop, width=None, expand=False, icon=False):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                            single_line_mode=True, max_width_chars=1,
                            margin_start=6, margin_end=6)
            if not icon:
                li.set_child(lbl)
                return
            box = Gtk.Box(spacing=4, margin_start=4)
            box.append(Gtk.Image(pixel_size=ICON_PX))
            box.append(lbl)
            li.set_child(box)

        def bind(_f, li):
            p = li.get_item()
            text = getattr(p, prop)
            child = li.get_child()
            lbl = child.get_last_child() if icon else child
            if lbl.get_label() != text:
                lbl.set_label(text)
            if icon:
                img, tex = child.get_first_child(), pkg_icon(p)
                if tex:
                    img.set_from_paintable(tex)
                else:  # many runtimes ship no icon
                    img.set_from_icon_name("package-x-generic-symbolic")
                img.set_opacity(1.0 if tex else 0.45)

        f.connect("setup", setup)
        f.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=f, resizable=True, expand=expand)
        if width:
            col.set_fixed_width(width)
        col.set_sorter(Gtk.CustomSorter.new(lambda a, b, *_: Gtk.Ordering.EQUAL))  # marker only
        self.col_props[title] = prop
        self.view.append_column(col)
        return col

    def toast(self, msg):
        self.overlay.add_toast(Adw.Toast.new(msg))

    # ---- loading
    def start(self):
        """Show the cached list immediately, then refresh it in the background."""
        self.data_sizes = read_data_cache()
        self._begin(True)

    def reload(self):
        self._begin(False)

    def _begin(self, first, quick=False):
        """quick: re-read local metadata only, without showing the cached list first."""
        self.gen += 1
        if not quick:
            self.scan_data()
        self.spin.start()
        threading.Thread(target=self._worker, args=(self.gen, first, quick),
                         daemon=True).start()

    def scan_data(self):
        self.scan_id += 1
        self.scanning = True
        if self.all:
            self.apply_data()
            self.refilter()
        sid, found, done = self.scan_id, {}, threading.Event()

        def work():
            user_data_sizes(found)
            done.set()
            GLib.idle_add(self._data_ready, sid, found, True)

        def poll():  # show partial results while the slow folders are still being measured
            if sid != self.scan_id or done.is_set():
                return False
            self._data_ready(sid, found, False)
            return True

        threading.Thread(target=work, daemon=True).start()
        GLib.timeout_add_seconds(2, poll)

    def apply_data(self):
        for p in self.all:
            p.data_bytes = self.data_sizes.get(p.id, 0) if p.installed else 0
            pending = self.scanning and p.installed and p.kind == "app" and p.id not in self.data_sizes
            p.data = GLib.format_size(p.data_bytes) if p.data_bytes else "…" if pending else ""

    def _data_ready(self, sid, found, final):
        if sid != self.scan_id:
            return False
        sizes = dict(found) if final else {**self.data_sizes, **found}
        if final:
            self.scanning = False
        if final or sizes != self.data_sizes:
            self.data_sizes = sizes
            self.apply_data()
            self.sort_all()
            self.refilter()
        if final:
            write_data_cache(sizes)
        return False

    def spin_off(self):
        if not self.busy:  # an install/uninstall keeps the spinner running
            self.spin.stop()

    def _worker(self, gen, first, quick=False):
        try:
            cached = read_cache() if first or quick else None
            if cached and not quick:
                GLib.idle_add(self._loaded, [Pkg(d) for d in cached], gen, False)
            raw = collect_raw(cached=first or quick)  # startup: local metadata; reload: network
            if cached is not None and raw == cached:
                GLib.idle_add(self._unchanged, gen)  # nothing new: leave the list alone
                return
            write_cache(raw)
            GLib.idle_add(self._loaded, [Pkg(d) for d in raw], gen, True)
        except Exception as e:
            GLib.idle_add(self._load_failed, str(e), gen)

    def _unchanged(self, gen):
        if gen == self.gen:
            self.spin_off()
        return False

    def _loaded(self, objs, gen, final):
        if gen != self.gen:
            return False
        self.all = objs
        self.update_origins(objs)
        self.n_upg = sum(p.upgradable for p in objs)
        self.apply_data()
        self.sort_all()
        self.refilter()
        self.update_buttons(self.selected())
        if final:
            self.detail_cache.clear()
            self.spin_off()
        return False

    def _load_failed(self, msg, gen):
        if gen == self.gen:
            self.spin_off()
            self.toast(f"Failed to load packages: {msg}")
        return False

    # ---- sorting / filtering (plain Python on plain lists)
    def on_sort_changed(self, sorter, _change):
        col = sorter.get_primary_sort_column()
        if col is None:
            return
        self.sort_prop = self.col_props[col.get_title()]
        self.sort_desc = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        self.sort_all()
        self.refilter()

    def sort_all(self):
        num = {"size": "size_bytes", "data": "data_bytes"}.get(self.sort_prop)
        if num:
            self.all.sort(key=operator.attrgetter(num), reverse=self.sort_desc)
            return
        get = operator.attrgetter(self.sort_prop)
        self.all.sort(key=lambda p: get(p).casefold(), reverse=self.sort_desc)

    def update_origins(self, objs):
        names = sorted({p.origin for p in objs if p.origin})
        labels = [ALL_REMOTES, *names]
        self.syncing_origin = True
        self.origin.set_model(Gtk.StringList.new(labels))
        if self.origin_name not in names:
            self.origin_name = ""
        self.origin.set_selected(labels.index(self.origin_name or ALL_REMOTES))
        self.origin.set_visible(len(names) > 1)
        self.syncing_origin = False

    def on_origin(self):
        if self.syncing_origin:
            return
        item = self.origin.get_selected_item()
        self.origin_name = "" if self.origin.get_selected() == 0 or item is None else item.get_string()
        self.refilter()

    def refilter(self):
        rows = filter_rows(self.all, self.mode.get_selected(), self.query, self.search_idx,
                           self.origin_name)
        if rows is self.all:
            rows = list(rows)
        self.model.set_rows(rows)
        text = plural(len(rows))
        if self.n_upg:
            text += f", {self.n_upg} upgradable"
        self.count.set_text(text + ", measuring user data…" if self.scanning else text)

    def on_search_mode(self, act, value):
        act.set_state(value)
        self.search_idx = int(value.get_string())
        self.search.set_placeholder_text(f"Search \u00b7 {SEARCH_MODES[self.search_idx]}")
        self.refilter()

    def on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self.refilter()

    # ---- details pane
    def selected(self):
        bs = self.selection.get_selection()
        rows = self.model.rows
        return [rows[bs.get_nth(i)] for i in range(bs.get_size())]

    def update_buttons(self, sel):
        self.btn_install.set_sensitive(not self.busy and any(not p.installed for p in sel))
        self.btn_remove.set_sensitive(not self.busy and any(p.installed for p in sel))
        one = sel[0] if len(sel) == 1 else None
        self.btn_launch.set_sensitive(bool(one and one.installed and one.kind == "app"))
        self.btn_folder.set_sensitive(bool(one and one.installed and os.path.isdir(data_dir(one))))
        can_edit = not self.busy and bool(one and one.installed)
        self.btn_pause.set_sensitive(can_edit)
        self.btn_pause.set_label("Resume updates" if one and one.paused else "Pause updates")
        self.btn_version.set_sensitive(can_edit)
        picked = any(p.upgradable for p in sel)
        self.btn_update.set_label("Update" if picked or not self.n_upg else "Update all")
        self.btn_update.set_sensitive(not self.busy and (picked or self.n_upg > 0))

    def set_busy(self, busy):
        self.busy = busy
        self.update_buttons(self.selected())

    def on_select(self, *_):
        sel = self.selected()
        self.update_buttons(sel)
        self.info_token += 1
        if self.info_timer:
            GLib.source_remove(self.info_timer)
            self.info_timer = 0
        self.cur = sel[0] if sel else None
        if self.cur is None:
            for i in range(len(TABS)):
                self.show_tab(i, "Select a package to see its details.")
            return
        # Debounce: holding an arrow key would otherwise spawn one flatpak process per row.
        self.info_timer = GLib.timeout_add(250, self._start_info, self.cur, self.info_token)

    # ---- install / uninstall
    def install_selected(self):
        pkgs = [p for p in self.selected() if not p.installed]
        if not pkgs:
            return

        def work():  # which installations have these remotes? (two quick flatpak calls)
            names = {s: remote_names(s) for s in ("user", "system")}
            GLib.idle_add(self.ask_install, pkgs, names)

        threading.Thread(target=work, daemon=True).start()

    def ask_install(self, pkgs, names):
        scopes = [s for s in ("user", "system") if all(p.origin in names[s] for p in pkgs)]
        if not scopes:
            self.toast("These packages come from remotes in different installations; "
                       "select them separately.")
            return False
        labels = {"user": "For this user only", "system": "System-wide (asks for authorization)"}
        drop = Gtk.DropDown.new_from_strings([labels[s] for s in scopes])
        dlg = Adw.AlertDialog(heading=f"Install {plural(len(pkgs))}?",
                              body=names_summary(pkgs))
        if len(scopes) > 1:
            dlg.set_extra_child(drop)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("install", "Install")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("install")
        dlg.set_close_response("cancel")

        def done(_d, resp):
            if resp == "install":
                self.run_batch(install_commands(pkgs, scopes[drop.get_selected()]),
                               f"Installed {plural(len(pkgs))}")

        dlg.connect("response", done)
        dlg.present(self)
        return False

    def uninstall_selected(self):
        pkgs = [p for p in self.selected() if p.installed]
        if not pkgs:
            return
        check = Gtk.CheckButton(label="Also delete app data (~/.var/app)")
        dlg = Adw.AlertDialog(heading=f"Uninstall {plural(len(pkgs))}?",
                              body=names_summary(pkgs) + "\n\nDeleted app data cannot be recovered.")
        dlg.set_extra_child(check)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Uninstall")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def done(_d, resp):
            if resp == "remove":
                self.run_batch(uninstall_commands(pkgs, check.get_active()),
                               f"Uninstalled {plural(len(pkgs))}")

        dlg.connect("response", done)
        dlg.present(self)

    def remove_unused(self):
        if self.busy:
            return
        self.spin.start()

        def work():
            found = []
            for s in SCOPES:
                _rc, out = flatpak("uninstall", "--unused", s, answer="n\n")
                found += [(i, b, s[2:]) for i, b in parse_unused(out)]
            GLib.idle_add(self.confirm_unused, found)

        threading.Thread(target=work, daemon=True).start()

    def confirm_unused(self, found):
        self.spin_off()
        if not found:
            self.toast("Nothing unused to remove")
            return
        sizes = {(p.id, p.branch, p.scope): p.size_bytes for p in self.all if p.installed}
        lines = [f"• {i} {b}" for i, b, _s in found[:12]]
        if len(found) > 12:
            lines.append(f"…and {len(found) - 12} more")
        total = sum(sizes.get(f, 0) for f in found)
        body = f"{len(found)} items, about {GLib.format_size(total)}:\n" + "\n".join(lines)
        dlg = Adw.AlertDialog(heading="Remove unused runtimes?", body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Remove")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def done(_d, resp):
            if resp == "remove":
                cmds = [["uninstall", "--unused", "--noninteractive", "-y", s] for s in SCOPES]
                self.run_batch(cmds, "Cleaned up unused runtimes")

        dlg.connect("response", done)
        dlg.present(self)

    def ask_repair(self):
        if self.busy:
            return
        dlg = Adw.AlertDialog(
            heading="Repair the user installation?",
            body="Checks the installed files, removes broken ones and reinstalls what is missing. "
                 "For the system installation run \u201csudo flatpak repair\u201d in a terminal.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("repair", "Repair")
        dlg.set_response_appearance("repair", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("repair")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "repair"
                    and self.run_batch([("repair", "--user")], "Repair finished"))
        dlg.present(self)

    def remove_leftovers(self):
        if self.busy or not self.all:  # with no package list every folder would look abandoned
            self.toast("Wait for the package list to load")
            return
        self.spin.start()
        installed = {p.id for p in self.all if p.installed}

        def work():
            names, sizes = leftover_data_dirs(installed), {}
            if names:
                user_data_sizes(sizes)
            GLib.idle_add(self.confirm_leftovers, names, sizes)

        threading.Thread(target=work, daemon=True).start()

    def confirm_leftovers(self, names, sizes):
        self.spin_off()
        if not names:
            self.toast("No leftover app data")
            return
        lines = [f"\u2022 {n} ({GLib.format_size(sizes.get(n, 0))})" for n in names[:12]]
        if len(names) > 12:
            lines.append(f"\u2026and {len(names) - 12} more")
        total = sum(sizes.get(n, 0) for n in names)
        dlg = Adw.AlertDialog(heading="Delete leftover app data?",
                              body=f"{plural(len(names), 'folder')} in ~/.var/app, about "
                                   f"{GLib.format_size(total)}, from apps that are no longer installed. "
                                   "This cannot be undone.\n" + "\n".join(lines))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def work():
            for n in names:
                shutil.rmtree(os.path.expanduser(f"~/.var/app/{n}"), ignore_errors=True)
            GLib.idle_add(self.leftovers_deleted, len(names))

        dlg.connect("response", lambda _d, r: r == "delete"
                    and threading.Thread(target=work, daemon=True).start())
        dlg.present(self)

    def leftovers_deleted(self, count):
        self.toast(f"Deleted {plural(count, 'folder')}")
        self.scan_data()
        return False

    @staticmethod
    def entry_label():
        return "Remove from app menu" if os.path.exists(DESKTOP_PATH) else "Add to app menu"

    def refresh_entry_label(self):
        self.app_menu.remove(3)
        self.app_menu.insert(3, self.entry_label(), "win.toggle-menu-entry")

    def show_about(self):
        Adw.AboutDialog(application_name="Flatpakshelf", application_icon=APP_ID, version=VERSION,
                        developer_name="outusuke", developers=["outusuke"],
                        copyright="\u00a9 2026 outusuke", license_type=Gtk.License.GPL_3_0,
                        website=REPO_URL, issue_url=REPO_URL + "/issues").present(self)

    def toggle_menu_entry(self):
        adding = not os.path.exists(DESKTOP_PATH)
        try:
            install_self() if adding else uninstall_self()
        except OSError as e:
            self.toast(f"Failed: {e}")
            return
        self.refresh_entry_label()
        self.toast("Added to the app menu" if adding else "Removed from the app menu")

    def update_selected(self):
        pkgs = [p for p in self.selected() if p.upgradable] or \
               [p for p in self.all if p.upgradable]
        if pkgs:
            self.run_batch(update_commands(pkgs), f"Updated {plural(len(pkgs))}")

    def toggle_pause(self):
        sel = self.selected()
        if len(sel) != 1 or not sel[0].installed:
            return
        p = sel[0]
        cmd = ["mask", *scope_args(p.scope), *(["--remove"] if p.paused else []), p.id]
        self.run_batch([cmd], f"Updates resumed for {p.name}" if p.paused
                       else f"Updates paused for {p.name}")

    def change_version(self):
        sel = self.selected()
        if len(sel) == 1 and sel[0].installed:
            VersionWindow(self, sel[0]).present()

    def launch_selected(self):
        sel = self.selected()
        if len(sel) != 1 or not sel[0].installed or sel[0].kind != "app":
            return
        try:
            subprocess.Popen(launch_command(sel[0]), env=FLATPAK_ENV, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as e:
            self.toast(f"Could not launch: {e}")

    def open_data_folder(self):
        sel = self.selected()
        if len(sel) == 1 and os.path.isdir(data_dir(sel[0])):
            Gtk.FileLauncher.new(Gio.File.new_for_path(data_dir(sel[0]))).launch(self, None, None)

    def ask_install_ref(self, location, info):
        title = info.get("Title") or info["Name"]
        drop = Gtk.DropDown.new_from_strings(["For this user only",
                                              "System-wide (asks for authorization)"])
        dlg = Adw.AlertDialog(heading=f"Install {title}?", body=info["Name"])
        dlg.set_extra_child(drop)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("install", "Install")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("install")
        dlg.set_close_response("cancel")

        def done(_d, resp):
            if resp == "install":
                scope = ("--user", "--system")[drop.get_selected()]
                self.run_batch([("install", "--from", location, "-y", scope)], f"Installed {title}")

        dlg.connect("response", done)
        dlg.present(self)

    def run_batch(self, cmds, ok_msg):
        self.set_busy(True)
        self.spin.start()
        self.prog = (None, "")
        self.progress.set_visible(True)
        self.prog_timer = GLib.timeout_add(120, self._tick_progress)

        def work():
            ok, err = run_ops(cmds, lambda frac, text: setattr(self, "prog", (frac, text)))
            GLib.idle_add(self.batch_done, ok, err, ok_msg)

        threading.Thread(target=work, daemon=True).start()

    def _tick_progress(self):
        frac, text = self.prog
        self.progress.set_text(text)
        if frac is None:
            self.progress.pulse()
        else:
            self.progress.set_fraction(frac)
        return True

    def batch_done(self, ok, err, ok_msg):
        GLib.source_remove(self.prog_timer)
        self.progress.set_visible(False)
        self.toast(ok_msg if ok else f"Failed: {err}")
        self.set_busy(False)
        self._begin(True, quick=True)  # refresh installed state from local metadata
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        act = Gio.SimpleAction.new("open", None)
        act.connect("activate", lambda *_: self.activate())
        self.add_action(act)
        act = Gio.SimpleAction.new("changes", None)
        act.connect("activate", lambda *_: self.show_changes())
        self.add_action(act)

    def show_changes(self):
        win = next((w for w in self.get_windows() if isinstance(w, Win)), None)
        if not win:
            self.activate()
            win = next(w for w in self.get_windows() if isinstance(w, Win))
        HistoryWindow(win, days=0).present()

    def do_open(self, files, _n_files, _hint):
        win = next((w for w in self.get_windows() if isinstance(w, Win)), None)
        if not win:
            self.activate()
            win = next(w for w in self.get_windows() if isinstance(w, Win))
        f = files[0]
        name, where = f.get_basename() or "", f.get_path() or f.get_uri()
        if name.endswith(".flatpakrepo"):
            remotes = RemotesWindow(win)
            remotes.present()
            remotes.ask_add(where)
        elif name.endswith(".flatpakref"):
            try:
                info = parse_flatpakref(f.load_contents(None)[1].decode("utf-8", "replace"))
            except GLib.Error:
                info = None
            if info:
                win.ask_install_ref(where, info)
            else:
                win.toast(f"Could not read {name}")

    def do_activate(self):
        # menu clicks and notification clicks all land here
        win = next((w for w in self.get_windows() if isinstance(w, Win)), None)
        if win:
            win.present()
            return
        win = Win(self)
        win.present()
        win.start()


if __name__ == "__main__":
    if "--install" in sys.argv[1:]:
        install_self()
    elif "--uninstall" in sys.argv[1:]:
        uninstall_self()
    elif "--auto-update" in sys.argv[1:]:
        sys.exit(auto_update("--no-deploy" in sys.argv[1:]))
    else:
        App().run(sys.argv)
