"""Functional evaluation for the two-container --cpu-shares contention run.

Everything here is pure Python over the cpu_limit_probe JSONL and orchestration
metadata: no Docker, no clocks, no threads.  The runner (run-cpu-shares.py)
collects evidence; this module decides whether it proves that --cpu-shares /
cpu.weight changes *runtime allocation* between two containers that contend for
one shared guest CPU.

Design of the decision
----------------------
* Both containers run the unchanged cpu_limit_probe ``spin`` workload with
  several busy threads, pinned (--cpuset-cpus) to one guest vCPU.  Every probe
  timestamp is guest CLOCK_MONOTONIC, so the two containers share a clock and
  the phase windows are the intersection of their sample timelines.  Startup,
  update and exit edges are excluded by guards, so staggered start/stop can
  never masquerade as a weight effect.
* Allocation is measured three independent ways per window: process CPU clock
  (CLOCK_PROCESS_CPUTIME_ID samples), the cgroup's own cpu.stat usage_usec, and
  per-thread CPU clocks over the whole run.  They must agree.
* Functional fairness bands are derived from scheduler/window granularity (see
  FRAC_TOL), never from a throughput target.  A phase passes only when the
  combined busy load saturates the shared CPU, neither container is starved,
  the in-container cpu.weight timeline matches the --cpu-shares plan, and the
  measured fraction lands in the weight-implied band.

Status semantics are strict: structural contradictions are FAIL, noisy or
insufficient evidence is INDETERMINATE, and work that was never measured is
NOT_TESTED.  Nothing passes on configuration fields alone.
"""
import json
import re

PASS = 'pass'
FAIL = 'fail'
INDETERMINATE = 'indeterminate'
NOT_TESTED = 'not_tested'

# Window geometry.  Probe samples arrive every ~100 ms; 0.6 s guards discard
# >=5 samples on each side of a `docker update`, comfortably exceeding CFS
# period (100 ms) plus exec/inspect edge jitter.  A shorter surviving window
# cannot support a verdict.
GUARD_S = 0.6
MIN_WINDOW_S = 4.0
MIN_SAMPLES = 20

# Fraction f_A = cpu(A) / (cpu(A) + cpu(B)) implied by shares w: w/(wa+wb).
# CFS slices last sched_min_granularity_ns (default 2.25 ms); a 4 s window
# holds well over a thousand slices, so scheduler quantization averages well
# below 1%.  The dominant residual errors are: +/-2 samples of window-edge
# quantization (~0.2 s ~= 5% of 4 s), usec/tick rounding of the independent
# counters, and transient sleeper slack near weight changes (excluded by the
# guards).  FRAC_TOL is roughly twice that residual budget -- a *functional*
# "weights steer allocation" test, not a near-native performance threshold.
# Expected 2:1 fractions (0.667 / 0.333) stay far from the equal-share 0.5 and
# from each other's bands, so a reversed configuration cannot pass.
FRAC_TOL = 0.09
EQUAL_TOL = 0.08
FRAC_AGREE = 0.05          # proc-clock and cpu.stat fractions must agree
ORDER_MARGIN = 0.05        # required f(2:1) > f(equal) > f(1:2) gap

# Counter agreement in delivered cores.
AGREE_REL = 0.05
AGREE_MIN = 0.03
WHOLE_RUN_AGREE = 0.08

# Contention quality gates.  Two multi-thread busy groups pinned to one CPU
# must keep that CPU ~fully occupied; the small slack covers usec rounding and
# guest accounting.  >1.15 cores through a one-CPU cpuset is impossible and
# therefore a counter contradiction (FAIL); <0.85 means the shared load was
# insufficient and the phase is INDETERMINATE, never a pass.  The CFS weight
# floor guarantees the low-weight (512) container roughly one third of the CPU,
# so anything below these floors is real starvation (FAIL).
SAT_LOW = 0.85
SAT_HIGH = 1.15
STARVATION_FRAC = 0.12
STARVATION_CORES = 0.10

CONTAINERS = ('a', 'b')
STAGES = 3
# Stage share plan, applied to the same two container IDs:
#   0: A=1024 B=512  (2:1)
#   1: A=512  B=1024 (reversed live)
#   2: A=512  B=512  (equal control)
DEFAULT_PLAN = ({'a': 1024, 'b': 512},
                {'a': 512, 'b': 1024},
                {'a': 512, 'b': 512})


def _result(status, reason, **evidence):
    return {'status': status, 'reason': reason, 'evidence': evidence}


def parse_events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def parse_weight(raw):
    """cpu.weight is a single integer in [1,10000] (cgroup v2)."""
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]+', raw.strip()):
        raise ValueError('cpu.weight is not an integer: %r' % (raw,))
    value = int(raw.strip())
    if not 1 <= value <= 10000:
        raise ValueError('cpu.weight out of range: %d' % value)
    return value


