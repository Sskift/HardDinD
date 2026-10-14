"""Synthetic, bounded tests for cpu_share_checks.

No Docker, no guests, no threads, no clocks: evidence streams are fabricated to
match the cpu_limit_probe JSONL shape.  The negative cases prove that opposite
allocation, unapplied updates, empty windows, starvation, missing commands and
missing fields can never produce a pass.
"""
import importlib.util
import json
import pathlib
import unittest

spec = importlib.util.spec_from_file_location(
    "cpu_share_checks",
    pathlib.Path(__file__).resolve().parents[1] / "scripts/cpu_share_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)

CPU = 2
THREADS = 4
# Guest-monotonic snapshot times (seconds).  B is deliberately staggered
# relative to A; the checker must intersect the timelines rather than trust
# orchestration order.
SNAP_TIMES = {
    'a': [1.0, 9.2, 17.4],
    'b': [1.2, 9.4, 17.6],
}
# True scheduler change points, between the guard bands of the snapshots.
SWITCH_A = [9.3, 17.5]
T_END = {'a': 24.9, 'b': 25.1}
RATE_PLAN = {'a': [2 / 3, 1 / 3, 0.5], 'b': [1 / 3, 2 / 3, 0.5]}
# shares -> synthetic cpu.weight (monotonic stand-in for the kernel mapping)
WEIGHT_OF = {1024: 39, 512: 20}


def cgroup(weight, cpu=CPU):
    return {
        "cpu.max": {"errno": 0, "raw": "max 100000", "quota": 0,
                    "period": 100000, "quota_max": 1},
        "cpuset.cpus": {"errno": 0, "raw": str(cpu)},
        "cpuset.cpus.effective": {"errno": 0, "raw": str(cpu)},
        "cpu.weight": {"errno": 0, "raw": str(weight)},
        "cpu.stat": {"present": 1, "usage_usec": 0, "nr_periods": 0,
                     "nr_throttled": 0, "throttled_usec": 0,
                     "nr_bursts": 0, "burst_usec": 0,
                     "raw": "usage_usec 0\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"}}


def cumulative_ns(segments, t):
    """Integral of a piecewise-constant per-core rate schedule, in ns."""
    total = 0.0
    for rate, lo, hi in segments:
        if t > lo:
            total += rate * (min(t, hi) - lo)
        if t <= hi:
            break
    return int(round(total * 1e9))


def segments_for(rate_stage):
    return [(rate_stage[0], 0.0, SWITCH_A[0]),
            (rate_stage[1], SWITCH_A[0], SWITCH_A[1]),
            (rate_stage[2], SWITCH_A[1], 26.0)]


def build_spin(container, rate_stages=None, *, weight_start=None, weight_end=None,
               residency_cpu=CPU, affinity_cpu=CPU, cpu=CPU, nproc=4,
               sample_fields=None, drop_sample_field=None, end=None):
    rate_stages = rate_stages or RATE_PLAN[container]
    weight_start = WEIGHT_OF[checks.DEFAULT_PLAN[0][container]] if weight_start is None else weight_start
    weight_end = WEIGHT_OF[checks.DEFAULT_PLAN[2][container]] if weight_end is None else weight_end
    segs = segments_for(rate_stages)
    tend = (end if end is not None else T_END[container])
    samples = []
    periods = 0
    t = 0.1
    while t < tend - 0.05:
        proc = cumulative_ns(segs, t)
        used = int(round(proc / 1000))
        periods += 1
        row = {"event": "sample", "wall_ns": int(round(t * 1e9)),
               "proc_cpu_ns": proc, "quota_max": 1, "quota": 0,
               "period": 100000, "max_errno": 0, "stat_errno": 0,
               "usage_usec": used, "nr_periods": periods,
               "nr_throttled": 0, "throttled_usec": 0}
        if sample_fields:
            row.update(sample_fields)
        if drop_sample_field:
            row.pop(drop_sample_field, None)
        samples.append(row)
        t += 0.1
    proc_total = cumulative_ns(segs, tend)
    base, rem = divmod(proc_total, THREADS)
    workers = []
    given = 0
    for i in range(THREADS):
        cpu_ns = base + (rem if i == THREADS - 1 else 0)
        given += cpu_ns
        workers.append({"idx": i, "cpu_ns": cpu_ns, "getcpu_errors": 0,
                        "getcpu_oor": 0, "affinity_start_errno": 0,
                        "affinity_end_errno": 0,
                        "cpus_seen": [residency_cpu],
                        "affinity_start": [affinity_cpu],
                        "affinity_end": [affinity_cpu], "sink": 1})
    assert given == proc_total
    end_stat = {"present": 1, "usage_usec": int(round(proc_total / 1000)),
                "nr_periods": periods, "nr_throttled": 0, "throttled_usec": 0,
                "nr_bursts": 0, "burst_usec": 0}
    return [
        {"event": "start", "wall_ns": 0, "proc_cpu_ns": 0, "threads": THREADS,
         "nproc": nproc, "clk_tck": 100, "affinity": [cpu],
         "cgroup": cgroup(weight_start, cpu)},
        *samples,
        {"event": "end", "wall_ns": int(round(tend * 1e9)),
         "duration_ns": int(round(tend * 1e9)), "proc_cpu_ns": proc_total,
         "workers": workers, "worker_cpu_ns": proc_total,
         "stat_start": {"present": 1, "usage_usec": 0, "nr_periods": 0,
                        "nr_throttled": 0, "throttled_usec": 0,
                        "nr_bursts": 0, "burst_usec": 0},
         "stat_end": end_stat, "cgroup": cgroup(weight_end, cpu)},
    ]


def build_snapshot(container, stage, weight=None, when=None, cpu=CPU):
    weight = WEIGHT_OF[checks.DEFAULT_PLAN[stage][container]] if weight is None else weight
    return {"event": "snapshot",
            "wall_ns": int(round((SNAP_TIMES[container][stage] if when is None else when) * 1e9)),
            "proc_cpu_ns": 1000, "pid": 42, "cgroup": cgroup(weight, cpu)}


def inspection(container, stage, cid, *, shares=None, running=True,
               exit_code=0, cpu_str=None, quota=0, id_override=None):
    plan_stage = min(stage, checks.STAGES - 1)
    shares = checks.DEFAULT_PLAN[plan_stage][container] if shares is None else shares
    return {"stage": stage, "shares": shares,
            "cpuset": str(CPU) if cpu_str is None else cpu_str,
            "quota": quota, "period": 0, "id": id_override or cid,
            "running": running, "exit_code": exit_code,
            "raw": "%s|%s|%s|0|%s|%s|%s" % (
                shares, cpu_str if cpu_str is not None else CPU, quota,
                id_override or cid, str(running).lower(), exit_code)}


def command_rows(labels=None, *, bad_label=None, bad_field='exit_code',
                 bad_value=1, timeout_labels=(), dirty_labels=()):
    labels = REQUIRED if labels is None else labels
    rows = []
    for label in labels:
        row = {"label": label, "command": ["docker"], "exit_code": 0,
               "timed_out": label in timeout_labels, "elapsed_s": 0.01,
               "cleanup": {"clean_shutdown": label not in dirty_labels,
                           "remaining_live_pids": [], "remaining_zombie_pids": []}}
        if label == bad_label:
            row[bad_field] = bad_value
        rows.append(row)
    return rows


REQUIRED = [
    'build-probe', 'gcc-version', 'probe-static-check', 'probe-selftest',
    'instance-start', 'daemon-info', 'image-import', 'start-a', 'start-b',
    'snapshot-a-0', 'snapshot-b-0', 'inspect-a-0', 'inspect-b-0',
    'update-a-1', 'update-b-1', 'snapshot-a-1', 'snapshot-b-1', 'inspect-a-1', 'inspect-b-1',
    'update-b-2', 'snapshot-a-2', 'snapshot-b-2', 'inspect-a-2', 'inspect-b-2',
    'wait-a', 'wait-b', 'logs-a', 'logs-b', 'final-a', 'final-b',
    'rm-a', 'rm-b', 'ps-leftovers', 'instance-stop']

CIDS = {'a': 'id-a' * 16, 'b': 'id-b' * 16}


def build_evidence(*, rates=None, snapshot_weights=None, snapshot_times=None,
                   residency_cpu=CPU, affinity_cpu=CPU, sample_fields=None,
                   drop_sample_field=None, inspect_overrides=None,
                   omit_command=None, omit_snapshot=None, omit_inspection=None,
                   final_state=None, leftovers='', hashes_same=True,
                   ncpu=4, cgroup_version='2', spin_error=None,
                   update_exit=0, extra_updates=(), stream_end=None):
    rates = rates or {c: RATE_PLAN[c] for c in ('a', 'b')}
    snapshot_weights = snapshot_weights or {}
    snapshot_times = snapshot_times or {}
    containers = {}
    for c in ('a', 'b'):
        events = build_spin(c, rates[c], residency_cpu=residency_cpu,
                            affinity_cpu=affinity_cpu, sample_fields=sample_fields,
                            drop_sample_field=drop_sample_field, end=stream_end)
        snaps = []
        for k in range(3):
            if omit_snapshot == (c, k):
                continue
            w = snapshot_weights.get((c, k))
            when = snapshot_times.get((c, k))
            snaps.append(build_snapshot(c, k, weight=w, when=when))
        insp = []
        for k in range(3):
            if omit_inspection == (c, k):
                continue
            override = (inspect_overrides or {}).get((c, k), {})
            insp.append(inspection(c, k, CIDS[c], **override))
        final_override = {'running': False}
        final_override.update((inspect_overrides or {}).get((c, 3), {}))
        insp.append(inspection(c, 3, CIDS[c], **final_override))
        containers[c] = {"name": "cpushare-" + c, "created_id": CIDS[c],
                         "snapshots": snaps, "inspections": insp,
                         "spin_events": events, "spin_error": spin_error,
                         "wait_exit": 0, "wait_output": "0", "probe_exit": "0",
                         "removed": True}
    labels = [l for l in REQUIRED if l != omit_command]
    plan = [dict(s) for s in checks.DEFAULT_PLAN]
    updates = []
    for stage in (1, 2):
        for c in ('a', 'b'):
            if plan[stage][c] != plan[stage - 1][c]:
                updates.append({"label": "update-%s-%d" % (c, stage), "stage": stage,
                                "container": c, "from_shares": plan[stage - 1][c],
                                "to_shares": plan[stage][c], "exit_code": update_exit,
                                "timed_out": False})
    updates.extend(extra_updates)
    digest = "deadbeef"
    return {
        "params": {"guest_cpus": 4, "shared_cpu": CPU, "threads": THREADS,
                   "phase_ms": 9000, "sample_ms": 100, "guard_s": 0.6,
                   "min_window_s": 4.0, "plan": plan},
        "daemon": {"ncpu": ncpu, "cgroup_version": cgroup_version,
                   "server_version": "27.0.0", "info_raw": "ncpu=4 cgroup=2"},
        "containers": containers,
        "updates": updates,
        "commands": command_rows(labels),
        "required_commands": list(REQUIRED),
        "optional_commands": [],
        "leftovers_stdout": leftovers,
        "final_state": final_state if final_state is not None else {
            "phase": "stopped", "forced_shutdown": False,
            "guest_cleanup": {"clean_shutdown": True},
            "network_cleanup": {"clean_shutdown": True}},
        "hashes_before": {"/k": digest} if hashes_same else {"/k": digest},
        "hashes_after": {"/k": digest if hashes_same else "abad1dea"}}


class HappyPathTests(unittest.TestCase):
    def test_full_plan_passes(self):
        ev = build_evidence()
        results = checks.evaluate(ev)
        self.assertEqual(checks.overall_verdict(results, []), checks.PASS,
                         {k: (v['status'], v['reason']) for k, v in results.items()
                          if v['status'] != checks.PASS})

    def test_every_verdict_is_json_native(self):
        # No Python sets or other non-JSON types may ever reach the report.
        results = checks.evaluate(build_evidence())
        blob = json.dumps(results)
        self.assertEqual(results, json.loads(blob))

    def test_statuses_separated_for_owner_summary(self):
        self.assertEqual({checks.PASS, checks.FAIL, checks.INDETERMINATE,
                          checks.NOT_TESTED},
                         {'pass', 'fail', 'indeterminate', 'not_tested'})


class WeightEffectTests(unittest.TestCase):
    def test_opposite_allocation_cannot_pass(self):
        # Stage 0 claims A heavy (1024) but A is observed light: weights did
        # not steer allocation (or evidence was swapped).
        ev = build_evidence(rates={'a': [1 / 3, 2 / 3, 0.5],
                                   'b': [2 / 3, 1 / 3, 0.5]})
        results = checks.evaluate(ev)
        self.assertEqual(results['phase_2to1']['status'], checks.FAIL, results['phase_2to1'])
        self.assertEqual(checks.overall_verdict(results, []), checks.FAIL)

    def test_equal_control_with_skew_fails(self):
        ev = build_evidence(rates={'a': [2 / 3, 1 / 3, 0.72],
                                   'b': [1 / 3, 2 / 3, 0.28]})
        results = checks.evaluate(ev)
        self.assertEqual(results['phase_equal_control']['status'], checks.FAIL)

    def test_opposite_weight_configuration_fails(self):
        # Snapshots show the heavy weight on B during stage 0 despite plan.
        ev = build_evidence(snapshot_weights={('a', 0): WEIGHT_OF[512],
                                              ('b', 0): WEIGHT_OF[1024]})
        results = checks.evaluate(ev)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL,
                         results['weight_timeline'])

    def test_unapplied_update_fails(self):
        # A stays at its stage-0 weight after the stage-1 update: the same
        # shares value would have to map to two different kernel weights.
        ev = build_evidence(snapshot_weights={('a', 1): WEIGHT_OF[1024]})
        results = checks.evaluate(ev)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL,
                         results['weight_timeline'])
        self.assertIn('cpu.weight', results['weight_timeline']['reason'])

    def test_equal_stage_with_unequal_weights_fails(self):
        ev = build_evidence(snapshot_weights={('b', 2): WEIGHT_OF[1024]})
        results = checks.evaluate(ev)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL)

    def test_missing_post_update_snapshot_fails(self):
        ev = build_evidence(omit_snapshot=('a', 1))
        results = checks.evaluate(ev)
        self.assertEqual(results['probe_streams']['status'], checks.FAIL)
        self.assertNotEqual(checks.overall_verdict(results, []), checks.PASS)

    def test_missing_stage_inspection_fails(self):
        ev = build_evidence(omit_inspection=('b', 2))
        results = checks.evaluate(ev)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL)

    def test_inspect_shares_drift_fails(self):
        ev = build_evidence(inspect_overrides={('a', 1): {'shares': 1024}})
        results = checks.evaluate(ev)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL)

    def test_quota_present_fails(self):
        ev = build_evidence(inspect_overrides={('b', 0): {'quota': 50000}})
        results = checks.evaluate(ev)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL)

    def test_observed_weight_change_is_required_not_just_inspect(self):
        # Inspect claims the change; in-container cpu.weight does not.
        ev = build_evidence(snapshot_weights={('b', 1): WEIGHT_OF[512]})
        results = checks.evaluate(ev)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL)


