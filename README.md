# Blast Radius — test-risk selector for sqlglot commits

Given a batch of patches to sqlglot (pinned commit `eb11aec`), predict for each
one which numbered tests could break, and pack them across up to 8 workers.

## How it works

1. **Map (build-time, from the pinned commit only).** Two full pytest passes at
   the pinned commit inside the scoring image produced:
   - `maps/cover.json.gz` — per-test file coverage of `sqlglot/` + `tests/`
   - `maps/lines.json.gz` — inverted line → test-ids index, identical test-sets deduped
   - `maps/reads.json.gz` — per-test file reads (pytest `sys.addaudithook` on
     `open`/`os.listdir`/`os.scandir`/`glob.glob`; `.py` excluded)
   - `maps/spans.json.gz` — function body spans per file
   - `maps/dumps.json.gz` — `ast.dump` hash of every pinned `.py` file
   - `maps/families.json` — dialect-subclass families (`issubclass`)
2. **Selector (run-time, pure stdlib).** Parses each patch, applies its hunks
   to the pinned source, and fails closed to "run all tests" on anything
   unexpected: binary patches, renames, unmapped files, unparseable new text.

   Selection rules (per-file):
   - comment/whitespace-only edits are proven no-ops via `ast.dump` hash
   - function-body edits select tests covering any line of that function
   - module/class-level edits select the file's coverage + dialect-family
     expansion + all tests of the matching `tests/dialects/test_<d>.py` files
   - fixture/data edits select their audit-hook readers plus listers of
     ancestor directories up to `tests/fixtures`; unmapped files fail closed
     to all tests

Scoring rules live in the kit's `SCORING.md`; only JSON answer lines go to
stdout, all logs to stderr.
