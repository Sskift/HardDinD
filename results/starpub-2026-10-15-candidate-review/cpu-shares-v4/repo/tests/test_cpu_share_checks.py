"""Bounded synthetic tests for the v3 bracketed-capture shares checker.

No guests, no busy load: contend JSONL is fabricated from explicit capture
spans [wall_ns, cap_end_ns], weight-bracket pairs and joint rate regimes. The
fixtures emphasize what v2 got wrong: captures that straddle weight changes,
multi-second stable captures whose duration uncertainty crosses a fraction
band edge, malformed/absent/resuming reads and non-overlap.

Set HARDDIND_SHARE_PROBE=/path/to/cpu-share-probe to additionally run the real
binary's capture-test against file/FIFO fixtures (no CPU load; reads only).
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location(
    "cpu_share_checks",
    pathlib.Path(__file__).resolve().parents[1] / "scripts/cpu_share_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)

CPU = 2
THREADS = 4
WHI, WLO = 100, 59           # computed labels from the PINNED log2-fit formula
FHI = WHI / (WHI + WLO)      # 0.6289...
FLO = 1.0 - FHI
DT = 0.1
CAP = 0.001                  # healthy bracketed capture ~1 ms
DURATION = 32.0
CIDS = {'a': 'ida' * 16, 'b': 'idb' * 16}
SHA64 = '0' * 64

# Planned transition fixtures: (container, nominal t0, t1, old, new).
TRANSITIONS = [('a', 9.20, 9.35, WHI, WLO),
               ('b', 9.40, 9.55, WLO, WHI),
               ('b', 18.50, 18.65, WHI, WLO)]


def cg_endpoint(weight, cpu=CPU):
    return {
        "cpu.max": {"errno": 0, "raw": "max 100000", "quota": 0,
                    "period": 100000, "quota_max": 1},
        "cpu.max2": {"errno": 0, "raw": "max 100000", "quota": 0,
                     "period": 100000, "quota_max": 1},
        "max_stable": 1,
        "cpu.weight": {"errno": 0, "raw": str(weight), "invalid": 0},
        "cpu.weight2": {"errno": 0, "raw": str(weight), "invalid": 0},
        "weight_stable": 1,
        "cpuset.cpus.effective": {"errno": 0, "raw": str(cpu)},
        "cpuset.cpus.effective2": {"errno": 0, "raw": str(cpu)},
        "cpuset_stable": 1,
        "cpu.stat": {"present": 1, "usage_usec": 0, "nr_periods": 0,
                     "nr_throttled": 0, "throttled_usec": 0, "nr_bursts": 0,
                     "burst_usec": 0,
                     "raw": "usage_usec 0\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"}}


def transition_at(t):
    for c, t0, t1, old, new in TRANSITIONS:
        if abs(t - t0) < 1e-9:
            return t1, old, new
    return None


def rate_for(c, t):
    if c == 'a':
        if t < 9.2:
            return FHI
        if 9.2 <= t < 9.4:
            return 0.5
        if 9.4 <= t < 18.7:
            return FLO
        return 0.5
    if t < 9.4:
        return FLO
    if 9.4 <= t < 9.6 or 18.5 <= t < 18.7:
        return 0.5
    if 9.6 <= t < 18.5:
        return FHI
    return 0.5


def build_stream(c, *, cap_overrides=None, weight_overrides=None, kind_overrides=None,
                 cpuset_overrides=None, extra_unstable=None, error_spans=(),
                 skip_holes=(), residency_cpu=CPU, affinity_cpu=CPU,
                 end_at=None, offset=0.10, sampler_cores=0.002, rate_fn=None):
    """cap_overrides: {t0: cap_end_time}; weight_overrides: {t0:(w1,w2)};
    kind_overrides: {t0: 'error'}; rate_fn: joint CPU accrual rate(t)."""
    rate_fn = rate_fn or (lambda t: rate_for(c, t))
    cap_overrides = cap_overrides or {}
    weight_overrides = weight_overrides or {}
    kind_overrides = kind_overrides or {}
    cpuset_overrides = cpuset_overrides or {}
    extra_unstable = extra_unstable or {}
    error_spans = set(error_spans)
    deadline = offset + DURATION
    samples = []
    usage_us = 0.0          # float residual, emitted as integer microseconds
    usage_int = 0
    sampler_cpu = 0.0
    prev_t = offset
    periods = 0
    t = round(offset + DT, 4)
    blocked_until = 0.0
    while t <= deadline + 0.001:
        if t < blocked_until:
            # A long bracketed capture in flight produces no other samples.
            prev_t = t
            t = round(t + DT, 4)
            continue
        if any(lo <= t < hi for lo, hi in skip_holes):
            prev_t = t
            t = round(t + DT, 4)
            continue
        tr = None
        for cc, t0c, t1c, oldc, newc in TRANSITIONS:
            if cc == c and abs(t - t0c) < 1e-9:
                tr = (t1c, oldc, newc)
                break
        if t in cap_overrides:
            cap_end = cap_overrides[t]
        elif tr is not None:
            cap_end = tr[0]
        else:
            cap_end = t + CAP
        if cap_end > t + CAP + 1e-9:
            blocked_until = cap_end
        # Integer-microsecond cgroup accounting; proc_ns is derived exactly so
        # the synthetic proc/stat intervals agree without rounding noise.
        usage_us += rate_fn(t) * (t - prev_t) * 1e6
        usage_int = int(round(usage_us))
        sampler_cpu += sampler_cores * (t - prev_t)
        prev_t = t
        periods += 1
        if c == 'a':
            w1 = w2 = WHI if t < 9.2 else WLO
        else:
            w1 = w2 = WLO if t < 9.4 else (WHI if t < 18.5 else WLO)
        if tr:
            w1, w2 = tr[1], tr[2]
        if t in weight_overrides:
            w1, w2 = weight_overrides[t]
            if t not in cap_overrides and tr is None:
                cap_end = t + CAP
        w1e = w2e = 0
        w1inv = w2inv = 0
        c1e = c2e = 0
        max1e = max2e = state = 0
        stable_w = int(w1 == w2)
        c1raw = c2raw = cpuset_overrides.get(t, str(CPU))
        if t in kind_overrides or t in error_spans:
            spec_kind = kind_overrides.get(t, 'error')
            if spec_kind == 'weight_malformed':
                w1 = w2 = 0
                w1e = w2e = 71
                w1inv = w2inv = 1
                stable_w = 0
            elif spec_kind == 'weight_absent':
                w1 = w2 = 0
                w1e = w2e = 2
                stable_w = 0
            elif spec_kind == 'cpuset_err':
                c1e = 2
            elif spec_kind == 'max_err':
                max1e = 2
        proc_ns = usage_int * 1000
        row = {"event": "sample", "wall_ns": int(round(t * 1e9)),
               "cap_end_ns": int(round(cap_end * 1e9)),
               "cap_dur_ns": int(round((cap_end - t) * 1e9)),
               "proc_cpu_ns": proc_ns,
               "sampler_cpu_ns": int(round(sampler_cpu * 1e9)),
               "weight1": w1, "weight2": w2, "weight1_errno": w1e,
               "weight2_errno": w2e, "weight1_invalid": w1inv,
               "weight2_invalid": w2inv, "weight_stable": stable_w,
               "cpuset1_errno": c1e, "cpuset2_errno": c2e,
               "cpuset_stable": int(c1e == 0 and c2e == 0 and c1raw == c2raw),
               "cpuset1_raw": c1raw, "cpuset2_raw": c2raw,
               "quota_max": 1, "quota": 0, "period": 100000, "max_errno": max1e,
               "q2_quota_max": 1, "q2_quota": 0, "q2_period": 100000,
               "max2_errno": max2e, "max_stable": int(max1e == 0 and max2e == 0),
               "share_valid": int(stable_w and c1e == 0 and c2e == 0
                                  and max1e == 0 and max2e == 0 and state == 0),
               "stat_errno": 0, "usage_usec": usage_int, "nr_periods": periods,
               "nr_throttled": 0, "throttled_usec": 0,
               "steal_ns": 0, "steal_errno": 0}
        samples.append(row)
        t = round(t + DT, 4)
    end_wall = (end_at if end_at is not None else deadline + 0.02)
    sampler_total = sampler_cpu + sampler_cores * (end_wall - deadline)
    proc_ns = usage_int * 1000
    sampler_ns = int(round(sampler_total * 1e9))
    worker_total = max(THREADS, proc_ns - sampler_ns)
    base, rem = divmod(worker_total, THREADS)
    workers = [{"idx": i,
                "cpu_ns": base + (rem if i == THREADS - 1 else 0),
                "getcpu_errors": 0, "getcpu_oor": 0,
                "affinity_start_errno": 0, "affinity_end_errno": 0,
                "cpus_seen": [residency_cpu],
                "affinity_start": [affinity_cpu], "affinity_end": [affinity_cpu],
                "sink": 1} for i in range(THREADS)]
    start_weight = WHI if c == 'a' else WLO
    end_weight = WLO
    start = {"event": "start", "wall_ns": int(round(offset * 1e9)),
             "cap_end_ns": int((offset + CAP) * 1e9), "mode": "contend",
             "proc_cpu_ns": 0, "sampler_cpu_ns": 0,
             "load_deadline_ns": int(round(deadline * 1e9)),
             "steal_ns": 0, "steal_errno": 0, "threads": THREADS,
             "nproc": 4, "clk_tck": 100, "affinity": [CPU],
             "cgroup": cg_endpoint(start_weight)}
    end = {"event": "end", "wall_ns": int(round(end_wall * 1e9)),
           "cap_end_ns": int(round((end_wall + CAP) * 1e9)),
           "duration_ns": int(round(DURATION * 1e9)), "proc_cpu_ns": proc_ns,
           "sampler_cpu_ns": sampler_ns, "steal_ns": 0, "steal_errno": 0,
           "workers": workers, "worker_cpu_ns": worker_total,
           "stat_start": {"present": 1, "usage_usec": 0, "nr_periods": 0,
                          "nr_throttled": 0, "throttled_usec": 0, "nr_bursts": 0,
                          "burst_usec": 0},
           "stat_end": {"present": 1, "usage_usec": usage_int,
                        "nr_periods": periods, "nr_throttled": 0,
                        "throttled_usec": 0, "nr_bursts": 0, "burst_usec": 0},
           "cgroup": cg_endpoint(end_weight)}
    return [start, *samples, end]


def snapshot(weight, t, cpu=CPU):
    return {"event": "snapshot", "wall_ns": int(round(t * 1e9)),
            "proc_cpu_ns": 1000, "pid": 42, "cgroup": {
                "cpu.max": {"errno": 0, "raw": "max 100000", "quota": 0,
                            "period": 100000, "quota_max": 1},
                "cpuset.cpus.effective": {"errno": 0, "raw": str(cpu)},
                "cpu.weight": {"errno": 0, "raw": str(weight)},
                "cpu.stat": {"present": 1, "usage_usec": 1, "nr_periods": 1,
                             "nr_throttled": 0, "throttled_usec": 0,
                             "nr_bursts": 0, "burst_usec": 0,
                             "raw": "usage_usec 1\n"}}}


STAGE_SNAP_T = {'a': [1.2, 9.9, 19.2], 'b': [1.4, 9.95, 19.25]}


def stage_snap_weight(c, k):
    return WHI if checks.PLAN[k][c] == 1024 else WLO


def inspection(c, stage, *, shares=None, running=None, exit_code=None,
               cpuset='2', quota=0, id_override=None):
    shares = checks.PLAN[min(stage, checks.STAGES - 1)][c] if shares is None else shares
    if running is None:
        running = stage != checks.STAGES
    if exit_code is None:
        exit_code = 0
    return {"stage": stage, "shares": shares, "cpuset": cpuset, "quota": quota,
            "period": 0, "id": id_override or CIDS[c], "running": running,
            "exit_code": exit_code}


def mandatory_commands():
    rows = [{"label": l, "command": ["docker"], "exit_code": 0, "timed_out": False,
             "elapsed_s": 0.01,
             "cleanup": {"clean_shutdown": True, "remaining_live_pids": [],
                         "remaining_zombie_pids": []}}
            for l in sorted(checks.MANDATORY_LABELS)]
    rows += [{"label": "prerm-a", "exit_code": 1, "timed_out": False,
              "cleanup": {"clean_shutdown": True}},
             {"label": "poll-a-0", "exit_code": 0, "timed_out": False,
              "cleanup": {"clean_shutdown": True}}]
    return rows


def input_rows(change=False, omit=()):
    out = []
    for role in checks.INPUT_ROLES:
        if role in omit:
            continue
        out.append({"role": role, "path": '/%s' % role,
                    "sha256_before": SHA64,
                    "sha256_after": ('1' * 64) if change else SHA64})
    return out


def build_evidence(*, a_kw=None, b_kw=None, commands=None, inputs=None,
                   leftovers='', include_snaps=True, include_brackets=True,
                   final_state=None, mutate=None, ncpu=4, cgroup_version='2'):
    a_kw, b_kw = a_kw or {}, b_kw or {}
    containers = {}
    updates = []
    for c in ('a', 'b'):
        events = build_stream(c, **(a_kw if c == 'a' else b_kw))
        snaps = [snapshot(stage_snap_weight(c, k), STAGE_SNAP_T[c][k])
                 for k in range(3)]
        if not include_snaps:
            snaps = []
        insps = [inspection(c, k) for k in range(3)] + [inspection(c, 3)]
        containers[c] = {"name": "cpushare-" + c, "created_id": CIDS[c],
                         "snapshots": snaps, "inspections": insps,
                         "spin_events": events, "spin_error": None,
                         "wait_exit": 0, "wait_output": "0", "probe_exit": "0",
                         "removed": True}
    bracket_spec = {(1, 'a'): (9.15, 9.4, WHI, WLO),
                    (1, 'b'): (9.35, 9.6, WLO, WHI),
                    (2, 'b'): (18.45, 18.7, WHI, WLO)}
    for (stage, c), (tpre, tpost, ow, nw) in sorted(bracket_spec.items()):
        old = checks.PLAN[stage - 1][c]
        new = checks.PLAN[stage][c]
        updates.append({"label": "update-%s-%d" % (c, stage), "stage": stage,
                        "container": c, "from_shares": old, "to_shares": new,
                        "exit_code": 0, "timed_out": False,
                        "pre": snapshot(ow, tpre), "post": snapshot(nw, tpost)}
                       if include_brackets else
                       {"label": "update-%s-%d" % (c, stage), "stage": stage,
                        "container": c, "from_shares": old, "to_shares": new,
                        "exit_code": 0, "timed_out": False})
    ev = {"params": {"guest_cpus": 4, "shared_cpu": CPU, "threads": THREADS,
                     "phase_ms": 9000, "sample_ms": 100, "start_trim_s": 0.5,
                     "min_window_s": 4.0, "min_samples": 20, "max_gap_s": 0.5,
                     "endpoint_skew_s": 0.5, "bracket_skew_s": 0.4,
                     "plan": [dict(s) for s in checks.PLAN]},
          "daemon": {"ncpu": ncpu, "cgroup_version": cgroup_version,
                     "server_version": "27.5.1", "info_raw": "ncpu=4 cgroup=2"},
          "containers": containers, "updates": updates,
          "commands": mandatory_commands() if commands is None else commands,
          "inputs": input_rows() if inputs is None else inputs,
          "leftovers_stdout": leftovers,
          "final_state": final_state or {"phase": "stopped", "forced_shutdown": False,
                                         "guest_cleanup": {"clean_shutdown": True},
                                         "network_cleanup": {"clean_shutdown": True}}}
    if mutate:
        mutate(ev)
    return ev


def eval_ev(ev):
    r = checks.evaluate(ev)
    return r, checks.overall_verdict(r, [])


# --------------------------------------------------------------------------- #
# Synthetic checker tests
# --------------------------------------------------------------------------- #

class HappyPathTests(unittest.TestCase):
    def test_baseline_passes(self):
        results, verdict = eval_ev(build_evidence())
        self.assertEqual(verdict, checks.PASS,
                         {k: (v['status'], v['reason']) for k, v in results.items()
                          if v['status'] != checks.PASS})

    def test_fractions_use_observed_weights_and_intervals(self):
        results, _ = eval_ev(build_evidence())
        ev = results['phase_2to1']['evidence']
        self.assertAlmostEqual(ev['expected_fraction_a_from_weight'], FHI, places=5)
        self.assertEqual(ev['observed_weights'], {'a': WHI, 'b': WLO})
        lo, hi = ev['fraction_interval']
        self.assertTrue(lo <= FHI <= hi)
        # Every plausible value is within the functional band.
        self.assertGreaterEqual(lo, FHI - 0.09)
        self.assertLessEqual(hi, FHI + 0.09)
        mp = results['weight_mapping']['evidence']
        self.assertEqual(mp['mapping'], {'512': WLO, '1024': WHI})
        self.assertEqual(mp['computed_weights_pending_runtime'], {'512': 59, '1024': 100})
        self.assertIn('computed_pending_runtime', mp['computed_source'])

    def test_json_native(self):
        results, _ = eval_ev(build_evidence())
        self.assertEqual(results, json.loads(json.dumps(results)))

    def test_unstable_transitions_are_quarantined(self):
        results, _ = eval_ev(build_evidence())
        for c in ('a', 'b'):
            u = results['capture_protocol']['evidence']['containers'][c]['unstable_captures']
            self.assertTrue(u, c)
        ev0 = results['phase_2to1']['evidence']
        ev1 = results['phase_reversed_1to2']['evidence']
        # No scored interval crosses the A transition quarantine [9.20,9.35].
        self.assertLessEqual(ev0['hi_ns'], int(9.20 * 1e9))
        self.assertGreaterEqual(ev1['lo_ns'], int(9.35 * 1e9))


class BracketCaptureTests(unittest.TestCase):
    def _kw_long_transition(self, delay):
        """One capture at the transition starts pre-change and ends after it."""
        end_t = 9.20 + delay
        return {'cap_overrides': {9.20: end_t}}

    def test_delayed_read_spanning_change_is_quarantined_not_misattributed(self):
        # Exact owner scenario class: 4.719 s capture starting under weight
        # 100 and ending under 59. v2's single wall_ns would have misdated it.
        delay = 4.719
        kw = {'cap_overrides': {9.20: 9.20 + delay}}

        def retime(ev):
            # The runner's post-update exec snapshot also comes back only
            # after the long capture completes; move A's stage-1 brackets.
            for u in ev['updates']:
                if (u['stage'], u['container']) == (1, 'a'):
                    u['post']['wall_ns'] = int((9.20 + delay + 0.1) * 1e9)
            ev['containers']['a']['snapshots'][1]['wall_ns'] = \
                int((9.20 + delay + 0.2) * 1e9)
        results, verdict = eval_ev(build_evidence(a_kw=kw, mutate=retime))
        self.assertEqual(verdict, checks.PASS,
                         {k: (v['status'], v['reason']) for k, v in results.items()
                          if v['status'] != checks.PASS})
        ev0 = results['phase_2to1']['evidence']
        ev1 = results['phase_reversed_1to2']['evidence']
        self.assertLessEqual(ev0['hi_ns'], int(9.20 * 1e9))
        self.assertGreaterEqual(ev1['lo_ns'], int((9.20 + delay) * 1e9))
        straddles = results['capture_protocol']['evidence']['containers']['a'] \
            ['unstable_captures']
        self.assertEqual(len(straddles), 1)
        self.assertAlmostEqual((straddles[0]['t1_ns'] - straddles[0]['t0_ns']) / 1e9,
                               delay, places=2)
        self.assertEqual((straddles[0]['w1'], straddles[0]['w2']), (WHI, WLO))

    def test_matching_stable_weight_long_capture_can_indeterminate(self):
        # Stable bracket pair (100/100) but the capture takes 4.719 s in the
        # MIDDLE of phase 1: evidence preserved, phase cannot pass through it.
        kw = {'cap_overrides': {13.5: 13.5 + 4.719}}
        results, verdict = eval_ev(build_evidence(a_kw=kw))
        self.assertIn(verdict, (checks.INDETERMINATE, checks.FAIL))
        self.assertEqual(results['phase_reversed_1to2']['status'],
                         checks.INDETERMINATE)
        caps = results['capture_protocol']['evidence']['containers']['a']['slow_captures']
        self.assertTrue(any(abs(c['dur_s'] - 4.719) < 0.01 for c in caps))

    def test_two_endpoint_uncertainty_straddles(self):
        # BOTH containers begin their post-trim phase-0 capture chain with a
        # 5 s STABLE capture: every plausible rate box is far wider than the
        # functional band, so a false PASS is impossible -> INDETERMINATE.
        kw = {'cap_overrides': {2.0: 7.0}}
        results, verdict = eval_ev(build_evidence(a_kw=kw, b_kw=dict(kw)))
        self.assertEqual(verdict, checks.INDETERMINATE,
                         {k: v['status'] for k, v in results.items()})
        self.assertEqual(results['phase_2to1']['status'], checks.INDETERMINATE)
        reason = results['phase_2to1']['reason']
        self.assertTrue('saturat' in reason or 'band edge' in reason
                        or 'measurable' in reason, reason)
        anchors = results['phase_2to1']['evidence'].get('slow_anchors', {})
        self.assertTrue(anchors)

    def test_weight_flip_inside_capture_at_planned_transition_ok(self):
        # Default fixture already has w1!=w2 at transitions; assert semantics.
        results, _ = eval_ev(build_evidence())
        self.assertEqual(results['weight_timeline']['status'], checks.PASS)

    def test_extra_weight_flip_inside_phase_fails(self):
        # Stable region interrupted by a flip-and-revert straddling capture.
        kw = {'weight_overrides': {13.0: (WLO, WHI)},
              'cap_overrides': {13.0: 13.0 + 0.2},
              'kind_overrides': {}}
        # Following samples read WLO again (A phase-1 weight), so the unstable
        # capture has equal weights on both sides -> rejected.
        results, verdict = eval_ev(build_evidence(a_kw=kw))
        self.assertEqual(verdict, checks.FAIL)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL)

    def test_unstable_backward_reads_fail(self):
        # Capture at A transition reads new->old (impossible order).
        kw = {'weight_overrides': {9.20: (WLO, WHI)},
              'cap_overrides': {9.20: 9.35}}
        results, verdict = eval_ev(build_evidence(a_kw=kw))
        self.assertEqual(verdict, checks.FAIL)
        self.assertEqual(results['weight_timeline']['status'], checks.FAIL)

    def test_malformed_weight_is_quarantined_and_counted(self):
        kw = {'kind_overrides': {13.5: 'weight_malformed'}}
        results, verdict = eval_ev(build_evidence(a_kw=kw))
        proto = results['capture_protocol']['evidence']['containers']['a']
        self.assertEqual(proto['error_count'], 1)
        self.assertTrue(any('error' in q['kind'] for q in proto['quarantine']))
        # The malformed interior read leaves <70% clean coverage in phase 1.
        self.assertEqual(results['phase_reversed_1to2']['status'],
                         checks.INDETERMINATE)
        self.assertNotEqual(verdict, checks.PASS)

    def test_absent_then_resume_leaves_phase_indeterminate(self):
        # weight file ENOENT across five captures IN phase 0, then resumes:
        # less than 70% of the phase is measurable -> indeterminate, and the
        # ENOENT evidence is counted.
        kw = {'kind_overrides': {round(5.0 + 0.1 * i, 1): 'weight_absent'
                                 for i in range(5)}}
        results, verdict = eval_ev(build_evidence(a_kw=kw))
        self.assertIn(verdict, (checks.INDETERMINATE, checks.FAIL))
        self.assertEqual(results['phase_2to1']['status'], checks.INDETERMINATE)
        errs = results['capture_protocol']['evidence']['containers']['a']['error_captures']
        self.assertEqual(len(errs), 5)
        self.assertTrue(all(e['weight1_errno'] == 2 for e in errs))

    def test_coverage_gap_never_passes(self):
        # 2 s with no captures inside B's heavy run: the phase can neither be
        # scored nor claimed -> INDETERMINATE (missing observation, not a
        # structural contradiction), and never PASS.
        kw = {'skip_holes': ((13.0, 15.0),)}
        results, verdict = eval_ev(build_evidence(b_kw=kw))
        self.assertEqual(verdict, checks.INDETERMINATE)
        self.assertEqual(results['phase_reversed_1to2']['status'],
                         checks.INDETERMINATE)

    def test_non_overlapping_heavy_run_cannot_pass(self):
        # B's transition-1 capture blocks for 5.5 s: its heavy stable run is
        # too short for the 4 s joint window with A's low run.
        delay = 5.5
        kw_b = {'cap_overrides': {9.40: 9.40 + delay}}

        def retime(ev):
            for u in ev['updates']:
                if (u['stage'], u['container']) == (1, 'b'):
                    u['post']['wall_ns'] = int((9.40 + delay + 0.1) * 1e9)
            ev['containers']['b']['snapshots'][1]['wall_ns'] = \
                int((9.40 + delay + 0.2) * 1e9)
        results, verdict = eval_ev(build_evidence(b_kw=kw_b, mutate=retime))
        self.assertNotEqual(verdict, checks.PASS)
        self.assertEqual(results['phase_reversed_1to2']['status'],
                         checks.INDETERMINATE)
        q = results['capture_protocol']['evidence']['containers']['b']['unstable_captures']
        self.assertTrue(any((x['t1_ns'] - x['t0_ns']) / 1e9 > 4.0 for x in q))


class MissingEvidenceTests(unittest.TestCase):
    def test_delete_commands_fails(self):
        ev = build_evidence(); ev.pop('commands')
        self.assertEqual(checks.overall_verdict(checks.evaluate(ev), []), checks.FAIL)

    def test_delete_inputs_fails(self):
        ev = build_evidence(); ev.pop('inputs')
        r = checks.evaluate(ev)
        self.assertEqual(r['input_provenance']['status'], checks.FAIL)

    def test_empty_hash_values_fail(self):
        ev = build_evidence(inputs=[{'role': r, 'path': '/x',
                                     'sha256_before': None, 'sha256_after': None}
                                    for r in checks.INPUT_ROLES])
        self.assertEqual(checks.overall_verdict(checks.evaluate(ev), []), checks.FAIL)

    def test_delete_leftovers_fails(self):
        ev = build_evidence(); ev.pop('leftovers_stdout')
        self.assertEqual(checks.evaluate(ev)['leftovers']['status'], checks.FAIL)

    def test_empty_string_leftovers_is_executed_inventory(self):
        results, verdict = eval_ev(build_evidence(leftovers=''))
        self.assertEqual(verdict, checks.PASS)

    def test_delete_snapshot_cpuset_fails(self):
        ev = build_evidence()
        for c in ('a', 'b'):
            for s in ev['containers'][c]['snapshots']:
                del s['cgroup']['cpuset.cpus.effective']
        r = checks.evaluate(ev)
        self.assertEqual(r['probe_streams']['status'], checks.FAIL)

    def test_delete_sample_bracket_field_fails(self):
        ev = build_evidence()
        del ev['containers']['a']['spin_events'][5]['weight2']
        r = checks.evaluate(ev)
        self.assertEqual(r['probe_streams']['status'], checks.FAIL)

    def test_delete_cap_end_fails(self):
        ev = build_evidence()
        del ev['containers']['a']['spin_events'][5]['cap_end_ns']
        self.assertEqual(checks.evaluate(ev)['probe_streams']['status'], checks.FAIL)

    def test_cap_duration_mismatch_fails(self):
        ev = build_evidence()
        ev['containers']['a']['spin_events'][5]['cap_dur_ns'] += 1
        self.assertEqual(checks.evaluate(ev)['probe_streams']['status'], checks.FAIL)

    def test_sample_cpuset_errno_fails(self):
        kw = {'kind_overrides': {5.0: 'cpuset_err'}}
        ev = build_evidence(a_kw=kw)
        self.assertEqual(checks.evaluate(ev)['probe_streams']['status'], checks.FAIL)

    def test_sample_max_errno_fails(self):
        kw = {'kind_overrides': {5.0: 'max_err'}}
        ev = build_evidence(a_kw=kw)
        self.assertEqual(checks.evaluate(ev)['probe_streams']['status'], checks.FAIL)

    def test_sample_quota_fails(self):
        ev = build_evidence()
        s = ev['containers']['a']['spin_events'][5]
        s['quota_max'] = 0; s['quota'] = 200000
        r = checks.evaluate(ev)
        self.assertEqual(r['probe_streams']['status'], checks.FAIL)

    def test_legacy_spin_mode_fails(self):
        ev = build_evidence()
        ev['containers']['a']['spin_events'][0]['mode'] = 'spin'
        self.assertEqual(checks.evaluate(ev)['probe_streams']['status'], checks.FAIL)


class FunctionalVerdictTests(unittest.TestCase):
    def test_opposite_allocation_fails(self):
        # A (heavy weight) accrues the light rate and B (light weight) the
        # heavy rate: saturated contention, fraction provably inverted.
        a_rate = lambda t: FLO if t < 9.2 else rate_for('a', t)
        b_rate = lambda t: FHI if t < 9.4 else rate_for('b', t)
        ev = build_evidence(a_kw={'rate_fn': a_rate}, b_kw={'rate_fn': b_rate})
        r = checks.evaluate(ev)
        self.assertEqual(r['phase_2to1']['status'], checks.FAIL,
                         r['phase_2to1']['reason'])
        self.assertEqual(checks.overall_verdict(r, []), checks.FAIL)

    def test_equal_control_skew_fails(self):
        # 0.72/0.28 during the equal-weight phase: provably unequal.
        a_rate = lambda t: 0.72 if t >= 18.7 else rate_for('a', t)
        b_rate = lambda t: 0.28 if t >= 18.7 else rate_for('b', t)
        ev = build_evidence(a_kw={'rate_fn': a_rate}, b_kw={'rate_fn': b_rate})
        r = checks.evaluate(ev)
        self.assertEqual(r['phase_equal_control']['status'], checks.FAIL)

    def test_insufficient_load_indeterminate(self):
        ev = build_evidence(
            a_kw={'rate_fn': lambda t: 0.30},
            b_kw={'rate_fn': lambda t: 0.20})
        r = checks.evaluate(ev)
        for name in checks.PHASE_LABELS:
            self.assertEqual(r[name]['status'], checks.INDETERMINATE)
            self.assertIn('saturat', r[name]['reason'])

    def test_more_than_one_core_fails(self):
        ev = build_evidence(
            a_kw={'rate_fn': lambda t: 0.9},
            b_kw={'rate_fn': lambda t: 0.9})
        r = checks.evaluate(ev)
        self.assertEqual(r['phase_2to1']['status'], checks.FAIL)

    def test_long_stable_capture_evidence_retained_not_passed(self):
        # 4.719 s stable capture in phase 1: it anchors the surrounding chains;
        # the phase cannot pass and the capture stays in the evidence.
        kw = {'cap_overrides': {13.5: 13.5 + 4.719}}
        results, verdict = eval_ev(build_evidence(a_kw=kw))
        self.assertNotEqual(verdict, checks.PASS)
        caps = results['capture_protocol']['evidence']['containers']['a']['slow_captures']
        self.assertTrue(any(abs(c['dur_s'] - 4.719) < 0.01 for c in caps))
        phase = results['phase_reversed_1to2']
        self.assertEqual(phase['status'], checks.INDETERMINATE)
        # Anchored intervals were still built and carry the slow-anchor flags.
        # Evidence was not dropped: quarantine or anchors name the span.
        ev_text = json.dumps(phase['evidence'])
        self.assertIn(str(int(13.5 * 1e9)), ev_text)

    def test_interval_math_straddle_is_indeterminate(self):
        # Pure unit test of the three-valued decision: fraction box crossing
        # the band edge -> INDETERMINATE; wholly outside -> FAIL; contained
        # -> PASS.
        def interval(pa_lo, pa_hi, pb_lo, pb_hi):
            return {'weights': {'a': WHI, 'b': WLO},
                    'lo_ns': 0, 'hi_ns': 0,
                    'containers': {
                        'a': {'proc_lo': pa_lo, 'proc_hi': pa_hi, 'stat_lo': pa_lo,
                              'stat_hi': pa_hi, 'wide_s': 5, 'narrow_s': 4.9,
                              'samples': 40, 'first_ns': 0, 'last_t1': 0,
                              'first_t1': 0, 'last_t0': 0},
                        'b': {'proc_lo': pb_lo, 'proc_hi': pb_hi, 'stat_lo': pb_lo,
                              'stat_hi': pb_hi, 'wide_s': 5, 'narrow_s': 4.9,
                              'samples': 40, 'first_ns': 0, 'last_t1': 0,
                              'first_t1': 0, 'last_t0': 0}}}
        W = {'a': WHI, 'b': WLO}
        r = checks._interval_decision(interval(0.40, 0.90, 0.10, 0.60), W, FHI)
        self.assertEqual(r['status'], checks.INDETERMINATE)
        r = checks._interval_decision(interval(0.05, 0.08, 0.90, 0.95), W, FHI)
        self.assertEqual(r['status'], checks.FAIL)
        r = checks._interval_decision(interval(0.60, 0.65, 0.35, 0.40), W, FHI)
        self.assertEqual(r['status'], checks.PASS)


class LedgerAndStructureTests(unittest.TestCase):
    def test_unknown_label(self):
        rows = mandatory_commands(); rows[0]['label'] = 'rogue-label'
        r = checks.evaluate(build_evidence(commands=rows))
        self.assertEqual(r['command_ledger']['status'], checks.FAIL)

    def test_duplicate_label(self):
        rows = mandatory_commands(); rows.append(dict(rows[0]))
        r = checks.evaluate(build_evidence(commands=rows))
        self.assertEqual(r['command_ledger']['status'], checks.FAIL)

    def test_missing_label(self):
        rows = [x for x in mandatory_commands() if x['label'] != 'wait-b']
        r = checks.evaluate(build_evidence(commands=rows))
        self.assertEqual(r['command_ledger']['status'], checks.FAIL)

    def test_poll_timeout_fails(self):
        rows = mandatory_commands()
        rows.append({'label': 'poll-a-9', 'exit_code': 124, 'timed_out': True,
                     'cleanup': {'clean_shutdown': True}})
        r = checks.evaluate(build_evidence(commands=rows))
        self.assertEqual(r['command_ledger']['status'], checks.FAIL)

    def test_optional_dirty_group_fails(self):
        rows = mandatory_commands()
        for x in rows:
            if x['label'] == 'prerm-a':
                x['cleanup'] = {'clean_shutdown': False}
        r = checks.evaluate(build_evidence(commands=rows))
        self.assertEqual(r['command_ledger']['status'], checks.FAIL)

    def test_unknown_role(self):
        rows = input_rows()
        rows.append({'role': 'rogue', 'path': '/x',
                     'sha256_before': SHA64, 'sha256_after': SHA64})
        r = checks.evaluate(build_evidence(inputs=rows))
        self.assertEqual(r['input_provenance']['status'], checks.FAIL)

    def test_changed_hash_fails(self):
        r = checks.evaluate(build_evidence(inputs=input_rows(change=True)))
        self.assertEqual(r['input_provenance']['status'], checks.FAIL)

    def test_wrong_guest_size(self):
        r = checks.evaluate(build_evidence(ncpu=1))
        self.assertEqual(r['guest_topology']['status'], checks.FAIL)

    def test_cgroup_v1(self):
        r = checks.evaluate(build_evidence(cgroup_version='1'))
        self.assertEqual(r['guest_topology']['status'], checks.FAIL)

    def test_residency_escape(self):
        ev = build_evidence(a_kw={'residency_cpu': 3})
        r = checks.evaluate(ev)
        self.assertEqual(r['probe_streams']['status'], checks.FAIL)

    def test_identity_change(self):
        def m(ev):
            ev['containers']['a']['inspections'][2]['id'] = 'idx' * 12
        r = checks.evaluate(build_evidence(mutate=m))
        self.assertEqual(r['container_identity']['status'], checks.FAIL)

    def test_nonzero_exit(self):
        def m(ev):
            ev['containers']['a']['probe_exit'] = '1'
            ev['containers']['a']['inspections'][-1]['running'] = True
            ev['containers']['a']['inspections'][-1]['exit_code'] = 1
        r = checks.evaluate(build_evidence(mutate=m))
        self.assertEqual(r['container_identity']['status'], checks.FAIL)

    def test_leftover_ids(self):
        r = checks.evaluate(build_evidence(leftovers='deadbeef\n'))
        self.assertEqual(r['leftovers']['status'], checks.FAIL)

    def test_forced_shutdown(self):
        r = checks.evaluate(build_evidence(final_state={
            'phase': 'failed', 'forced_shutdown': True,
            'guest_cleanup': {'clean_shutdown': True},
            'network_cleanup': {'clean_shutdown': True}}))
        self.assertEqual(r['lifecycle']['status'], checks.FAIL)

    def test_missing_update(self):
        ev = build_evidence()
        ev['updates'] = [u for u in ev['updates'] if (u['stage'], u['container']) != (1, 'a')]
        r = checks.evaluate(ev)
        self.assertEqual(r['update_ledger']['status'], checks.FAIL)

    def test_missing_brackets_fail(self):
        ev = build_evidence(include_brackets=False)
        r = checks.evaluate(ev)
        self.assertEqual(r['capture_protocol']['status'], checks.FAIL)

    def test_missing_check_row_fails(self):
        r = checks.evaluate(build_evidence())
        del r['leftovers']
        self.assertEqual(checks.overall_verdict(r, []), checks.FAIL)

    def test_runner_errors_fail(self):
        r = checks.evaluate(build_evidence())
        self.assertEqual(checks.overall_verdict(r, [{'x': 1}]), checks.FAIL)

    def test_empty_evidence_closed(self):
        r = checks.evaluate({})
        for name in ('command_ledger', 'input_provenance', 'leftovers',
                     'lifecycle', 'guest_topology', 'update_ledger',
                     'container_identity'):
            self.assertEqual(r[name]['status'], checks.FAIL, name)
        self.assertEqual(checks.overall_verdict(r, []), checks.FAIL)

    def test_computed_formula_matches_pinned_source(self):
        self.assertEqual(checks.computed_weight(1024), 100)
        self.assertEqual(checks.computed_weight(512), 59)
        self.assertEqual(checks.computed_weight(2), 1)
        self.assertEqual(checks.computed_weight(262144), 10000)
        self.assertEqual(checks.computed_weight(0), 0)


# --------------------------------------------------------------------------- #
# Real binary capture-logic tests (env gated; no busy load)
# --------------------------------------------------------------------------- #

PROBE = os.environ.get('HARDDIND_SHARE_PROBE')
STAT = ('usage_usec %d\nnr_periods %d\nnr_throttled 0\nthrottled_usec 0\n')


@unittest.skipUnless(PROBE, 'set HARDDIND_SHARE_PROBE to run binary capture fixtures')
class RealCaptureFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='capfix-'))
        self.maxf = self.tmp / 'max'; self.maxf.write_text('max 100000\n')
        self.statf = self.tmp / 'stat'
        self.statf.write_text('usage_usec 1000\nnr_periods 10\nnr_throttled 0\n'
                              'throttled_usec 0\n')
        self.cpuf = self.tmp / 'cpu'; self.cpuf.write_text('2\n')
        self.weight = self.tmp / 'weight'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, count=1, interval=0, weight_path=None):
        cmd = [PROBE, 'capture-test', '--max', str(self.maxf),
               '--stat', str(self.statf), '--proc', '/proc/stat',
               '--weight', str(weight_path or self.weight),
               '--cpuset', str(self.cpuf), '--count', str(count)]
        if interval:
            cmd += ['--interval-ms', str(interval)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [json.loads(l) for l in out.stdout.splitlines() if l.strip()]

    def test_stable_regular_files(self):
        self.weight.write_text('100\n')
        es = self._run(count=2)
        for e in es:
            self.assertEqual(e['weight_stable'], 1)
            self.assertEqual(e['share_valid'], 1)
            self.assertEqual(e['weight1'], 100)

    def test_malformed_weight(self):
        self.weight.write_text('nope\n')
        e = self._run()[0]
        self.assertEqual(e['weight1_errno'], 71)
        self.assertEqual(e['weight1_invalid'], 1)
        self.assertEqual(e['share_valid'], 0)

    def test_absent_resume(self):
        self.weight.unlink(missing_ok=True)
        feeder = subprocess.Popen(
            ['sh', '-c', 'sleep 0.4; echo 59 > "$1"', '_', str(self.weight)])
        try:
            es = self._run(count=3, interval=250)
        finally:
            feeder.wait(timeout=5)
        self.assertEqual(es[0]['weight1_errno'], 2)
        self.assertIn(0, (es[1]['weight1_errno'], es[2]['weight1_errno']))

    def test_fifo_flip_inside_capture(self):
        fifo = self.tmp / 'wf'
        os.mkfifo(fifo)
        feeder = subprocess.Popen(
            ['sh', '-c', 'exec 9>"$1"; echo 100 >&9; sleep 1; echo 59 >&9',
             '_', str(fifo)])
        try:
            self.statf.write_text(STAT % (1000, 10))
            e = self._run(weight_path=fifo)[0]
        finally:
            feeder.wait(timeout=5)
        self.assertEqual((e['weight1'], e['weight2']), (100, 59))
        self.assertEqual(e['weight_stable'], 0)
        self.assertGreater(e['cap_dur_ns'], int(0.9e9))

    def test_fifo_long_stable_capture(self):
        fifo = self.tmp / 'ws'
        os.mkfifo(fifo)
        feeder = subprocess.Popen(
            ['sh', '-c', 'exec 9>"$1"; echo 100 >&9; sleep 1; echo 100 >&9',
             '_', str(fifo)])
        try:
            self.statf.write_text(STAT % (1000, 10))
            e = self._run(weight_path=fifo)[0]
        finally:
            feeder.wait(timeout=5)
        self.assertEqual(e['weight_stable'], 1)
        self.assertEqual(e['share_valid'], 1)
        self.assertGreater(e['cap_dur_ns'], int(0.9e9))



@unittest.skipUnless(os.environ.get('HARDDIND_SHARE_PROBE'),
                     'requires a compiled real probe on Linux')
class RealContendStream(unittest.TestCase):
    def test_every_real_contend_row_is_json_including_endpoints(self):
        # Endpoints use a different serializer from capture-test samples.
        # Exercise that actual path, with only a bounded 100 ms workload.
        proc = subprocess.run(
            [os.environ['HARDDIND_SHARE_PROBE'], 'contend', '--threads', '1',
             '--duration-ms', '100', '--sample-ms', '20'],
            capture_output=True, text=True, timeout=5, check=True)
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertGreaterEqual(len(rows), 3)
        self.assertEqual(rows[0]['event'], 'start')
        self.assertEqual(rows[-1]['event'], 'end')
        self.assertTrue(all(row['event'] == 'sample' for row in rows[1:-1]))
        for row in (rows[0], rows[-1]):
            self.assertIsInstance(row['cgroup']['cpu.stat'], dict)
            self.assertIn('cpu.weight', row['cgroup'])
        self.assertEqual(len(rows[-1]['workers']), 1)
        self.assertGreater(rows[-1]['wall_ns'], rows[0]['wall_ns'])


if __name__ == '__main__':
    unittest.main()
