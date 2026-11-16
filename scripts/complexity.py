#!/usr/bin/env python3
"""Compare per-function cyclomatic complexity between two Git revisions.

    python scripts/complexity.py <base-ref> <head-ref> <path.py> [<path.py> ...]

Reads each requested file from both revisions' Git objects (never the working
tree), measures it with the pinned `radon cc` over stdin, and prints one JSON
report on stdout, conforming to the team plugin's complexity report contract
(`schemaVersion: 1`). Source is parsed, never imported or executed.
See docs/complexity-reporting.md for inputs, metric, effects and failures.

Standard library only: it must be able to report a missing analyzer itself.
"""

from ast import AsyncFunctionDef, ClassDef, FunctionDef, parse
from hashlib import sha256
from json import dumps, loads
from os import environ
from pathlib import Path
from platform import python_version
from re import fullmatch
from subprocess import run
from sys import argv, executable, version_info
from tempfile import TemporaryDirectory

COMMAND_PATH = Path(__file__).resolve()
CONFIG_PATH = COMMAND_PATH.parent / 'complexity-config.json'

# Where a revision keeps its own copy of the reporting configuration.
HISTORICAL_CONFIG_PATH = 'scripts/complexity-config.json'

SCHEMA_VERSION = 1
REGULAR_FILE_MODES = ('100644', '100755')
BYTE_ORDER_MARK = b'\xef\xbb\xbf'
PATH_PATTERN = r'[^:\\]+\.py'
FUNCTION_NODE_TYPES = (FunctionDef, AsyncFunctionDef)
RADON_RECORD_TYPES = ('function', 'method')
# Statement lists that can hold nested definitions without changing the qualified name.
BLOCK_FIELDS = ('body', 'orelse', 'finalbody', 'handlers', 'cases')

USAGE = 'Usage: python scripts/complexity.py <base> <head> <path.py>... (1-{0} paths)'

LIMITS = [
    'Both revisions use the current pinned analyzer/configuration; not historical settings.',
    'Matches by file/kind/qualified name are heuristic; ambiguous, renamed or edited '
    'moved functions remain one-sided with a null delta.',
    'Functions radon does not measure (methods of classes defined inside functions) are '
    'listed per file as unmeasured; lambdas and module-level code have no records.',
    'Metrics alone establish neither a performance regression nor an actionable defect.',
]

MATCHED_REASON = 'Unique file/kind/qualified-name match; identity is heuristic.'
RELOCATED_REASON = 'Unique identical function source; inspect location/name changes.'
BEFORE_ONLY_REASON = 'Deleted, renamed, moved or ambiguous identity; comparison unavailable.'
AFTER_ONLY_REASON = 'Added, renamed, moved or ambiguous identity; comparison unavailable.'


class ReportFailure(Exception):
    """A run that cannot produce a metric report, with its contract status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def hash_bytes(value):
    return sha256(value).hexdigest()


def load_config():
    """The checked-in configuration as (raw bytes, parsed object)."""
    try:
        config_bytes = CONFIG_PATH.read_bytes()
        return config_bytes, loads(config_bytes)
    except (OSError, ValueError) as error:
        raise ReportFailure(
            'failed', 'Unreadable {0}: {1}'.format(CONFIG_PATH.name, error)
        ) from error


def is_same_config(historical_bytes, config):
    """Whether a revision's copy of the configuration matches the current one."""
    try:
        return loads(historical_bytes) == config
    except ValueError:
        return False


def is_literal_python_path(path):
    """A repository-relative, literal `.py` path: no globs, traversal or options."""
    if fullmatch(PATH_PATTERN, path) is None:
        return False
    if any(ord(character) < 32 for character in path) is True:
        return False
    if path.startswith('/') is True or path.startswith('-') is True:
        return False
    return all(part not in ('', '.', '..') for part in path.split('/'))


def is_excluded(path, config):
    return any(part in config['excludedDirectories'] for part in path.split('/'))


def git(arguments):
    """Run a read-only Git command and return its raw stdout bytes."""
    try:
        completed = run(['git', *arguments], capture_output=True)
    except FileNotFoundError as error:
        raise ReportFailure('unavailable', 'Git is unavailable: {0}'.format(error)) from error
    if completed.returncode != 0:
        raise ReportFailure(
            'failed',
            'Git operation failed: {0}: {1}'.format(
                arguments[0], completed.stderr.decode('utf-8', 'replace').strip()
            ),
        )
    return completed.stdout


