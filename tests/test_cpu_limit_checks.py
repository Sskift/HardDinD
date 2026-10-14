import importlib.util
import copy
import json
import pathlib
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
    start = {"event": "start", "wall_ns": 0, "proc_cpu_ns": 0, "threads": threads,
             "sampler_cpu_ns": 0, "load_deadline_ns": wall_ns,
             "nproc": 4, "clk_tck": 100, "affinity": affinity_cpus,
             "cgroup": cgroup(quota, period_us, 0, 0, 0.0, cpuset_eff, 0)}
    workers = [{"idx": i, "cpu_ns": worker_cpu, "cpus_seen": worker_cpus,
                "getcpu_errors": 0, "getcpu_oor": 0,
                "affinity_start_errno": 0, "affinity_end_errno": 0,
                "affinity_start": affinity_cpus, "affinity_end": affinity_cpus,
                "sink": 1} for i in range(threads)]
    end = {"event": "end", "wall_ns": wall_ns, "duration_ns": wall_ns,
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


def sample(t, proc_cpu_s, stat_cpu_s, quota_max, quota, period, throttled, throttled_us):
    return {"event": "sample", "wall_ns": int(t * 1e9),
            "proc_cpu_ns": int(proc_cpu_s * 1e9),
            "quota_max": quota_max, "quota": quota, "period": period,
            "stat_errno": 0, "max_errno": 0, "usage_usec": int(stat_cpu_s * 1e6),
            "nr_periods": int(t * 10), "nr_throttled": throttled,
            "throttled_usec": throttled_us}


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


if __name__ == '__main__':
    unittest.main()
