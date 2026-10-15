import importlib.util
import copy
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location(
    "cpu_limit_checks", pathlib.Path(__file__).resolve().parents[1] / "scripts/cpu_limit_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def stat(usage_usec, periods=100, throttled=0, throttled_usec=0):
    return {"present": 1, "usage_usec": usage_usec, "nr_periods": periods,
            "nr_throttled": throttled, "throttled_usec": throttled_usec,
            "nr_bursts": 0, "burst_usec": 0}


def cgroup(quota=None, period=100000, throttled=0, throttled_usec=0,
           usage_s=0.0, cpuset_eff="0-3", nr_periods=100):
    if quota is None:
        cmax = {"errno": 0, "raw": f"max {period}", "quota": 0, "period": period, "quota_max": 1}
    else:
        cmax = {"errno": 0, "raw": f"{int(quota * period)} {period}",
                "quota": int(quota * period), "period": period, "quota_max": 0}
    usage = int(usage_s * 1e6)
    return {"cpu.max": cmax,
            "cpuset.cpus.effective": {"errno": 0, "raw": cpuset_eff},
            "cpu.stat": {**stat(usage, nr_periods, throttled, throttled_usec),
                         "raw": "usage_usec %d\nnr_periods %d\nnr_throttled %d\nthrottled_usec %d\n"
                                % (usage, nr_periods, throttled, throttled_usec)}}


def spin_event_stream(*, threads=4, wall_s=6.0, consumed_cores=None, quota=None,
                      period_us=100000, throttled=0, throttled_usec=0,
                      stat_cores=None, proc_cores=None, cpuset_eff="0-3",
                      worker_cpus=None, affinity_cpus=None, sampler_cores=0):
    """Build a probe JSONL text blob with controllable consumption rates."""
    if consumed_cores is None:
        consumed_cores = threads
    if worker_cpus is None:
        worker_cpus = list(range(min(threads, 4)))
    if affinity_cpus is None:
        affinity_cpus = sorted(checks.parse_cpu_list(cpuset_eff))
    wall_ns = int(wall_s * 1e9)
    worker_cpu = int(consumed_cores * wall_s * 1e9) // threads
    proc_used = int((proc_cores if proc_cores is not None else consumed_cores + sampler_cores) * wall_s * 1e9)
    stat_used = int((stat_cores if stat_cores is not None else consumed_cores + sampler_cores) * wall_s * 1e6)
    start = {"event": "start", "wall_ns": 0, "cap_end_ns": 0, "proc_cpu_ns": 0, "threads": threads,
             "sampler_cpu_ns": 0, "load_deadline_ns": wall_ns,
             "nproc": 4, "clk_tck": 100, "affinity": affinity_cpus,
             "cgroup": cgroup(quota, period_us, 0, 0, 0.0, cpuset_eff, 0)}
    workers = [{"idx": i, "cpu_ns": worker_cpu, "cpus_seen": worker_cpus,
                "getcpu_errors": 0, "getcpu_oor": 0,
                "affinity_start_errno": 0, "affinity_end_errno": 0,
                "affinity_start": affinity_cpus, "affinity_end": affinity_cpus,
                "sink": 1} for i in range(threads)]
    end = {"event": "end", "wall_ns": wall_ns, "cap_end_ns": wall_ns, "duration_ns": wall_ns,
           "sampler_cpu_ns": int(sampler_cores * wall_s * 1e9),
           "proc_cpu_ns": proc_used, "workers": workers,
           "worker_cpu_ns": worker_cpu * threads,
           "stat_start": stat(0, 0, 0, 0),
           "stat_end": stat(stat_used, int(wall_s * 1e6 / period_us) if quota else 600,
                            throttled, throttled_usec),
           "cgroup": cgroup(quota, period_us, throttled, throttled_usec,
                            stat_used / 1e6, cpuset_eff,
                            int(wall_s * 1e6 / period_us) if quota else 600)}
    return [start, end]


def classify(text_events, fn, *args, **kwargs):
    summary = checks.spin_summary(text_events)
    return fn(summary, *args, **kwargs), summary


class BandTests(unittest.TestCase):
    def test_bands_widen_with_few_periods(self):
        lo6, up6 = checks.cfs_band(1.0, 0.1, 6.0)
        lo2, up2 = checks.cfs_band(1.0, 0.1, 2.0)
        self.assertAlmostEqual(up6, 1.0 * 6.225 / 6.0, places=6)
        self.assertGreater(up2 - 1.0, up6 - 1.0)
        self.assertGreater(lo6, lo2)

    def test_bands_are_accounting_not_performance(self):
        # Slack shrinks with the run length instead of demanding a fixed
        # near-native throughput ratio.
        _, up_long = checks.cfs_band(0.5, 0.05, 6.0)
        self.assertLess(up_long, 0.5 * 1.03)

    def test_parse_cpu_list(self):
        self.assertEqual(checks.parse_cpu_list("0"), {0})
        self.assertEqual(checks.parse_cpu_list("1-2"), {1, 2})
        self.assertEqual(checks.parse_cpu_list("0,2-3,5"), {0, 2, 3, 5})
        self.assertEqual(checks.parse_cpu_list(""), set())


class ReferenceTests(unittest.TestCase):
    def test_idle_tail_after_worker_deadline_is_accounted(self):
        events = spin_event_stream(consumed_cores=4)
        events[-1]['wall_ns'] += 100000000
        events[-1]['cap_end_ns'] += 100000000
        events[-1]['duration_ns'] += 100000000
        r, _ = classify(events, checks.classify_reference, 4)
        self.assertEqual(r['status'], checks.PASS, r)

    def test_full_consumption_passes(self):
        events = spin_event_stream(consumed_cores=4)
        r, _ = classify(events, checks.classify_reference, 4)
        self.assertEqual(r['status'], checks.PASS, r)

    def test_throttled_unthrottled_reference_fails(self):
        events = spin_event_stream(consumed_cores=4, throttled=5, throttled_usec=1000)
        r, _ = classify(events, checks.classify_reference, 4)
        self.assertEqual(r['status'], checks.FAIL, r)

    def test_low_consumption_is_indeterminate_not_pass(self):
        events = spin_event_stream(consumed_cores=1.2)
        r, _ = classify(events, checks.classify_reference, 4)
        self.assertEqual(r['status'], checks.INDETERMINATE, r)
        self.assertIn('offered load', r['reason'])

    def test_disagreeing_counters_indeterminate(self):
        events = spin_event_stream(consumed_cores=4, stat_cores=2.0)
        r, _ = classify(events, checks.classify_reference, 4)
        self.assertEqual(r['status'], checks.INDETERMINATE, r)


class QuotaTests(unittest.TestCase):
    def test_enforced_quota_passes_with_throttle_evidence(self):
        events = spin_event_stream(consumed_cores=1.01, quota=1.0,
                                   throttled=40, throttled_usec=int(4.5e6))
        ref_events = spin_event_stream(consumed_cores=4)
        ref = checks.spin_summary(ref_events)
        r, _ = classify(events, checks.classify_quota, 1.0, 0.1, ref)
        self.assertEqual(r['status'], checks.PASS, r)

    def test_over_quota_fails(self):
        # 4 busy threads consuming 3.6 cores under a 1-core quota: not enforced.
        events = spin_event_stream(consumed_cores=3.6, quota=1.0,
                                   throttled=0, throttled_usec=0)
        r, _ = classify(events, checks.classify_quota, 1.0, 0.1, None)
        self.assertEqual(r['status'], checks.FAIL, r)
        self.assertIn('exceeds', r['reason'])

    def test_within_band_without_throttle_counters_indeterminate(self):
        events = spin_event_stream(consumed_cores=1.0, quota=1.0)
        r, _ = classify(events, checks.classify_quota, 1.0, 0.1, None)
        self.assertEqual(r['status'], checks.INDETERMINATE, r)

    def test_starved_quota_indeterminate(self):
        events = spin_event_stream(consumed_cores=0.2, quota=1.0,
                                   throttled=40, throttled_usec=int(5e6))
        r, _ = classify(events, checks.classify_quota, 1.0, 0.1, None)
        self.assertEqual(r['status'], checks.INDETERMINATE, r)

    def test_sampler_cpu_is_part_of_cgroup_quota(self):
        # Real v1 run: worker clocks omit the sampler's CPU consumption.
        # Total cgroup delivery is in band, and independent counters agree.
        events = spin_event_stream(consumed_cores=.477, proc_cores=.493,
                                   stat_cores=.493, quota=.5, period_us=50000,
                                   throttled=100, throttled_usec=2000000)
        r, _ = classify(events, checks.classify_quota, .5, .05)
        self.assertEqual(r['status'], checks.PASS, r)

    def test_large_unexplained_nonworker_load_is_not_pass(self):
        events = spin_event_stream(consumed_cores=.1, proc_cores=.493,
                                   stat_cores=.493, quota=.5, period_us=50000,
                                   throttled=100, throttled_usec=2000000)
        r, _ = classify(events, checks.classify_quota, .5, .05)
        self.assertEqual(r['status'], checks.INDETERMINATE, r)

    def test_insufficient_reference_load_indeterminate(self):
        # Limit is nominally respected, but the reference cannot clear the
        # band: enforcement was not functionally distinguished.
        events = spin_event_stream(consumed_cores=0.51, quota=0.5,
                                   throttled=50, throttled_usec=int(5e6))
        ref_events = spin_event_stream(consumed_cores=0.6)
        ref = checks.spin_summary(ref_events)
        r, _ = classify(events, checks.classify_quota, 0.5, 0.1, ref)
        self.assertEqual(r['status'], checks.INDETERMINATE, r)

    def test_wrong_cpumax_fails(self):
        events = spin_event_stream(consumed_cores=1.0, quota=2.0,
                                   throttled=10, throttled_usec=1000)
        r, _ = classify(events, checks.classify_quota, 1.0, 0.1, None)
        self.assertEqual(r['status'], checks.FAIL, r)

    def test_explicit_short_period_bounds(self):
        lo, up = checks.cfs_band(0.5, 0.05, 6.0)
        events = spin_event_stream(consumed_cores=(lo + up) / 2, quota=0.5,
                                   period_us=50000, throttled=100,
                                   throttled_usec=int(5.6e6))
        r, _ = classify(events, checks.classify_quota, 0.5, 0.05, None)
        self.assertEqual(r['status'], checks.PASS, r)


class CpusetTests(unittest.TestCase):
    def test_sampler_thread_accounted_separately_on_one_cpu(self):
        events = spin_event_stream(consumed_cores=.92, sampler_cores=.08,
                                   cpuset_eff='0', worker_cpus=[0], affinity_cpus=[0])
        r, summary = classify(events, checks.classify_cpuset, {0})
        self.assertEqual(r['status'], checks.PASS, r)
        self.assertAlmostEqual(summary['sampler_cpu_cores'], .08)
        self.assertAlmostEqual(summary['rates']['thread'], 1.0)

    def test_single_cpu_pinning(self):
        events = spin_event_stream(consumed_cores=1.0, cpuset_eff="0",
                                   worker_cpus=[0], affinity_cpus=[0])
        r, _ = classify(events, checks.classify_cpuset, {0})
        self.assertEqual(r['status'], checks.PASS, r)

    def test_thread_outside_cpuset_fails(self):
        events = spin_event_stream(consumed_cores=1.0, cpuset_eff="0",
                                   worker_cpus=[0, 1], affinity_cpus=[0])
        r, _ = classify(events, checks.classify_cpuset, {0})
        self.assertEqual(r['status'], checks.FAIL, r)

    def test_affinity_mismatch_fails(self):
        events = spin_event_stream(consumed_cores=1.0, cpuset_eff="0",
                                   worker_cpus=[0], affinity_cpus=[0, 1])
        r, _ = classify(events, checks.classify_cpuset, {0})
        self.assertEqual(r['status'], checks.FAIL, r)

    def test_cpuset_plus_quota_uses_quota_classifier(self):
        events = spin_event_stream(consumed_cores=0.51, quota=0.5,
                                   throttled=40, throttled_usec=int(5e6),
                                   cpuset_eff="0", worker_cpus=[0],
                                   affinity_cpus=[0])
        r, _ = classify(events, checks.classify_cpuset, {0},
                        expected_quota=0.5, period_s=0.1)
        self.assertEqual(r['status'], checks.PASS, r)


def sample(t, proc_cpu_s, stat_cpu_s, quota_max, quota, period, throttled, throttled_us,
           cap_us=500, stable=True, q2=None, steal_s=None, nr_periods=None):
    wall_ns = int(t * 1e9)
    cap_end_ns = wall_ns + int(cap_us * 1000)
    q2 = q2 if q2 is not None else (quota_max, quota, period)
    s = {"event": "sample", "wall_ns": wall_ns, "cap_end_ns": cap_end_ns,
         "cap_dur_ns": cap_end_ns - wall_ns,
         "proc_cpu_ns": int(proc_cpu_s * 1e9),
         "quota_max": quota_max, "quota": quota, "period": period,
         "max_errno": 0, "q2_quota_max": q2[0], "q2_quota": q2[1], "q2_period": q2[2],
         "max2_errno": 0, "max_stable": 1 if stable else 0,
         "stat_errno": 0, "usage_usec": int(stat_cpu_s * 1e6),
         "nr_periods": int(t * 10) if nr_periods is None else nr_periods,
         "nr_throttled": throttled, "throttled_usec": throttled_us,
         "steal_errno": 0 if steal_s is not None else 2,
         "steal_ns": int(steal_s * 1e9) if steal_s is not None else 0}
    return s


class UpdateWindowTests(unittest.TestCase):
    def test_three_segment_schedule_enforced(self):
        samples = []
        t = proc = used = 0.0
        thr = thr_us = 0

        def emit(cores, quota, period, length):
            nonlocal t, proc, used, thr, thr_us
            for _ in range(int(length * 10)):
                t += 0.1
                proc += cores * 0.1
                used += cores * 0.1
                if quota:
                    thr += 1
                    thr_us += 50000
                samples.append(sample(t, proc, used, 0 if quota else 1,
                                      quota, period, thr, thr_us))
        emit(2.0, 200000, 100000, 3.5)
        emit(0.5, 50000, 100000, 4.0)
        emit(2.0, 200000, 100000, 4.0)
        workers = [{"idx": 0, "cpu_ns": 1, "cpus_seen": [0],
                    "affinity_start": [0, 1], "affinity_end": [0, 1]}]
        summary = {"samples": samples, "wall_s": t,
                   "rates": {}, "stat_delta": {}, "workers": workers,
                   "start": {"threads": 4}, "end": {"cgroup": {}}}
        results = checks.classify_update_run(summary, [(2.0, 100000), (0.5, 100000), (2.0, 100000)])
        self.assertEqual([r['status'] for r in results],
                         [checks.PASS, checks.PASS, checks.PASS], results)

    def test_segment_shortage_is_indeterminate(self):
        samples = []
        t = proc = used = thr = thr_us = 0.0

        def emit(cores, quota, period, length):
            nonlocal t, proc, used, thr, thr_us
            for _ in range(int(length * 10)):
                t += 0.1
                proc += cores * 0.1
                used += cores * 0.1
                thr += 1
                thr_us += 50000
                samples.append(sample(t, proc, used, 0, quota, period,
                                      int(thr), int(thr_us)))
        emit(2.0, 200000, 100000, 3.5)
        emit(0.5, 50000, 100000, 0.5)   # too short after guards
        emit(2.0, 200000, 100000, 4.0)
        summary = {"samples": samples}
        results = checks.classify_update_run(summary, [(2.0, 100000), (0.5, 100000), (2.0, 100000)])
        self.assertEqual(results[1]['status'], checks.INDETERMINATE)

    def test_unexpected_segment_timeline_fails(self):
        samples = []
        t = proc = used = thr = thr_us = 0.0
        for cores, quota, length in [(2.0, 200000, 4.0), (1.0, 100000, 4.0), (2.0, 200000, 4.0)]:
            for _ in range(int(length * 10)):
                t += 0.1
                proc += cores * 0.1
                used += cores * 0.1
                thr += 1
                thr_us += 50000
                samples.append(sample(t, proc, used, 0, quota, 100000, int(thr), int(thr_us)))
        summary = {"samples": samples}
        results = checks.classify_update_run(summary, [(2.0, 100000), (0.5, 100000), (2.0, 100000)])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], checks.FAIL)