class WindowAndLoadTests(unittest.TestCase):
    def test_empty_window_is_indeterminate_never_pass(self):
        # Stage-1 snapshots land milliseconds apart: no sustained interior.
        ev = build_evidence(snapshot_times={('a', 1): 9.25, ('b', 1): 9.3,
                                            ('a', 2): 9.4, ('b', 2): 9.45})
        results = checks.evaluate(ev)
        self.assertEqual(results['phase_reversed_1to2']['status'],
                         checks.INDETERMINATE, results['phase_reversed_1to2'])
        self.assertNotEqual(checks.overall_verdict(results, []), checks.PASS)

    def test_insufficient_busy_load_is_indeterminate(self):
        ev = build_evidence(rates={'a': [0.32, 0.16, 0.25],
                                   'b': [0.18, 0.34, 0.25]})
        results = checks.evaluate(ev)
        for name in ('phase_2to1', 'phase_reversed_1to2', 'phase_equal_control'):
            self.assertEqual(results[name]['status'], checks.INDETERMINATE,
                             (name, results[name]))
            self.assertIn('saturate', results[name]['reason'])

    def test_starvation_fails(self):
        ev = build_evidence(rates={'a': [0.97, 0.03, 0.5],
                                   'b': [0.03, 0.97, 0.5]})
        results = checks.evaluate(ev)
        self.assertEqual(results['phase_2to1']['status'], checks.FAIL)
        self.assertIn('starved', results['phase_2to1']['reason'])

    def test_counter_disagreement_is_indeterminate(self):
        # proc clock and cpu.stat stay internally monotonic but tell different
        # allocation stories; the run cannot be scored.
        ev = build_evidence()
        for s in ev['containers']['a']['spin_events']:
            if s.get('event') == 'sample':
                s['usage_usec'] = int(s['proc_cpu_ns'] / 1000 * 1.4)
        results = checks.evaluate(ev)
        self.assertEqual(results['phase_2to1']['status'], checks.INDETERMINATE)
        self.assertIn('disagree', results['phase_2to1']['reason'])

    def test_more_than_one_core_through_cpuset_fails(self):
        ev = build_evidence(rates={'a': [1.2, 0.6, 0.9],
                                   'b': [0.6, 1.2, 0.9]})
        results = checks.evaluate(ev)
        self.assertEqual(results['phase_2to1']['status'], checks.FAIL)

    def test_staggered_edges_are_excluded(self):
        summaries = {c: checks.validate_stream(build_spin(c), CPU) for c in ('a', 'b')}
        snaps = {c: [build_snapshot(c, k) for k in range(3)] for c in ('a', 'b')}
        windows = checks.build_windows(summaries, snaps)
        lo0_ns = max(SNAP_TIMES[c][0] for c in ('a', 'b')) * 1e9 + 0.6e9
        hi0_ns = min(SNAP_TIMES[c][1] for c in ('a', 'b')) * 1e9 - 0.6e9
        self.assertEqual((windows[0]['lo_ns'], windows[0]['hi_ns']),
                         (int(lo0_ns), int(hi0_ns)))
        self.assertTrue(all(w['adequate'] for w in windows))


