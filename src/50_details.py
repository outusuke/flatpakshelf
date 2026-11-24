TABS = ("Info", "Permissions", "Metadata", "Changes")
TAB_EMPTY = ("No information available.", "No special permissions declared.",
             "No metadata available.", "No update notes available.")
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


def appstream_files(p):
    scopes = [p.scope] if p.installed else ["user", "system"]
    roots = {"user": os.path.join(GLib.get_user_data_dir(), "flatpak"),
             "system": "/var/lib/flatpak"}
    return [os.path.join(roots.get(sc, roots["system"]), "appstream", p.origin, p.arch,
                         "active", "appstream.xml.gz") for sc in scopes]


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