class EvidenceFailureTests(unittest.TestCase):
    def test_missing_quota_metadata_rejected(self):
        events = spin_event_stream()
        del events[-1]['cgroup']['cpu.max']['quota_max']
        with self.assertRaises(ValueError):
            checks.spin_summary(events)

    def test_unlimited_cannot_pass_quota_check(self):
        events = spin_event_stream(consumed_cores=1, throttled=10, throttled_usec=1000)
        r, _ = classify(events, checks.classify_quota, 1, .1)
        self.assertEqual(r['status'], checks.FAIL)

    def test_wrong_period_same_ratio_fails(self):
        events = spin_event_stream(consumed_cores=1, quota=1, period_us=50000,
                                   throttled=40, throttled_usec=1000)
        r, _ = classify(events, checks.classify_quota, 1, .1)
        self.assertEqual(r['status'], checks.FAIL)

    def test_partial_stats_rejected(self):
        events = spin_event_stream()
        events[-1]['stat_start']['present'] = 0
        with self.assertRaises(ValueError):
            checks.spin_summary(events)

    def test_failed_cpu_observation_rejected(self):
        for key in ('getcpu_errors', 'getcpu_oor', 'affinity_start_errno', 'affinity_end_errno'):
            with self.subTest(key=key):
                events = spin_event_stream()
                events[-1]['workers'][0][key] = 1
                with self.assertRaises(ValueError):
                    checks.spin_summary(events)

    def test_empty_residency_is_not_cpuset_evidence(self):
        events = spin_event_stream()
        events[-1]['workers'][0]['cpus_seen'] = []
        with self.assertRaises(ValueError):
            checks.spin_summary(events)

    def test_missing_worker_rejected(self):
        events = spin_event_stream()
        events[-1]['workers'].pop()
        with self.assertRaises(ValueError):
            checks.spin_summary(events)

    def test_duplicate_end_rejected(self):
        events = spin_event_stream()
        events.append(copy.deepcopy(events[-1]))
        with self.assertRaises(ValueError):
            checks.spin_summary(events)

    def test_high_cpu_ids_preserved(self):
        events = spin_event_stream(consumed_cores=2, cpuset_eff='64,71',
                                   worker_cpus=[64, 71], affinity_cpus=[64, 71])
        r, _ = classify(events, checks.classify_cpuset, {64, 71})
        self.assertEqual(r['status'], checks.PASS)

    def test_sample_max_read_error_fails(self):
        s = sample(1, 1, 1, 0, 100000, 100000, 1, 1)
        s['max_errno'] = 5
        r = checks.classify_update_run({'samples': [s]}, [(1, 100000)])
        self.assertEqual(r[0]['status'], checks.FAIL)


