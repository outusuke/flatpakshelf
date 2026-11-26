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
