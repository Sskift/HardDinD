import contextlib
import json
from pathlib import Path
import signal
import tempfile
import types
import unittest
from unittest.mock import patch

from pipeline import bench_ab, config


class ABTests(unittest.TestCase):
    def test_suspend_leaves_seccomp_filesystem_waiting(self):
        state = {'guest_pid': 111, 'network_pid': 222, 'filesystems': [{'pid': 333}]}
        with patch.object(bench_ab.os, 'getpgrp', return_value=444), \
             patch.object(bench_ab.os, 'killpg') as kill, \
             patch.object(bench_ab.instance, 'group_members', return_value=([], [])):
            bench_ab.suspend(state, True)
            bench_ab.suspend(state, False)
        self.assertEqual([c.args for c in kill.call_args_list],
                         [(111, signal.SIGSTOP), (222, signal.SIGSTOP),
                          (111, signal.SIGCONT), (222, signal.SIGCONT)])

    def test_rotating_triples_keep_pair_indices(self):
        active, calls, counts = [None], [], dict.fromkeys(('native', 'guest', 'guest_b'), 0)
        def switch(side):
            active[0] = side
        def function(side):
            def run():
                self.assertEqual(active[0], side)
                calls.append(side)
                counts[side] += 1
                return json.dumps({'value': {'native': 100, 'guest': 50, 'guest_b': 75}[side] * counts[side]})
            return run
        metric = {'native': [], 'guest': [], 'guest_b': []}
        bench_ab.measure(None, 'cpu.single', metric, {s: function(s) for s in counts}, switch, [], 3)
        self.assertEqual(calls, ['native', 'guest', 'guest_b', 'native', 'guest', 'guest_b',
                                 'guest', 'guest_b', 'native', 'guest_b', 'native', 'guest'])
        self.assertEqual(metric['native'], [200, 300, 400])
        self.assertEqual(metric['guest'], [100, 150, 200])
        self.assertEqual(metric['guest_b'], [150, 225, 300])
        rows = metric['detail']['samples']
        self.assertEqual([r['pair'] for r in rows], [None] * 3 + [0] * 3 + [1] * 3 + [2] * 3)

    def test_control_pairs_rotate_without_native_and_errors_recorded(self):
        metric = {'native': [], 'guest': [], 'guest_b': []}
        calls = []
        functions = {side: (lambda side=side: calls.append(side)) for side in ('guest', 'guest_b')}
        bench_ab.measure(None, 'docker.run', metric, functions, lambda _: None, [], 2)
        self.assertEqual(calls, ['guest', 'guest_b', 'guest', 'guest_b', 'guest_b', 'guest'])
        self.assertEqual(len(metric['guest_b']), 2)
        def fail():
            raise RuntimeError('bad checksum')
        with self.assertRaisesRegex(RuntimeError, 'bad checksum'):
            bench_ab.measure(None, 'cpu.single', metric, {'guest': fail, 'guest_b': fail}, lambda _: None, [], 1)
        self.assertIn('bad checksum', metric['detail']['samples'][-1]['error'])

    def test_context_cleanup_activates_its_guest(self):
        active = [None]
        @contextlib.contextmanager
        def fixture(name):
            self.assertEqual(active[0], 'guest')
            try:
                yield None, None
            finally:
                self.assertEqual(active[0], 'guest')
        runner = types.SimpleNamespace(samples=fixture)
        with self.assertRaisesRegex(RuntimeError, 'sample failed'):
            with bench_ab.samples(runner, 'cpu.single', 'guest', lambda side: active.__setitem__(0, side)):
                active[0] = 'guest_b'
                raise RuntimeError('sample failed')

    def test_guest_configuration_overlay_and_shutdown(self):
        with tempfile.TemporaryDirectory() as td, patch.object(config, 'INSTANCES', Path(td) / 'instances'):
            root = Path(td); contexts = {}
            args = types.SimpleNamespace(kernel_arg=['base=1'], ab=['test=2'], share_opt=['thread_pool_size=4'],
                                          ab_share_opt=['thread_pool_size=0'], quick=True, only='cpu.single', keep=False)
            built = dict(kernel='kernel', rootfs='disk', network_backend='network', client='client', virtiofsd='fs')
            def workload(ctxs, switch):
                contexts.update(ctxs)
                switch('guest'); switch('guest_b'); switch('native')
                return {'metrics': {}}
            with patch.object(bench_ab.instance, 'start', return_value={}), \
                 patch.object(bench_ab.instance, 'stop', return_value={'phase': 'stopped'}) as stop, \
                 patch.object(bench_ab.instance, 'archive_logs'), patch.object(bench_ab.instance, 'remove'), \
                 patch.object(bench_ab, 'suspend'), patch.object(bench_ab, 'workloads', side_effect=workload):
                data = bench_ab.run('id', root, built, {**built, 'kernel': 'kernel-b'}, args, '2,4,6,8')
            a, b = contexts['guest'], contexts['guest_b']
            self.assertEqual(a.bench_cpus, b.bench_cpus)
            self.assertEqual(a.instance_options['kernel_args'], ['base=1'])
            self.assertEqual(b.instance_options['kernel_args'], ['base=1', 'test=2'])
            self.assertEqual(b.instance_options['share_options']['thread_pool_size'], 0)
            self.assertEqual(a.instance_options['share_options']['thread_pool_size'], 4)
            self.assertEqual(b.artifacts['kernel'], 'kernel-b')
            self.assertEqual(stop.call_count, 2)
            self.assertEqual(set(data['guest_states']), {'guest', 'guest_b'})