class EvidenceIntegrityTests(unittest.TestCase):
    def test_missing_sample_field_rejected(self):
        ev = build_evidence(drop_sample_field='usage_usec')
        results = checks.evaluate(ev)
        self.assertEqual(results['probe_streams']['status'], checks.FAIL)

    def test_residency_escape_fails(self):
        ev = build_evidence(residency_cpu=3)
        results = checks.evaluate(ev)
        self.assertEqual(results['probe_streams']['status'], checks.FAIL)
        self.assertIn('outside the shared CPU', results['probe_streams']['reason'])

    def test_affinity_escape_fails(self):
        ev = build_evidence(affinity_cpu=0)
        results = checks.evaluate(ev)
        self.assertEqual(results['probe_streams']['status'], checks.FAIL)

    def test_spin_parse_error_fails(self):
        ev = build_evidence(spin_error='JSONDecodeError: line 3')
        results = checks.evaluate(ev)
        self.assertEqual(results['probe_streams']['status'], checks.FAIL)
        self.assertEqual(results['phase_2to1']['status'], checks.NOT_TESTED)

    def test_quota_max_sample_fails(self):
        ev = build_evidence(sample_fields={'quota_max': 0, 'quota': 200000})
        results = checks.evaluate(ev)
        self.assertEqual(results['probe_streams']['status'], checks.FAIL)

    def test_non_monotonic_snapshot_time_fails(self):
        ev = build_evidence(snapshot_times={('b', 2): 9.0})
        results = checks.evaluate(ev)
        self.assertEqual(results['probe_streams']['status'], checks.FAIL)

    def test_container_identity_change_fails(self):
        ev = build_evidence(inspect_overrides={('a', 2): {'id_override': 'id-x' * 12}})
        results = checks.evaluate(ev)
        self.assertEqual(results['container_identity']['status'], checks.FAIL)

    def test_nonzero_probe_exit_fails(self):
        ev = build_evidence(
            inspect_overrides={('a', 3): {'running': True, 'exit_code': 1}})
        # The final inspect above already carries the failure; the observed
        # probe wait output must agree with it.
        ev['containers']['a']['probe_exit'] = '1'
        results = checks.evaluate(ev)
        self.assertEqual(results['container_identity']['status'], checks.FAIL)
        # And a clean exit code with running still true must likewise fail.
        ev2 = build_evidence(inspect_overrides={('a', 3): {'running': True}})
        results2 = checks.evaluate(ev2)
        self.assertEqual(results2['container_identity']['status'], checks.FAIL)

    def test_wrong_guest_size_fails(self):
        ev = build_evidence(ncpu=1)
        results = checks.evaluate(ev)
        self.assertEqual(results['guest_topology']['status'], checks.FAIL)

    def test_cgroup_v1_fails(self):
        ev = build_evidence(cgroup_version='1')
        results = checks.evaluate(ev)
        self.assertEqual(results['guest_topology']['status'], checks.FAIL)


