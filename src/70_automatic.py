def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True).stdout.strip()


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def automatic_settings():
    exec_line = re.search(r"^ExecStart=(.*)$", read_text(os.path.join(UNIT_DIR, AUTO_SERVICE)), re.M)
    cal = re.search(r"^OnCalendar=(.*?)\s*$", read_text(os.path.join(UNIT_DIR, AUTO_TIMER)), re.M)
    mode = 1 if exec_line and "--no-deploy" not in exec_line.group(1) else 0
    return {"mode": mode, "freq": cal.group(1) if cal else "daily"}


def timer_state():
    """(systemctl usable, enabled, next run) for the user timer."""
    try:
        enabled = systemctl("is-enabled", AUTO_TIMER) == "enabled"
        nxt = systemctl("show", AUTO_TIMER, "-p", "NextElapseUSecRealtime", "--value")
    except OSError:
        return False, False, ""
    return True, enabled, nxt


def gnome_software_settings():
    """GNOME Software's settings, or None when it isn't installed (a missing schema would abort)."""
    src = Gio.SettingsSchemaSource.get_default()
    schema = src and src.lookup("org.gnome.software", True)
    if schema and schema.has_key("download-updates"):
        return Gio.Settings.new_full(schema, None, None)
    return None


GS_SERVICE = "gnome-software.service"


def gs_paused():
    try:
        r = subprocess.run(["systemctl", "--user", "is-enabled", GS_SERVICE],
                           capture_output=True, text=True)
    except OSError:
        return False
    return r.stdout.strip() == "masked"


def set_gs_paused(on):
    """Mask or unmask GNOME Software's background service; returns an error message or ""."""
    verb = ["mask", "--now"] if on else ["unmask"]
    try:
        r = subprocess.run(["systemctl", "--user", *verb, GS_SERVICE],
                           capture_output=True, text=True)
    except OSError as e:
        return str(e)
    return "" if r.returncode == 0 else (r.stderr or r.stdout).strip() or f"systemctl exited with {r.returncode}"


def write_units(mode, freq):
    os.makedirs(UNIT_DIR, exist_ok=True)
    # --no-deploy only pulls the new commits; the app's Update button then deploys them
    extra = " --no-deploy" if mode == 0 else ""
    for name, text in ((AUTO_SERVICE, SERVICE_UNIT.format(exe=BIN_PATH, extra=extra)),
                       (AUTO_TIMER, TIMER_UNIT.format(freq=freq))):
        with open(os.path.join(UNIT_DIR, name), "w") as f:
            f.write(text)


def notify(title, body, action="app.open"):
    app = Gio.Application(application_id=APP_ID)
    app.register(None)
    note = Gio.Notification.new(title)
    note.set_body(body)
    note.set_default_action(action)
    app.send_notification("auto-update", note)
    conn = app.get_dbus_connection()
    if conn:
        conn.flush_sync(None)  # we exit right after, so push the message out first


def update_line(i, before, after):
    old, new = before.get(i), after.get(i)
    row = new or old
    ov, nv = old[3] if old else "", new[3] if new else ""
    name = row[2] if row and row[2] else i
    return f"{name} {ov} \u2192 {nv}" if ov and nv and ov != nv else name


GAMEMODE = "com.feralinteractive.GameMode"


def gamemode_active():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(GAMEMODE, "/com/feralinteractive/GameMode",
                              "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", (GAMEMODE, "ClientCount")), None,
                              Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
    except GLib.Error:
        return False
    return reply.unpack()[0] > 0


def update_blocker():
    """Why automatic updates should wait, or "". Same checks as GNOME Software, plus GameMode."""
    if Gio.NetworkMonitor.get_default().get_network_metered():
        return "the connection is metered"
    monitor = Gio.PowerProfileMonitor.dup_default()
    if monitor and monitor.get_power_saver_enabled():
        return "power saver is on"
    if gamemode_active():
        return "GameMode is active"
    return ""


def retry_later():
    """Run the update service again in 30 minutes, unless the timer gets switched off."""
    start = f"systemctl --user is-enabled --quiet {AUTO_TIMER} && " \
            f"systemctl --user --no-block start {AUTO_SERVICE}"
    subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--on-active=30min",
                    "--unit=flatpakshelf-retry", "sh", "-c", start], capture_output=True)


def auto_update(download_only):
    why = update_blocker()
    if why:
        print(f"Automatic update skipped: {why}", file=sys.stderr)
        retry_later()
        return 0
    before = {r[1]: r for r in list_installed()}
    pending = set().union(*(list_updates(s) for s in SCOPES))
    if not pending:
        return 0
    rc, msg = flatpak_op("update", "--noninteractive", "-y", *(["--no-deploy"] if download_only else []))
    if rc:
        notify("Flatpak update failed", (msg.splitlines() or [f"flatpak exited with {rc}"])[-1])
        return 1
    ids = sorted({i for i, _a, _b in pending})
    after = before
    if download_only:
        key = [f"{i}/{a}/{b}" for i, a, b in sorted(pending)]
        try:
            seen = json.loads(read_text(STATE_FILE))
        except ValueError:
            seen = None
        if seen == key:
            return 0
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump(key, f)
        except OSError:
            pass
    else:
        after = {r[1]: r for r in list_installed()}
    lines = [update_line(i, before, after) for i in ids]
    if len(lines) > 8:
        lines[8:] = [f"\u2026 and {len(lines) - 8} more"]
    if download_only:
        notify(f"{plural(len(ids), 'Flatpak update')} ready",
               "\n".join(lines + ["Open Flatpakshelf to install them"]))
    else:
        notify(f"{plural(len(ids), 'Flatpak')} updated", "\n".join(lines), "app.changes")
    return 0


def save_automatic(now, loaded, freq):
    """Write the units and switch the timer as needed; returns an error message or ""."""
    sched = (now["mode"], now["freq"]) != (loaded["mode"], loaded["freq"])
    toggled = now["enabled"] != loaded["enabled"]
    steps = []
    try:
        if sched or (now["enabled"] and toggled):
            write_units(now["mode"], freq)
            steps.append(["daemon-reload"])
        if now["enabled"] and (sched or toggled):
            steps += [["enable", AUTO_TIMER], ["restart", AUTO_TIMER]]
        elif toggled:
            steps.append(["disable", "--now", AUTO_TIMER])
        for step in steps:
            r = subprocess.run(["systemctl", "--user", *step], capture_output=True, text=True)
            if r.returncode:
                return (r.stderr or r.stdout).strip() or f"systemctl exited with {r.returncode}"
    except OSError as e:
        return str(e)
    return ""
