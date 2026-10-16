import contextlib
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from pipeline import bench, build, cli, functional, instance, locks, runs


class FunctionalNoiseTests(unittest.TestCase):
    def test_counter_delta_requires_a_measured_window(self):
        self.assertEqual(runs.throttle_delta((100, 30), (120, 35)), .25)
        self.assertEqual(runs.throttle_delta((100, 30), (120, 30)), 0)
        for a, b in [(None, (1, 0)), ((1, 0), None), ((2, 1), (2, 1)),
                     ((2, 1), (1, 0)), ((2, 1), (3, 0))]:
            self.assertIsNone(runs.throttle_delta(a, b))

    def test_test_and_ci_wait_before_functional_boot_and_keep_evidence(self):
        for kind in ('test', 'ci'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                events = []
                quiet = {'quiet': False, 'waited': 20, 'throttled': [.8]}
                args = types.SimpleNamespace(label=None, with_=[], kernel_arg=[], only=None, quick=False)
                def wait(**_kwargs):
                    events.append('quiet')
                    return quiet
                def guest(_id, _dir, _artifacts, _args, suite, **_kwargs):
                    events.append(suite)
                    state = {'phase': 'stopped'}
                    if suite == 'test':
                        return {'checks': [], 'guest_state': state,
                                'shutdown': {'seconds': 1, 'throttled': .2}}
                    return {'metrics': {}, 'guest_state': state}
                with patch.object(cli, 'require_linux'), patch.object(cli, 'latest_card', return_value=None), \
                     patch.object(runs, 'new_run', return_value=('test-id', pathlib.Path(directory), {})), \
                     patch.object(runs, 'host_info', return_value={}), \
                     patch.object(runs, 'wait_quiet', side_effect=wait) as waiting, \
                     patch.object(runs, 'throttle', side_effect=[(100, 10), (200, 40)]), \
                     patch.object(runs, 'pick_cpus', return_value='0,1'), \
                     patch.object(runs, 'write_json') as write, \
                     patch.object(build, 'everything', return_value={'ids': {}}), \
                     patch.object(bench, 'bench_root'), \
                     patch.object(locks, 'measure', side_effect=lambda **_: contextlib.nullcontext()), \
                     patch.object(cli, 'with_guest', side_effect=guest), \
                     patch.object(cli, 'backend_selftest', return_value={'id': 'backend', 'status': 'pass'}), \
                     contextlib.redirect_stdout(None):
                    self.assertEqual(cli.run_suite(kind, args), 0)
                self.assertEqual(events, ['quiet', 'test'] + (['quiet', 'bench'] if kind == 'ci' else []))
                self.assertEqual(waiting.call_args_list[0].kwargs, {})  # HD_QUIET_WAIT default, also for test.
                result = write.call_args.args[1]['test']
                self.assertEqual(result['quiet'], quiet)
                self.assertEqual(result['throttled'], .3)
                self.assertEqual(result['checks'][-1]['throttled'], .2)

    def test_backend_and_shutdown_have_their_own_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = types.SimpleNamespace(kernel_arg=[], only=None)
            with patch.object(runs, 'throttle', side_effect=[(0, 0), (10, 3)]), \
                 patch.object(cli.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')):
                row = cli.backend_selftest(root, {'network_backend': 'backend'}, None)
            self.assertEqual(row['throttled'], .3)
            with patch.object(runs, 'instance_options', return_value={}), \
                 patch.object(runs, 'throttle', side_effect=[(100, 10), (110, 12)]), \
                 patch.object(instance, 'start'), patch.object(instance, 'stop', return_value={'phase': 'stopped'}), \
                 patch.object(instance, 'archive_logs'), patch.object(instance, 'remove'), \
                 patch.object(functional, 'run', return_value={'checks': []}), \
                 contextlib.redirect_stdout(None):
                result = cli.with_guest('test-id', root, {}, args, 'test', None)
            row = cli.shutdown_check(result['guest_state'], result['shutdown'])
            self.assertEqual((row['status'], row['throttled']), ('pass', .2))