class CommandAndLifecycleTests(unittest.TestCase):
    def test_missing_required_command_fails(self):
        ev = build_evidence(omit_command='update-b-2')
        results = checks.evaluate(ev)
        self.assertEqual(results['command_ledger']['status'], checks.FAIL)
        self.assertIn('update-b-2', results['command_ledger']['evidence']['missing'])

    def test_nonzero_exit_fails(self):
        rows = command_rows(bad_label='wait-a', bad_field='exit_code', bad_value=1)
        result = checks.command_ledger(rows, REQUIRED)
        self.assertEqual(result['status'], checks.FAIL)

    def test_timeout_fails(self):
        rows = command_rows(timeout_labels={'snapshot-a-1'})
        result = checks.command_ledger(rows, REQUIRED)
        self.assertEqual(result['status'], checks.FAIL)

    def test_dirty_process_group_fails(self):
        rows = command_rows(dirty_labels={'instance-stop'})
        result = checks.command_ledger(rows, REQUIRED)
        self.assertEqual(result['status'], checks.FAIL)
        self.assertFalse(result['evidence']['problems'][0]['group_clean'])

    def test_optional_command_still_audited(self):
        rows = command_rows()
        rows.append({'label': 'cleanup-a', 'exit_code': 1, 'timed_out': True,
                     'cleanup': {'clean_shutdown': False}})
        result = checks.command_ledger(rows, REQUIRED, ['cleanup-a'])
        self.assertEqual(result['status'], checks.FAIL)

    def test_leftover_containers_fail(self):
        result = checks.leftovers_verdict('abc123\n', ['cpushare-a', 'cpushare-b'])
        self.assertEqual(result['status'], checks.FAIL)

    def test_never_collected_leftover_check_fails(self):
        result = checks.leftovers_verdict(None, ['cpushare-a', 'cpushare-b'])
        self.assertEqual(result['status'], checks.FAIL)

    def test_forced_shutdown_fails(self):
        result = checks.lifecycle_verdict(
            {'phase': 'failed', 'forced_shutdown': True,
             'guest_cleanup': {'clean_shutdown': True},
             'network_cleanup': {'clean_shutdown': True}})
        self.assertEqual(result['status'], checks.FAIL)

    def test_changed_inputs_fail(self):
        result = checks.inputs_verdict({'/k': 'aa'}, {'/k': 'bb'})
        self.assertEqual(result['status'], checks.FAIL)
        result = checks.inputs_verdict({'/k': 'aa'}, {})
        self.assertEqual(result['status'], checks.FAIL)

    def test_missing_update_record_fails(self):
        plan = [dict(s) for s in checks.DEFAULT_PLAN]
        result = checks.update_ledger([], plan)
        self.assertEqual(result['status'], checks.FAIL)

    def test_failed_update_command_fails(self):
        ev = build_evidence(update_exit=1)
        results = checks.evaluate(ev)
        self.assertEqual(results['update_ledger']['status'], checks.FAIL)

    def test_leftovers_check_live(self):
        ev = build_evidence(leftovers='deadbeef\n')
        results = checks.evaluate(ev)
        self.assertEqual(results['leftovers']['status'], checks.FAIL)


