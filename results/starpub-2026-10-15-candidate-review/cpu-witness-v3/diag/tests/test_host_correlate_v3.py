#!/usr/bin/env python3
"""v3 targeted tests for the strict fail-open fixes plus the carried v2
guarantees. Pure/offline: no /proc sampling, no guest.
"""
import argparse
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import host_correlate as hc  # noqa: E402

STATUS_FIXTURE = ("Name:\tu\nState:\tS (sleeping)\nSigPnd:\t0000000000000004\n"
                  "ShdPnd:\t0000000000000080\nSigBlk:\t0000000000004000\n"
                  "SigIgn:\t0000000000000000\nSigCgt:\tffffffffffffffff\n")


def ent(key, start, utime=10, exec_ns=1000, wait_ns=500, gen=1, acq=0,
        schedstat=True, state='S'):
    tid = key
    return {
        'id': tid, 'key': '%d@%d' % (tid, start), 'starttime': start,
        'comm': 'uml-userspace', 'generation_consistent': gen,
        'acq_begin_ns': acq, 'acq_mid_ns': acq + 1, 'acq_end_ns': acq + 2,
        'stat_pre': {'state': state, 'utime': utime, 'stime': 20,
                     'starttime': start, 'processor': 0},
        'stat_post': {'state': state, 'utime': utime, 'stime': 20,
                      'starttime': start, 'processor': 0},
        'wchan': 'futex_wait_queue' if state == 'S' else '0',
        'schedstat': {'exec_ns': exec_ns, 'wait_ns': wait_ns,
                      'switches': 3} if schedstat else None,
        'sigblk': '4000', 'sigpnd': '0', 'shdpnd': '0', 'cgroup': '/x'}


def row(seq, rt, tb, te, runner=None, threads=None, leader=True):
    return {
        'seq': seq, 'rt_ns': rt, 'rt_mono_ns': rt + 1,
        'tick_begin_ns': tb, 'tick_end_ns': te,
        'leader_alive': True, 'leader_generation_consistent': leader,
        'threads': threads or {}, 'runners': {'%d@%d' % (runner[0], runner[1]): ent(*runner, acq=tb)}
        if runner else {}, 'events': [], 'errors': []}


def sync(tag, rt, g0, g1):
    return {'event': 'sync', 'tag': tag, 'rt_ns': rt,
            'mono_before_ns': g0, 'mono_after_ns': g1}


def cap(k=0, begin=100_000_000, end=150_000_000, tcpu0=0, tcpu1=10_000_000):
    stages = []
    names = hc.STAGE_NAMES
    for i, nm in enumerate(names):
        stages.append({'name': nm, 'begin_ns': begin + i, 'end_ns': begin + i + 1,
                       'tcpu_begin_ns': tcpu0, 'tcpu_end_ns': tcpu1})
    return {'event': 'cap', 'k': k, 'mono_begin_ns': begin,
            'proc_cpu_ns': 0, 'stages': stages,
            'max1': {'errno': 0, 'len': 15, 'raw': '50000 100000'},
            'stat': {'errno': 0, 'len': 100, 'present': 1, 'usage_usec': 1,
                     'nr_periods': 1, 'nr_throttled': 0, 'throttled_usec': 0},
            'max2': {'errno': 0, 'len': 15, 'raw': '50000 100000'},
            'proc': {'errno': 0, 'len': 90}}


def summary(requested=1, completed=1, status='complete'):
    return {'event': 'summary', 'requested': requested, 'completed': completed,
            'status': status, 'start_mono_ns': 0, 'end_mono_ns': 1,
            'deadline_ns': 2}


def rows_for(lo_start=111, hi_start=111, lo_ut=10, hi_ut=15,
             lo_exec=1000, hi_exec=3000, lo_wait=500, hi_wait=900,
             lo_ss=True, hi_ss=True):
    specs = [
        (0, 10_000_000, lo_start, lo_ut, lo_exec, lo_wait, lo_ss),
        (1, 50_000_000, lo_start, lo_ut, lo_exec, lo_wait, lo_ss),
        (2, 120_000_000, hi_start, hi_ut, hi_exec, hi_wait, hi_ss),
        (3, 190_000_000, hi_start, hi_ut, hi_exec, hi_wait, hi_ss),
    ]
    out = []
    for seq, tb, start, ut, ex, wa, ss in specs:
        e = ent(100, start, utime=ut, exec_ns=ex, wait_ns=wa,
                schedstat=ss, acq=tb)
        r = row(seq, tb, tb, tb + 1000)
        r['runners'] = {e['key']: e}
        out.append(r)
    return out


def manifest(reason='signal'):
    return {'termination_reason': reason, 'samples': 4,
            'schedstat': {'self_schedstat_present': True}}