def resolve_commit(reference):
    output = git(['rev-parse', '--verify', '--end-of-options', reference + '^{commit}'])
    return output.decode('utf-8').strip()


def read_blob(revision, path, config):
    """The file's bytes at `revision`, or None when the path is absent there."""
    output = git(['--literal-pathspecs', 'ls-tree', '-z', revision, '--', path])
    for entry in output.decode('utf-8').split('\0'):
        if len(entry) == 0:
            continue
        metadata, entry_path = entry.split('\t', 1)
        if entry_path != path:
            continue
        mode, object_type, object_id = metadata.split(' ')
        if mode not in REGULAR_FILE_MODES or object_type != 'blob':
            raise ReportFailure('unsupported', 'Not a regular file: {0}:{1}'.format(revision, path))
        size = int(git(['cat-file', '-s', object_id]).decode('utf-8'))
        if size > config['maxFileBytes']:
            raise ReportFailure(
                'unsupported', 'File exceeds {0} bytes: {1}'.format(config['maxFileBytes'], path)
            )
        return git(['cat-file', 'blob', object_id])
    return None


def group_by(items, key):
    """Map each key to the items carrying it, preserving input order."""
    groups = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return groups


def character_column(line_bytes, byte_offset):
    """One-based character column for an `ast` UTF-8 byte offset within a line."""
    return len(line_bytes[:byte_offset].decode('utf-8')) + 1


def function_source(lines, node):
    """The function's exact source bytes, sliced from already-split lines.

    `ast` offsets are UTF-8 byte offsets, so slicing bytes needs no conversion.
    Lines are rejoined with `\\n`, so a line-ending-only change keeps the hash.
    """
    first = node.lineno - 1
    last = node.end_lineno - 1
    if first == last:
        return lines[first][node.col_offset : node.end_col_offset]
    parts = [lines[first][node.col_offset :], *lines[first + 1 : last]]
    parts.append(lines[last][: node.end_col_offset])
    return b'\n'.join(parts)


def iter_functions(statements, prefix):
    """Yield (qualified name, function node) pairs named by `__qualname__` rules.

    Compound statements (if/for/while/with/try/match) keep the enclosing prefix;
    functions add `.<locals>.` and classes add their own name.
    """
    for statement in statements:
        if isinstance(statement, FUNCTION_NODE_TYPES) is True:
            name = prefix + statement.name
            yield name, statement
            yield from iter_functions(statement.body, name + '.<locals>.')
        elif isinstance(statement, ClassDef) is True:
            yield from iter_functions(statement.body, prefix + statement.name + '.')
        else:
            for field in BLOCK_FIELDS:
                yield from iter_functions(getattr(statement, field, []), prefix)


def iter_radon_records(records):
    """Flatten radon JSON blocks (classes, methods, closures) into function records."""
    for record in records:
        if record['type'] in RADON_RECORD_TYPES:
            yield record
        yield from iter_radon_records(record.get('methods', []))
        yield from iter_radon_records(record.get('closures', []))


def run_radon(arguments, source, working_directory):
    """Run radon in isolation so no project, user or env radon config applies.

    `-I` ignores PYTHON* variables, the user site and the current directory on
    sys.path; the empty working/home directory hides radon.cfg, setup.cfg,
    pyproject.toml and ~/.radon.cfg; RADONCFG is removed from the environment.
    """
    environment = {key: value for key, value in environ.items() if key != 'RADONCFG'}
    environment['HOME'] = working_directory
    return run(
        [executable, '-I', '-X', 'utf8', '-m', 'radon', *arguments],
        input=source,
        capture_output=True,
        cwd=working_directory,
        env=environment,
    )


def check_analyzer(config, working_directory):
    """Fail unless the pinned Python minor version and radon version are in use."""
    running_python = '{0}.{1}'.format(version_info.major, version_info.minor)
    if running_python != config['pythonVersion']:
        raise ReportFailure(
            'unsupported',
            'Expected Python {0}; running {1}'.format(config['pythonVersion'], running_python),
        )
    completed = run_radon(['--version'], b'', working_directory)
    if completed.returncode != 0:
        raise ReportFailure(
            'unavailable',
            'Pinned radon is unavailable: {0}'.format(
                completed.stderr.decode('utf-8', 'replace').strip()
            ),
        )
    installed = completed.stdout.decode('utf-8').strip()
    if installed != config['radonVersion']:
        raise ReportFailure(
            'unsupported',
            'Expected radon {0}; got {1}'.format(config['radonVersion'], installed),
        )
    return installed


