import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pipeline import score


def metric(native, guest, higher=True, **extra):
    return {'unit': 'x', 'higher_is_better': higher, 'native': native, 'guest': guest, **extra}


class ScoreTests(unittest.TestCase):
    def rows(self, metrics):
        return {r['metric']: r for r in score.score_bench({'metrics': metrics})}

    def test_missing_and_errors_never_pass(self):
        rows = self.rows({'cpu.single': metric([1.0], [], error='boom'), 'cpu.multi': metric([1.0], [])})
        self.assertEqual(rows['cpu.single']['status'], 'fail')
        self.assertEqual(rows['cpu.multi']['status'], 'fail')
        self.assertEqual(rows['mem.bandwidth']['status'], 'not_tested')
        self.assertEqual(rows['gpu.compute']['status'], 'not_tested')

    def test_ratio_direction_and_threshold(self):
        rows = self.rows({'cpu.single': metric([100, 100, 100], [81, 79, 80]),
                          'app.compile': metric([10, 10], [25, 15], higher=False)})
        self.assertAlmostEqual(rows['cpu.single']['ratio'], 0.80)
        self.assertEqual(rows['cpu.single']['status'], 'pass')
        self.assertAlmostEqual(rows['app.compile']['ratio'], (0.4 + 10 / 15) / 2, places=4)
        self.assertEqual(rows['app.compile']['status'], 'pass')
        self.assertEqual(rows['app.compile']['spread'], [0.4, 0.6667])

    def test_pair_median_ignores_one_contended_pair(self):
        rows = self.rows({'cpu.multi': metric([12.0, 12.8, 12.4, 8.5, 9.9], [10.4, 11.3, 3.4, 8.7, 8.9])})
        self.assertAlmostEqual(rows['cpu.multi']['ratio'], 11.3 / 12.8, places=4)
        self.assertEqual(rows['cpu.multi']['status'], 'pass')

    def test_native_drift_is_flagged(self):
        rows = self.rows({'cpu.single': metric([100, 140], [90, 120]), 'cpu.multi': metric([100, 110], [90, 99])})
        self.assertTrue(rows['cpu.single']['noisy'])
        self.assertFalse(rows['cpu.multi']['noisy'])

    def test_reference_metrics_use_reference_seconds(self):
        rows = self.rows({'docker.run': {'unit': 's', 'higher_is_better': False, 'guest': [0.8, 1.0, 1.2]}})
        self.assertAlmostEqual(rows['docker.run']['ratio'], 0.4)
        self.assertEqual(rows['docker.run']['status'], 'fail')
        self.assertTrue(rows['docker.run']['reference'])

    def test_functional_gate_requires_every_check(self):
        self.assertFalse(score.score_test({'checks': []})['gate'])
        card = score.score_test({'checks': [{'id': 'a', 'status': 'pass'}, {'id': 'b', 'status': 'not_tested'}]})
        self.assertEqual((card['pass'], card['not_tested'], card['gate']), (1, 1, False))
        self.assertTrue(score.score_test({'checks': [{'id': 'a', 'status': 'pass'}]})['gate'])

    def test_markdown_renders_delta(self):
        summary = {'run_id': 'r2', 'revision': {'git': 'abc'}, 'bench': {'metrics': {'cpu.single': metric([100], [90])}}}
        previous = score.scorecard({'run_id': 'r1', 'revision': {}, 'bench': {'metrics': {'cpu.single': metric([100], [60])}}})
        text = score.markdown(score.scorecard(summary), previous)
        self.assertIn('| key | cpu.single |', text)
        self.assertIn('+50%', text)


if __name__ == '__main__':
    unittest.main()