def event_weight(event):
    field = (event.get('cgroup') or {}).get('cpu.weight') or {}
    if field.get('errno', -1) != 0 or 'raw' not in field:
        raise ValueError('cpu.weight evidence missing or unreadable')
    return parse_weight(field['raw'])


def parse_cpu_list(text):
    cpus = set()
    for part in (text or '').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            lo, hi = part.split('-', 1)
            cpus.update(range(int(lo), int(hi) + 1))
        else:
            cpus.add(int(part))
    return cpus


def validate_stream(events, shared_cpu):
    """Validate one container's spin JSONL and return a numeric summary."""
    if not events:
        raise ValueError('spin stream is empty')
    starts = [e for e in events if e.get('event') == 'start']
    ends = [e for e in events if e.get('event') == 'end']
    samples = [e for e in events if e.get('event') == 'sample']
    if len(starts) != 1 or len(ends) != 1 or not samples:
        raise ValueError('spin stream needs one start, one end and samples')
    start, end = starts[0], ends[0]
    if events[0] is not start or events[-1] is not end:
        raise ValueError('spin stream is not start..sample*..end')
    required_cpus = {shared_cpu}
    for label, event in (('start', start), ('end', end)):
        cg = event.get('cgroup') or {}
        cmax = cg.get('cpu.max') or {}
        if cmax.get('errno') != 0 or cmax.get('quota_max') != 1:
            raise ValueError('%s cpu.max must be unlimited "max"' % label)
        eff = (cg.get('cpuset.cpus.effective') or {})
        if eff.get('errno') != 0 or parse_cpu_list(eff.get('raw', '')) != required_cpus:
            raise ValueError('%s effective cpuset is not the shared CPU %d' % (label, shared_cpu))
        event_weight(event)  # raises unless present and numeric
        st = cg.get('cpu.stat') or {}
        if st.get('errno', 0) or st.get('present') != 1 or not st.get('raw'):
            raise ValueError('%s cpu.stat evidence missing' % label)
    if set(start.get('affinity') or []) != required_cpus:
        raise ValueError('process affinity at start escapes the shared CPU')
    if start.get('nproc', 0) <= shared_cpu:
        raise ValueError('guest reports fewer online CPUs than the shared CPU index')
    for i, s in enumerate(samples):
        if s.get('max_errno') or s.get('stat_errno'):
            raise ValueError('sample %d could not read cpu.max/cpu.stat' % i)
        if s.get('quota_max') != 1:
            raise ValueError('sample %d shows a quota; shares must be measured without quota' % i)
        for key in ('wall_ns', 'proc_cpu_ns', 'usage_usec', 'nr_periods',
                    'nr_throttled', 'throttled_usec'):
            if not isinstance(s.get(key), int):
                raise ValueError('sample %d missing integer field %s' % (i, key))
        if i:
            prev = samples[i - 1]
            if s['wall_ns'] <= prev['wall_ns']:
                raise ValueError('sample wall clock moved backwards')
            for key in ('proc_cpu_ns', 'usage_usec', 'nr_periods',
                        'nr_throttled', 'throttled_usec'):
                if s[key] < prev[key]:
                    raise ValueError('counter %s moved backwards' % key)
    workers = end.get('workers') or []
    if len(workers) != start.get('threads') or not workers:
        raise ValueError('worker accounting does not match announced thread count')
    if sorted(w['idx'] for w in workers) != list(range(start['threads'])):
        raise ValueError('worker indices are incomplete')
    if sum(w['cpu_ns'] for w in workers) != end.get('worker_cpu_ns'):
        raise ValueError('per-worker CPU clocks do not match worker_cpu_ns total')
    for w in workers:
        if w.get('cpu_ns', 0) <= 0:
            raise ValueError('worker %d recorded no CPU time' % w['idx'])
        if any(w.get(k) != 0 for k in
               ('getcpu_errors', 'getcpu_oor', 'affinity_start_errno', 'affinity_end_errno')):
            raise ValueError('worker %d has failed affinity/getcpu observations' % w['idx'])
        for key in ('affinity_start', 'affinity_end'):
            if set(w.get(key) or []) != required_cpus:
                raise ValueError('worker %d %s is not pinned to CPU %d' % (w['idx'], key, shared_cpu))
        seen = set(w.get('cpus_seen') or [])
        if not seen or not seen <= required_cpus:
            raise ValueError('worker %d ran outside the shared CPU: %r' % (w['idx'], sorted(seen)))
    wall_ns = end['wall_ns'] - start['wall_ns']
    if wall_ns <= 0:
        raise ValueError('spin wall duration is non-positive')
    wall_s = wall_ns / 1e9
    proc_ns = end['proc_cpu_ns'] - start['proc_cpu_ns']
    if proc_ns <= 0:
        raise ValueError('process CPU clock did not advance')
    stat_us = end['stat_end']['usage_usec'] - end['stat_start']['usage_usec']
    if stat_us < 0:
        raise ValueError('cpu.stat usage moved backwards')
    thread_ns = end['worker_cpu_ns']
    return {
        'start': start, 'end': end, 'samples': samples,
        'wall_s': wall_s, 'weight_start': event_weight(start),
        'weight_end': event_weight(end),
        'rates': {'thread': thread_ns / 1e9 / wall_s,
                  'proc': proc_ns / 1e9 / wall_s,
                  'stat': stat_us / 1e6 / wall_s},
        'workers': workers, 'shared_cpu': shared_cpu}