PERIOD_US = 100000


def irregular_schedule_stream(profile, *, end_s=20.076):
    """Build a v4 sample stream with arbitrary per-interval delivery rates.

    profile: [(t_s, cores_limit, quota_us, {'cap_us':.., 'stable':.., 'q2':..,
              'rate': rate across the interval from the previous sample})].
    Counters are integrated per interval so sparse, irregular timelines (the
    v3 live-update evidence shape) can be reproduced exactly.
    """
    samples = []
    t = proc = used = 0.0
    thr = thr_us = periods = 0
    for t_new, cores, quota, opt in profile:
        dt = t_new - t
        rate = opt.get('rate', cores)
        proc += rate * dt
        used += rate * dt
        if quota:
            events = max(1, int(round(dt / (PERIOD_US / 1e6))))
            thr += events
            thr_us += events * 50000
        periods = max(periods + 1, int(t_new * 10))
        samples.append(sample(
            t_new, proc, used, 0 if quota else 1, quota, PERIOD_US,
            int(thr), int(thr_us), cap_us=opt.get('cap_us', 500),
            stable=opt.get('stable', True), q2=opt.get('q2'),
            steal_s=opt.get('steal_s'), nr_periods=periods))
        t = t_new
    return {'samples': samples, 'wall_s': end_s, 'rates': {},
            'stat_delta': {}, 'workers': [], 'start': {'threads': 4},
            'end': {'wall_ns': int(end_s * 1e9), 'cgroup': {}}}


