"""Pytest plugin: record which repo files/dirs each test reads.

Install with: pytest tests -q -p read_audit
Writes /out/reads_raw.json at session finish:
  {"tests": {nodeid: [path...]}, "collection": {nodeid_of_file: [path...]}, "global": [path...]}

Only open/listdir/scandir/glob events are captured (C-level opens like duckdb's are invisible).
.py / .pyc / __pycache__ paths are dropped here; coverage handles python files.
"""

import json
import os
import sys

import pytest

ROOT = os.getcwd()

# The test/collected-file currently running; "global" bucket = anything before/after
_cur = {"id": None, "bucket": "global"}
_reads = {"tests": {}, "collection": {}, "global": set()}


def _record(path):
    if not isinstance(path, str):
        return
    try:
        rel = os.path.relpath(path, ROOT)
    except (ValueError, OSError):
        return
    if rel.startswith(".."):
        return
    rel = rel.replace(os.sep, "/")
    if rel.endswith(".py") or rel.endswith(".pyc") or "__pycache__" in rel:
        return
    if rel == ".coverage":
        return
    if _cur["bucket"] == "test":
        _reads["tests"].setdefault(_cur["id"], set()).add(rel)
    elif _cur["bucket"] == "collection":
        _reads["collection"].setdefault(_cur["id"], set()).add(rel)
    else:
        _reads["global"].add(rel)


def _as_dir(pattern_base):
    if pattern_base.endswith("/") or os.path.isdir(pattern_base):
        return pattern_base
    return os.path.dirname(pattern_base)


def _hook(event, args):
    if event == "open":
        mode = args[1]
        if mode is None or (isinstance(mode, str) and (mode == "" or mode[0] in "r")):
            _record(args[0])
    elif event in ("os.listdir", "os.scandir"):
        _record(args[0])
    elif event == "glob.glob":
        pattern = args[0]
        if isinstance(pattern, str):
            base = pattern.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
            _record(_as_dir(base))


sys.addaudithook(_hook)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _cur["id"] = item.nodeid
    _cur["bucket"] = "test"
    yield
    _cur["id"] = None
    _cur["bucket"] = "global"


def pytest_collect_file(file_path, parent):
    # reads made while collecting this file are attributed to it, then to its tests
    _cur["id"] = str(file_path)
    _cur["bucket"] = "collection"
    return None


def pytest_sessionfinish(session, exitstatus):
    _cur["id"] = None
    _cur["bucket"] = "global"
    out = {
        "tests": {k: sorted(v) for k, v in _reads["tests"].items()},
        "collection": {k: sorted(v) for k, v in _reads["collection"].items()},
        "global": sorted(_reads["global"]),
    }
    with open("/out/reads_raw.json", "w") as f:
        json.dump(out, f)