class ParserTests(unittest.TestCase):
    def test_parse_weight(self):
        self.assertEqual(checks.parse_weight('100'), 100)
        for bad in ('', '0', '10001', 'x', '100\nj', None):
            with self.assertRaises(ValueError):
                checks.parse_weight(bad)

    def test_parse_cpu_list(self):
        self.assertEqual(checks.parse_cpu_list('2'), {2})
        self.assertEqual(checks.parse_cpu_list('0-3'), {0, 1, 2, 3})
        self.assertEqual(checks.parse_cpu_list(''), set())

    def test_overall_verdict_precedence_and_missing_rows(self):
        row = {'status': checks.PASS, 'reason': '', 'evidence': {}}
        self.assertEqual(checks.overall_verdict({'a': row}, required=['a']), checks.PASS)
        # A required check whose row is absent is a FAIL, never silent pass.
        self.assertEqual(checks.overall_verdict({}, ['ghost']), checks.FAIL)
        nottest = {'status': checks.NOT_TESTED, 'reason': '', 'evidence': {}}
        self.assertEqual(checks.overall_verdict({'a': nottest}, required=['a']),
                         checks.NOT_TESTED)
        indet = {'status': checks.INDETERMINATE, 'reason': '', 'evidence': {}}
        self.assertEqual(checks.overall_verdict({'a': indet, 'b': nottest},
                                                required=['a', 'b']),
                         checks.INDETERMINATE)
        fail = {'status': checks.FAIL, 'reason': '', 'evidence': {}}
        self.assertEqual(checks.overall_verdict({'a': fail, 'b': indet},
                                                required=['a', 'b']), checks.FAIL)
        self.assertEqual(checks.overall_verdict({'a': row}, errors=['boom'],
                                                required=['a']),
                         checks.FAIL)


if __name__ == '__main__':
    unittest.main()