# t=5.6 first stable half sample -> head guard to 6.1; t=11.5 last half
# stable -> tail guard to 11.3. These profiles are reused below.
SPARSE_TIMES = {
    'seg0': [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5],
    'half': [5.6, 6.7, 8.7, 9.2, 11.5],
    'seg2': [12.3, 12.8, 13.3, 13.8, 14.3, 14.8, 15.3, 15.8,
             16.3, 16.8, 17.3, 17.8, 18.3, 18.8, 19.3, 19.8],
}


def sparse_profile(half_rates=None, half_opts=None):
    """2.0 -> 0.5 -> 2.0 stream at irregular sparse v3-like times."""
    profile = []
    for tt in SPARSE_TIMES['seg0']:
        profile.append((tt, 2.0, 200000, {}))
    half_rates = half_rates or {}
    half_opts = half_opts or {}
    for tt in SPARSE_TIMES['half']:
        opt = dict(half_opts.get(tt, {}))
        if tt in half_rates:
            opt['rate'] = half_rates[tt]
        profile.append((tt, 0.5, 50000, opt))
    for tt in SPARSE_TIMES['seg2']:
        profile.append((tt, 2.0, 200000, {}))
    return profile


SCHEDULE_V = [(2.0, 100000), (0.5, 100000), (2.0, 100000)]


class SparseWindowTests(unittest.TestCase):
    def test_sparse_irregular_samples_yield_valid_windows(self):
        # Seconds-long gaps between samples do not matter: the cgroup counters
        # integrate over them. At-quota delivery on an irregular 0.5 segment
        # passes on the stable interior, with no sample-count guards.
        summary = irregular_schedule_stream(sparse_profile())
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual([r['status'] for r in results],
                         [checks.PASS, checks.PASS, checks.PASS], results)
        half = results[1]['evidence']
        self.assertLess(half['wall_narrow_s'], 2.6)
        self.assertGreater(half['wall_wide_s'], 2.4)
        self.assertEqual(half['used'], 3)       # 6.7, 8.7, 9.2
        self.assertEqual(half['endpoint_indexes'], [10, 12])
        # irregular gaps stay raw: all samples preserved, none silently dropped
        self.assertEqual(half['samples_total'], 30)
        self.assertEqual(half['unstable_total'], 0)

    def test_starved_sparse_window_stays_indeterminate(self):
        # Reproduces the v3 half-segment delivery: multi-second gaps with the
        # whole cgroup getting 0.087-0.40 cores (far below the 0.5 quota).
        rates = {6.7: 1.18, 8.7: 0.145, 9.2: 0.40, 11.5: 0.087}
        summary = irregular_schedule_stream(sparse_profile(half_rates=rates))
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual([r['status'] for r in results],
                         [checks.PASS, checks.INDETERMINATE, checks.PASS], results)
        self.assertIn('below quota bound', results[1]['reason'])
        self.assertLess(results[1]['evidence']['rates_interval']['stat']['low'],
                        results[1]['evidence']['lower_wide'])

    def test_long_capture_excluded_and_window_invalid(self):
        # A 50 ms capture straddles far more than the 25 ms accounting sliver:
        # it is preserved (slow_indexes) but cannot be a rate endpoint, and the
        # interior that depended on it stays indeterminate rather than passing.
        # 6.7 is the only post-guard anchor; with it slow the interior cannot
        # span the required second, and no verdict is allowed.
        opts = {6.7: {'cap_us': 50000}}
        summary = irregular_schedule_stream(sparse_profile(half_opts=opts))
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual(results[1]['status'], checks.INDETERMINATE, results[1])
        self.assertIn('too short', results[1]['reason'])
        self.assertIn(10, results[1]['evidence']['slow_indexes'])
        self.assertEqual(results[1]['evidence']['candidate_groups'], [2])

    def _boundary_profile(self, edge_rate, edge_cap_us):
        # One isolated 1 s endpoint pair inside the half segment; later half
        # samples are slow so the pair is the only interior group.
        opts = {6.7: {'cap_us': edge_cap_us}, 7.7: {'cap_us': edge_cap_us,
                                                    'rate': edge_rate},
                8.7: {'cap_us': 50000}, 9.2: {'cap_us': 50000}}
        profile = []
        for tt in SPARSE_TIMES['seg0']:
            profile.append((tt, 2.0, 200000, {}))
        for tt in sorted(SPARSE_TIMES['half'] + [7.7]):
            profile.append((tt, 0.5, 50000, dict(opts.get(tt, {}))))
        for tt in SPARSE_TIMES['seg2']:
            profile.append((tt, 2.0, 200000, {}))
        return profile

    def test_capture_uncertainty_crossing_is_indeterminate(self):
        # v5 owner counterexample shape: nominal 0.625 edge over a 1 s pair
        # with 20 ms captures -> narrow 0.98 s (worst rate 0.638 > U 0.628)
        # while wide 1.02 s stays schedulable (least rate < U(wide)).
        # Violation is not proven: INDETERMINATE, never FAIL, never PASS.
        summary = irregular_schedule_stream(self._boundary_profile(0.625, 20000))
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual(results[1]['status'], checks.INDETERMINATE, results[1])
        self.assertIn('crosses the quota upper bound', results[1]['reason'])
        ev = results[1]['evidence']
        self.assertLessEqual(ev['least_rate'], ev['upper_wide'])
        self.assertGreater(ev['worst_rate'], ev['upper_narrow'])

    def test_capture_uncertainty_definite_violation_fails(self):
        # Same brackets, but 0.70 cores: even the least plausible rate over
        # the wide interval clears U(wide) -> violation proven -> FAIL.
        summary = irregular_schedule_stream(self._boundary_profile(0.70, 20000))
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual(results[1]['status'], checks.FAIL, results[1])
        ev = results[1]['evidence']
        self.assertGreater(ev['least_rate'], ev['upper_wide'])
        self.assertIn('every plausible capture interval', results[1]['reason'])

    def test_capture_uncertainty_definite_compliance_passes(self):
        # 0.60 cores: even the worst plausible rate over the narrow interval
        # stays inside U(narrow); all other gates hold -> PASS.
        summary = irregular_schedule_stream(self._boundary_profile(0.60, 20000))
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual(results[1]['status'], checks.PASS, results[1])
        ev = results[1]['evidence']
        self.assertLessEqual(ev['worst_rate'], ev['upper_narrow'])