def validate_snapshots(events, summary, shared_cpu):
    """Three post-stage `docker exec ... snapshot` events, in guest time order."""
    if len(events) != STAGES:
        raise ValueError('expected %d post-stage snapshots, got %d' % (STAGES, len(events)))
    weights, times = [], []
    for i, event in enumerate(events):
        if event.get('event') != 'snapshot':
            raise ValueError('snapshot %d is not a snapshot event' % i)
        if not isinstance(event.get('wall_ns'), int):
            raise ValueError('snapshot %d lacks a guest wall_ns timestamp' % i)
        cg = event.get('cgroup') or {}
        eff = cg.get('cpuset.cpus.effective') or {}
        if eff.get('errno') == 0 and parse_cpu_list(eff.get('raw', '')) != {shared_cpu}:
            raise ValueError('snapshot %d cpuset escaped the shared CPU' % i)
        weights.append(event_weight(event))
        times.append(event['wall_ns'])
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('snapshot timestamps are not strictly ordered')
    if not (summary['start']['wall_ns'] <= times[0] and times[-1] <= summary['end']['wall_ns']):
        raise ValueError('snapshots fall outside the spin interval')
    return [{'wall_ns': t, 'weight': w} for t, w in zip(times, weights)]


def build_windows(summaries, snapshots, guard_s=GUARD_S, min_window_s=MIN_WINDOW_S):
    """Intersect both containers' guest-monotonic timelines into 3 windows.

    Windows are sustained interiors only: stage snapshots bracket the applied
    weight on *both* containers, and guards discard edges.  Returns per-stage
    dicts with bounds, selected sample counts and per-container rates; a window
    that is too short is reported as such and never scored.
    """
    t_start = {c: summaries[c]['start']['wall_ns'] for c in CONTAINERS}
    t_end = {c: summaries[c]['end']['wall_ns'] for c in CONTAINERS}
    last_sample = {c: summaries[c]['samples'][-1]['wall_ns'] for c in CONTAINERS}
    bounds = []
    # Stage 0 begins after both baseline snapshots; ends before either stage-1
    # post-update snapshot.  Stage 1 analogously between snapshots 1 and 2.
    bounds.append((max(snapshots[c][0]['wall_ns'] for c in CONTAINERS),
                   min(snapshots[c][1]['wall_ns'] for c in CONTAINERS)))
    bounds.append((max(snapshots[c][1]['wall_ns'] for c in CONTAINERS),
                   min(snapshots[c][2]['wall_ns'] for c in CONTAINERS)))
    # Stage 2 (equal control) runs to the tail of both spin streams.
    bounds.append((max(snapshots[c][2]['wall_ns'] for c in CONTAINERS),
                   min(last_sample[c] for c in CONTAINERS)))
    windows = []
    guard_ns = int(guard_s * 1e9)
    for stage, (raw_lo, raw_hi) in enumerate(bounds):
        lo, hi = raw_lo + guard_ns, raw_hi - guard_ns
        per = {}
        adequate = True
        reasons = []
        for c in CONTAINERS:
            selected = [s for s in summaries[c]['samples'] if lo <= s['wall_ns'] <= hi]
            entry = {'lo_ns': lo, 'hi_ns': hi, 'samples': len(selected)}
            if len(selected) < 2:
                adequate = False
                reasons.append('container %s has %d samples in window' % (c, len(selected)))
            else:
                a, b = selected[0], selected[-1]
                wall = (b['wall_ns'] - a['wall_ns']) / 1e9
                entry['wall_s'] = wall
                entry['proc'] = ((b['proc_cpu_ns'] - a['proc_cpu_ns']) / 1e9) / wall
                entry['stat'] = ((b['usage_usec'] - a['usage_usec']) / 1e6) / wall
                entry['first_wall_ns'] = a['wall_ns']
                entry['last_wall_ns'] = b['wall_ns']
                if len(selected) < MIN_SAMPLES:
                    adequate = False
                    reasons.append('container %s only %d samples' % (c, len(selected)))
                if wall < min_window_s:
                    adequate = False
                    reasons.append('container %s window %.2fs < %.1fs' % (c, wall, min_window_s))
            per[c] = entry
        # The window must also sit inside both spin streams.
        if any(lo < t_start[c] or hi > t_end[c] for c in CONTAINERS):
            adequate = False
            reasons.append('window escapes a spin stream')
        windows.append({'stage': stage, 'lo_ns': lo, 'hi_ns': hi,
                        'adequate': adequate, 'reasons': reasons, 'containers': per})
    return windows


