"""Build the selector maps from one pinned-source test run.

Inputs: /src (sqlglot checkout), /src/.coverage (pytest-cov contexts run),
/out/reads_raw.json (read_audit plugin run), /map/tests.json (runtime test ids).
Outputs to /out/: cover.json.gz, lines.json.gz, reads.json.gz, families.json,
spans.json.gz, dumps.json.gz — all keyed by test nodeid, never by runtime number.
"""

import ast
import gzip
import importlib
import json
import os
import pkgutil
import sys
import tarfile
import tempfile
import zipfile
import hashlib

SRC = "/src"
OUT = "/out"
PY_DIRS = ("sqlglot/", "tests/")


def gz(path, obj):
    with gzip.open(os.path.join(OUT, path), "wt") as f:
        json.dump(obj, f, separators=(",", ":"))


def j(path, obj):
    with open(os.path.join(OUT, path), "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def main():
    os.makedirs(OUT, exist_ok=True)
    tests = json.load(open("/map/tests.json"))["tests"]
    ids = [t["id"] for t in tests]
    id_set = set(ids)

    # ---------------------------------------------------------------- coverage
    from coverage import CoverageData

    cd = CoverageData(basename=os.path.join(SRC, ".coverage"))
    cd.read()

    def context_ids(stem):
        if stem in id_set:
            return [stem]
        # parametrized contexts look like <nodeid>[param]
        out = [i for i in ids if stem.startswith(i + "[")]
        return out

    # file -> ids covering any line (from line data)
    cover = {}  # file -> set(ids)
    # inverted line map, deduped by identical id-set
    line_sets = []  # list of sorted id-lists
    set_index = {}  # id-tuple -> index
    lines_out = {}  # rel file -> {line: set_index}

    measured = list(cd.measured_files())
    contexts = list(cd.measured_contexts())
    ctx_map = {}  # context string -> list of ids
    unknown = 0
    for ctx in contexts:
        if not ctx:
            continue
        stem = ctx.rsplit("|", 1)[0]
        got = context_ids(stem)
        if not got:
            unknown += 1
        ctx_map[ctx] = got
    print(f"contexts: {len(contexts)}, unmatched: {unknown}", file=sys.stderr)

    for f in measured:
        rel = os.path.relpath(f, SRC).replace(os.sep, "/")
        if not rel.startswith(PY_DIRS):
            continue
        covered = {}
        for ctx in contexts:
            got = ctx_map.get(ctx)
            if not got:
                continue
            lines = cd.lines(f, context=ctx) or []
            for ln in lines:
                covered.setdefault(ln, set()).update(got)
        if covered:
            cover[rel] = set()
            for ln, idset in covered.items():
                key = tuple(sorted(idset))
                idx = set_index.get(key)
                if idx is None:
                    idx = len(line_sets)
                    set_index[key] = idx
                    line_sets.append(sorted(idset))
                cover[rel].update(idset)
        lines_map = {str(ln): set_index[tuple(sorted(idset))] for ln, idset in sorted(covered.items())}
        if lines_map:
            lines_out[rel] = lines_map

    line_sets_out = line_sets
    cover_out = {k: sorted(v) for k, v in cover.items()}
    print(f"cover: {len(cover_out)} files, {len(line_sets_out)} distinct sets", file=sys.stderr)

    # ---------------------------------------------------------------- reads
    raw = json.load(open(os.path.join(OUT, "reads_raw.json")))
    # global reads apply to every test
    g = set(raw.get("global", []))
    reads_files = {}  # path -> set(ids)
    reads_dirs = {}  # dir -> set(ids)
    for nodeid, paths in raw.get("tests", {}).items():
        got = context_ids(nodeid)
        if not got:
            got = [i for i in ids if nodeid.startswith(i + "[")]
        for p in paths:
            reads_files.setdefault(p, set()).update(got)
    # collection reads: path -> the collected test file's tests
    file_to_ids = {}
    for t in tests:
        file_to_ids.setdefault(t["file"], set()).add(t["id"])
    for nodeid, paths in raw.get("collection", {}).items():
        base = nodeid[len(SRC) + 1 :] if nodeid.startswith(SRC) else nodeid
        own = file_to_ids.get(base, set())
        for p in paths:
            reads_files.setdefault(p, set()).update(own)
    for p in g:
        reads_files.setdefault(p, set()).update(ids)
    # move dir-shaped entries into reads_dirs
    for p in list(reads_files):
        if p.endswith("/") or os.path.isdir(os.path.join(SRC, p)):
            reads_dirs.setdefault(p, set()).update(reads_files.pop(p))
    reads = {
        "files": {k: sorted(v) for k, v in sorted(reads_files.items())},
        "dirs": {k: sorted(v) for k, v in sorted(reads_dirs.items())},
    }
    print(f"reads: {len(reads['files'])} files, {len(reads['dirs'])} dirs", file=sys.stderr)

    # ---------------------------------------------------------------- families
    import sqlglot
    from sqlglot.dialects import Dialect

    classes = {}
    for m in pkgutil.iter_modules(sqlglot.dialects.__path__):
        mod = importlib.import_module(f"sqlglot.dialects.{m.name}")
        for name in dir(mod):
            obj = getattr(mod, name)
            if isinstance(obj, type) and issubclass(obj, Dialect) and obj.__module__ == mod.__name__:
                classes.setdefault(m.name, obj)
    families = {}
    for name, cls in classes.items():
        fam = sorted(m2 for m2, c2 in classes.items() if issubclass(c2, cls))
        families[name] = fam
    print(f"families: { {k: v for k, v in sorted(families.items()) if k in ('hive','spark','databricks','tsql')} }", file=sys.stderr)

    # ---------------------------------------------------------------- spans + dumps
    spans = {}
    dumps = {}

    def func_spans(tree):
        out = []

        def walk(node):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min(getattr(s, "lineno", 10**9) for s in ch.body) if ch.body else ch.lineno
                    out.append((start, ch.end_lineno or ch.lineno))
                    walk(ch)
                elif isinstance(ch, ast.ClassDef):
                    walk(ch)
                else:
                    walk(ch)

        walk(tree)
        return sorted(out)

    for base, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            if not rel.startswith(PY_DIRS):
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            try:
                tree = ast.parse(data)
            except SyntaxError:
                continue
            dumps[rel] = hashlib.sha1(ast.dump(tree).encode()).hexdigest()
            spans[rel] = func_spans(tree)

    # ---------------------------------------------------------------- write
    gz("cover.json.gz", cover_out)
    gz("lines.json.gz", {"sets": line_sets, "files": lines_out})
    gz("reads.json.gz", reads)
    j("families.json", families)
    gz("spans.json.gz", spans)
    gz("dumps.json.gz", dumps)
    print("maps written", file=sys.stderr)


if __name__ == "__main__":
    main()