def witness_pair():
    return ([sync('start', 10_000_000, 10_000_000, 10_000_500),
             sync('end', 190_000_000, 190_000_000, 190_000_500)], [cap()])


class TestCarriedV2(unittest.TestCase):
    def test_masks(self):
        m = hc.parse_status_masks(STATUS_FIXTURE)
        self.assertEqual(m['sigblk'], '0000000000004000')
        self.assertEqual(m['shdpnd'], '0000000000000080')

    def test_pid_reuse_missing_not_merged(self):
        rows = rows_for(lo_start=111, hi_start=222, hi_ut=1)
        s, c = witness_pair()
        r = hc.analyze_capture(manifest(), rows, s + c, summary(), 1)
        self.assertTrue(r['attribution_valid'])
        st = r['stages'][0]
        self.assertEqual(st['coverage'], 'covered')
        self.assertEqual(st['entities']['runner:100@111']['identity'], 'missing')

    def test_counter_regression_raw(self):
        rows = rows_for(hi_ut=5, hi_exec=500)
        s, c = witness_pair()
        r = hc.analyze_capture(manifest(), rows, s + c, summary(), 1)
        ent = r['stages'][0]['entities']['runner:100@111']
        self.assertEqual(ent['identity'], 'counter_regression')
        self.assertEqual(ent['deltas']['utime_ticks'], -5)
        self.assertEqual(ent['deltas']['exec_ns'], -500)

    def test_unknown_schedstt_never_zero_wait_claim(self):
        times = [10_000_000, 50_000_000, 120_000_000, 190_000_000]
        rows = []
        for i, tb in enumerate(times):
            ut = 10 if i < 2 else 11
            e = ent(100, 111, utime=ut, schedstat=False, acq=tb)
            r = row(i, tb, tb, tb + 1000)
            r['runners'] = {e['key']: e}
            rows.append(r)
        s, c = witness_pair()
        result = hc.analyze_capture(manifest(), rows, s + c, summary(), 1)
        entity = result['stages'][0]['entities']['runner:100@111']
        self.assertFalse(entity['run_wait_available'])
        self.assertNotIn('wait_ns', entity['deltas'])


class TestStrictAlignment(unittest.TestCase):
    def _rows(self):
        return [row(i, i * 10_000_000, i * 10_000_000,
                    i * 10_000_000 + 100_000) for i in range(6)]

    def test_single_tag_rejected(self):
        # v2 fail-open: one good tag used to be enough.
        a = hc.build_alignment([sync('start', 0, 0, 100)], self._rows())
        self.assertEqual(a['status'], 'unavailable')
        self.assertIn('start', a['reason'])

    def test_duplicate_tag_rejected(self):
        syncs = [sync('start', 0, 0, 100), sync('start', 10_000_000,
                                                10_000_000, 10_000_100),
                 sync('end', 50_000_000, 50_000_000, 50_000_100)]
        a = hc.build_alignment(syncs, self._rows())
        self.assertEqual(a['status'], 'unavailable')

    def test_missing_end_rejected(self):
        a = hc.build_alignment([sync('start', 0, 0, 100),
                                sync('middle', 1, 1, 2)], self._rows())
        self.assertEqual(a['status'], 'unavailable')

    def test_skew_rejected(self):
        syncs = [sync('start', 0, 0, 100),
                 sync('end', 50_000_000, 45_000_000, 45_000_100)]
        a = hc.build_alignment(syncs, self._rows())
        self.assertEqual(a['status'], 'unavailable')

    def test_both_tags_required_even_one_bracketable(self):
        rows = [row(0, 100_000_000, 0, 5_000_000)]
        a = hc.build_alignment([sync('start', 100_000_000, 0, 100),
                                sync('end', 0, 0, 100)], rows)
        self.assertEqual(a['status'], 'unavailable')

    def test_valid_pair_still_available(self):
        rows = [row(i, i * 10_000_000, i * 10_000_000,
                    i * 10_000_000 + 5_000_000) for i in range(6)]
        a = hc.build_alignment(
            [sync('start', 10_000_000, 10_000_000, 10_001_000),
             sync('end', 41_000_000, 40_000_000, 40_001_000)], rows)
        self.assertEqual(a['status'], 'available')