def measure(source_bytes, path, revision, config, working_directory):
    """Return (function records, unmeasured functions) for one file revision."""
    # Python accepts a leading UTF-8 byte order mark; ast and radon reject it in
    # text. It precedes line 1, column 1, so removing it leaves positions intact.
    if source_bytes.startswith(BYTE_ORDER_MARK) is True:
        source_bytes = source_bytes[len(BYTE_ORDER_MARK) :]
    try:
        source = source_bytes.decode('utf-8')
    except UnicodeDecodeError:
        raise ReportFailure(
            'unsupported', 'Not UTF-8 source: {0}:{1}'.format(revision, path)
        ) from None
    try:
        tree = parse(source, filename=path)
    except SyntaxError as error:
        raise ReportFailure(
            'failed',
            '{0}:{1}: {2}:{3} {4}'.format(revision, path, error.lineno, error.offset, error.msg),
        ) from error
    except ValueError as error:
        # Python 3.11 rejects null bytes with ValueError rather than SyntaxError.
        raise ReportFailure('failed', '{0}:{1}: {2}'.format(revision, path, error)) from error

    completed = run_radon(config['radonArguments'], source_bytes, working_directory)
    if completed.returncode != 0:
        raise ReportFailure(
            'failed',
            'radon failed on {0}:{1}: {2}'.format(
                revision, path, completed.stderr.decode('utf-8', 'replace').strip()
            ),
        )
    try:
        parsed = loads(completed.stdout)
    except ValueError as error:
        raise ReportFailure(
            'failed', 'Unreadable radon output for {0}:{1}: {2}'.format(revision, path, error)
        ) from error
    if isinstance(parsed, dict) is False:
        raise ReportFailure(
            'failed', 'Unreadable radon output for {0}:{1}: {2}'.format(revision, path, parsed)
        )
    # radon prints `{}`, with no `-` entry, for source that defines no functions. Accept that
    # only when the parse agrees, so a missing measurement never reads as a clean file.
    if '-' in parsed:
        output = parsed['-']
    elif len(list(iter_functions(tree.body, ''))) == 0:
        output = []
    else:
        raise ReportFailure(
            'failed', 'radon reported no functions for {0}:{1}'.format(revision, path)
        )
    if isinstance(output, dict) is True:
        raise ReportFailure('failed', 'radon rejected {0}:{1}: {2}'.format(revision, path, output))

    complexity_by_position = {}
    for record in iter_radon_records(output):
        complexity_by_position[(record['lineno'], record['col_offset'])] = record['complexity']

    lines = source_bytes.splitlines()
    functions = []
    unmeasured = []
    for name, node in iter_functions(tree.body, ''):
        position = (node.lineno, node.col_offset)
        start = {
            'line': node.lineno,
            'column': character_column(lines[node.lineno - 1], node.col_offset),
        }
        if position not in complexity_by_position:
            unmeasured.append({'function': name, 'kind': type(node).__name__, 'start': start})
            continue
        functions.append(
            {
                'file': path,
                'revision': revision,
                'function': name,
                'kind': type(node).__name__,
                'start': start,
                'end': {
                    'line': node.end_lineno,
                    'column': character_column(lines[node.end_lineno - 1], node.end_col_offset),
                },
                'value': complexity_by_position.pop(position),
                'sourceHash': hash_bytes(function_source(lines, node)),
            }
        )
    if len(complexity_by_position) > 0:
        raise ReportFailure(
            'failed',
            'radon records without a parsed function in {0}:{1}: {2}'.format(
                revision, path, sorted(complexity_by_position)
            ),
        )
    functions.sort(key=lambda item: (item['start']['line'], item['start']['column']))
    return functions, unmeasured


