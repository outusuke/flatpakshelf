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
