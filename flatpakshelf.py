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
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

APP_ID = "org.example.Flatpakshelf"
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "flatpakshelf")
CACHE_FILE = os.path.join(CACHE_DIR, "packages.json")
# Plain tab-separated output, no headers or fancy formatting.
FLATPAK_ENV = {**os.environ, "FLATPAK_FANCY_OUTPUT": "0"}
SCOPES = ("--user", "--system")
LIST_COLS = ("ref", "application", "name", "version", "branch", "arch", "origin",
             "installation", "description")
REMOTE_COLS = ("ref", "application", "name", "version", "branch", "arch", "origin",
               "description")
MODES = ["All", "Installed", "Not installed", "Applications", "Runtimes"]


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


def _kind(ref):
    return "runtime" if ref.startswith("runtime/") else "app"


def collect_raw(cached=True):
    """[[kind, id, name, version, branch, arch, origin, scope, summary, installed(0/1)]]
    sorted by name. Installed refs come from `flatpak list`, the rest from `remote-ls`."""
    with ThreadPoolExecutor(max_workers=3) as ex:  # the three calls are independent
        f_inst = ex.submit(list_installed)
        f_user = ex.submit(list_remote, SCOPES[0], cached)
        f_sys = ex.submit(list_remote, SCOPES[1], cached)
        inst, avail = f_inst.result(), f_user.result() + f_sys.result()
    pk = {}
    for ref, i, n, v, b, a, o, sc, s in inst:
        pk[(i, a, b, sc)] = [_kind(ref), i, n, v, b, a, o, sc, s, 1]
    have = {k[:3] for k in pk}
    for ref, i, n, v, b, a, o, s in avail:
        if (i, a, b) not in have:  # first remote wins; installed refs already listed
            have.add((i, a, b))
            pk[(i, a, b, "")] = [_kind(ref), i, n, v, b, a, o, "", s, 0]
    if not pk:
        raise RuntimeError("flatpak returned no packages (no remotes configured?)")
    return sorted(pk.values(), key=lambda d: ((d[2] or d[1]).lower(), d[1], d[4], d[7]))


def read_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) and data else None
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
         self.scope, self.summary, inst) = d
        self.name = name or self.id
        self.installed = bool(inst)
        self.status = "installed" if self.installed else ""
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
        self.search = Gtk.SearchEntry(placeholder_text="Search name, id or description",
                                      width_request=420)
        self.search.connect("search-changed", self.on_search)
        hb.set_title_widget(self.search)

        self.view = Gtk.ColumnView(show_row_separators=True)
        self.add_col("Status", "status", width=100)
        name_col = self.add_col("Name", "name", width=230)
        self.add_col("Application ID", "id", width=280)
        self.add_col("Version", "version", width=120)
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

        self.count = Gtk.Label(label="", xalign=1, margin_top=6, margin_bottom=6,
                               margin_start=12, margin_end=12)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(paned)
        body.append(self.count)
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
        self._begin(True)

    def reload(self):
        self._begin(False)

    def _begin(self, first):
        self.gen += 1
        self.spin.start()
        threading.Thread(target=self._worker, args=(self.gen, first), daemon=True).start()

    def _worker(self, gen, first):
        try:
            cached = read_cache() if first else None
            if cached:
                GLib.idle_add(self._loaded, [Pkg(d) for d in cached], gen, False)
            raw = collect_raw(cached=first)  # startup: local metadata; reload: network
            if cached is not None and raw == cached:
                GLib.idle_add(self._unchanged, gen)  # nothing new: leave the list alone
                return
            write_cache(raw)
            GLib.idle_add(self._loaded, [Pkg(d) for d in raw], gen, True)
        except Exception as e:
            GLib.idle_add(self._load_failed, str(e), gen)

    def _unchanged(self, gen):
        if gen == self.gen:
            self.spin.stop()
        return False

    def _loaded(self, objs, gen, final):
        if gen != self.gen:
            return False
        self.all = objs
        self.sort_all()
        self.refilter()
        if final:
            self.detail_cache.clear()
            self.spin.stop()
        return False

    def _load_failed(self, msg, gen):
        if gen == self.gen:
            self.spin.stop()
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
        get = operator.attrgetter(self.sort_prop)
        self.all.sort(key=lambda p: get(p).casefold(), reverse=self.sort_desc)

    def refilter(self):
        rows = filter_rows(self.all, self.mode.get_selected(), self.query)
        if rows is self.all:
            rows = list(rows)
        self.model.set_rows(rows)
        self.count.set_text(f"{len(rows)} packages")

    def on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self.refilter()

    # ---- details pane
    def selected(self):
        bs = self.selection.get_selection()
        rows = self.model.rows
        return [rows[bs.get_nth(i)] for i in range(bs.get_size())]

    def on_select(self, *_):
        sel = self.selected()
        self.info_token += 1
        if self.info_timer:
            GLib.source_remove(self.info_timer)
            self.info_timer = 0
        self.cur = sel[0] if sel else None
        if self.cur is None:
            for tv in self.tab_views:
                tv.get_buffer().set_text("Select a package to see its details.")
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
        buf = self.tab_views[idx].get_buffer()
        if key in self.detail_cache:
            buf.set_text(self.detail_cache[key])
            return
        buf.set_text("Loading…")

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
            self.tab_views[idx].get_buffer().set_text(text)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = Win(self)
        win.present()
        win.start()


if __name__ == "__main__":
    App().run(None)
