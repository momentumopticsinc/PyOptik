"""Behavioral tests for the public `scripts/complexity.py` command.

Every test builds an isolated Git history from synthetic source in a temporary
directory, runs the real command as a subprocess from that repository's root, and
checks the JSON report it prints. Nothing here imports the command.
"""

from json import dumps, loads
from pathlib import Path
from re import escape, fullmatch, search
from shutil import copyfile
from subprocess import run
from sys import executable, platform
from venv import EnvBuilder

from pytest import fixture

# This file lives in PyOptik/tests/, two levels below the repository root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = REPOSITORY_ROOT / 'scripts' / 'complexity.py'
CONFIG_PATH = REPOSITORY_ROOT / 'scripts' / 'complexity-config.json'

# Vocabularies from the schemaVersion 1 complexity report contract.
RUN_STATUSES = frozenset({'executed', 'failed', 'unavailable', 'unsupported', 'incomparable'})
FAILURE_STATUSES = RUN_STATUSES - {'executed'}
COMPARISON_STATUSES = frozenset({'matched', 'relocated', 'before-only', 'after-only'})
FILE_STATUSES = frozenset({'analyzed', 'excluded'})
# Provenance checked as nonempty text: the five fields the contract requires, then this
# command's runtime-version additions.
PROVENANCE_TEXT_FIELDS = ('language', 'analyzer', 'version', 'metric', 'units', 'python', 'git')
EXECUTED_ONLY_FIELDS = frozenset({'revisions', 'provenance', 'files', 'comparisons', 'limits'})
PROVENANCE_KEYS = frozenset(
    {
        'language',
        'analyzer',
        'version',
        'metric',
        'units',
        'config',
        'configSha256',
        'commandSha256',
        'python',
        'git',
    }
)
SIDE_KEYS = frozenset(
    {'file', 'revision', 'function', 'kind', 'start', 'end', 'value', 'sourceHash'}
)
COMMIT_PATTERN = r'[0-9a-f]{40}'
SHA256_PATTERN = r'[0-9a-f]{64}'


def assert_text(value):
    assert isinstance(value, str) is True
    assert len(value) > 0


def assert_integer(value):
    # `type` rather than `isinstance`: a JSON boolean is not a contract integer.
    assert type(value) is int


def assert_position(position):
    assert set(position.keys()) == {'line', 'column'}
    assert_integer(position['line'])
    assert_integer(position['column'])
    assert position['line'] >= 1
    assert position['column'] >= 1


def assert_side(side, report):
    assert set(side.keys()) == SIDE_KEYS
    for field in ('file', 'revision', 'function', 'kind'):
        assert_text(side[field])
    assert side['revision'] in (report['revisions']['base'], report['revisions']['head'])
    assert_position(side['start'])
    assert_position(side['end'])
    assert_integer(side['value'])
    assert side['value'] >= 1
    assert fullmatch(SHA256_PATTERN, side['sourceHash']) is not None


def assert_comparison(row, report):
    assert row['status'] in COMPARISON_STATUSES
    assert_text(row['reason'])
    if row['before'] is not None:
        assert_side(row['before'], report)
        assert row['before']['revision'] == report['revisions']['base']
    if row['after'] is not None:
        assert_side(row['after'], report)
        assert row['after']['revision'] == report['revisions']['head']
    if row['status'] in ('matched', 'relocated'):
        assert row['before'] is not None
        assert row['after'] is not None
        assert row['delta'] == row['after']['value'] - row['before']['value']
    elif row['status'] == 'before-only':
        assert row['before'] is not None
        assert row['after'] is None
        assert row['delta'] is None
    else:
        assert row['before'] is None
        assert row['after'] is not None
        assert row['delta'] is None


def assert_file_entry(file_entry, report):
    assert file_entry['status'] in FILE_STATUSES
    assert_text(file_entry['file'])
    if file_entry['status'] == 'excluded':
        assert set(file_entry.keys()) == {'file', 'status', 'reason'}
        assert_text(file_entry['reason'])
        return
    assert isinstance(file_entry['basePresent'], bool) is True
    assert isinstance(file_entry['headPresent'], bool) is True
    assert file_entry['basePresent'] is True or file_entry['headPresent'] is True
    # `unmeasured` is this command's Python-specific addition to the contract.
    assert set(file_entry.keys()).issubset(
        {'file', 'status', 'basePresent', 'headPresent', 'unmeasured'}
    )
    for item in file_entry.get('unmeasured', []):
        assert item['side'] in ('base', 'head')
        assert item['revision'] == report['revisions'][item['side']]
        assert_text(item['function'])
        assert_position(item['start'])