def fixed_boundary_stream(delta_cores):
    """1 s busy run, quota 0.5/100ms, with owner-review geometry: start
    capture ends 20 ms after wall 0; end capture starts at 1.020 s and ends
    1.040 s. Counter deltas are delta_cores*1.0 s."""
    events = spin_event_stream(threads=1, wall_s=1.0, consumed_cores=delta_cores,
                               quota=0.5, period_us=100000,
                               throttled=8, throttled_usec=500000)
    events[0]['cap_end_ns'] = 20_000_000
    events[-1]['wall_ns'] = 1_020_000_000
    events[-1]['duration_ns'] = 1_020_000_000
    events[-1]['cap_end_ns'] = 1_040_000_000
    return events


class FixedCaptureBoundaryTests(unittest.TestCase):
    def setUp(self):
        ref_events = spin_event_stream(consumed_cores=4)
        self.ref = checks.spin_summary(ref_events)

    def _classify(self, delta):
        summary = checks.spin_summary(fixed_boundary_stream(delta))
        return checks.classify_quota(summary, 0.5, 0.1, self.ref), summary

    def test_fixed_crossing_is_indeterminate(self):
        r, s = self._classify(0.635)
        self.assertEqual(r['status'], checks.INDETERMINATE, r)
        self.assertIn('crosses the quota upper bound', r['reason'])
        ev = r['evidence']
        self.assertLessEqual(ev['least_rate'], ev['upper_wide'])
        self.assertGreater(ev['worst_rate'], ev['upper_narrow'])

    def test_fixed_definite_violation_fails(self):
        r, s = self._classify(0.70)
        self.assertEqual(r['status'], checks.FAIL, r)
        self.assertGreater(r['evidence']['least_rate'], r['evidence']['upper_wide'])
        self.assertIn('every plausible capture interval', r['reason'])

    def test_fixed_definite_compliance_passes(self):
        r, s = self._classify(0.60)
        self.assertEqual(r['status'], checks.PASS, r)
        self.assertLessEqual(r['evidence']['worst_rate'], r['evidence']['upper_narrow'])

    def test_zero_width_captures_collapse_to_nominal(self):
        events = fixed_boundary_stream(0.70)
        events[0]['cap_end_ns'] = events[0]['wall_ns']
        events[-1]['cap_end_ns'] = events[-1]['wall_ns']
        summary = checks.spin_summary(events)
        r = checks.classify_quota(summary, 0.5, 0.1, self.ref)
        self.assertEqual(r['status'], checks.FAIL, r)
        self.assertAlmostEqual(r['evidence']['upper_wide'], r['evidence']['upper_narrow'])
        events = fixed_boundary_stream(0.60)
        events[0]['cap_end_ns'] = events[0]['wall_ns']
        events[-1]['cap_end_ns'] = events[-1]['wall_ns']
        r = checks.classify_quota(checks.spin_summary(events), 0.5, 0.1, self.ref)
        self.assertEqual(r['status'], checks.PASS, r)


class OwnerUncertaintyCounterexampleTests(unittest.TestCase):
    """Verbatim synthetic counterexample from
    cpu-sampler-owner-review-v4/owner-uncertainty-counterexample.json:
    v4 returned FAIL; the wide interval admits legal scheduling, so v5 must
    return INDETERMINATE. Not real CPU enforcement."""

    A = {'wall': 0, 'cap_end': 20_000_000, 'proc_ns': 0, 'usage_usec': 0,
         'nr_throttled': 0, 'throttled_usec': 0, 'steal_ns': None}
    B = {'wall': 1_020_000_000, 'cap_end': 1_040_000_000, 'proc_ns': 635_000_000,
         'usage_usec': 635000, 'nr_throttled': 8, 'throttled_usec': 500000,
         'steal_ns': None}

    def test_owner_counterexample_is_indeterminate(self):
        r = checks._window_result(0, 0.5, 100000, dict(self.A), dict(self.B),
                                  None, {'used': 2}, {})
        self.assertEqual(r['status'], checks.INDETERMINATE, r)
        self.assertIn('crosses the quota upper bound', r['reason'])
        ev = r['evidence']
        self.assertAlmostEqual(ev['least_rate'], 0.635 / 1.04)
        self.assertAlmostEqual(ev['upper_wide'], checks.cfs_band(0.5, 0.1, 1.04)[1])
        self.assertAlmostEqual(ev['worst_rate'], 0.635)
        self.assertAlmostEqual(ev['upper_narrow'], checks.cfs_band(0.5, 0.1, 1.0)[1])
        self.assertLess(ev['least_rate'], ev['upper_wide'])
        self.assertGreater(ev['worst_rate'], ev['upper_narrow'])

    def test_owner_geometry_boundaries(self):
        # Same endpoints; 0.70 s delta is a proven violation, 0.60 s a
        # complete upper-bound proof (remaining gates hold -> PASS).
        for delta, expected in ((0.70, checks.FAIL), (0.60, checks.PASS)):
            b = dict(self.B)
            b['proc_ns'] = int(delta * 1e9)
            b['usage_usec'] = int(delta * 1e6)
            r = checks._window_result(0, 0.5, 100000, dict(self.A), b,
                                      None, {'used': 2}, {})
            self.assertEqual(r['status'], expected, (delta, r))