def _rates_agree(a, b, scale=1.0):
    return abs(a - b) <= max(AGREE_MIN, AGREE_REL * scale)


def classify_phase(window, shares, plan_stage):
    """Score one aligned contention window against its planned shares."""
    if not window['adequate']:
        return _result(INDETERMINATE,
                       'overlapping sustained window too short or under-sampled: '
                       + '; '.join(window['reasons']),
                       stage=plan_stage, lo_ns=window['lo_ns'], hi_ns=window['hi_ns'],
                       containers=window['containers'])
    per = window['containers']
    expected_frac = shares['a'] / (shares['a'] + shares['b'])
    tol = EQUAL_TOL if shares['a'] == shares['b'] else FRAC_TOL
    rates = {c: {'proc': per[c]['proc'], 'stat': per[c]['stat'],
                 'wall_s': per[c]['wall_s'], 'samples': per[c]['samples']}
             for c in CONTAINERS}
    evidence = {'stage': plan_stage, 'shares': dict(shares),
                'expected_fraction_a': expected_frac, 'tolerance': tol,
                'lo_ns': window['lo_ns'], 'hi_ns': window['hi_ns'], 'rates': rates}

    # 1. Independent counters agree per container.
    for c in CONTAINERS:
        if not _rates_agree(per[c]['proc'], per[c]['stat']):
            return _result(INDETERMINATE,
                           'container %s proc clock and cpu.stat disagree in window' % c, **evidence)

    frac_proc = per['a']['proc'] / (per['a']['proc'] + per['b']['proc'])
    frac_stat = per['a']['stat'] / (per['a']['stat'] + per['b']['stat'])
    total_proc = per['a']['proc'] + per['b']['proc']
    total_stat = per['a']['stat'] + per['b']['stat']
    evidence.update({'fraction_a_proc': frac_proc, 'fraction_a_stat': frac_stat,
                     'total_proc': total_proc, 'total_stat': total_stat})

    # 2. Shared-CPU saturation (functional contention prerequisite).
    if total_proc < SAT_LOW or total_stat < SAT_LOW:
        return _result(INDETERMINATE,
                       'combined busy load does not saturate the shared CPU; '
                       'weight effect cannot be demonstrated', **evidence)
    if total_proc > SAT_HIGH or total_stat > SAT_HIGH:
        return _result(FAIL,
                       'more than ~1 core delivered through a one-CPU cpuset; '
                       'counter evidence is contradictory', **evidence)

    # 3. No starvation: the low-weight container must still be scheduled.
    for c in CONTAINERS:
        frac = frac_proc if c == 'a' else 1 - frac_proc
        if per[c]['proc'] < STARVATION_CORES or frac < STARVATION_FRAC:
            return _result(FAIL, 'container %s starved despite shares=%d' % (c, shares[c]), **evidence)

    # 4. The two independent allocations must tell the same story.
    if abs(frac_proc - frac_stat) > FRAC_AGREE:
        return _result(INDETERMINATE,
                       'proc clock and cpu.stat disagree on the allocation fraction', **evidence)

    # 5. Measured fraction must match the weight-implied band.
    if abs(frac_proc - expected_frac) > tol or abs(frac_stat - expected_frac) > tol:
        return _result(FAIL,
                       'measured allocation does not match --cpu-shares weights',
                       observed_band=(min(frac_proc, frac_stat), max(frac_proc, frac_stat)),
                       **evidence)
    return _result(PASS, 'runtime allocation follows --cpu-shares weights', **evidence)


