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
        self.mode.set_selected(MODES.index("Installed"))
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
        menu.append("Automatic updates", "win.auto-updates")
        menu.append("Recent changes", "win.history")
        self.app_menu = menu
        menu.append(self.entry_label(), "win.toggle-menu-entry")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        act = Gio.SimpleAction.new("remove-unused", None)
        act.connect("activate", lambda *_: self.remove_unused())
        self.add_action(act)
        act = Gio.SimpleAction.new("history", None)
        act.connect("activate", lambda *_: HistoryWindow(self).present())
        self.add_action(act)
        act = Gio.SimpleAction.new("auto-updates", None)
        act.connect("activate", lambda *_: AutoUpdatesWindow(self).present())
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
    def detail_row(title, sub="", warn=False, prop=False, changed=False, switch=None, p=None):
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

    def show_tab(self, idx, text):
        view = self.tab_views[idx]
        if idx >= 2:
            view.get_buffer().set_text(text)
            return
        while child := view.get_first_child():
            view.remove(child)
        parsed = (parse_info, parse_permissions)[idx](text)
        p = self.cur
        editable = p if p and p.installed and p.kind == "app" else None
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
                for t, sub, warn, ctl, changed in rows:
                    group.add(self.detail_row(t, sub, warn, changed=changed, switch=ctl, p=editable))
                view.append(group)
            if editable and any(r[4] for _t, rows in parsed for r in rows):
                reset = Gtk.Button(label="Reset overrides", halign=Gtk.Align.START,
                                   tooltip_text="Remove all your permission changes for this app")
                reset.connect("clicked", lambda *_: self.reset_overrides(editable))
                view.append(reset)

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

    @staticmethod
    def entry_label():
        return "Remove from app menu" if os.path.exists(DESKTOP_PATH) else "Add to app menu"

    def refresh_entry_label(self):
        self.app_menu.remove(3)
        self.app_menu.insert(3, self.entry_label(), "win.toggle-menu-entry")

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