class CaptureStabilityTests(unittest.TestCase):
    def test_transition_during_capture_is_quarantined(self):
        # Both real v3 transition shapes (5.x reads straddle 200k->50k, 11.9
        # straddles 50k->200k) appear as unstable captures on the transition.
        profile = sparse_profile()
        profile.insert(9, (5.4, 2.0, 200000, {'stable': False,
                                              'q2': (0, 50000, 100000), 'rate': 2.0}))
        profile.append((11.9, 0.5, 50000, {'stable': False,
                                           'q2': (0, 200000, 100000), 'rate': 0.5}))
        profile.sort(key=lambda p: p[0])
        summary = irregular_schedule_stream(profile)
        status, ev = checks.stable_timeline(summary['samples'], SCHEDULE_V)
        self.assertEqual(status, checks.PASS, ev)
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual([r['status'] for r in results],
                         [checks.PASS, checks.PASS, checks.PASS], results)
        half = results[1]['evidence']
        self.assertEqual(half['unstable_indexes'], [9, 15])
        self.assertEqual(half['endpoint_indexes'], [11, 13])
        self.assertNotIn(9, half['endpoint_indexes'])
        self.assertNotIn(15, half['endpoint_indexes'])

    def test_unstable_capture_within_one_setting_fails(self):
        # An in-capture flip that returns to the same setting cannot be hidden:
        # it is outside any scheduled transition.
        profile = sparse_profile()
        for p in profile:
            if p[0] == 8.7:
                p[3].update(stable=False, q2=(0, 200000, 100000), rate=0.5)
        summary = irregular_schedule_stream(profile)
        status, ev = checks.stable_timeline(summary['samples'], SCHEDULE_V)
        self.assertEqual(status, checks.FAIL)
        self.assertIn('outside the scheduled transition', ev['reason'])

    def test_stable_key_churn_mid_segment_fails(self):
        # v3-style misdate reconstructed with single reads: a stable 200k
        # observation appearing inside the half segment flunks the timeline.
        profile = sparse_profile()
        for p in profile:
            if p[0] == 9.2:
                p[3].update(rate=2.0)
        # force that one sample to report the recovered quota early
        rebuilt = []
        for tt, cores, quota, opt in profile:
            if tt == 9.2:
                rebuilt.append((tt, 2.0, 200000, opt))
            else:
                rebuilt.append((tt, cores, quota, opt))
        summary = irregular_schedule_stream(rebuilt)
        results = checks.classify_update_run(summary, SCHEDULE_V)
        self.assertEqual(results[0]['status'], checks.FAIL)
        self.assertIn('timeline', results[0]['reason'])


class SampleIntegrityTests(unittest.TestCase):
    def test_stability_marker_must_match_both_reads(self):
        for stable in (0, 1):
            with self.subTest(stable=stable):
                s = sample(1, 1, 1, 0, 100000, 100000, 1, 1)
                s['max_stable'] = stable
                if stable:
                    s['q2_quota'] = 50000
                result = checks.classify_update_run({'samples': [s]}, [(1, 100000)])
                self.assertEqual(result[0]['status'], checks.FAIL)
                self.assertIn('contradicts', result[0]['reason'])

    def test_sequential_captures_cannot_overlap(self):
        a = sample(1, 0.5, 0.5, 0, 50000, 100000, 1, 1, cap_us=300000)
        b = sample(1.2, 0.6, 0.6, 0, 50000, 100000, 2, 2)
        result = checks.classify_update_run({'samples': [a, b]}, [(0.5, 100000)])
        self.assertEqual(result[0]['status'], checks.FAIL)
        self.assertIn('overlap', result[0]['reason'])

    def test_missing_stat_sample_fails(self):
        s = sample(1, 1, 1, 0, 100000, 100000, 1, 1)
        s['stat_errno'] = 5
        r = checks.classify_update_run({'samples': [s]}, [(1, 100000)])
        self.assertEqual(r[0]['status'], checks.FAIL)
        self.assertIn('cpu.stat', r[0]['reason'])

    def test_missing_capture_field_fails(self):
        s = sample(1, 1, 1, 0, 100000, 100000, 1, 1)
        del s['cap_end_ns']
        r = checks.classify_update_run({'samples': [s]}, [(1, 100000)])
        self.assertEqual(r[0]['status'], checks.FAIL)
        self.assertIn('capture fields', r[0]['reason'])

    def test_reverse_capture_clock_fails(self):
        s1 = sample(1.0, 0.5, 0.5, 0, 50000, 100000, 1, 50000)
        s2 = sample(1.2, 0.6, 0.6, 0, 50000, 100000, 2, 100000)
        s2['cap_end_ns'] = s2['wall_ns'] - 1
        s2['cap_dur_ns'] = -1
        r = checks.classify_update_run({'samples': [s1, s2]}, [(0.5, 100000)])
        self.assertEqual(r[0]['status'], checks.FAIL)
        self.assertIn('capture clock reversed', r[0]['reason'])

    def test_reverse_wall_timeline_fails(self):
        s1 = sample(1.2, 0.6, 0.6, 0, 50000, 100000, 2, 100000)
        s2 = sample(1.0, 0.5, 0.5, 0, 50000, 100000, 1, 50000)
        r = checks.classify_update_run({'samples': [s1, s2]}, [(0.5, 100000)])
        self.assertEqual(r[0]['status'], checks.FAIL)
        self.assertIn('timeline', r[0]['reason'])

    def test_reverse_sample_counters_fail(self):
        s1 = sample(1.0, 0.6, 0.6, 0, 50000, 100000, 2, 100000)
        s2 = sample(1.2, 0.5, 0.5, 0, 50000, 100000, 3, 150000)
        r = checks.classify_update_run({'samples': [s1, s2]}, [(0.5, 100000)])
        self.assertEqual(r[0]['status'], checks.FAIL)
        self.assertIn('counters moved backwards', r[0]['reason'])

    def test_sampler_clock_missing_rejected(self):
        for key in ('sampler_cpu_ns',):
            with self.subTest(endpoint=key):
                events = spin_event_stream()
                del events[-1][key]
                with self.assertRaises(ValueError):
                    checks.spin_summary(events)
        events = spin_event_stream()
        del events[0]['sampler_cpu_ns']
        with self.assertRaises(ValueError):
            checks.spin_summary(events)

    def test_steal_reverse_rejected(self):
        s1 = sample(1.0, 0.5, 0.5, 0, 50000, 100000, 1, 50000, steal_s=0.2)
        s2 = sample(1.2, 0.6, 0.6, 0, 50000, 100000, 2, 100000, steal_s=0.1)
        r = checks.classify_update_run({'samples': [s1, s2]}, [(0.5, 100000)])
        self.assertEqual(r[0]['status'], checks.FAIL)
        self.assertIn('steal', r[0]['reason'])

    def test_steal_absent_is_diagnostic_only(self):
        # steal_errno != 0 must never change an enforcement verdict
        s1 = sample(0.1, 0.05, 0.05, 0, 50000, 100000, 1, 50000)
        s2 = sample(1.0, 0.5, 0.5, 0, 50000, 100000, 10, 500000)
        s3 = sample(3.0, 1.5, 1.5, 0, 50000, 100000, 30, 1500000)
        s4 = sample(5.5, 2.75, 2.75, 0, 50000, 100000, 55, 2750000)
        for s in (s1, s2, s3, s4):
            s['steal_errno'] = 2
        r = checks.classify_update_run({'samples': [s1, s2, s3, s4]}, [(0.5, 100000)])
        self.assertEqual(r[0]['status'], checks.PASS, r)
        self.assertIsNone(r[0]['evidence']['steal_cores'])


