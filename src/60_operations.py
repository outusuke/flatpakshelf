def parse_unused(out):
    """[(id, branch)] from the table `flatpak uninstall --unused --assumeno` prints."""
    return [m.groups() for line in out.splitlines()
            if (m := re.match(r"\s*\d+\.\s+(\S+\.\S+)\s+(\S+)", line))]


def flatpak_op(*args):
    """(returncode, message) of one flatpak call that changes something."""
    try:
        r = subprocess.run(["flatpak", *args], capture_output=True, text=True,
                           env=FLATPAK_ENV)
    except OSError as e:
        return 1, str(e)
    return r.returncode, (r.stderr or r.stdout).strip()


def remote_names(scope):
    """Names of the enabled remotes of one installation ('user' or 'system')."""
    _rc, out = flatpak("remotes", "--" + scope)
    return {line.split("\t")[0].strip() for line in out.splitlines() if line.strip()}


Tx = namedtuple("Tx", "action scope refs origin commit delete_data",
                defaults=(None, None, False))


def install_commands(pkgs, scope):
    """One install per origin remote."""
    by_origin = {}
    for p in pkgs:
        by_origin.setdefault(p.origin, []).append(p.ref)
    return [Tx("install", scope, refs, origin) for origin, refs in by_origin.items()]


def uninstall_commands(pkgs, delete_data):
    """One uninstall per installation, so a ref present in both is unambiguous."""
    by_scope = {}
    for p in pkgs:
        by_scope.setdefault(p.scope, []).append(p.ref)
    return [Tx("uninstall", scope, refs, delete_data=delete_data)
            for scope, refs in by_scope.items()]


def update_commands(pkgs):
    by_scope = {}
    for p in pkgs:
        by_scope.setdefault(p.scope, []).append(p.ref)
    return [Tx("update", scope, refs) for scope, refs in by_scope.items()]


def launch_command(p):
    return ["flatpak", "run", f"--branch={p.branch}", *scope_args(p.scope), p.id]


def data_dir(p):
    return os.path.expanduser(f"~/.var/app/{p.id}")


def installation(scope):
    if scope == "user":
        return Flatpak.Installation.new_user(None)
    if scope == "system":
        return Flatpak.Installation.new_system(None)
    for inst in Flatpak.get_system_installations(None):
        if inst.get_id() == scope:
            return inst
    raise RuntimeError(f"Unknown installation: {scope}")


def run_transaction(tx, report):
    """(returncode, message) of a Tx run through libflatpak; report(fraction, text) as it goes."""
    verbs = {Flatpak.TransactionOperationType.INSTALL: "Installing",
             Flatpak.TransactionOperationType.UPDATE: "Updating",
             Flatpak.TransactionOperationType.UNINSTALL: "Removing"}
    state = {"done": 0, "total": 0}

    def ready(t):
        state["total"] = len(t.get_operations())
        return True

    def new_operation(_t, op, progress):
        label = (f"{verbs.get(op.get_operation_type(), 'Working on')} "
                 f"{op.get_ref().split('/')[1]} ({state['done'] + 1}/{state['total']})")

        def changed(prog):
            report((state["done"] + prog.get_progress() / 100) / state["total"], label)

        progress.set_update_frequency(100)
        progress.connect("changed", changed)
        changed(progress)

    report(None, "Preparing")
    try:
        t = Flatpak.Transaction.new_for_installation(installation(tx.scope), None)
        t.set_no_interaction(True)
        t.connect("ready", ready)
        t.connect("new-operation", new_operation)
        t.connect("operation-done", lambda *_: state.update(done=state["done"] + 1))
        for ref in tx.refs:
            if tx.action == "install":
                t.add_install(tx.origin, ref, None)
            elif tx.action == "update":
                t.add_update(ref, None, tx.commit)
            else:
                t.add_uninstall(ref)
        t.run(None)
    except (GLib.Error, RuntimeError) as e:
        return 1, getattr(e, "message", None) or str(e)
    if tx.delete_data:
        for ref in tx.refs:
            if ref.startswith("app/"):
                shutil.rmtree(os.path.expanduser(f"~/.var/app/{ref.split('/')[1]}"),
                              ignore_errors=True)
    return 0, ""


def run_ops(cmds, on_progress=None):
    """Run the steps in order, stopping at the first failure: (ok, last message line).

    A step is a Tx or the arguments of a flatpak call; on_progress(fraction or None, text)
    gets the overall progress, with None while there is nothing to measure.
    """
    for n, step in enumerate(cmds):
        def report(frac, text, n=n):
            if on_progress:
                on_progress(None if frac is None else (n + frac) / len(cmds), text)

        if isinstance(step, Tx):
            rc, msg = run_transaction(step, report)
        else:
            report(None, "Working")
            rc, msg = flatpak_op(*step)
        if rc:
            lines = msg.splitlines()
            return False, lines[-1] if lines else f"flatpak exited with {rc}"
    return True, ""