def classify_ordering(phase_results, windows):
    """f_A(2:1) > f_A(equal) > f_A(1:2), with a real margin."""
    if any(r['status'] != PASS for r in phase_results):
        return _result(NOT_TESTED, 'ordering needs all three phase verdicts')
    fracs = [r['evidence'].get('fraction_a_proc') for r in phase_results]
    if any(f is None for f in fracs):
        return _result(FAIL, 'passing phase lacks fraction evidence')
    f21, f12, feq = fracs
    evidence = {'fraction_2to1': f21, 'fraction_reversed': f12,
                'fraction_equal': feq, 'required_margin': ORDER_MARGIN}
    if not (f21 > feq + ORDER_MARGIN and feq > f12 + ORDER_MARGIN):
        return _result(FAIL, 'weight reversal did not reverse the observed allocation', **evidence)
    return _result(PASS, 'reversing weights on the same IDs reversed allocation order', **evidence)


def classify_weight_timeline(snapshots, inspections, plan, summaries):
    """Actual in-container cpu.weight and docker inspect must follow the plan."""
    try:
        matrix = {c: [snapshots[c][k]['weight'] for k in range(STAGES)]
                  for c in CONTAINERS}
    except (KeyError, IndexError, TypeError):
        return _result(FAIL, 'missing post-update cpu.weight snapshots')
    # Same share value must map to the same kernel weight everywhere; this
    # catches an unapplied update (weight stays at the previous stage).
    share_to_weight = {}
    for k, shares in enumerate(plan):
        for c in CONTAINERS:
            share, weight = shares[c], matrix[c][k]
            old = share_to_weight.setdefault(share, weight)
            if old != weight:
                return _result(FAIL,
                               'same --cpu-shares value maps to different cpu.weight values',
                               share=share, weights=(old, weight), matrix=matrix, stage=k,
                               container=c)
    # Exact ordering at every stage.
    for k, shares in enumerate(plan):
        wa, wb = matrix['a'][k], matrix['b'][k]
        if shares['a'] > shares['b'] and not wa > wb:
            return _result(FAIL, 'stage %d cpu.weight ordering contradicts --cpu-shares plan' % k,
                           matrix=matrix, plan=plan)
        if shares['a'] < shares['b'] and not wa < wb:
            return _result(FAIL, 'stage %d cpu.weight ordering contradicts --cpu-shares plan' % k,
                           matrix=matrix, plan=plan)
        if shares['a'] == shares['b'] and wa != wb:
            return _result(FAIL, 'equal-weight control stage shows unequal cpu.weight',
                           matrix=matrix, stage=k)
    # docker inspect evidence at every stage: exact CpuShares, shared cpuset,
    # and no quota that would dominate the shares decision.
    insp_matrix = {}
    for c in CONTAINERS:
        rows = [row for row in inspections.get(c, []) if row.get('stage') in (0, 1, 2)]
        if len(rows) != STAGES:
            return _result(FAIL, 'container %s needs one inspect per stage' % c,
                           found=len(rows))
        insp_matrix[c] = rows
        for k, row in enumerate(rows):
            if row.get('shares') != plan[k][c]:
                return _result(FAIL,
                               'inspect HostConfig.CpuShares does not match plan on %s stage %d'
                               % (c, k), observed=row.get('shares'), expected=plan[k][c])
            if str(row.get('cpuset')) != str(summaries[c]['shared_cpu']):
                return _result(FAIL, 'inspect cpuset drifted off the shared CPU on %s stage %d'
                               % (c, k), observed=row.get('cpuset'))
            if row.get('quota') not in (0, None):
                return _result(FAIL, 'a CpuQuota is set; it would dominate --cpu-shares',
                               container=c, stage=k, quota=row.get('quota'))
            if not row.get('running'):
                return _result(FAIL, 'container %s not running at stage %d inspect' % (c, k))
    # Probe start/end weights agree with the surrounding stages.
    if summaries['a']['weight_start'] != matrix['a'][0] or \
            summaries['b']['weight_start'] != matrix['b'][0]:
        return _result(FAIL, 'spin start cpu.weight differs from baseline snapshot',
                       matrix=matrix,
                       start=(summaries['a']['weight_start'], summaries['b']['weight_start']))
    if summaries['a']['weight_end'] != matrix['a'][2] or \
            summaries['b']['weight_end'] != matrix['b'][2]:
        return _result(FAIL, 'spin end cpu.weight differs from equal-control snapshot',
                       matrix=matrix,
                       end=(summaries['a']['weight_end'], summaries['b']['weight_end']))
    return _result(PASS,
                   'cpu.weight and docker inspect follow the planned share timeline',
                   weight_matrix=matrix,
                   share_to_weight={str(k): v for k, v in sorted(share_to_weight.items())})