class FixedPhaseCaptureTests(unittest.TestCase):
    def test_boundary_capture_end_is_required(self):
        for idx in (0, -1):
            with self.subTest(endpoint=idx):
                events = spin_event_stream()
                del events[idx]['cap_end_ns']
                with self.assertRaisesRegex(ValueError, 'capture window end missing'):
                    checks.spin_summary(events)

    def test_start_end_capture_window_plumbed(self):
        events = spin_event_stream(consumed_cores=1.01, quota=1.0,
                                   throttled=40, throttled_usec=int(4.5e6))
        events[0]['cap_end_ns'] = events[0]['wall_ns']
        events[-1]['cap_end_ns'] = events[-1]['wall_ns']
        summary = checks.spin_summary(events)
        self.assertIn('proc', summary['rates_interval'])
        lo, hi = summary['rates_interval']['stat']
        self.assertTrue(lo <= summary['rates']['stat'] <= hi)

    def test_capture_window_reversal_rejected(self):
        events = spin_event_stream()
        events[0]['cap_end_ns'] = events[-1]['wall_ns'] + 1000
        with self.assertRaises(ValueError):
            checks.spin_summary(events)

    def test_steal_surfaced_for_fixed_phases(self):
        events = spin_event_stream(consumed_cores=1.01, quota=1.0,
                                   throttled=40, throttled_usec=int(4.5e6))
        events[0]['steal_errno'] = events[-1]['steal_errno'] = 0
        events[0]['steal_ns'] = 0
        events[-1]['steal_ns'] = int(0.006 * 1e9)
        summary = checks.spin_summary(events)
        self.assertAlmostEqual(summary['steal_s'], 0.001, places=6)

    def test_steal_reverse_fixed_phases_rejected(self):
        events = spin_event_stream()
        events[0]['steal_errno'] = events[-1]['steal_errno'] = 0
        events[0]['steal_ns'] = int(1e9)
        events[-1]['steal_ns'] = 0
        with self.assertRaises(ValueError):
            checks.spin_summary(events)


