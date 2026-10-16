import contextlib
import pathlib
import unittest
from unittest.mock import Mock, patch

from pipeline import bench, runs, score


class PressureTests(unittest.TestCase):
    def test_cgroup_preferred_and_per_resource_fallback(self):
        reads = []
        data = {'/sys/fs/cgroup/cpu.pressure': 'some avg10=12.34 avg60=2.00 total=10\nfull avg10=99.0\n',
                '/proc/pressure/cpu': 'some avg10=99.00\n',
                '/proc/pressure/io': 'some avg10=3.25 avg60=1.00 total=20\n',
                '/sys/fs/cgroup/memory.pressure': 'full avg10=1.00\n',
                '/proc/pressure/memory': 'some avg10=0.12\n'}
        def read(path):
            reads.append(str(path))
            if str(path) not in data:
                raise FileNotFoundError(path)
            return data[str(path)]
        with patch.object(pathlib.Path, 'read_text', read):
            self.assertEqual(runs.pressure(), {'cpu': 12.34, 'io': 3.25, 'memory': .12})
        self.assertNotIn('/proc/pressure/cpu', reads)

    def test_unavailable_and_invalid_are_not_quiet(self):
        for text in ('', 'some avg10=nan', 'some avg10=inf', 'some avg10=-1', 'some avg10=101'):
            with self.subTest(text=text), patch.object(pathlib.Path, 'read_text', return_value=text):
                psi = runs.pressure()
                self.assertEqual(psi, dict.fromkeys(runs.PSI_LIMITS))
                self.assertTrue(runs.psi_noisy(psi))

    def test_each_noise_boundary(self):
        low = dict.fromkeys(runs.PSI_LIMITS, 0.)
        self.assertFalse(runs.psi_noisy(low))
        for resource, limit in runs.PSI_LIMITS.items():
            self.assertFalse(runs.psi_noisy({**low, resource: limit - .01}))
            self.assertTrue(runs.psi_noisy({**low, resource: limit}))
            self.assertTrue(runs.psi_noisy({**low, resource: limit + .01}))

    def test_mid_window_peak_and_exception_cleanup(self):
        low = {'cpu': 1., 'io': 0., 'memory': 0.}
        stopped = Mock(); stopped.wait.side_effect = [False, True]
        worker = Mock()
        def thread(target, daemon):
            worker.start.side_effect = target
            return worker
        with patch.object(runs, 'pressure', side_effect=[low, {**low, 'io': 12.}, low]), \
             patch.object(runs.threading, 'Event', return_value=stopped), \
             patch.object(runs.threading, 'Thread', side_effect=thread):
            with self.assertRaisesRegex(RuntimeError, 'workload'):
                with runs.pressure_window() as psi:
                    raise RuntimeError('workload')
        self.assertEqual(psi, {**low, 'io': 12.})
        stopped.set.assert_called_once(); worker.join.assert_called_once()

    def test_wait_requires_both_quota_and_pressure(self):
        low = {'cpu': 1., 'io': 0., 'memory': 0.}
        for fraction, psi in ((.2, low), (0., {**low, 'cpu': 21.}), (0., {**low, 'io': 11.}),
                              (0., {**low, 'memory': 3.}), (0., dict.fromkeys(low))):
            with self.subTest(fraction=fraction, psi=psi), \
                 patch.object(runs, 'pressure_window', return_value=contextlib.nullcontext(psi)), \
                 patch.object(runs, 'throttled_fraction', return_value=fraction):
                result = runs.wait_quiet(max_wait=0)
            self.assertFalse(result['quiet'])
            self.assertEqual(result['psi'], [psi])
        with patch.object(runs, 'pressure_window', side_effect=[contextlib.nullcontext({**low, 'io': 11.}),
                                                               contextlib.nullcontext(low)]), \
             patch.object(runs, 'throttled_fraction', return_value=.10), \
             patch.object(runs.time, 'sleep') as sleep:
            result = runs.wait_quiet(max_wait=60, log=lambda _: None)
        self.assertTrue(result['quiet'])
        self.assertEqual(result['throttled'], [.10, .10])
        self.assertEqual(result['psi'], [{**low, 'io': 11.}, low])
        sleep.assert_called_once_with(40)

    def test_bench_records_psi_on_success_and_failure(self):
        psi = {'cpu': 5., 'io': 12., 'memory': 0.}
        metric = {}
        with patch.object(runs, 'pressure', return_value=psi):
            bench.sample(None, 'cpu.single', metric, lambda: '{"value": 1}', 'native', 0)
            with self.assertRaisesRegex(RuntimeError, 'broken'):
                bench.sample(None, 'cpu.single', metric, Mock(side_effect=RuntimeError('broken')), 'guest', 0)
        self.assertEqual([s['psi'] for s in metric['detail']['samples']], [psi, psi])

    def test_noise_assigned_to_correct_ab_side_without_changing_score(self):
        psi = {'cpu': 5., 'io': 12., 'memory': 0.}
        for side, expected in [('native', (True, True, False)), ('guest', (True, False, True)),
                               ('guest_b', (False, True, True))]:
            metric = {'native': [100, 100], 'guest': [90, 90], 'guest_b': [95, 95],
                      'detail': {'samples': [{'phase': 'sample', 'side': side, 'psi': psi}]}}
            row = score.score_bench({'metrics': {'cpu.single': metric}})[0]
            self.assertEqual(tuple(row[k] for k in ('noisy', 'noisy_b', 'noisy_ba')), expected)
            self.assertEqual((row['status'], row['status_b']), ('pass', 'pass'))
            metric['detail']['samples'][0]['phase'] = 'warmup'
            row = score.score_bench({'metrics': {'cpu.single': metric}})[0]
            self.assertFalse(row['noisy'] or row['noisy_b'] or row['noisy_ba'])

    def test_pressure_only_quiet_failure_banner(self):
        card = score.scorecard({'run_id': 'psi', 'quiet': {'quiet': False, 'throttled': [0.],
                                'psi': [{'cpu': 1., 'io': 12., 'memory': 0.}]}})
        text = score.markdown(card)
        self.assertIn('Host was contended', text)
        self.assertIn('PSI some avg10 peaks', text)
        self.assertIn('not decision-grade', text)
