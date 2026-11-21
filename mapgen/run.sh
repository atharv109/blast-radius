#!/bin/sh
# Two pytest passes inside the pinned image, then map generation.
set -x
export PYTHONPATH=/mapgen:/src
cd /src

SKIP_INTEGRATION=0 pytest tests --cov=sqlglot --cov=tests --cov-context=test -q 2>&1 | tail -5
SKIP_INTEGRATION=0 pytest tests -q -p read_audit 2>&1 | tail -5

SKIP_INTEGRATION=0 python /mapgen/gen_map.py
ls -la /out
