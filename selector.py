"""Blast Radius selector — phase 1 (file/function-level selection).

For each commit patch, decide which numbered tests could break, then pack them
across up to 8 workers by longest-processing-time-first and print one JSON line
per commit. Every failure mode fails closed to running all tests.
"""

import ast
import gzip
import hashlib
import json
import os
import re
import sys

DATA = "/data"
SRC = "/src"          # sqlglot checkout at the pinned commit, laid down at build
MAPS = "/app/maps"    # committed map files

DIALECT_DIRS = (
    "sqlglot/dialects",
    "sqlglot/generators",
    "sqlglot/parsers",
    "sqlglot/tokenizers",
    "sqlglot/typing",
)

HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class AllError(Exception):
    """Raised to force "run all tests" for this commit."""


def load_json_gz(path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def sha1(text):
    return hashlib.sha1(text.encode()).hexdigest()


def func_spans(tree):
    """Sorted (start, end) spans of every function body (first body line to end line)."""
    out = []

    def walk(node):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if ch.body:
                    out.append((min(s.lineno for s in ch.body), ch.end_lineno))
            walk(ch)

    walk(tree)
    return sorted(out)


def strip_prefix(p):
    if p in (None, "/dev/null"):
        return None
    if p.startswith('"') and p.endswith('"'):
        p = json.loads(p)
    if p.startswith("a/"):
        p = p[2:]
    elif p.startswith("b/"):
        p = p[2:]
    return p


def parse_diff(text):
    """Parse a unified diff into [{old, new, created, deleted, rename, binary, hunks}]."""
    entries = []
    cur = None
    in_body = False
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # artifact of a trailing newline, never a hunk body line
    for raw in lines:
        if raw.startswith("diff --git ") or (cur is None and raw.startswith("--- ")):
            if cur:
                entries.append(cur)
                cur = None
            if raw.startswith("diff --git "):
                cur = {"old": None, "new": None, "created": False, "deleted": False,
                       "rename": False, "binary": False, "hunks": []}
            in_body = False
        elif cur is None:
            continue
        else:
            if in_body:
                c = raw[0] if raw else " "
                if raw and c == "\\":
                    continue
                if raw == "" or c in "+- ":
                    cur["hunks"][-1]["body"].append(raw if raw else " ")
                    continue
                in_body = False
            if raw.startswith("@@"):
                m = HUNK_RE.match(raw)
                if m:
                    cur["hunks"].append({
                        "old_start": int(m[1]), "old_cnt": int(m[2] or 1),
                        "new_start": int(m[3]), "new_cnt": int(m[4] or 1),
                        "body": [],
                    })
                    in_body = True
            elif raw.startswith("--- "):
                cur["old"] = strip_prefix(raw[4:].strip())
            elif raw.startswith("+++ "):
                cur["new"] = strip_prefix(raw[4:].strip())
            elif raw.startswith("new file mode"):
                cur["created"] = True
            elif raw.startswith("deleted file mode"):
                cur["deleted"] = True
            elif raw.startswith("rename from "):
                cur["rename"] = True
                cur["old"] = raw[12:].strip()
            elif raw.startswith("rename to "):
                cur["rename"] = True
                cur["new"] = raw[11:].strip()
            elif raw.startswith(("Binary files", "GIT binary patch")):
                cur["binary"] = True
    if cur:
        entries.append(cur)
    return entries


def innermost(spans, ln):
    best = None
    best_w = None
    for a, b in spans:
        if a <= ln <= b and (best_w is None or b - a < best_w):
            best, best_w = (a, b), b - a
    return best


def changed_lines(hunks):
    """1-based changed line numbers on each side (removed+context / added+context)."""
    old_ch, new_ch = set(), set()
    for h in hunks:
        opos = h["old_start"] - 1
        npos = h["new_start"] - 1
        for raw in h["body"]:
            c = raw[0] if raw else " "
            if c == "-":
                old_ch.add(opos + 1)
                opos += 1
            elif c == "+":
                new_ch.add(npos + 1)
                npos += 1
            else:
                old_ch.add(opos + 1)
                opos += 1
                new_ch.add(npos + 1)
                npos += 1
    return old_ch, new_ch


def apply_hunks(old_lines, hunks):
    """Apply hunks to old lines (no trailing newlines). Raises AllError on mismatch."""
    result = []
    pos = 0
    for h in hunks:
        ostart = h["old_start"] - 1 if h["old_start"] > 0 else 0
        if ostart < pos:
            raise AllError()  # overlapping hunks
        result.extend(old_lines[pos:ostart])
        pos = ostart
        i = pos
        for raw in h["body"]:
            c = raw[0] if raw else " "
            content = raw[1:] if raw else ""
            if c == " ":
                if i >= len(old_lines) or old_lines[i] != content:
                    raise AllError()
                result.append(old_lines[i])
                i += 1
            elif c == "-":
                if i >= len(old_lines) or old_lines[i] != content:
                    raise AllError()
                i += 1
            elif c == "+":
                result.append(content)
        pos = i
    result.extend(old_lines[pos:])
    return result


def pack(nums, dur):
    """Choose w in 1..8 minimizing max load + 2000*w, via LPT."""
    if not nums:
        return []
    nums = sorted(nums, key=lambda n: -dur.get(n, 0))
    best, best_cost = None, None
    for w in range(1, 9):
        loads = [0] * w
        bins = [[] for _ in range(w)]
        for n in nums:
            i = loads.index(min(loads))
            bins[i].append(n)
            loads[i] += dur.get(n, 0)
        cost = max(loads) + 2000 * w
        if best is None or cost < best_cost:
            best, best_cost = bins, cost
    return best


def main():
    test_list = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))["tests"]
    num_of = {t["id"]: t["number"] for t in test_list}
    dur = {t["number"]: round(t["seconds"] * 1000) for t in test_list}
    file_nums = {}
    for t in test_list:
        file_nums.setdefault(t["file"], []).append(t["number"])

    cover = load_json_gz(os.path.join(MAPS, "cover.json.gz"))
    reads = load_json_gz(os.path.join(MAPS, "reads.json.gz"))
    families = json.load(open(os.path.join(MAPS, "families.json")))
    spans = load_json_gz(os.path.join(MAPS, "spans.json.gz"))
    dumps = load_json_gz(os.path.join(MAPS, "dumps.json.gz"))
    blind_ids = json.load(open(os.path.join(MAPS, "blind.json")))
    blind = {num_of[i] for i in blind_ids if i in num_of}  # numbers
    rfiles, rdirs = reads["files"], reads["dirs"]
    lines_cache = {}

    def to_nums(ids):
        return {num_of[i] for i in ids if i in num_of}

    def file_level_ids(rel):
        got = to_nums(cover.get(rel, ()))
        stem = rel.rsplit("/", 1)[-1][:-3]
        parent = rel.rsplit("/", 1)[0]
        if parent in DIALECT_DIRS and stem in families:
            for m in families[stem]:
                for d in DIALECT_DIRS:
                    got.update(to_nums(cover.get(f"{d}/{m}.py", ())))
                got.update(file_nums.get(f"tests/dialects/test_{m}.py", ()))
            # Cross-dialect consistency suite (TestDialect) exercises any
            # dialect's class attributes; import-time lines give it no
            # per-test coverage of a dialect module.
            got.update(file_nums.get("tests/dialects/test_dialect.py", ()))
        return got  # numbers

    def data_ids(rel):
        """Readers of the file + listers of ancestor dirs up to tests/fixtures.
        Unmapped file -> AllError -> all tests."""
        if rel not in rfiles and rel not in rdirs:
            raise AllError()
        got = set()
        for i in rfiles.get(rel, ()):
            if i in num_of:
                got.add(num_of[i])
        d = os.path.dirname(rel)
        while d:
            for i in rdirs.get(d, ()):
                if i in num_of:
                    got.add(num_of[i])
            if d == "tests/fixtures":
                break
            d = os.path.dirname(d)
        return got

    def level_of(spans_list, ch):
        for ln in ch:
            if innermost(spans_list, ln) is None:
                return "module"
        return "function"

    def line_ids(rel, span):
        lm = lines_cache.get(rel)
        if lm is None:
            data = load_json_gz(os.path.join(MAPS, "lines.json.gz"))
            sets = data["sets"]
            lm = (data["files"].get(rel, {}), sets)
            lines_cache[rel] = lm
        fm, sets = lm
        out = set()
        for ln in range(span[0], span[1] + 1):
            si = fm.get(str(ln))
            if si is not None:
                out.update(sets[si])
        return out

    def select_py(rel, e):
        if rel not in spans:
            raise AllError()
        try:
            with open(os.path.join(SRC, rel), "rb") as f:
                old_text = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise AllError()
        try:
            old_lines = old_text.splitlines()
            new_lines = apply_hunks(old_lines, e["hunks"])
        except AllError:
            raise
        except Exception:
            raise AllError()
        new_text = "\n".join(new_lines)
        try:
            tree = ast.parse(new_text)
        except (SyntaxError, ValueError):
            raise AllError()
        if dumps.get(rel) == sha1(ast.dump(tree)):
            return set()

        blind_here = blind if rel.startswith("sqlglot/") else set()

        old_spans = spans.get(rel, [])
        new_spans = func_spans(tree)
        old_ch, new_ch = changed_lines(e["hunks"])

        def flevel():
            got = file_level_ids(rel) | blind_here
            if rel.startswith("tests/"):
                got.update(file_nums.get(rel, ()))
            return got

        if level_of(old_spans, old_ch) == "module" or level_of(new_spans, new_ch) == "module":
            if rel.startswith("sqlglot/"):
                # Import-time code (class attrs, top-level statements) runs once
                # at module import and belongs to no test's coverage context, so
                # a per-test line map can never attribute its blast radius.
                # Fail closed: run everything.
                raise AllError()
            return flevel()

        # Function-level coverage uses OLD-side lines only: the hunk's old
        # context sits in the same old function as any insertion, and new-file
        # line numbers are not comparable to the old-file lines map.
        sel = set(blind_here)
        for ln in old_ch:
            span = innermost(old_spans, ln)
            if span is None:
                return flevel()
            sel.update(to_nums(line_ids(rel, span)))
        return sel

    def select_file(e):
        old, new = e["old"], e["new"]
        if e["binary"] or e["rename"]:
            raise AllError()
        if old is None:  # created: any new file -> all tests
            raise AllError()
        if new is None:  # deleted: any removed file -> all tests
            raise AllError()
        if old != new:
            raise AllError()  # treat in-place rename as conservative
        if old.endswith(".py") and (old.startswith("sqlglot/") or old.startswith("tests/")):
            return select_py(old, e)
        if old.endswith(".py"):
            return data_ids(old)  # scripts/, benchmarks/ python: reads map only
        return data_ids(old)

    patches = sorted(os.listdir(os.path.join(DATA, "commits")))
    all_nums = set(num_of.values())
    for patch in patches:
        commit = patch[:-6]
        sel = None  # None = all tests
        try:
            with open(os.path.join(DATA, "commits", patch), "rb") as f:
                text = f.read().decode("utf-8")
            entries = parse_diff(text)
            sel = set()
            for e in entries:
                r = select_file(e)
                if r is None:  # library-code change -> all tests
                    sel = None
                    break
                sel |= r
        except AllError:
            sel = None
        except Exception as ex:
            print(f"selector error on {commit}: {ex!r}", file=sys.stderr)
            sel = None
        if sel is None:
            nums = all_nums
        else:
            nums = sorted(sel)
        workers = pack(nums, dur)
        print(json.dumps({"commit": commit, "workers": workers}))

    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