class CaptureExecutableTests(unittest.TestCase):
    """Malformed/missing/resuming fixtures driven through the probe's real
    capture() via the capture-test subcommand (owner WIP requirement)."""

    GOOD_MAX = "50000 100000\n"
    BAD_MAX = "garbage line\n"
    GOOD_STAT = ("usage_usec 1000\nnr_periods 10\nnr_throttled 2\n"
                 "throttled_usec 500\n")
    BAD_STAT = "usage broken\n"
    GOOD_PROC = "cpu  1 2 3 4 5 6 7 123 9 0\n"
    BAD_PROC = "weird content\n"

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='cpu-cap-fixtures-')
        # Prefer an explicitly supplied already-built probe (callers running
        # under the global build-lock protocol point here); otherwise compile
        # this test's own bounded static binary.
        supplied = os.environ.get('CPU_LIMIT_PROBE_BIN')
        if supplied and pathlib.Path(supplied).is_file() and os.access(supplied, os.X_OK):
            cls.binary = pathlib.Path(supplied)
            cls.supplied = True
            return
        gcc = shutil.which('gcc')
        if gcc is None:
            raise unittest.SkipTest('gcc unavailable')
        cls.binary = pathlib.Path(cls.tmpdir) / 'probe'
        cls.supplied = False
        src = pathlib.Path(__file__).resolve().parents[1] / 'src/cpu_limit_probe.c'
        subprocess.run([gcc, '-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread',
                        str(src), '-o', str(cls.binary)],
                       check=True, timeout=60, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _path(self, name, content=None):
        p = pathlib.Path(self.tmpdir) / name
        if content is not None:
            p.write_text(content)
        return str(p)

    def _capture(self, max_arg, stat_name='good.stat', proc_name='good.proc',
                 count=1, interval_ms=0):
        run = subprocess.run(
            [str(self.binary), 'capture-test', '--max', max_arg,
             '--stat', self._path(stat_name), '--proc', self._path(proc_name),
             '--count', str(count), '--interval-ms', str(interval_ms)],
            check=True, timeout=30, capture_output=True, text=True)
        return [json.loads(line) for line in run.stdout.splitlines() if line]

    def _fixtures(self):
        return {n: self._path(n, c) for n, c in (
            ('good.max', self.GOOD_MAX), ('bad.max', self.BAD_MAX),
            ('good.stat', self.GOOD_STAT), ('bad.stat', self.BAD_STAT),
            ('good.proc', self.GOOD_PROC), ('bad.proc', self.BAD_PROC))}

    def test_malformed_cpu_max_through_capture(self):
        fx = self._fixtures()
        s = self._capture(fx['bad.max'])[0]
        self.assertEqual(s['max_errno'], 71)          # EPROTO
        self.assertEqual(s['max2_errno'], 71)
        self.assertEqual(s['quota_max'], -1)
        self.assertEqual(s['max_stable'], 0)
        self.assertEqual(s['stat_errno'], 0)
        _, failure = checks.normalize_update_samples([s])
        self.assertEqual(failure['status'], checks.FAIL)

    def test_missing_cpu_max_through_capture(self):
        fx = self._fixtures()
        s = self._capture(str(pathlib.Path(self.tmpdir) / 'absent.max'))[0]
        self.assertEqual(s['max_errno'], 2)           # ENOENT
        self.assertEqual(s['max2_errno'], 2)
        self.assertEqual(s['max_stable'], 0)
        _, failure = checks.normalize_update_samples([s])
        self.assertEqual(failure['status'], checks.FAIL)

    def test_malformed_stat_through_capture(self):
        fx = self._fixtures()
        s = self._capture(fx['good.max'], stat_name='bad.stat')[0]
        self.assertEqual(s['stat_errno'], 71)
        self.assertEqual(s['max_errno'], 0)
        self.assertEqual(s['max_stable'], 1)
        _, failure = checks.normalize_update_samples([s])
        self.assertEqual(failure['status'], checks.FAIL)
        self.assertIn('cpu.stat', failure['reason'])

    def test_malformed_proc_is_diagnostic_only_through_capture(self):
        fx = self._fixtures()
        s = self._capture(fx['good.max'], proc_name='bad.proc')[0]
        self.assertEqual(s['steal_errno'], 71)
        self.assertEqual(s['max_stable'], 1)
        self.assertEqual(s['stat_errno'], 0)
        norm, failure = checks.normalize_update_samples([s])
        self.assertIsNone(failure)
        self.assertIsNone(norm[0]['steal_ns'])

    def test_good_fixture_parsed_and_steal_converted(self):
        fx = self._fixtures()
        s = self._capture(fx['good.max'])[0]
        self.assertEqual((s['quota_max'], s['quota'], s['period']), (0, 50000, 100000))
        self.assertEqual((s['q2_quota_max'], s['q2_quota'], s['q2_period']),
                         (0, 50000, 100000))
        self.assertEqual(s['max_stable'], 1)
        self.assertEqual(s['steal_errno'], 0)
        self.assertEqual(s['steal_ns'], 1_230_000_000)   # 123 ticks at CLK_TCK=100

    def test_failed_capture_preserved_then_resumes_next_sample(self):
        fx = self._fixtures()
        appearing = pathlib.Path(self.tmpdir) / 'appearing.max'
        if appearing.exists():
            appearing.unlink()
        timer = threading.Timer(0.05, lambda: shutil.copyfile(fx['good.max'], appearing))
        timer.start()
        try:
            samples = self._capture(str(appearing), count=2, interval_ms=200)
        finally:
            timer.join()
        self.assertEqual(samples[0]['max_errno'], 2)       # first errno preserved
        self.assertEqual(samples[0]['max_stable'], 0)
        self.assertEqual(samples[1]['max_errno'], 0)       # reopened next capture
        self.assertEqual(samples[1]['quota'], 50000)
        self.assertEqual(samples[1]['max_stable'], 1)


class CompletedWorkTests(unittest.TestCase):
    protocol = {'workload_revision': 1, 'steps_per_block': 1024}

    @staticmethod
    def worker(idx, blocks):
        # Literal dependent busy-loop reference, independent of the checker's
        # affine exponentiation. Check at multiple worker seeds and counts.
        value = 0x9e3779b97f4a7c15 ^ idx
        for _ in range(blocks):
            for k in range(1024):
                value = (value * 6364136223846793005 + k + 1) % (1 << 64)
        return {'idx': idx, 'work_blocks': blocks, 'sink': value}

    def test_completed_work_matches_literal_loop(self):
        workers = [self.worker(i, n) for i, n in enumerate((0, 1, 2, 37))]
        r = checks.completed_work(self.protocol, workers, 2.0)
        self.assertEqual(r['status'], checks.PASS)
        self.assertEqual(r['worker_blocks'], [0, 1, 2, 37])
        self.assertEqual(r['total_blocks'], 40)
        self.assertEqual(r['blocks_per_wall_second'], 20)

    def test_missing_counter_does_not_invent_work(self):
        self.assertEqual(checks.completed_work({}, [], 1)['status'], checks.NOT_TESTED)
        with self.assertRaisesRegex(ValueError, 'completed-work evidence'):
            checks.completed_work(self.protocol, [{'idx': 0, 'sink': 1}], 1)

    def test_changed_count_or_result_is_rejected(self):
        original = self.worker(3, 17)
        for key in ('work_blocks', 'sink', 'idx'):
            worker = dict(original)
            worker[key] += 1
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'does not match'):
                checks.completed_work(self.protocol, [worker], 1)

    def test_bad_protocol_and_counters_are_rejected(self):
        for protocol in ({'workload_revision': 2, 'steps_per_block': 1024},
                         {'workload_revision': True, 'steps_per_block': 1024},
                         {'workload_revision': 1, 'steps_per_block': 512}):
            with self.subTest(protocol=protocol), self.assertRaises(ValueError):
                checks.completed_work(protocol, [self.worker(0, 0)], 1)
        for key in ('work_blocks', 'sink'):
            for value in (-1, 1 << 64, 0.5, True, None):
                worker = self.worker(0, 0)
                worker[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    checks.completed_work(self.protocol, [worker], 1)


if __name__ == '__main__':
    unittest.main()
