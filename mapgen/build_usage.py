"""Build the dialect-usage maps (names, family closure, test mentions).

Runs inside the pinned source image (/src). Outputs JSON to /out2:
  dialect_names.json  {dialect_module: [mention names]}
  famclosure.json     {dialect_module: [family member modules incl. self]}
  mentions.json.gz    {test_id: [names]}
"""
import ast
import gzip
import json
import os
import re
import sys

SRC = "/src"
OUT = "/out2"
TESTS = "/data/tests.json"
READS = "/data/reads.json.gz"

import importlib
import pkgutil

sys.path.insert(0, "/src")

Dialect = importlib.import_module("sqlglot.dialects.dialect").Dialect
pkg = importlib.import_module("sqlglot.dialects")

mods = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if m.name != "dialect")
classes_by_mod = {}
for m in mods:
    mod = importlib.import_module(f"sqlglot.dialects.{m}")
    clss = [
        c
        for n, c in vars(mod).items()
        if isinstance(c, type)
        and c is not Dialect
        and issubclass(c, Dialect)
        and c.__module__ == f"sqlglot.dialects.{m}"
    ]
    if clss:
        classes_by_mod[m] = clss

# enum names (sqlglot.enums.Dialects): key -> value
vals = {}
try:
    import sqlglot.enums as _e

    if hasattr(_e, "Dialects"):
        for n, v in vars(_e.Dialects).items():
            if not n.startswith("_") and isinstance(v, str):
                vals[n.lower()] = v.lower()
except Exception:
    pass
if not vals:
    try:
        src = open(os.path.join(SRC, "sqlglot", "enums.py"), encoding="utf-8").read()
        m = re.search(r"class Dialects\b.*?(?=\nclass |\Z)", src, re.S)
        if m:
            for k, v in re.findall(r"^\s+([A-Z0-9_]+)\s*=\s*\"([^\"]+)\"", m.group(0), re.M):
                vals[k.lower()] = v.lower()
    except Exception:
        pass

dialect_names = {}
for m, clss in classes_by_mod.items():
    names = {m}
    for c in clss:
        names.add(c.__name__.lower())
    for en, ev in vals.items():
        if en == m or en in {c.__name__.lower() for c in clss} or ev == m:
            names.add(en)
            names.add(ev)
    dialect_names[m] = sorted(names)

# family closure: module -> descendant dialect modules
all_dc = [
    (c, c.__module__.split(".")[-1])
    for mod, cl in classes_by_mod.items()
    for c in cl
]

def family_mod(module):
    fam = {module}
    for c in classes_by_mod.get(module, ()):
        for d, dmod in all_dc:
            if d is not c and issubclass(d, c):
                fam.add(dmod)
    return sorted(fam)

familys = {m: family_mod(m) for m in classes_by_mod}

os.makedirs(OUT, exist_ok=True)
json.dump(dialect_names, open(os.path.join(OUT, "dialect_names.json"), "w"))
json.dump(familys, open(os.path.join(OUT, "famclosure.json"), "w"))

# ---- test mentions ----
reads = json.load(gzip.open(READS, "rt"))
rfiles = reads["files"]
tests = json.load(open(TESTS))["tests"]

def has_name(text, name):
    return re.search(r"\b" + re.escape(name) + r"\b", text) is not None

# fixture files read by each test
test_fixtures = {}
for f, ids in rfiles.items():
    if not f or not f.startswith("tests/fixtures"):
        continue
    for i in ids:
        test_fixtures.setdefault(i, []).append(f)

all_names = sorted({n for ns in dialect_names.values() for n in ns})

mentions = {}
for t in tests:
    hit = set()

    def scan(text, _hit=hit):
        if not text:
            return
        for n in all_names:
            if re.search(r"\b" + re.escape(n) + r"\b", text):
                _hit.add(n)

    p = os.path.join(SRC, t["file"])
    src_text = None
    try:
        src_text = open(p, encoding="utf-8").read()
        tree = ast.parse(src_text)
        parts = t["id"].split("::")[1:]
        cls_name = parts[0] if len(parts) > 1 else None
        fn_name = parts[-1] if parts else None
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if node.name == cls_name:
                    # class body includes the test function + setUp/setUpClass
                    scan(ast.get_source_segment(src_text, node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if cls_name is None and node.name == fn_name:
                    scan(ast.get_source_segment(src_text, node))
            else:
                # module-level code (assignments, imports, decorators, etc.)
                seg = ast.get_source_segment(src_text, node)
                if seg and len(seg) < 50000:
                    scan(seg)
    except Exception:
        scan(src_text)
    for f in test_fixtures.get(t["id"], ()):
        if not f.endswith((".sql", ".txt", ".json", ".csv", ".env", ".py")):
            continue
        try:
            scan(open(os.path.join(SRC, f), encoding="utf-8", errors="ignore").read())
        except Exception:
            pass
    mentions[t["id"]] = sorted(hit)

with gzip.open(os.path.join(OUT, "mentions.json.gz"), "wt") as f:
    json.dump(mentions, f)
print(
    "usage maps written:", OUT,
    "dialects:", len(dialect_names),
    "families:", {k: len(v) for k, v in list(familys.items())[:8]},
    "mentions:", len(mentions),
)
