# ---------------------------------------------------------------- data layer
def flatpak(*args, answer=None):
    """(returncode, stdout) of one flatpak call; (1, "") if flatpak is missing."""
    try:
        r = subprocess.run(["flatpak", *args], capture_output=True, text=True,
                           env=FLATPAK_ENV, input=answer)
    except OSError:
        return 1, ""
    return r.returncode, r.stdout


def parse_rows(out, ncols):
    """Tab-separated rows; anything without an app id in column 2 (a header) is dropped."""
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == ncols and "." in parts[1]:
            rows.append(parts)
    return rows


def size_bytes(text):
    num, _, unit = text.replace("\xa0", " ").partition(" ")
    try:
        return float(num.replace(",", ".")) * SIZE_UNITS.get(unit.strip(), 0)
    except ValueError:
        return 0


def user_data_sizes(found):
    """Fill `found` with {app id: bytes} for each folder in ~/.var/app, as each is measured."""
    root = os.path.expanduser("~/.var/app")
    try:
        dirs = [e.path for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return

    def one(path):
        try:
            out = subprocess.run(["du", "-sb", "--", path], capture_output=True,
                                 text=True).stdout
        except OSError:
            return
        num = out.partition("\t")[0]  # du exits non-zero on unreadable files but still reports
        if num.isdigit():
            found[os.path.basename(path)] = int(num)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, dirs))


def read_data_cache():
    try:
        with open(DATA_CACHE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_data_cache(sizes):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(DATA_CACHE, "w") as f:
            json.dump(sizes, f)
    except OSError:
        pass


def list_installed():
    _rc, out = flatpak("list", "--columns=" + ",".join(LIST_COLS))
    return parse_rows(out, len(LIST_COLS))


def list_remote(scope, cached):
    """Everything the configured remotes of one scope offer."""
    args = ["remote-ls", scope, "--columns=" + ",".join(REMOTE_COLS)]
    if cached:  # local metadata only; fall back to a normal call if that gives nothing
        rc, out = flatpak(*args, "--cached")
        rows = parse_rows(out, len(REMOTE_COLS))
        if rc == 0 and rows:
            return rows
    _rc, out = flatpak(*args)
    return parse_rows(out, len(REMOTE_COLS))


def mask_patterns(scope):
    """Patterns `flatpak mask` holds for one scope option such as --user."""
    _rc, out = flatpak("mask", scope)
    return [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]


def is_masked(patterns, app_id, arch, branch):
    """Port of flatpak's mask matching: id[/arch[/branch]], empty or missing parts match anything."""
    for pat in patterns:
        pat = pat.split("/", 1)[1] if pat.startswith(("app/", "runtime/")) else pat
        segs = (pat.split("/") + ["", ""])[:3]
        if all(not s or fnmatchcase(v, s) for s, v in zip(segs, (app_id, arch, branch))):
            return True
    return False


def parse_versions(out):
    """[(commit, subject, date)] from the History block of `flatpak remote-info --log`."""
    rows = []
    for key, val in re.findall(r"^\s*(Commit|Subject|Date):[ \t]*(.*)$", out.partition("History:")[2], re.M):
        if key == "Commit":
            rows.append([val.strip(), "", ""])
        elif rows:
            rows[-1][1 if key == "Subject" else 2] = val.strip()
    return [tuple(r) for r in rows]


def parse_remotes(out, scope):
    """[(scope, name, title, url, enabled)] from `flatpak remotes --columns=name,title,url,options`."""
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0].strip():
            rows.append((scope, parts[0].strip(), parts[1].strip(), parts[2].strip(),
                         "disabled" not in parts[3]))
    return rows


def list_remotes():
    rows = []
    for scope in ("user", "system"):
        _rc, out = flatpak("remotes", "--" + scope, "--show-disabled",
                           "--columns=name,title,url,options")
        rows += parse_remotes(out, scope)
    return rows