def assert_conforms_to_contract(report):
    """Check a report against the team plugin's complexity report contract, schemaVersion 1."""
    assert_integer(report['schemaVersion'])
    assert report['schemaVersion'] == 1
    assert report['status'] in RUN_STATUSES

    if report['status'] in FAILURE_STATUSES:
        # A failure is never a partial or zero-valued metric report.
        assert set(report.keys()) == {'schemaVersion', 'status', 'error'}
        assert_text(report['error'])
        return

    assert 'error' not in report
    assert EXECUTED_ONLY_FIELDS.issubset(report.keys()) is True
    assert fullmatch(COMMIT_PATTERN, report['revisions']['base']) is not None
    assert fullmatch(COMMIT_PATTERN, report['revisions']['head']) is not None
    assert report['revisions']['mode'] == 'direct'
    assert PROVENANCE_KEYS.issubset(report['provenance'].keys()) is True
    for field in PROVENANCE_TEXT_FIELDS:
        assert_text(report['provenance'][field])
    assert report['provenance']['config'] is not None
    assert fullmatch(SHA256_PATTERN, report['provenance']['configSha256']) is not None
    assert fullmatch(SHA256_PATTERN, report['provenance']['commandSha256']) is not None
    for field in ('files', 'comparisons', 'limits'):
        assert isinstance(report[field], list) is True
    for file_entry in report['files']:
        assert_file_entry(file_entry, report)
    for row in report['comparisons']:
        assert_comparison(row, report)
    assert len(report['limits']) > 0
    for limit in report['limits']:
        assert_text(limit)


def rows_by_function(report):
    """Comparison rows keyed by the qualified name present on either side."""
    rows = {}
    for row in report['comparisons']:
        side = row['before'] if row['before'] is not None else row['after']
        rows[(row['status'], side['function'])] = row
    return rows


