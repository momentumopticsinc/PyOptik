# Complexity reporting

`scripts/complexity.py` is this repository's analysis entry point for reviews of
Python changes. It compares per-function cyclomatic complexity between two Git
revisions and prints one JSON report on stdout. It is evidence for a reviewer, not
a gate: there is no threshold, and it does not run in CI.

The report format, status vocabularies, null-delta semantics, identity guidance,
comparability rules and review handoff are defined by the team plugin's complexity
report contract, `schemaVersion: 1`. This guide keeps what is specific to this
Python entry point. [CLAUDE.md](../CLAUDE.md) declares the entry point under that
contract's discovery convention.

## Analyzer and rationale

The analyzer is [radon](https://radon.readthedocs.io/) `cc` (McCabe cyclomatic
complexity). radon is the complexity tool of the team's Python template
(mo-python-template), so this repository uses the same analyzer rather than a
second one. radon's A–F ranks, maintainability index and raw metrics are not part
of the comparison; only the integer complexity per function is.

radon decides what counts. The command only adds identity and location: it parses
each file with Python's `ast` to give every function a qualified name and exact
start/end positions, then attaches radon's value to the function at the same
position.

## Command and inputs

From the repository root, with Git on PATH and the testing dependencies installed
(`pip install -e ".[testing]"`):

```sh
python scripts/complexity.py <base-ref> <head-ref> path/to/a.py path/to/b.py
```

- Supply 1–100 literal, repository-relative `.py` paths with forward slashes.
  Absolute paths, `.`/`..` segments, empty segments and paths starting with `-`
  are refused. Glob characters are not expanded: `*.py` names a file literally
  called `*.py`, so it normally fails as absent from both revisions.
- Both refs must resolve to commits already available locally; nothing is fetched.
- The comparison is **direct base versus head**, not an implicit merge base.
  Resolve and pass a merge-base commit yourself when that is the review slice.
- Only committed content is read, from Git objects. The working tree, the index and
  untracked files are never analyzed.
- For a file rename or move, pass both the old and the new path. The command does
  not discover paths you did not list. Unchanged requested files are included.
  A path absent from both revisions is an error.

## Pinned versions and configuration

[`scripts/complexity-config.json`](../scripts/complexity-config.json) pins:

| Setting | Value |
| ------- | ----- |
| `radonVersion` | **6.0.1** — also pinned in the `testing` extra of `pyproject.toml` |
| `pythonVersion` | **3.10** — the `requires-python` floor in `pyproject.toml`; the interpreter's minor version decides what syntax `ast` and radon accept |
| `radonArguments` | `cc --json --show-closures --min A --max F -` (every function, nested ones included, read from stdin) |
| `excludedDirectories` | `.venv`, `venv`, `build`, `dist`, `vendor`, `generated`, `PyOptik.egg-info`, `_version.py` (exact path segments, file names included) |
| `maxFiles` / `maxFileBytes` | 100 paths / 2 MiB per file |

The command, and therefore `PyOptik/tests/test_complexity.py`, must run on the
pinned Python minor version. `requires-python` allows newer versions for the
package, but on any other minor version the command reports `unsupported`, so those
tests fail. Run them in a Python 3.10 environment.

Each exclusion is compared with every segment of a requested path, the file name
included. `_version.py` is excluded by name because setuptools_scm generates
`PyOptik/_version.py`; passed anyway, it is reported as `excluded`.
`Examples/` and `docs/` are not excluded: they are hand-written Python and are
analyzed when passed.

Upgrading radon or Python means changing the pin in every place listed above and
the radon version stated in [CLAUDE.md](../CLAUDE.md), then rerunning
`pytest PyOptik/tests/test_complexity.py`; `test_checked_in_pins_agree` and
`test_agent_instructions_declare_the_complexity_entry_point` fail if the pins
drift apart.

If either revision contains a `scripts/complexity-config.json` that differs from
the current one, the command refuses the comparison as `incomparable`. A missing
historical config is fine, so commits from before this command can be compared.

## Supported files and syntax

UTF-8 Python source that the pinned Python version can parse, up to 2 MiB per
file. A leading UTF-8 byte order mark is accepted, as Python accepts it; any other
encoding, including one declared in a `# coding:` line, is `unsupported` unless the
bytes are also valid UTF-8. Records cover `def` and `async def` at any depth: module functions, methods,
methods of nested classes and functions nested inside other functions.

- **Names** follow `__qualname__`: `Circle.area`, `Outer.Inner.method`,
  `outer.<locals>.inner`. Same-named methods in different classes therefore stay
  distinct and are paired by their qualified name, not by position.
- **Nested functions** are separate records. An inner function's branches count
  toward the inner function, not toward the function that contains it.
- **Unmeasured functions:** radon 6.0.1 does not report methods of a class defined
  *inside a function*. The command lists them on the file entry as `unmeasured`
  (with side, revision, name and start), never as zero.
- **No records:** lambdas and module-level code (radon does not report them).
- `kind` is the `ast` node type, `FunctionDef` or `AsyncFunctionDef`.