def classify_identity(containers):
    rows = {}
    for c in CONTAINERS:
        data = containers.get(c, {})
        created = data.get('created_id')
        inspections = data.get('inspections') or []
        if not created or not inspections:
            return _result(FAIL, 'container %s lacks identity evidence' % c)
        ids = {created} | {r.get('id') for r in inspections}
        if len(ids) != 1 or None in ids:
            return _result(FAIL, 'container %s changed identity across live updates' % c,
                           created_id=created, inspect_ids=[r.get('id') for r in inspections])
        final = next((r for r in inspections if r.get('stage') == STAGES), None)
        if final is None:
            return _result(FAIL, 'container %s lacks a post-wait final inspect' % c)
        if final.get('running') or final.get('exit_code') != 0 or data.get('probe_exit') != '0':
            return _result(FAIL, 'container %s did not finish cleanly' % c,
                           running=final.get('running'), exit_code=final.get('exit_code'),
                           probe_exit=data.get('probe_exit'))
        rows[c] = {'id': created, 'wait_exit': data.get('wait_exit'),
                   'probe_exit': data.get('probe_exit'), 'removed': data.get('removed')}
    if rows['a']['id'] == rows['b']['id']:
        return _result(FAIL, 'the two contention containers share one ID', rows=rows)
    return _result(PASS, 'both containers kept one identity through all live updates and exits 0',
                   containers=rows)


def classify_residency(summaries, shared_cpu):
    evidence = {'shared_cpu': shared_cpu, 'containers': {}}
    for c in CONTAINERS:
        s = summaries[c]
        seen = sorted({cpu for w in s['workers'] for cpu in w['cpus_seen']})
        affs = sorted({tuple(w[key]) for w in s['workers']
                       for key in ('affinity_start', 'affinity_end')})
        evidence['containers'][c] = {
            'workers': [{'idx': w['idx'], 'cpu_ns': w['cpu_ns'],
                         'cpus_seen': w['cpus_seen'],
                         'affinity_start': w['affinity_start'],
                         'affinity_end': w['affinity_end']} for w in s['workers']],
            'residency_union': seen, 'affinity_masks': [list(a) for a in affs]}
        if seen != [shared_cpu]:
            return _result(FAIL, 'container %s residency escaped CPU %d: %r'
                           % (c, shared_cpu, seen), **evidence)
        if [list(a) for a in affs] != [[shared_cpu]]:
            return _result(FAIL, 'container %s exposed a non-pinned affinity mask' % c, **evidence)
    return _result(PASS, 'every busy thread of both containers stayed on the shared CPU',
                   **evidence)


def classify_whole_run_accounting(summaries):
    rows = {}
    for c in CONTAINERS:
        rates = summaries[c]['rates']
        ordered = sorted(rates.values())
        spread = ordered[-1] - ordered[0]
        tol = max(AGREE_MIN, WHOLE_RUN_AGREE)
        rows[c] = {'rates': rates, 'spread': spread, 'tolerance': tol}
        if spread > tol:
            return _result(INDETERMINATE,
                           'container %s whole-run thread/proc/stat counters disagree' % c,
                           containers=rows)
        if min(rates.values()) < STARVATION_CORES:
            return _result(FAIL, 'container %s consumed almost no CPU over the whole run' % c,
                           containers=rows)
    return _result(PASS, 'whole-run per-thread, process and cgroup counters agree',
                   containers=rows)


def command_ledger(command_rows, required_labels, optional_labels=()):
    """Every required command must exist, exit 0, not time out, clean group.

    Optional labels (best-effort pre-removes, fallback stop) are still audited
    for timeouts and process-group cleanliness; their exit codes are tolerated.
    """
    by_label = {}
    for row in command_rows:
        by_label.setdefault(row.get('label'), []).append(row)
    missing = [label for label in required_labels if label not in by_label]
    if missing:
        return _result(FAIL, 'required commands were never executed', missing=sorted(missing))
    problems = []
    for label in required_labels:
        for row in by_label[label]:
            audit = row.get('cleanup') or {}
            if row.get('exit_code') != 0:
                problems.append({'label': label, 'exit_code': row.get('exit_code')})
            if row.get('timed_out'):
                problems.append({'label': label, 'timed_out': True})
            if not audit.get('clean_shutdown'):
                problems.append({'label': label, 'group_clean': False,
                                 'remaining_live_pids': audit.get('remaining_live_pids'),
                                 'remaining_zombie_pids': audit.get('remaining_zombie_pids')})
    for label in optional_labels:
        for row in by_label.get(label, []):
            audit = row.get('cleanup') or {}
            if row.get('timed_out') or not audit.get('clean_shutdown'):
                problems.append({'label': label, 'optional_audit_failed': True,
                                 'timed_out': row.get('timed_out')})
    duplicates = sorted(label for label, rows_ in by_label.items() if len(rows_) > 1)
    if problems:
        return _result(FAIL, 'one or more required commands failed or leaked a process group',
                       problems=problems, executed=sorted(by_label))
    return _result(PASS, 'every required command exited 0 with exact process-group cleanup',
                   commands=len(command_rows), duplicates=duplicates)


