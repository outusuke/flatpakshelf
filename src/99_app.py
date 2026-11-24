class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
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

    def do_activate(self):
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
        App().run(None)
