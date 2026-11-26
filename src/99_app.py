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
        repo = next((f for f in files if (f.get_basename() or "").endswith(".flatpakrepo")), None)
        if repo:
            remotes = RemotesWindow(win)
            remotes.present()
            remotes.ask_add(repo.get_path() or repo.get_uri())

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