class TestStrictWitnessSchema(unittest.TestCase):
    def test_no_caps(self):
        problems = hc.validate_witness([sync('start', 0, 0, 1),
                                        sync('end', 2, 2, 3)], [], None, 1)
        self.assertTrue(any('summary' in p for p in problems))
        self.assertTrue(any('zero cap' in p for p in problems))

    def test_count_mismatch(self):
        s = [sync('start', 0, 0, 1), sync('end', 2, 2, 3)]
        problems = hc.validate_witness(s, [cap()], summary(60, 59, 'complete'), 60)
        self.assertTrue(any('59' in p for p in problems))

    def test_deadline_status_rejected(self):
        s = [sync('start', 0, 0, 1), sync('end', 2, 2, 3)]
        problems = hc.validate_witness(s, [cap()], summary(60, 1, 'deadline'), 60)
        self.assertTrue(problems)

    def test_complete_mismatch(self):
        s = [sync('start', 0, 0, 1), sync('end', 2, 2, 3)]
        problems = hc.validate_witness(s, [cap()],
                                       summary(3, 2, 'complete'), None)
        self.assertTrue(any('complete' in p or 'cap event' in p
                            for p in problems))

    def test_malformed_stage_and_group(self):
        s = [sync('start', 0, 0, 1), sync('end', 2, 2, 3)]
        c = cap()
        c['stages'][0]['name'] = 'wrong'
        del c['max2']
        problems = hc.validate_witness(s, [c], summary(), 1)
        self.assertTrue(any('stage' in p for p in problems))
        self.assertTrue(any('max2' in p for p in problems))

    def test_malformed_sync(self):
        problems = hc.validate_witness([{'tag': 'start'},
                                        sync('end', 2, 2, 3)], [cap()],
                                       summary(), 1)
        self.assertTrue(problems)

    def test_valid(self):
        s = [sync('start', 0, 0, 1), sync('end', 2, 2, 3)]
        self.assertEqual(hc.validate_witness(s, [cap()], summary(), 1), [])


class TestAnalyzeFailClosed(unittest.TestCase):
    def test_ok_requires_all_three(self):
        rows = rows_for()
        s, c = witness_pair()
        # Bad schema (summary says 60, one cap) -> not valid
        r = hc.analyze_capture(manifest(), rows, s + c,
                               summary(60, 60, 'complete'), 60)
        self.assertFalse(r['attribution_valid'])
        self.assertFalse(r['schema']['witness_complete'])
        # Bad alignment -> not valid even with good schema
        r = hc.analyze_capture(manifest(), rows, [sync('start', 10_000_000,
                                                       10_000_000,
                                                       10_000_500)] + c,
                               summary(), 1)
        self.assertFalse(r['attribution_valid'])
        self.assertEqual(r['clock_alignment']['status'], 'unavailable')
        # Bad integrity -> not valid
        r = hc.analyze_capture(manifest('leader_replaced'), rows, s + c,
                               summary(), 1)
        self.assertFalse(r['attribution_valid'])
        self.assertFalse(r['run_integrity']['ok'])

    def test_cmd_analyze_requires_summary_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / 'manifest.json').write_text('{}')
            (p / 'samples.jsonl').write_text('')
            rc = hc.cmd_analyze(argparse.Namespace(
                samples=str(p), witness=str(p / 'samples.jsonl'),
                summary=str(p / 'missing'), expect_count=1, out=str(p / 'o')))
            self.assertEqual(rc, 2)

    def test_cmd_analyze_end_to_end_fail_on_bad_count(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            s, c = witness_pair()
            rows = rows_for()
            (p / 'manifest.json').write_text(json.dumps(manifest()))
            (p / 'samples.jsonl').write_text(
                '\n'.join(json.dumps(r) for r in rows) + '\n')
            (p / 'w.jsonl').write_text('\n'.join(json.dumps(x) for x in s + c))
            (p / 'sum.jsonl').write_text(json.dumps(summary(60, 1)) + '\n')
            rc = hc.cmd_analyze(argparse.Namespace(
                samples=str(p), witness=str(p / 'w.jsonl'),
                summary=str(p / 'sum.jsonl'), expect_count=60,
                out=str(p / 'o')))
            self.assertEqual(rc, 1)
            out = json.loads((p / 'o/correlation.json').read_text())
            self.assertFalse(out['attribution_valid'])

    def test_cmd_analyze_two_summary_lines_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / 'manifest.json').write_text(json.dumps(manifest()))
            (p / 'samples.jsonl').write_text('')
            (p / 'w.jsonl').write_text('')
            (p / 'sum.jsonl').write_text(
                json.dumps(summary()) + '\n' + json.dumps(summary()) + '\n')
            rc = hc.cmd_analyze(argparse.Namespace(
                samples=str(p), witness=str(p / 'w.jsonl'),
                summary=str(p / 'sum.jsonl'), expect_count=1,
                out=str(p / 'o')))
            self.assertEqual(rc, 2)


class TestPidParser(unittest.TestCase):
    def test_fractional_and_garbage_rejected(self):
        for bad in ('1.5', '0x10', '1e3', '-1', '+1', '', '0',
                    '99999999999999999999', '1 ', 'nan'):
            with self.assertRaises(argparse.ArgumentTypeError):
                hc.strict_pid(bad)

    def test_accepts_plain_positive(self):
        self.assertEqual(hc.strict_pid('42'), 42)


if __name__ == '__main__':
    unittest.main(verbosity=2)
