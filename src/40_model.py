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