Counting rules for statements (`if`, loops, `except` and so on) are radon's; see
its [complexity documentation](https://radon.readthedocs.io/en/latest/intro.html#cyclomatic-complexity).
The constructs below are locked in by `test_counting_rules_are_locked_in` and
probed directly against radon 6.0.1. Every function starts at 1, then:

| Construct | Adds |
| --------- | ---- |
| Boolean operators | 1 per extra operand: `a and b and c` and `a and b or c` are both +2 |
| Comprehensions | 1 per `for` clause and 1 per `if` clause |
| Conditional expression `x if c else y` | 1 |
| `match` | 1 per `case`, except a trailing wildcard `case _:` |
| `assert` | 1 (radon counts asserts unless told otherwise) |

## Output

Stdout holds one JSON object conforming to the team plugin's complexity report
contract, `schemaVersion: 1`; `assert_conforms_to_contract` in
`PyOptik/tests/test_complexity.py` checks every report the tests produce against it. The
`unmeasured` file field is this entry point's Python-specific addition beyond the
contract's required fields. A successful run exits 0 with `status: executed`,
including when every requested file is excluded or holds no functions. That means analysis
ran, not that the code is clean.

- `revisions`: full base and head commit IDs and `mode: direct`.
- `provenance`: language (`python`), analyzer (`radon cc`) and version, metric (`cyclomatic complexity`)
  and units (`independent source-code paths`), the full configuration and its
  SHA-256, the command's SHA-256, and the Python and Git versions.
- `files`: each requested path with `analyzed` (plus `basePresent`/`headPresent`
  and any `unmeasured` functions) or `excluded` (plus the reason).
- `comparisons`: one row per function with `status`, `reason`, `before`, `after`
  and `delta`. Each present side has file, revision, qualified `function` name,
  `kind`, one-based `start`/`end` line and column (end exclusive, in characters),
  integer `value` and a SHA-256 `sourceHash` of the function's source text, from
  `def` (or `async def`) to its end, excluding decorators, with lines joined by
  `\n`.
- `limits`: what the report cannot establish.

`delta` is head minus base. A function changing from `return x` to
`return 1 if x else 0` reports `before.value: 1`, `after.value: 2`, `delta: 1`.

Pairing works in three passes:

1. **`matched`** — a unique file/kind/qualified-name pair on both sides. This is a
   heuristic: a different function given the same name still matches.
2. **`relocated`** — among the rest, a unique identical `sourceHash` on both sides.
   This covers file moves and renames, and renamed enclosing classes, when the
   function itself is unchanged. Because the hash includes the `def` line, renaming
   the function itself never relocates. A method moved to a
   different indentation level has different source text and does not relocate.
3. **`before-only`** / **`after-only`** — everything left: additions, deletions,
   function renames (even with identical bodies), edited moves and ambiguous duplicates. The
   missing side is `null` and `delta` is **`null`, never zero**. Inspect these and
   explain any manual correspondence in the review.

Reports are deterministic: the same command, configuration, revisions and paths
produce byte-identical output.

## Permissions and effects

The command **parses** source; it never imports or executes it. It needs to read
this checkout's command and configuration and the repository's Git objects, and
to start local `git` and radon processes.

- It does not write to the repository, change Git state, fetch, contact the network,
  install anything or run target hooks, plugins or configuration.
- radon runs as `python -I -X utf8 -m radon` in a new, empty temporary directory
  used as both working directory and `HOME`, with `RADONCFG` removed. This stops a
  `radon.cfg`, `setup.cfg`, `pyproject.toml` `[tool.radon]` section or
  `~/.radon.cfg` from changing what is counted or shown. The temporary directory is
  removed when the command exits.
- The report contains function names, line numbers and source hashes. It is only
  saved if you redirect stdout; treat a saved report as source-derived information.

## Failure behavior

Any failure exits 1 and prints only `schemaVersion`, `status` and `error`. There is
never a partial report, and a failure is never a zero or a clean result.

| Status | When |
| ------ | ---- |
| `failed` | Wrong argument count; unknown ref; path absent from both revisions; not run from the repository root; syntax error or other parser rejection; radon error; unreadable configuration; Git failure |
| `unavailable` | radon is not installed for the running interpreter; `git` is not on PATH |
| `unsupported` | Path is not a literal repository-relative `.py`; Python minor version differs from `pythonVersion`; radon version differs from `radonVersion`; non-UTF-8 source; file over the size limit; path is not a regular file (symlink, submodule, directory) |
| `incomparable` | A revision's `scripts/complexity-config.json` differs from the current one |

Restore the missing prerequisite (for example `pip install -e ".[testing]"` on the
pinned Python), then rerun the same command. Do not substitute another analyzer or
version.

## Review handoff

A reviewer finds this command through the `## Complexity report` declaration in
[CLAUDE.md](../CLAUDE.md). Attach the command,
requested paths, exclusions, revision IDs, provenance, per-function rows,
`unmeasured` entries and limits to the tooling part of the review. Read changes
against the source: a metric alone never shows a defect or a measured performance
regression. `test_misleading_unchanged_metric_hides_quadratic_work` is the example:
replacing `rows[x]` with `rows.index(x)` inside a comprehension keeps the
complexity unchanged while the work grows from linear to quadratic.

If the run is `unavailable`, `unsupported`, `failed` or `incomparable`, keep the
source review and state the evidence gap.

## Tests

`pytest PyOptik/tests/test_complexity.py` builds throwaway Git repositories from synthetic
source, runs this exact command as a subprocess, and validates every report against
the contract. It covers a 1→2 change, unchanged values, a misleading metric,
additions, deletions, renames and moves, same-named methods in different classes,
nested and unmeasured functions, the counting rules above, repeated-run
determinism, and each non-executed status: invalid input, running outside the
root, syntax errors, non-UTF-8 source (and an accepted byte order mark), oversized
and non-regular files, incomparable configuration, wrong radon or
Python versions, and radon missing from a fresh virtual environment.