def leftovers_verdict(ps_output, names):
    if ps_output is None:
        return _result(FAIL, 'post-cleanup container inventory was never collected',
                       names=list(names))
    found = [line for line in ps_output.splitlines() if line.strip()]
    if found:
        return _result(FAIL, 'experiment containers remain after cleanup',
                       leftover_ids=found, names=list(names))
    return _result(PASS, 'no experiment containers remain after cleanup', names=list(names))


def lifecycle_verdict(final_state):
    if not final_state:
        return _result(FAIL, 'guest instance final state is missing')
    ok = (final_state.get('phase') == 'stopped'
          and not final_state.get('forced_shutdown')
          and final_state.get('guest_cleanup', {}).get('clean_shutdown')
          and final_state.get('network_cleanup', {}).get('clean_shutdown'))
    if ok:
        return _result(PASS, 'guest stopped cleanly without forced shutdown',
                       phase=final_state.get('phase'))
    return _result(FAIL, 'guest lifecycle was not clean',
                   phase=final_state.get('phase'),
                   forced_shutdown=final_state.get('forced_shutdown'),
                   error=final_state.get('error'))


def inputs_verdict(before, after):
    changed, vanished = [], []
    for path, digest in sorted((before or {}).items()):
        if path not in (after or {}):
            vanished.append(path)
        elif after[path] != digest:
            changed.append(path)
    if changed or vanished:
        return _result(FAIL, 'hashed inputs changed during the run',
                       changed=sorted(changed), vanished=sorted(vanished))
    return _result(PASS, 'kernel, disk, probe source/binary and scripts unchanged',
                   files=sorted((before or {}).keys()))


def update_ledger(updates, plan):
    expected = []
    for k in range(1, STAGES):
        for c in CONTAINERS:
            if plan[k][c] != plan[k - 1][c]:
                expected.append((k, c, plan[k - 1][c], plan[k][c]))
    seen = {(u.get('stage'), u.get('container')): u for u in (updates or [])}
    if len(seen) != len(updates or []):
        return _result(FAIL, 'duplicate update records')
    missing, failed = [], []
    for stage, c, old, new in expected:
        u = seen.get((stage, c))
        if u is None:
            missing.append({'stage': stage, 'container': c, 'from': old, 'to': new})
            continue
        if u.get('from_shares') != old or u.get('to_shares') != new:
            failed.append({'record': u, 'expected': {'stage': stage, 'container': c,
                                                     'from': old, 'to': new}})
        if u.get('exit_code') != 0 or u.get('timed_out'):
            failed.append({'record': u, 'reason': 'update command failed or timed out'})
    if missing:
        return _result(FAIL, 'planned live weight updates were never issued', missing=missing,
                       seen=sorted((str(k) for k in seen)))
    if failed:
        return _result(FAIL, 'live weight update record does not match plan', failed=failed)
    return _result(PASS, 'all planned weight updates issued successfully on unchanged IDs',
                   updates=[{'stage': s, 'container': c, 'from': o, 'to': n}
                            for s, c, o, n in expected])


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as error:  # evidence defects must become verdicts, never crashes
        return _result(FAIL, '%s: %s' % (type(error).__name__, error))


