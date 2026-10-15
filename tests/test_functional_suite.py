import ast
import json
import pathlib
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from pipeline import functional as f


class FakeContext:
    def __init__(self, root, only=None, responses=None):
        self.run_dir = pathlib.Path(root)
        self.only = only
        self.responses = list(responses or [])
        self.calls = []
        self.artifacts = {'alpine_tar': self.run_dir / 'alpine.tar'}

    def selected(self, name):
        return self.only is None or re.search(self.only, name) is not None

    def docker(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        answer = self.responses.pop(0) if self.responses else (0, '', '')
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(args, *answer)

    def log(self, _text):
        pass


def spec(name, callback, covers=('run',), timeout=1, base=False):
    return name, list(covers), timeout, base, False, callback


class FunctionalSuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = FakeContext(self.tmp.name)
        self.suite = f.Suite(self.ctx)

    def execute(self, specs):
        with mock.patch.object(f, 'CHECKS', specs):
            return f.run(self.ctx)

    def test_selection_never_invokes_unselected_check_or_fixture(self):
        self.ctx.only = '^selected$'
        called = []
        data = self.execute([spec('omitted', lambda _: called.append('bad'), base=True),
                             spec('selected', lambda _: called.append('selected'))])
        self.assertEqual(called, ['selected'])
        self.assertEqual(self.ctx.calls, [])
        self.assertEqual([r['status'] for r in data['checks']], ['not_tested', 'pass'])
        self.assertEqual(data['schema'], 'hd.test/1')
        self.assertTrue((self.ctx.run_dir / 'functional/omitted.log').is_file())

    def test_no_selection_keeps_complete_matrix(self):
        self.ctx.only = '^nothing$'
        data = f.run(self.ctx)
        self.assertEqual(len(data['checks']), len(f.CHECKS))
        self.assertEqual({r['status'] for r in data['checks']}, {'not_tested'})
        self.assertFalse(data['coverage']['covered'])
        self.assertEqual(data['coverage']['missing'], data['coverage']['commands'])
        self.assertEqual(self.ctx.calls, [])

    def test_failures_and_skips_do_not_count_as_coverage(self):
        data = f.coverage([{'status': 'pass', 'covers': ['run']},
                           {'status': 'fail', 'covers': ['container exec']},
                           {'status': 'not_tested', 'covers': ['image pull']}],
                          ['image pull', 'run', 'container exec'])
        self.assertEqual(data['covered'], ['run'])
        self.assertEqual(data['missing'], ['container exec', 'image pull'])

    def test_skip_preserves_reason_and_continues(self):
        def skip(_c):
            raise f.NotTested('pinned registry unavailable')
        rows = self.execute([spec('skip', skip), spec('next', lambda _: None)])['checks']
        self.assertEqual([r['status'] for r in rows], ['not_tested', 'pass'])
        self.assertIn('registry unavailable', rows[0]['detail'])

    def test_error_preserves_last_output_and_continues(self):
        self.ctx.responses = [(7, 'x' * 1500, 'meaningful-end')]
        rows = self.execute([spec('broken', lambda c: c.d('run', 'image')), spec('next', lambda _: None)])['checks']
        self.assertEqual([r['status'] for r in rows], ['fail', 'pass'])
        self.assertLessEqual(len(rows[0]['detail']), 800)
        self.assertIn('meaningful-end', rows[0]['detail'])
        self.assertIn('x' * 1500, (self.ctx.run_dir / 'functional/broken.log').read_text())

    def test_subprocess_timeout_is_failure(self):
        self.ctx.responses = [subprocess.TimeoutExpired(['docker'], .01)]
        rows = self.execute([spec('timeout', lambda c: c.d('run')), spec('next', lambda _: None)])['checks']
        self.assertEqual([r['status'] for r in rows], ['fail', 'pass'])
        self.assertLessEqual(self.ctx.calls[0][1]['timeout'], 1)

    def test_callback_deadline_and_cleanup(self):
        def hangs(c):
            c.defer('rm', '-f', 'owned')
            time.sleep(.2)
        rows = self.execute([spec('timeout', hangs, timeout=.02), spec('next', lambda _: None)])['checks']
        self.assertEqual([r['status'] for r in rows], ['fail', 'pass'])
        self.assertIn('deadline', rows[0]['detail'])
        self.assertEqual(self.ctx.calls[0][0], ('rm', '-f', 'owned'))

    def test_cleanup_runs_after_exception(self):
        def broken(c):
            c.object('volume', 'data')
            c.object('container', 'app')
            raise ValueError('broken')
        rows = self.execute([spec('broken', broken)])['checks']
        self.assertEqual(rows[0]['status'], 'fail')
        self.assertEqual([x[0][:2] for x in self.ctx.calls], [('rm', '-fv'), ('volume', 'rm')])

    def test_cleanup_failure_changes_pass_to_fail(self):
        self.ctx.responses = [(1, '', 'permission denied')]
        data = self.execute([spec('cleanup', lambda c: c.defer('rm', '-f', 'owned'))])
        self.assertEqual(data['checks'][0]['status'], 'fail')
        self.assertIn('permission denied', data['checks'][0]['detail'])
        self.assertFalse(data['coverage']['covered'])

    def test_cleanup_missing_object_is_idempotent(self):
        self.ctx.responses = [(1, '', 'Error: No such container: owned')]
        data = self.execute([spec('cleanup', lambda c: c.defer('rm', '-f', 'owned'))])
        self.assertEqual(data['checks'][0]['status'], 'pass')

    def test_explicit_remove_deregisters_object(self):
        def remove(c):
            name = c.object('container')
            c.d('rm', '-f', name)
        self.execute([spec('remove', remove)])
        self.assertEqual(len(self.ctx.calls), 1)

    def test_failed_remove_does_not_deregister(self):
        self.ctx.responses = [(1, '', 'busy'), (0, 'removed', '')]
        def remove(c):
            name = c.object('container')
            c.d('rm', '-f', name)
        rows = self.execute([spec('remove', remove)])['checks']
        self.assertEqual(rows[0]['status'], 'fail')
        self.assertEqual(len(self.ctx.calls), 2)

    def test_absence_does_not_accept_dead_daemon(self):
        self.ctx.responses = [(1, '', 'Cannot connect to the Docker daemon')]
        rows = self.execute([spec('absent', lambda c: c.absent('inspect', 'owned'))])['checks']
        self.assertEqual(rows[0]['status'], 'fail')
        self.assertIn('absence not established', rows[0]['detail'])

    def test_fixture_failure_is_not_retried_or_passed(self):
        self.ctx.responses = [(1, '', 'import failed'), (0, '', '')]
        rows = self.execute([spec('a', lambda _: None, base=True), spec('b', lambda _: None, base=True)])['checks']
        self.assertEqual([r['status'] for r in rows], ['fail', 'not_tested'])
        self.assertIn('fixture unavailable', rows[1]['detail'])
        self.assertEqual(sum(args[0] == 'import' for args, _ in self.ctx.calls), 1)

    def test_fixture_cleanup_failure_is_visible(self):
        self.ctx.responses = [(0, 'sha256:abc', ''), (0, 'fixture.base\n', ''), (1, '', 'image in use')]
        rows = self.execute([spec('a', lambda _: None, base=True)])['checks']
        self.assertEqual([r['status'] for r in rows], ['pass', 'fail'])
        self.assertEqual(rows[-1]['id'], 'fixture.cleanup')

    def test_suite_budget_marks_remaining_not_tested(self):
        self.suite.end = time.monotonic() - 1
        rows = f.run_checks(self.suite, [spec('a', lambda _: self.fail('must not run'))])
        self.assertEqual(rows[0]['status'], 'not_tested')
        self.assertIn('time budget', rows[0]['detail'])

    def test_checks_and_tracked_commands_are_consistent(self):
        names = [row[0] for row in f.CHECKS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[-1], 'persistence.reboot')
        tracked = set(f.coverage([])['commands'])
        declared = {cmd for row in f.CHECKS for cmd in row[1]}
        self.assertEqual(tracked, declared)
        self.assertGreaterEqual(len(tracked), 80)
        self.assertTrue(all(re.fullmatch(r'[a-zA-Z0-9_.-]+', name) for name in names))
        self.assertTrue(all(row[2] > 0 for row in f.CHECKS))

    def test_python_310_syntax_and_fixtures(self):
        for path in [pathlib.Path(f.__file__), *f.FIXTURES.glob('*.py')]:
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 10))
        fixture = json.loads((f.FIXTURES / 'compose/compose.yaml').read_text())
        self.assertEqual(set(fixture['services']), {'db', 'web', 'worker', 'builder'})
        self.assertTrue(all(s['pull_policy'] == 'never' for s in fixture['services'].values()))

    def test_pins_are_exact(self):
        for key in ('functional_pull_image', 'functional_registry_image'):
            self.assertIn('@sha256:', f.pinned(key))
        item = f.build.LOCK['functional_https']
        self.assertTrue(item['url'].startswith('https://'))
        self.assertRegex(item['sha256'], r'^[0-9a-f]{64}$')

    def test_invalid_pin_rejected(self):
        with mock.patch.dict(f.build.LOCK, {'functional_pull_image': {'reference': 'alpine:latest', 'sha256': 'abc'}}):
            with self.assertRaises(AssertionError):
                f.pinned('functional_pull_image')

    def test_quota_effect(self):
        reference = {'wall': 3., 'cpu': 2.8}
        f.quota_effect(reference, {'wall': 4., 'cpu': 1., 'throttled': 25})
        with self.assertRaises(AssertionError):
            f.quota_effect(reference, {'wall': 4., 'cpu': 3., 'throttled': 0})
        with self.assertRaises(AssertionError):
            f.quota_effect(reference, {'wall': 4., 'cpu': 1., 'throttled': 0})
        with self.assertRaises(f.NotTested):
            f.quota_effect({'wall': 3., 'cpu': .5}, {'wall': 4., 'cpu': .3, 'throttled': 25})

    def test_cpu_measurement_requires_work_and_duration(self):
        for row in ({'wall': 4, 'cpu': 0, 'iterations': 1}, {'wall': .1, 'cpu': .1, 'iterations': 100}):
            with self.assertRaises(AssertionError):
                f.cpu_result(json.dumps(row))

    def test_nanosecond_docker_timestamp(self):
        self.assertEqual(f.parse_time('2026-10-15T01:02:03.123456789Z'),
                         f.parse_time('2026-10-15T01:02:03.123456+00:00'))

    def test_embedded_shell_syntax(self):
        scripts = []
        tree = ast.parse(pathlib.Path(f.__file__).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            values = [arg.value if isinstance(arg, ast.Constant) else None for arg in node.args]
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'shell' and values:
                if isinstance(values[0], str):
                    scripts.append(values[0])
            for index in range(len(values) - 2):
                if values[index] == 'sh' and values[index + 1] in ('-c', '-ec'):
                    if isinstance(values[index + 2], str):
                        scripts.append(values[index + 2])
        fixture = json.loads((f.FIXTURES / 'compose/compose.yaml').read_text())
        scripts += [v['command'][2].replace('$$', '$') for v in fixture['services'].values()
                    if v['command'][0] == 'sh']
        for script in scripts:
            with self.subTest(script=script[:70]):
                result = subprocess.run(['sh', '-n'], input=script, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_context_contains_every_helper(self):
        copied = (f.FIXTURES / 'Dockerfile').read_text().split('COPY ', 1)[1].split()[:-1]
        self.assertEqual(set(copied), {p.stem for p in (f.FIXTURES / 'helpers').glob('*.c')} | {'network_probe'})
