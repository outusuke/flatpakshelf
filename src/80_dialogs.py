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
