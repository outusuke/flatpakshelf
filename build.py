#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Build flatpakshelf.py from src/NN_name.py (joined in NN order; NN is the layer).

    python3 build.py           write flatpakshelf.py
    python3 build.py --check   exit 1 if flatpakshelf.py is stale
    python3 build.py --map     show which output lines come from which fragment

The build fails if a fragment uses a name defined in a higher layer.
"""
import argparse
import builtins
import difflib
import os
import re
import shutil
import symtable
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "flatpakshelf.py")
FRAGMENT = re.compile(r"^(\d\d)_[a-z0-9_]+\.py$")
JOIN = "\n\n\n"  # two blank lines between fragments
MODULE_DUNDERS = {"__name__", "__file__", "__doc__", "__builtins__"}


def read_fragments():
    found = []
    for name in sorted(os.listdir(SRC)):
        m = FRAGMENT.match(name)
        if not m:
            if name.endswith(".py"):
                raise SystemExit(f"src/{name}: fragment names must look like 40_model.py")
            continue
        with open(os.path.join(SRC, name), encoding="utf-8") as f:
            found.append((int(m.group(1)), name, f.read()))
    layers = [layer for layer, _n, _t in found]
    if len(layers) != len(set(layers)):
        raise SystemExit("two fragments share a layer number")
    if not found:
        raise SystemExit("no fragments in src/")
    return found


def module_names(table):
    return {s.get_name() for s in table.get_symbols()
            if s.is_assigned() or s.is_imported() or s.is_namespace()}


def global_refs(table, top=True):
    # symtable, so a local that shares a name with another layer's global is not counted
    refs = set()
    for s in table.get_symbols():
        if top:
            if s.is_referenced():
                refs.add(s.get_name())
        elif s.is_global() and s.is_referenced():
            refs.add(s.get_name())
    for child in table.get_children():
        refs |= global_refs(child, top=False)
    return refs


def check_layers(frags):
    defined, owner = {}, {}
    errors, warnings = [], []
    for layer, name, text in frags:
        try:
            table = symtable.symtable(text, name, "exec")
        except SyntaxError as e:
            raise SystemExit(f"src/{name}: {e}")
        defined[name] = module_names(table)
        for n in defined[name]:
            if n in owner:
                errors.append(f"{n!r} is defined in both src/{owner[n]} and src/{name}")
            owner.setdefault(n, name)
    layer_of = {name: layer for layer, name, _t in frags}
    known = set(dir(builtins)) | MODULE_DUNDERS
    for layer, name, text in frags:
        for n in sorted(global_refs(symtable.symtable(text, name, "exec"))):
            if n in defined[name] or n in known:
                continue
            if n not in owner:
                warnings.append(f"src/{name}: {n!r} is not defined anywhere")
            elif layer_of[owner[n]] > layer:
                errors.append(f"src/{name} (layer {layer}) uses {n!r}, which is defined in "
                              f"src/{owner[n]} (layer {layer_of[owner[n]]})")
    return errors, warnings


def build(frags):
    return JOIN.join(text.strip("\n") for _l, _n, text in frags) + "\n"


def line_map(frags):
    out, line = [], 1
    for _l, name, text in frags:
        n = len(text.strip("\n").split("\n"))
        out.append((name, line, line + n - 1))
        line += n + JOIN.count("\n") - 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if out of date")
    ap.add_argument("--map", action="store_true", help="print the fragment -> output line map")
    args = ap.parse_args()

    frags = read_fragments()
    errors, warnings = check_layers(frags)
    for w in warnings:
        print("warning:", w, file=sys.stderr)
    if errors:
        for e in errors:
            print("layer error:", e, file=sys.stderr)
        return 1
    text = build(frags)
    compile(text, OUT, "exec")

    if args.map:
        for name, a, b in line_map(frags):
            print(f"{a:5}-{b:5}  src/{name}")
        return 0
    try:
        with open(OUT, encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        current = None
    if args.check:
        if current == text:
            print("flatpakshelf.py is up to date")
            return 0
        diff = difflib.unified_diff((current or "").splitlines(), text.splitlines(),
                                    "flatpakshelf.py (committed)", "built from src/", lineterm="")
        print("\n".join(list(diff)[:60]), file=sys.stderr)
        print("flatpakshelf.py is out of date: run python3 build.py", file=sys.stderr)
        return 1
    if current != text:
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if os.path.exists(OUT):
            shutil.copymode(OUT, tmp)
        os.replace(tmp, OUT)
    print(f"wrote {os.path.relpath(OUT, ROOT)}: {len(text.splitlines())} lines "
          f"from {len(frags)} fragments" + (f", {len(warnings)} warning(s)" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
