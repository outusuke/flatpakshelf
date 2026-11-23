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
import json
import operator
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

APP_ID = "io.github.outusuke.Flatpakshelf"
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "flatpakshelf")
CACHE_FILE = os.path.join(CACHE_DIR, "packages.json")
DATA_CACHE = os.path.join(CACHE_DIR, "userdata.json")
# Plain tab-separated output, no headers or fancy formatting.
FLATPAK_ENV = {**os.environ, "FLATPAK_FANCY_OUTPUT": "0"}
SCOPES = ("--user", "--system")
LIST_COLS = ("ref", "application", "name", "version", "branch", "arch", "origin",
             "installation", "description", "size")
REMOTE_COLS = ("ref", "application", "name", "version", "branch", "arch", "origin",
               "description", "installed-size")
SIZE_UNITS = {"bytes": 1, "byte": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
MODES = ["All", "Installed", "Not installed", "Applications", "Runtimes", "Upgradable"]


# ---------------------------------------------------------------- data layer
def flatpak(*args):
    """(returncode, stdout) of one flatpak call; (1, "") if flatpak is missing."""
    try:
        r = subprocess.run(["flatpak", *args], capture_output=True, text=True,
                           env=FLATPAK_ENV)
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


def list_updates(scope, cached):
    """(id, arch, branch) of every installed ref in one scope that has an update."""
    args = ["remote-ls", scope, "--updates", "--columns=ref,application,branch,arch,origin"]
    if cached:
        args.append("--cached")
    _rc, out = flatpak(*args)
    return {(i, a, b) for _ref, i, b, a, _o in parse_rows(out, 5)}


def _kind(ref):
    return "runtime" if ref.startswith("runtime/") else "app"


def collect_raw(cached=True):
    """[[kind, id, name, version, branch, arch, origin, scope, summary, installed(0/1/2), size]]
    sorted by name. Installed refs come from `flatpak list`, the rest from `remote-ls`."""
    with ThreadPoolExecutor(max_workers=5) as ex:  # the calls are independent
        f_inst = ex.submit(list_installed)
        f_user = ex.submit(list_remote, SCOPES[0], cached)
        f_sys = ex.submit(list_remote, SCOPES[1], cached)
        f_upd = {s[2:]: ex.submit(list_updates, s, cached) for s in SCOPES}
        inst, avail = f_inst.result(), f_user.result() + f_sys.result()
        upd = {s: f.result() for s, f in f_upd.items()}
    pk = {}
    for ref, i, n, v, b, a, o, sc, s, sz in inst:
        flag = 2 if (i, a, b) in upd.get(sc, ()) else 1  # 2: installed, update pending
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
        self.name = name or self.id
        self.installed = bool(inst)
        self.upgradable = inst == 2
        self.status = "upgradable" if self.upgradable else "installed" if self.installed else ""
        self.ref = f"{self.kind}/{self.id}/{self.arch}/{self.branch}"
        self.hay = f"{self.name} {self.id} {self.summary}".lower()


TABS = ("Info", "Permissions", "Metadata")
TAB_EMPTY = ("No information available.", "No special permissions declared.",
             "No metadata available.")
PERM_SECTIONS = ("Context", "Session Bus Policy", "System Bus Policy", "Environment")


def scope_args(scope):
    """flatpak scope option for an installation id from `flatpak list`."""
    if scope in ("user", "system"):
        return ["--" + scope]
    return [f"--installation={scope}"] if scope else []


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
    """(title, subtitle, risky) for one entry of a Context permission list."""
    if key != "filesystems":
        return PERM_LABELS.get(item, item), "", item in PERM_RISKY and not item.startswith("!")
    denied = item.startswith("!")
    base, _, mode = item.lstrip("!").partition(":")
    title = PERM_LABELS.get(base)
    if title is None:
        title = base[4:].replace("-", " ").capitalize() + " folder" if base.startswith("xdg-") else base
    if denied:
        return title, "Denied", False
    return title, FS_MODES.get(mode, "Read and write"), base in PERM_RISKY and mode != "ro"


def parse_permissions(text):
    """[(group title, [(title, subtitle, risky)])] from a permissions keyfile, else None."""
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
                rows = [(key, BUS_POLICY.get(val, val), val == "own")]
            else:
                title = "Environment variables" if section == "Environment" else section
                rows = [(key, val, False)]
            groups.setdefault(title, []).extend(rows)
    return [g for g in groups.items() if g[1]] or None


def detail_text(p, idx):
    """Text for one tab of the details pane (0 info, 1 permissions, 2 metadata)."""
    if p.installed:
        flags = ([], ["--show-permissions"], ["--show-metadata"])[idx]
        _rc, text = flatpak("info", *scope_args(p.scope), f"--arch={p.arch}", *flags,
                            p.id, p.branch)
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
    _rc, out = flatpak("remotes", "--" + scope)
    return {line.split("\t")[0].strip() for line in out.splitlines() if line.strip()}


def install_commands(pkgs, scope):
    """One `flatpak install` per origin remote."""
    by_origin = {}
    for p in pkgs:
        by_origin.setdefault(p.origin, []).append(p.ref)
    return [["install", "--noninteractive", "-y", "--" + scope, origin, *refs]
            for origin, refs in by_origin.items()]


def uninstall_commands(pkgs, delete_data):
    """One `flatpak uninstall` per installation, so a ref present in both is unambiguous."""
    by_scope = {}
    for p in pkgs:
        by_scope.setdefault(p.scope, []).append(p.ref)
    extra = ["--delete-data"] if delete_data else []
    return [["uninstall", "--noninteractive", "-y", *extra, *scope_args(scope), *refs]
            for scope, refs in by_scope.items()]


def update_commands(pkgs):
    by_scope = {}
    for p in pkgs:
        by_scope.setdefault(p.scope, []).append(p.ref)
    return [["update", "--noninteractive", "-y", *scope_args(scope), *refs]
            for scope, refs in by_scope.items()]


def launch_command(p):
    return ["flatpak", "run", f"--branch={p.branch}", *scope_args(p.scope), p.id]


def data_dir(p):
    return os.path.expanduser(f"~/.var/app/{p.id}")


def run_ops(cmds):
    """Run flatpak calls in order, stopping at the first failure: (ok, last message line)."""
    for args in cmds:
        rc, msg = flatpak_op(*args)
        if rc:
            lines = msg.splitlines()
            return False, lines[-1] if lines else f"flatpak exited with {rc}"
    return True, ""


def names_summary(pkgs, limit=8):
    names = [p.name for p in pkgs]
    text = "\n".join(names[:limit])
    return text + (f"\n… and {len(names) - limit} more" if len(names) > limit else "")


def plural(n, word="package"):
    return f"{n} {word}" + ("" if n == 1 else "s")


def filter_rows(rows, mode, query):
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
    if query:
        rows = [p for p in rows if query in p.hay]
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


# ---------------------------------------------------------------- main window
class Win(Adw.ApplicationWindow):
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
        self.detail_cache = {}
        self.busy = False      # an install/uninstall is running
        self.n_upg = 0
        self.data_sizes = {}
        self.scan_id = 0
        self.scanning = False

        hb = Adw.HeaderBar()
        self.mode = Gtk.DropDown.new_from_strings(MODES)
        self.mode.connect("notify::selected", lambda *_: self.refilter())
        hb.pack_start(self.mode)
        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                     tooltip_text="Reload package list from the remotes")
        self.reload_btn.connect("clicked", lambda *_: self.reload())
        hb.pack_start(self.reload_btn)
        self.spin = Gtk.Spinner()
        hb.pack_start(self.spin)
        menu = Gio.Menu()
        menu.append("Remove unused runtimes", "win.remove-unused")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        act = Gio.SimpleAction.new("remove-unused", None)
        act.connect("activate", lambda *_: self.remove_unused())
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
        self.search = Gtk.SearchEntry(placeholder_text="Search name, id or description",
                                      width_request=420)
        self.search.connect("search-changed", self.on_search)
        hb.set_title_widget(self.search)

        self.view = Gtk.ColumnView(show_row_separators=True)
        self.add_col("Status", "status", width=100)
        name_col = self.add_col("Name", "name", width=230)
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
        for idx, title in enumerate(TABS):
            if idx < 2:  # Info and Permissions are shown as native rows
                tv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                             margin_bottom=12, margin_start=12, margin_end=12)
            else:
                tv = Gtk.TextView(editable=False, monospace=True, left_margin=8, top_margin=8,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.tab_views.append(tv)
            self.notebook.append_page(
                Gtk.ScrolledWindow(vexpand=True, min_content_height=170, child=tv),
                Gtk.Label(label=title))
        self.notebook.connect("switch-page", self.on_tab_switch)
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, position=440,
                          start_child=Gtk.ScrolledWindow(vexpand=True, child=self.view),
                          end_child=self.notebook, shrink_start_child=False,
                          shrink_end_child=False)

        self.count = Gtk.Label(label="", xalign=1, hexpand=True)
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12,
                      margin_end=12)
        for w in (self.btn_install, self.btn_update, self.btn_launch, self.btn_folder,
                  self.btn_remove, self.count):
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

    def add_col(self, title, prop, width=None, expand=False):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            li.set_child(Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                                   single_line_mode=True, max_width_chars=1,
                                   margin_start=6, margin_end=6))

        def bind(_f, li):
            text = getattr(li.get_item(), prop)
            lbl = li.get_child()
            if lbl.get_label() != text:
                lbl.set_label(text)

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

    def refilter(self):
        rows = filter_rows(self.all, self.mode.get_selected(), self.query)
        if rows is self.all:
            rows = list(rows)
        self.model.set_rows(rows)
        text = plural(len(rows))
        if self.n_upg:
            text += f", {self.n_upg} upgradable"
        self.count.set_text(text + ", measuring user data…" if self.scanning else text)

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

    @staticmethod
    def detail_row(title, sub="", warn=False, prop=False):
        row = Adw.ActionRow(title=title, subtitle=sub, use_markup=False, title_lines=0,
                            subtitle_lines=0, subtitle_selectable=True)
        if prop:  # small dimmed key above a larger value
            row.add_css_class("property")
        if warn:
            icon = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text="Broad access")
            icon.add_css_class("warning")
            row.add_suffix(icon)
        return row

    def show_tab(self, idx, text):
        view = self.tab_views[idx]
        if idx == 2:
            view.get_buffer().set_text(text)
            return
        while child := view.get_first_child():
            view.remove(child)
        parsed = (parse_info, parse_permissions)[idx](text)
        if parsed is None:  # status message such as "Loading…" or "No special permissions"
            note = Gtk.Label(label=text, wrap=True, xalign=0, selectable=True)
            note.add_css_class("dim-label")
            view.append(note)
        elif idx == 0:
            title, rows = parsed
            if title:
                head = Gtk.Label(label=title, wrap=True, xalign=0, selectable=True)
                head.add_css_class("title-4")
                view.append(head)
            group = Adw.PreferencesGroup()
            for key, value in rows:
                group.add(self.detail_row(key, value, prop=True))
            view.append(group)
        else:
            for title, rows in parsed:
                group = Adw.PreferencesGroup(title=title)
                for t, sub, warn in rows:
                    group.add(self.detail_row(t, sub, warn))
                view.append(group)

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
        dlg = Adw.AlertDialog(
            heading="Remove unused runtimes?",
            body="Removes runtimes and extensions no installed app needs, "
                 "in both the user and system installations.")
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

    def update_selected(self):
        pkgs = [p for p in self.selected() if p.upgradable] or \
               [p for p in self.all if p.upgradable]
        if pkgs:
            self.run_batch(update_commands(pkgs), f"Updated {plural(len(pkgs))}")

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

    def run_batch(self, cmds, ok_msg):
        self.set_busy(True)
        self.spin.start()

        def work():
            ok, err = run_ops(cmds)
            GLib.idle_add(self.batch_done, ok, err, ok_msg)

        threading.Thread(target=work, daemon=True).start()

    def batch_done(self, ok, err, ok_msg):
        self.toast(ok_msg if ok else f"Failed: {err}")
        self.set_busy(False)
        self._begin(True, quick=True)  # refresh installed state from local metadata
        return False


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


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = Win(self)
        win.present()
        win.start()


if __name__ == "__main__":
    if "--install" in sys.argv[1:]:
        install_self()
    elif "--uninstall" in sys.argv[1:]:
        uninstall_self()
    else:
        App().run(None)