def evaluate(evidence):
    """Build the full check dictionary from runner-assembled evidence.

    Never raises: malformed evidence yields FAIL/INDETERMINATE/NOT_TESTED rows.
    All returned structures contain only JSON-native types.
    """
    params = evidence.get('params') or {}
    plan = params.get('plan') or [dict(s) for s in DEFAULT_PLAN]
    shared_cpu = params.get('shared_cpu')
    checks = {}
    containers = evidence.get('containers') or {}

    daemon = evidence.get('daemon') or {}
    ncpu = daemon.get('ncpu')
    if ncpu != 4:
        checks['guest_topology'] = _result(
            FAIL, 'protocol requires a 4-vCPU guest; observed %r' % (ncpu,), daemon=daemon)
    elif str(daemon.get('cgroup_version')) != '2':
        checks['guest_topology'] = _result(
            FAIL, 'cpu.weight evidence needs cgroup v2; observed %r'
            % (daemon.get('cgroup_version'),), daemon=daemon)
    else:
        checks['guest_topology'] = _result(
            PASS, '4-vCPU guest with cgroup v2; contention pinned to one shared CPU',
            ncpu=ncpu, cgroup_version=daemon.get('cgroup_version'),
            shared_cpu=shared_cpu, server=daemon.get('server_version'))

    checks['container_identity'] = _safe(classify_identity, containers)

    summaries, snapshots, stream_errors = {}, {}, []
    for c in CONTAINERS:
        data = containers.get(c) or {}
        raw_events = data.get('spin_events') or []
        parse_error = data.get('spin_error')
        try:
            if parse_error:
                raise ValueError(parse_error)
            summaries[c] = validate_stream(raw_events, shared_cpu)
        except Exception as error:
            stream_errors.append('%s: %s: %s' % (c, type(error).__name__, error))
        try:
            if c in summaries:
                snapshots[c] = validate_snapshots(data.get('snapshots') or [],
                                                  summaries[c], shared_cpu)
        except Exception as error:
            stream_errors.append('%s snapshots: %s: %s' % (c, type(error).__name__, error))

    observed = any((containers.get(c) or {}).get('spin_events')
                   or (containers.get(c) or {}).get('spin_error')
                   for c in CONTAINERS)
    if stream_errors:
        status = FAIL if observed else INDETERMINATE
        checks['probe_streams'] = _result(
            status,
            'probe evidence missing or invalid: ' + '; '.join(stream_errors))
    else:
        checks['probe_streams'] = _result(
            PASS, 'both spin streams complete with samples, counters and worker accounting',
            samples={c: len(summaries[c]['samples']) for c in CONTAINERS},
            wall_s={c: round(summaries[c]['wall_s'], 3) for c in CONTAINERS})

    phase_ready = len(summaries) == 2 and len(snapshots) == 2
    windows = []
    if phase_ready:
        windows = _safe(build_windows, summaries, snapshots,
                        params.get('guard_s', GUARD_S), params.get('min_window_s', MIN_WINDOW_S))
        if not isinstance(windows, list) or len(windows) != STAGES:
            phase_ready = False
    labels = ('phase_2to1', 'phase_reversed_1to2', 'phase_equal_control')
    phase_results = []
    for k in range(STAGES):
        if phase_ready:
            result = _safe(classify_phase, windows[k], plan[k], k)
        else:
            result = _result(NOT_TESTED, 'no aligned contention window: probe evidence invalid')
        checks[labels[k]] = result
        phase_results.append(result)
    if phase_ready:
        checks['phase_ordering'] = _safe(classify_ordering, phase_results, windows)
        checks['weight_timeline'] = _safe(classify_weight_timeline,
                                          snapshots,
                                          {c: (containers.get(c) or {}).get('inspections') or []
                                           for c in CONTAINERS},
                                          plan, summaries)
        checks['residency_affinity'] = _safe(classify_residency, summaries, shared_cpu)
        checks['whole_run_accounting'] = _safe(classify_whole_run_accounting, summaries)
    else:
        checks['phase_ordering'] = _result(NOT_TESTED, 'depends on phase windows')
        checks['weight_timeline'] = _result(NOT_TESTED, 'depends on probe streams')
        checks['residency_affinity'] = _result(NOT_TESTED, 'depends on probe streams')
        checks['whole_run_accounting'] = _result(NOT_TESTED, 'depends on probe streams')

    checks['update_ledger'] = _safe(update_ledger, evidence.get('updates') or [], plan)
    checks['command_ledger'] = _safe(command_ledger,
                                     evidence.get('commands') or [],
                                     evidence.get('required_commands') or [],
                                     evidence.get('optional_commands') or [])
    checks['leftovers'] = _safe(leftovers_verdict,
                                evidence.get('leftovers_stdout', ''),
                                [((containers.get(c) or {}).get('name')) for c in CONTAINERS])
    checks['lifecycle'] = _safe(lifecycle_verdict, evidence.get('final_state') or {})
    checks['inputs_unchanged'] = _safe(inputs_verdict,
                                       evidence.get('hashes_before') or {},
                                       evidence.get('hashes_after') or {})
    return checks


REQUIRED_CHECK_NAMES = (
    'guest_topology', 'container_identity', 'probe_streams',
    'phase_2to1', 'phase_reversed_1to2', 'phase_equal_control', 'phase_ordering',
    'weight_timeline', 'residency_affinity', 'whole_run_accounting',
    'update_ledger', 'command_ledger', 'leftovers', 'lifecycle', 'inputs_unchanged')


def overall_verdict(checks, errors=(), required=REQUIRED_CHECK_NAMES):
    """FAIL beats INDETERMINATE beats NOT_TESTED beats PASS; a missing verdict
    row is itself a FAIL so checker regressions cannot silently pass."""
    statuses = [checks.get(name, {}).get('status') for name in required]
    missing = [name for name in required if checks.get(name, {}).get('status') is None]
    if FAIL in statuses or missing or errors:
        return FAIL
    if INDETERMINATE in statuses:
        return INDETERMINATE
    if NOT_TESTED in statuses:
        return NOT_TESTED
    return PASS