def git(repository, *arguments):
    completed = run(['git', *arguments], cwd=repository, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


class SyntheticRepository:
    """A throwaway Git repository plus a way to run the command inside it."""

    def __init__(self, root):
        self.root = root
        git(root, 'init', '--quiet')
        git(root, 'config', 'user.name', 'Synthetic Test')
        git(root, 'config', 'user.email', 'synthetic@example.invalid')
        git(root, 'config', 'commit.gpgsign', 'false')
        git(root, 'config', 'core.hooksPath', '/dev/null')
        git(root, 'config', 'core.autocrlf', 'false')

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def remove(self, name):
        (self.root / name).unlink()

    def commit(self):
        git(self.root, 'add', '--all')
        git(self.root, 'commit', '--quiet', '--allow-empty', '-m', 'synthetic')
        return git(self.root, 'rev-parse', 'HEAD')

    def run(self, arguments, command_path=COMMAND_PATH, interpreter=executable):
        completed = run(
            [str(interpreter), str(command_path), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        assert completed.stderr == ''
        report = loads(completed.stdout)
        assert_conforms_to_contract(report)
        return completed.returncode, report


def assert_not_executed(code, report, status, message_pattern):
    assert code == 1
    assert report['status'] == status
    assert search(message_pattern, report['error']) is not None
    assert 'comparisons' not in report


def copy_command_with_config(directory, **overrides):
    """Copy the command beside a modified configuration, as a tool checkout would be."""
    directory.mkdir()
    command_path = directory / 'complexity.py'
    copyfile(COMMAND_PATH, command_path)
    config = loads(CONFIG_PATH.read_text(encoding='utf-8'))
    config.update(overrides)
    (directory / 'complexity-config.json').write_text(dumps(config), encoding='utf-8')
    return command_path


@fixture
def repository(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    return SyntheticRepository(root)


def test_reports_a_one_to_two_change_and_an_unchanged_function(repository):
    repository.write(
        'sample.py', 'def classify(x):\n    return x\n\n\ndef stable(x):\n    return x\n'
    )
    base = repository.commit()
    repository.write(
        'sample.py',
        'def classify(x):\n    return 1 if x else 0\n\n\ndef stable(x):\n    return x\n',
    )
    head = repository.commit()

    code, report = repository.run([base, head, 'sample.py'])

    assert code == 0
    assert report['status'] == 'executed'
    assert report['revisions'] == {'base': base, 'head': head, 'mode': 'direct'}
    assert report['provenance']['language'] == 'python'
    assert report['provenance']['analyzer'] == 'radon cc'
    assert report['provenance']['version'] == report['provenance']['config']['radonVersion']
    assert report['files'] == [
        {'file': 'sample.py', 'status': 'analyzed', 'basePresent': True, 'headPresent': True}
    ]
    rows = rows_by_function(report)
    changed = rows[('matched', 'classify')]
    assert (changed['before']['value'], changed['after']['value'], changed['delta']) == (1, 2, 1)
    assert changed['after']['kind'] == 'FunctionDef'
    assert changed['after']['start'] == {'line': 1, 'column': 1}
    assert changed['after']['end'] == {'line': 2, 'column': 25}
    unchanged = rows[('matched', 'stable')]
    assert (unchanged['before']['value'], unchanged['after']['value'], unchanged['delta']) == (
        1,
        1,
        0,
    )


def test_repeated_runs_on_the_same_snapshots_are_identical(repository):
    repository.write('a.py', 'class A:\n    def run(self, x):\n        return x and 1\n')
    repository.write('b.py', 'def gone():\n    return 0\n')
    base = repository.commit()
    repository.write('a.py', 'class A:\n    def run(self, x):\n        return x or 1\n')
    repository.remove('b.py')
    head = repository.commit()

    first = repository.run([base, head, 'b.py', 'a.py'])
    second = repository.run([base, head, 'b.py', 'a.py'])

    assert first == second


def test_misleading_unchanged_metric_hides_quadratic_work(repository):
    repository.write('lookup.py', 'def lookup(xs, rows):\n    return [rows[x] for x in xs]\n')
    base = repository.commit()
    repository.write('lookup.py', 'def lookup(xs, rows):\n    return [rows.index(x) for x in xs]\n')
    head = repository.commit()

    code, report = repository.run([base, head, 'lookup.py'])

    assert code == 0
    row = rows_by_function(report)[('matched', 'lookup')]
    # A dict lookup became a linear scan per element, yet the metric does not move.
    assert row['delta'] == 0
    assert row['before']['sourceHash'] != row['after']['sourceHash']
    assert any('performance' in limit for limit in report['limits']) is True


def test_additions_deletions_renames_and_moves_are_explicit(repository):
    repository.write('old.py', 'def moved(x):\n    return x\n')
    repository.write('sample.py', 'def removed():\n    pass\n\n\ndef old_name(x):\n    return x\n')
    base = repository.commit()
    repository.remove('old.py')
    repository.write('pkg/new.py', 'def moved(x):\n    return x\n')
    repository.write('sample.py', 'def added():\n    pass\n\n\ndef new_name(x):\n    return x\n')
    head = repository.commit()

    code, report = repository.run(
        [base, head, 'old.py', 'pkg/new.py', 'sample.py', 'vendor/ignored.py']
    )

    assert code == 0
    rows = rows_by_function(report)
    moved = rows[('relocated', 'moved')]
    assert (moved['before']['file'], moved['after']['file'], moved['delta']) == (
        'old.py',
        'pkg/new.py',
        0,
    )
    # The source hash includes the `def` line, so a symbol rename is never relocated,
    # even with an identical body; only file moves and renames relocate.
    assert rows[('before-only', 'old_name')]['delta'] is None
    assert rows[('after-only', 'new_name')]['delta'] is None
    assert rows[('before-only', 'removed')]['delta'] is None
    assert rows[('after-only', 'added')]['delta'] is None
    one_sided = [row for row in report['comparisons'] if row['status'].endswith('-only')]
    assert all(row['delta'] is None for row in one_sided) is True
    files = {entry['file']: entry for entry in report['files']}
    assert files['old.py'] == {
        'file': 'old.py',
        'status': 'analyzed',
        'basePresent': True,
        'headPresent': False,
    }
    assert files['vendor/ignored.py']['status'] == 'excluded'


def test_files_without_functions_are_analyzed_with_no_rows(repository):
    # radon prints `{}` rather than an empty `-` entry when a file has no functions.
    repository.write('package/__init__.py', '')
    repository.write('package/constants.py', '"""Constants only."""\n\nLIMIT = 3\n')
    base = repository.commit()
    repository.write('package/constants.py', '"""Constants only."""\n\nLIMIT = 4\n')
    head = repository.commit()

    code, report = repository.run([base, head, 'package/__init__.py', 'package/constants.py'])

    assert code == 0
    assert report['status'] == 'executed'
    assert {(entry['file'], entry['status']) for entry in report['files']} == {
        ('package/__init__.py', 'analyzed'),
        ('package/constants.py', 'analyzed'),
    }
    assert report['comparisons'] == []


def test_renamed_symbol_with_edited_body_is_not_paired(repository):
    repository.write('sample.py', 'def old_name(x):\n    return x\n')
    base = repository.commit()
    repository.write('sample.py', 'def new_name(x):\n    return x or 0\n')
    head = repository.commit()

    code, report = repository.run([base, head, 'sample.py'])

    assert code == 0
    assert sorted(rows_by_function(report).keys()) == [
        ('after-only', 'new_name'),
        ('before-only', 'old_name'),
    ]


def test_same_named_methods_pair_by_qualified_name_not_position(repository):
    repository.write(
        'shapes.py',
        'class Circle:\n    def area(self):\n        return 1\n\n\n'
        'class Square:\n    def area(self):\n        return 2\n',
    )
    base = repository.commit()
    repository.write(
        'shapes.py',
        'class Square:\n    def area(self):\n        return 2\n\n\n'
        'class Circle:\n    def area(self):\n'
        '        if self:\n            return 1\n        return 0\n',
    )
    head = repository.commit()

    code, report = repository.run([base, head, 'shapes.py'])

    assert code == 0
    rows = rows_by_function(report)
    assert set(rows.keys()) == {('matched', 'Circle.area'), ('matched', 'Square.area')}
    assert rows[('matched', 'Circle.area')]['delta'] == 1
    assert rows[('matched', 'Square.area')]['delta'] == 0
    assert rows[('matched', 'Circle.area')]['after']['start'] == {'line': 7, 'column': 5}


def test_nested_functions_use_the_nesting_path_and_unmeasured_ones_are_listed(repository):
    source = (
        'def outer(x):\n'
        '    def inner(y):\n'
        '        return y\n'
        '\n'
        '    class Local:\n'
        '        def method(self):\n'
        '            return 1\n'
        '\n'
        '    return inner(x)\n'
        '\n'
        '\n'
        'class Holder:\n'
        '    async def run(self, x):\n'
        '        def helper():\n'
        '            return x\n'
        '\n'
        '        return helper()\n'
        '\n'
        '    class Inner:\n'
        '        def deep(self):\n'
        '            return self\n'
    )
    repository.write('nested.py', source)
    base = repository.commit()
    repository.write('nested.py', source.replace('return y\n', 'return y if y else 0\n'))
    head = repository.commit()

    code, report = repository.run([base, head, 'nested.py'])

    assert code == 0
    rows = rows_by_function(report)
    assert rows[('matched', 'outer')]['delta'] == 0
    assert rows[('matched', 'outer.<locals>.inner')]['delta'] == 1
    assert rows[('matched', 'Holder.run')]['after']['kind'] == 'AsyncFunctionDef'
    assert ('matched', 'Holder.run.<locals>.helper') in rows
    assert rows[('matched', 'Holder.Inner.deep')]['after']['value'] == 1
    # radon does not measure methods of classes defined inside functions; they are
    # listed as unmeasured, never reported as zero.
    assert all('Local' not in name for _, name in rows.keys()) is True
    unmeasured = report['files'][0]['unmeasured']
    assert [(item['side'], item['function']) for item in unmeasured] == [
        ('base', 'outer.<locals>.Local.method'),
        ('head', 'outer.<locals>.Local.method'),
    ]


def test_counting_rules_are_locked_in(repository):
    repository.write(
        'counting.py',
        'def plain():\n    return 0\n\n\n'
        'def and_chain(a, b, c):\n    return a and b and c\n\n\n'
        'def and_or(a, b, c):\n    return a and b or c\n\n\n'
        'def comprehension(xs):\n    return [x for x in xs if x]\n\n\n'
        'def nested_comprehension(xs):\n    return {x: y for x in xs for y in x}\n\n\n'
        'def conditional_expression(x):\n    return 1 if x else 0\n\n\n'
        'def match_three_cases(x):\n'
        '    match x:\n'
        '        case 1:\n            return 1\n'
        '        case [a, b]:\n            return 2\n'
        '        case _:\n            return 3\n\n\n'
        'def match_two_cases(x):\n'
        '    match x:\n'
        '        case 1:\n            return 1\n'
        '        case 2:\n            return 2\n\n\n'
        'def with_assert(x):\n    assert x\n    return x\n',
    )
    revision = repository.commit()

    code, report = repository.run([revision, revision, 'counting.py'])

    assert code == 0
    values = {
        name: row['after']['value'] for (status, name), row in rows_by_function(report).items()
    }
    # radon 6.0.1 rules: each extra boolean operand +1; each comprehension `for` and
    # `if` +1; a conditional expression +1; each `case` +1 except a trailing `case _`;
    # `assert` +1.
    assert values == {
        'plain': 1,
        'and_chain': 3,
        'and_or': 3,
        'comprehension': 3,
        'nested_comprehension': 3,
        'conditional_expression': 2,
        'match_three_cases': 3,
        'match_two_cases': 3,
        'with_assert': 2,
    }


def test_invalid_input_is_refused(repository):
    repository.write('sample.py', 'def f():\n    return 0\n')
    revision = repository.commit()

    assert_not_executed(*repository.run([]), 'failed', 'Usage')
    assert_not_executed(*repository.run([revision, revision]), 'failed', 'Usage')
    for path in ('../sample.py', '/sample.py', 'sample.txt', './sample.py', 'a//b.py', '-x.py'):
        assert_not_executed(
            *repository.run([revision, revision, path]), 'unsupported', 'literal .py path'
        )
    assert_not_executed(
        *repository.run(['no-such-ref', revision, 'sample.py']), 'failed', 'rev-parse'
    )
    assert_not_executed(
        *repository.run([revision, revision, 'missing.py']), 'failed', 'absent from both'
    )


def test_running_outside_the_repository_root_is_refused(repository):
    repository.write('pkg/sample.py', 'def f():\n    return 0\n')
    revision = repository.commit()

    completed = run(
        [executable, str(COMMAND_PATH), revision, revision, 'sample.py'],
        cwd=repository.root / 'pkg',
        capture_output=True,
        text=True,
    )

    report = loads(completed.stdout)
    assert_conforms_to_contract(report)
    assert_not_executed(completed.returncode, report, 'failed', 'repository root')


def test_syntax_errors_fail_instead_of_reporting_zero(repository):
    repository.write('broken.py', 'def f():\n    return 0\n')
    base = repository.commit()
    repository.write('broken.py', 'def f(:\n    return 0\n')
    head = repository.commit()

    assert_not_executed(*repository.run([base, head, 'broken.py']), 'failed', 'broken.py')


def test_leading_byte_order_mark_is_accepted(repository):
    (repository.root / 'bom.py').write_bytes(b'\xef\xbb\xbfdef f(x):\n    return x or 1\n')
    revision = repository.commit()

    code, report = repository.run([revision, revision, 'bom.py'])

    assert code == 0
    row = rows_by_function(report)[('matched', 'f')]
    assert row['after']['value'] == 2
    assert row['after']['start'] == {'line': 1, 'column': 1}


def test_oversized_file_is_unsupported(repository, tmp_path):
    repository.write('big.py', 'def f():\n    return 0\n' + '#' * 64 + '\n')
    revision = repository.commit()
    command_path = copy_command_with_config(tmp_path / 'tool', maxFileBytes=32)

    assert_not_executed(
        *repository.run([revision, revision, 'big.py'], command_path=command_path),
        'unsupported',
        'exceeds 32 bytes',
    )


def test_non_regular_file_is_unsupported(repository, tmp_path):
    # Git stores a symbolic link as a mode 120000 blob holding the target path. Staging that
    # entry directly needs no filesystem symlink, which default Windows setups cannot create.
    repository.write('real.py', 'def f():\n    return 0\n')
    link_target = tmp_path / 'link-target'
    link_target.write_bytes(b'real.py')
    target_blob = git(repository.root, 'hash-object', '-w', str(link_target))
    git(repository.root, 'add', 'real.py')
    git(
        repository.root,
        'update-index',
        '--add',
        '--cacheinfo',
        '120000,{0},link.py'.format(target_blob),
    )
    git(repository.root, 'commit', '--quiet', '-m', 'synthetic')
    revision = git(repository.root, 'rev-parse', 'HEAD')

    assert_not_executed(
        *repository.run([revision, revision, 'link.py']), 'unsupported', 'Not a regular file'
    )


def test_non_utf8_source_is_unsupported(repository):
    (repository.root / 'latin.py').write_bytes(b'# caf\xe9\ndef f():\n    return 0\n')
    revision = repository.commit()

    assert_not_executed(*repository.run([revision, revision, 'latin.py']), 'unsupported', 'UTF-8')


def test_differing_historical_configuration_is_incomparable(repository):
    config = loads(CONFIG_PATH.read_text(encoding='utf-8'))
    config['excludedDirectories'] = []
    repository.write('scripts/complexity-config.json', dumps(config))
    repository.write('sample.py', 'def f():\n    return 0\n')
    base = repository.commit()
    repository.write('scripts/complexity-config.json', CONFIG_PATH.read_text(encoding='utf-8'))
    head = repository.commit()

    assert_not_executed(*repository.run([base, head, 'sample.py']), 'incomparable', base)


def test_identical_historical_configuration_is_comparable(repository):
    repository.write('scripts/complexity-config.json', CONFIG_PATH.read_text(encoding='utf-8'))
    repository.write('sample.py', 'def f():\n    return 0\n')
    revision = repository.commit()

    code, report = repository.run([revision, revision, 'sample.py'])

    assert code == 0
    assert report['status'] == 'executed'


def test_wrong_radon_version_is_unsupported(repository, tmp_path):
    repository.write('sample.py', 'def f():\n    return 0\n')
    revision = repository.commit()
    command_path = copy_command_with_config(tmp_path / 'tool', radonVersion='0.0.0')

    assert_not_executed(
        *repository.run([revision, revision, 'sample.py'], command_path=command_path),
        'unsupported',
        'radon 0.0.0',
    )


def test_wrong_python_minor_version_is_unsupported(repository, tmp_path):
    repository.write('sample.py', 'def f():\n    return 0\n')
    revision = repository.commit()
    command_path = copy_command_with_config(tmp_path / 'tool', pythonVersion='3.0')

    assert_not_executed(
        *repository.run([revision, revision, 'sample.py'], command_path=command_path),
        'unsupported',
        'Python 3.0',
    )


def test_missing_radon_is_unavailable(repository, tmp_path):
    repository.write('sample.py', 'def f():\n    return 0\n')
    revision = repository.commit()
    environment = tmp_path / 'no-radon'
    # Symlink the interpreter as `python -m venv` does off Windows: a copied interpreter made
    # from inside another venv cannot always find its standard library (seen on macOS 3.10).
    builder = EnvBuilder(with_pip=False, symlinks=platform != 'win32')
    builder.create(environment)
    # The interpreter's location and name differ by platform (bin/python3.X on POSIX,
    # Scripts\python.exe on Windows), so ask venv where it put it.
    interpreter = Path(builder.ensure_directories(environment).env_exe)

    assert_not_executed(
        *repository.run([revision, revision, 'sample.py'], interpreter=interpreter),
        'unavailable',
        'radon',
    )


def test_checked_in_pins_agree():
    config = loads(CONFIG_PATH.read_text(encoding='utf-8'))
    pyproject = (REPOSITORY_ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    # The `testing` extra pins radon in this repository's `"name ==version"` style.
    pin = 'radon =={0}'.format(config['radonVersion'])
    # No .python-version here: the pinned minor is the `requires-python` floor.
    requires_python = 'requires-python = ">={0}"'.format(config['pythonVersion'])

    assert '"{0}"'.format(pin) in pyproject
    assert requires_python in pyproject


def test_agent_instructions_declare_the_complexity_entry_point():
    instructions = (REPOSITORY_ROOT / 'CLAUDE.md').read_text(encoding='utf-8')
    sections = instructions.split('\n## ')
    declarations = [
        section for section in sections if 'Complexity report' in section.split('\n')[0]
    ]

    assert len(declarations) == 1
    # Compare prose with line wrapping collapsed to single spaces.
    declaration = ' '.join(declarations[0].split())
    assert "the team plugin's complexity report contract" in declaration
    assert 'schemaVersion: 1' in declaration
    assert 'python scripts/complexity.py <base> <head> <path.py>...' in declaration
    assert '(docs/complexity-reporting.md)' in declaration
    config = loads(CONFIG_PATH.read_text(encoding='utf-8'))
    # Anchored so a longer version such as 6.0.10 or 6.0.1rc1 does not match 6.0.1.
    pattern = r'radon `cc` {0}(?![\w.])'.format(escape(config['radonVersion']))
    assert search(pattern, declaration) is not None