def compare(before, after):
    """Pair base and head records into contract comparison rows."""
    rows = []
    paired = set()

    def identity(item):
        return (item['file'], item['kind'], item['function'])

    def source_hash(item):
        return item['sourceHash']

    def pair(left, right, status, reason):
        delta = None
        if left is not None:
            paired.add(id(left))
        if right is not None:
            paired.add(id(right))
        if left is not None and right is not None:
            delta = right['value'] - left['value']
        rows.append(
            {'status': status, 'reason': reason, 'before': left, 'after': right, 'delta': delta}
        )

    def pair_unique(key, status, reason):
        """Pair records whose key occurs exactly once on each side, in base order."""
        remaining_before = [item for item in before if id(item) not in paired]
        remaining_after = [item for item in after if id(item) not in paired]
        before_groups = group_by(remaining_before, key)
        after_groups = group_by(remaining_after, key)
        for left in remaining_before:
            candidates = after_groups.get(key(left), [])
            if len(before_groups[key(left)]) == 1 and len(candidates) == 1:
                pair(left, candidates[0], status, reason)

    pair_unique(identity, 'matched', MATCHED_REASON)
    pair_unique(source_hash, 'relocated', RELOCATED_REASON)
    for left in before:
        if id(left) not in paired:
            pair(left, None, 'before-only', BEFORE_ONLY_REASON)
    for right in after:
        if id(right) not in paired:
            pair(None, right, 'after-only', AFTER_ONLY_REASON)

    return rows


def build_report(arguments, config, config_bytes, working_directory):
    if len(arguments) < 3 or len(arguments) - 2 > config['maxFiles']:
        raise ReportFailure('failed', USAGE.format(config['maxFiles']))
    base_reference, head_reference = arguments[0], arguments[1]
    paths = arguments[2:]
    for path in paths:
        if is_literal_python_path(path) is False:
            raise ReportFailure(
                'unsupported', 'Expected a repository-relative, literal .py path: {0}'.format(path)
            )

    root = git(['rev-parse', '--show-toplevel']).decode('utf-8').strip()
    if Path(root).resolve() != Path.cwd().resolve():
        raise ReportFailure('failed', 'Run from the target repository root.')

    base = resolve_commit(base_reference)
    head = resolve_commit(head_reference)
    version = check_analyzer(config, working_directory)

    for revision in (base, head):
        historical = read_blob(revision, HISTORICAL_CONFIG_PATH, config)
        if historical is not None and is_same_config(historical, config) is False:
            raise ReportFailure(
                'incomparable',
                'Reporting configuration differs at {0}; reconcile settings before '
                'comparison.'.format(revision),
            )

    report = {
        'schemaVersion': SCHEMA_VERSION,
        'status': 'executed',
        'revisions': {'base': base, 'head': head, 'mode': 'direct'},
        'provenance': {
            'language': 'python',
            'analyzer': 'radon cc',
            'version': version,
            'metric': 'cyclomatic complexity',
            'units': 'independent source-code paths',
            'config': config,
            'configSha256': hash_bytes(config_bytes),
            'commandSha256': hash_bytes(COMMAND_PATH.read_bytes()),
            'python': python_version(),
            'git': git(['--version']).decode('utf-8').strip(),
        },
        'files': [],
        'comparisons': [],
        'limits': LIMITS,
    }

    before = []
    after = []
    for path in sorted(set(paths)):
        if is_excluded(path, config) is True:
            report['files'].append(
                {'file': path, 'status': 'excluded', 'reason': 'Generated/vendor directory policy'}
            )
            continue
        base_source = read_blob(base, path, config)
        head_source = read_blob(head, path, config)
        if base_source is None and head_source is None:
            raise ReportFailure('failed', 'Path absent from both revisions: {0}'.format(path))
        entry = {
            'file': path,
            'status': 'analyzed',
            'basePresent': base_source is not None,
            'headPresent': head_source is not None,
        }
        unmeasured = []
        for side, revision, source, records in (
            ('base', base, base_source, before),
            ('head', head, head_source, after),
        ):
            if source is None:
                continue
            functions, missing = measure(source, path, revision, config, working_directory)
            records.extend(functions)
            unmeasured.extend({'side': side, 'revision': revision, **item} for item in missing)
        if len(unmeasured) > 0:
            entry['unmeasured'] = unmeasured
        report['files'].append(entry)

    report['comparisons'] = compare(before, after)
    return report


def main():
    try:
        config_bytes, config = load_config()
        with TemporaryDirectory(prefix='complexity-radon-') as working_directory:
            report = build_report(argv[1:], config, config_bytes, working_directory)
        exit_code = 0
    except ReportFailure as failure:
        report = {'schemaVersion': SCHEMA_VERSION, 'status': failure.status, 'error': str(failure)}
        exit_code = 1
    print(dumps(report, indent=2))
    raise SystemExit(exit_code)


if __name__ == '__main__':
    main()
