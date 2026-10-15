#!/usr/bin/env python3
"""Every text that needs a translation, and what each catalog is missing.

    python3 tools/i18n_keys.py                # how many texts, and the gaps of every catalog
    python3 tools/i18n_keys.py --missing de   # the German gaps, as JSON ready to fill in

Texts are found in the Python code (calls to _(), N_() and ngettext(), read with the ast module, so
implicit string concatenation is handled) and in the panel's templates ([[...]] and _t('...')).
Stdlib only.
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dobermann import i18n  # noqa: E402

PY_DIRS = ("dobermann", "tools")
PY_FILES = ("wizard.py",)
TEMPLATES = os.path.join(ROOT, "dobermann", "web", "templates")


def _python_files():
    for d in PY_DIRS:
        for base, _dirs, files in os.walk(os.path.join(ROOT, d)):
            for f in sorted(files):
                if f.endswith(".py"):
                    yield os.path.join(base, f)
    for f in PY_FILES:
        yield os.path.join(ROOT, f)


def python_keys(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        n = {"_": 1, "N_": 1, "ngettext": 2}.get(node.func.id, 0)
        for arg in node.args[:n]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append(arg.value)
    return out


def all_keys():
    """{text: [where it is used]}, in a stable order."""
    found = {}
    for path in _python_files():
        for k in python_keys(path):
            found.setdefault(k, []).append(os.path.relpath(path, ROOT))
    for name in sorted(os.listdir(TEMPLATES)):
        if name.endswith(".html"):
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as f:
                html_keys, js_keys = i18n.template_keys(f.read())
            for k in html_keys + js_keys:
                found.setdefault(k, []).append(f"templates/{name}")
    return dict(sorted(found.items()))


def missing(code, keys=None):
    keys = all_keys() if keys is None else keys
    cat = i18n.catalog(code)
    return [k for k in keys if not cat.get(k)]


def unused(code, keys=None):
    keys = all_keys() if keys is None else keys
    return [k for k in i18n.catalog(code) if k not in keys]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--missing", metavar="LANG", help="print the texts missing from this catalog as JSON")
    args = ap.parse_args(argv)
    keys = all_keys()
    if args.missing:
        print(json.dumps({k: "" for k in missing(args.missing, keys)}, ensure_ascii=False, indent=1))
        return 0
    print(f"{len(keys)} texts")
    for code in i18n.LANGUAGES:
        if code == i18n.SOURCE:
            continue
        print(f"  {code}: {len(missing(code, keys))} missing, {len(unused(code, keys))} unused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