def list_updates(scope):
    """(id, arch, branch) of every installed ref in one scope that has an update."""
    _rc, names = flatpak("remotes", scope, "--columns=name")
    found = set()
    for remote in names.split():  # one broken remote makes remote-ls fail for the whole scope
        _rc, out = flatpak("remote-ls", scope, remote, "--updates",
                           "--columns=ref,application,branch,arch,origin")
        found |= {(i, a, b) for _ref, i, b, a, _o in parse_rows(out, 5)}
    masks = mask_patterns(scope)  # remote-ls ignores masks, flatpak update skips them
    return {t for t in found if not is_masked(masks, *t)}


def _kind(ref):
    return "runtime" if ref.startswith("runtime/") else "app"


def collect_raw(cached=True):
    """[[kind, id, name, version, branch, arch, origin, scope, summary, installed(0/1/2), size]]
    sorted by name. Installed refs come from `flatpak list`, the rest from `remote-ls`."""
    with ThreadPoolExecutor(max_workers=7) as ex:  # the calls are independent
        f_inst = ex.submit(list_installed)
        f_user = ex.submit(list_remote, SCOPES[0], cached)
        f_sys = ex.submit(list_remote, SCOPES[1], cached)
        f_upd = {s[2:]: ex.submit(list_updates, s) for s in SCOPES}
        f_mask = {s[2:]: ex.submit(mask_patterns, s) for s in SCOPES}
        inst, avail = f_inst.result(), f_user.result() + f_sys.result()
        upd = {s: f.result() for s, f in f_upd.items()}
        masks = {s: f.result() for s, f in f_mask.items()}
    pk = {}
    for ref, i, n, v, b, a, o, sc, s, sz in inst:
        # 1 installed, 2 installed with an update pending, 3 installed with updates paused
        flag = 3 if is_masked(masks.get(sc, ()), i, a, b) else 2 if (i, a, b) in upd.get(sc, ()) else 1
        pk[(i, a, b, sc)] = [_kind(ref), i, n, v, b, a, o, sc, s, flag, sz]
    have = {k[:3] for k in pk}
    for ref, i, n, v, b, a, o, s, sz in avail:
        if (i, a, b) not in have:  # first remote wins; installed refs already listed
            have.add((i, a, b))
            pk[(i, a, b, "")] = [_kind(ref), i, n, v, b, a, o, "", s, 0, sz]
    if not pk:
        raise RuntimeError("flatpak returned no packages (no remotes configured?)")
    return sorted(pk.values(), key=lambda d: ((d[2] or d[1]).lower(), d[1], d[4], d[7]))


def read_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) and data and len(data[0]) == 11 else None
    except Exception:
        return None


def write_cache(raw):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, separators=(",", ":"))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def scope_args(scope):
    """flatpak scope option for an installation id from `flatpak list`."""
    if scope in ("user", "system"):
        return ["--" + scope]
    return [f"--installation={scope}"] if scope else []


def list_history(days):
    """Newest first: [time, change, app id, branch, installation, remote]."""
    _rc, out = flatpak("history", "--reverse", f"--since={days}days",
                       "--columns=" + ",".join(HISTORY_COLS))
    rows = (line.split("\t") for line in out.splitlines())
    return [r for r in rows if len(r) == len(HISTORY_COLS) and "." in r[2]]


def media_path(url):
    ext = os.path.splitext(url.partition("?")[0])[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        ext = ".png"
    return os.path.join(MEDIA_DIR, hashlib.sha1(url.encode()).hexdigest() + ext)


def fetch_media(url):
    if not url.startswith(("https://", "http://")):
        return ""
    path = media_path(url)
    if os.path.exists(path):
        return path
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "flatpakshelf"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(MEDIA_MAX_BYTES + 1)
        if not data or len(data) > MEDIA_MAX_BYTES:
            return ""
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    except Exception:  # a missing picture is not worth an error
        return ""
