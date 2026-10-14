"""Functional evaluation for cpu_limit_probe evidence.

Everything here is pure Python over the probe's JSONL: no Docker, no clocks.
Bounds come from CFS bandwidth accounting, not from an accepted performance
target:

* quota q is replenished every period p; a run of wall length w can legitimately
  touch at most floor(w/p) full quotas plus one initial and one trailing partial
  period, plus one coarse accounting sliver (SLIVER_S) covering tick/stat
  rounding.  Symmetric subtraction gives the lower delivery bound.
* enforcement also requires the cgroup's own nr_throttled/throttled_usec
  counters to advance and three independent consumption measurements
  (per-thread CPU clocks, process CPU clock, cpu.stat usage_usec) to agree.
  Quota delivery applies to total cgroup usage: sampler work and concurrent
  docker exec observations also consume that quota. Worker/process time must
  respect the upper bound and agree with total usage, but cannot individually
  be required to consume the entire cgroup budget.
* if the unthrottled reference does not clearly exceed the upper enforcement
  band, offered load was insufficient and the result is indeterminate --
  never pass.

v4 measurement integrity: probe samples bracket every cgroup read with
wall_ns (capture start) and cap_end_ns (capture end) and read cpu.max twice
around cpu.stat (max_stable). Transition guards are TIME based, not sample
count based: under a 0.5-core quota the sampler can miss seconds of wakeups,
so a fixed head/tail sample count deleted the whole interior window in v3.
Rate denominators carry the capture interval as explicit uncertainty, and a
slow capture or a cpu.max change observed mid-capture is preserved as raw
evidence and excluded from rate endpoints, never silently averaged over.
"""
import json

PASS = 'pass'
FAIL = 'fail'
INDETERMINATE = 'indeterminate'
NOT_TESTED = 'not_tested'

SLIVER_S = 0.025          # one aggregate tick/stat rounding sliver per run
EXTRA_PERIODS = 2         # one leading + one trailing partial CFS period
REF_SLACK_CORES = 0.05    # unthrottled busy loop delivery slack in cores
AGREE_REL = 0.05          # independent counters must agree within 5%
AGREE_MIN = 0.03          # ... or 30 millicores, whichever is larger
SEPARATION_CORES = 0.10   # required gap between reference band and limit band
HEAD_GUARD_S = 0.5        # time excluded after an observed quota transition
TAIL_GUARD_S = 0.2        # time excluded before the next observed transition
CAPTURE_MAX_S = 0.025     # capture slower than the accounting sliver is stale
MIN_INTERIOR_S = 1.0      # shortest stable interior that may yield a verdict

SAMPLE_REQUIRED = (
    'wall_ns', 'cap_end_ns', 'proc_cpu_ns',
    'quota_max', 'quota', 'period', 'max_errno',
    'q2_quota_max', 'q2_quota', 'q2_period', 'max2_errno', 'max_stable',
    'stat_errno', 'usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec')
SAMPLE_COUNTERS = ('proc_cpu_ns', 'usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec')


def parse_events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _cap_end(event):
    end = event.get('cap_end_ns')
    wall = event['wall_ns']
    if end is None:
        return wall                 # legacy pre-v4 boundary events: assume instant
    if not isinstance(end, int) or end < wall:
        raise ValueError('capture window ends before it started')
    return end


def spin_summary(events):
    if sum(e.get('event') == 'start' for e in events) != 1 or sum(e.get('event') == 'end' for e in events) != 1:
        raise ValueError('spin stream needs exactly one start and end')
    start = next((e for e in events if e.get('event') == 'start'), None)
    end = next((e for e in events if e.get('event') == 'end'), None)
    samples = [e for e in events if e.get('event') == 'sample']
    if not start or not end:
        raise ValueError('spin stream missing start/end events')
    wall_s = (end['wall_ns'] - start['wall_ns']) / 1e9
    if wall_s <= 0 or events[0] is not start or events[-1] is not end:
        raise ValueError('invalid spin timeline')
    # The sampler thread is part of the measured cgroup; its clock must be
    # present on both ends or worker-only consumption cannot be reconciled.
    for ev in (start, end):
        if 'sampler_cpu_ns' not in ev or not isinstance(ev.get('sampler_cpu_ns'), int):
            raise ValueError('sampler CPU clock missing from spin stream')
    start_cap_end, end_cap_start = _cap_end(start), end['wall_ns']
    end_cap_end = _cap_end(end)
    if start_cap_end > end_cap_start or end_cap_end < end_cap_start:
        raise ValueError('start/end capture windows overlap or reverse')
    for event in (start, end):
        cg = event['cgroup']
        cmax = cg['cpu.max']
        if (cmax.get('errno') != 0 or cmax.get('quota_max') not in (0, 1)
                or cmax.get('period', 0) <= 0
                or (cmax['quota_max'] == 0 and cmax.get('quota', 0) <= 0)):
            raise ValueError('missing or invalid cpu.max evidence')
        st = cg['cpu.stat']
        if st.get('errno', 0) or st.get('present') != 1 or not st.get('raw'):
            raise ValueError('missing or invalid cpu.stat evidence')
    workers = end['workers']
    if (len(workers) != start['threads'] or sorted(w['idx'] for w in workers) != list(range(start['threads']))
            or sum(w['cpu_ns'] for w in workers) != end['worker_cpu_ns']):
        raise ValueError('incomplete worker accounting')
    for w in workers:
        if (any(w.get(k) != 0 for k in ('getcpu_errors', 'getcpu_oor', 'affinity_start_errno', 'affinity_end_errno'))
                or not w['cpus_seen'] or not w['affinity_start'] or not w['affinity_end'] or w['cpu_ns'] <= 0):
            raise ValueError('missing or failed worker CPU observations')
    active_wall_s = (min(start['load_deadline_ns'], end['wall_ns']) - start['wall_ns']) / 1e9
    sampler_ns = end['sampler_cpu_ns'] - start['sampler_cpu_ns']
    if active_wall_s <= 0 or sampler_ns < 0:
        raise ValueError('invalid busy interval or sampler counter')
    if end['proc_cpu_ns'] < start['proc_cpu_ns']:
        raise ValueError('CPU counters moved backwards')
    rates = {}
    # Sum every thread, including the observer, before comparing with process
    # accounting. Preserve worker-only and sampler rates separately below.
    rates['thread'] = ((end['worker_cpu_ns'] + sampler_ns) / 1e9) / wall_s
    rates['proc'] = ((end['proc_cpu_ns'] - start['proc_cpu_ns']) / 1e9) / wall_s if wall_s else 0.0
    d0, d1 = end['stat_start'], end['stat_end']
    if d0.get('present') != 1 or d1.get('present') != 1:
        raise ValueError('missing start/end cpu.stat counters')
    stat_delta = {k: d1[k] - d0[k]
                  for k in ('usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec',
                            'nr_bursts', 'burst_usec')}
    if any(v < 0 for v in stat_delta.values()):
        raise ValueError('CPU counters moved backwards')
    rates['stat'] = (stat_delta['usage_usec'] / 1e6) / wall_s if wall_s else 0.0
    # Counter reads sit somewhere inside the start/end capture windows, so the
    # elapsed time of the process/stat deltas is itself an interval.
    elapsed_wide_s = max(1e-9, (end_cap_end - start['wall_ns']) / 1e9)
    elapsed_narrow_s = max(1e-9, (end_cap_start - start_cap_end) / 1e9)
    proc_d_s = (end['proc_cpu_ns'] - start['proc_cpu_ns']) / 1e9
    stat_d_s = stat_delta['usage_usec'] / 1e6
    rates_interval = {
        'proc': (proc_d_s / elapsed_wide_s, proc_d_s / elapsed_narrow_s),
        'stat': (stat_d_s / elapsed_wide_s, stat_d_s / elapsed_narrow_s),
    }
    steal_s = None
    if start.get('steal_errno', 0) == 0 and end.get('steal_errno', 0) == 0 \
            and isinstance(start.get('steal_ns'), int) and isinstance(end.get('steal_ns'), int):
        if end['steal_ns'] < start['steal_ns']:
            raise ValueError('steal counter moved backwards')
        steal_s = (end['steal_ns'] - start['steal_ns']) / 1e9 / wall_s
    return {'start': start, 'end': end, 'samples': samples, 'wall_s': wall_s,
            'active_wall_s': active_wall_s,
            'elapsed_wide_s': elapsed_wide_s, 'elapsed_narrow_s': elapsed_narrow_s,
            'rates_interval': rates_interval, 'steal_s': steal_s,
            'thread_basis': 'workers_plus_sampler',
            'worker_cpu_cores': end['worker_cpu_ns'] / 1e9 / wall_s,
            'sampler_cpu_cores': sampler_ns / 1e9 / wall_s,
            'rates': rates, 'stat_delta': stat_delta, 'workers': end['workers']}


def cfs_band(expected_cores, period_s, wall_s):
    """Return (lower, upper) delivered-cores bounds from CFS period accounting."""
    quota_cores_time = expected_cores * period_s
    upper = (expected_cores * wall_s + EXTRA_PERIODS * quota_cores_time + SLIVER_S) / wall_s
    lower = max(0.0, (expected_cores * wall_s - EXTRA_PERIODS * quota_cores_time - SLIVER_S) / wall_s)
    return lower, upper


def rates_agree(rates, scale):
    tol = max(AGREE_MIN, AGREE_REL * max(0.01, scale))
    ordered = sorted(rates[k] for k in ('thread', 'proc', 'stat'))
    return ordered[-1] - ordered[0] <= tol, tol


def controller_present(summary):
    st = summary['end'].get('cgroup', {}).get('cpu.stat', {})
    return st.get('present') == 1 and 'raw' in st


def quota_setting(summary):
    cmax = summary['end'].get('cgroup', {}).get('cpu.max', {})
    if cmax.get('quota_max') == 1:
        return None
    return cmax['quota'] / cmax['period']


def _result(status, reason, **evidence):
    return {'status': status, 'reason': reason, 'evidence': evidence}


def classify_reference(summary, ncpu):
    if not controller_present(summary):
        return _result(FAIL, 'cpu.stat controller absent in container')
    threads = summary['start']['threads']
    # The final sleeping sampler can wake after busy workers' deadline. That
    # known idle tail is not an offered-load failure or extra usable capacity.
    expected = min(threads, ncpu) * summary['active_wall_s'] / summary['wall_s']
    rates = summary['rates']
    delta = summary['stat_delta']
    if quota_setting(summary) is not None:
        return _result(FAIL, 'reference unexpectedly has a CPU quota')
    agree, tol = rates_agree(rates, expected)
    if delta['nr_throttled']:
        return _result(FAIL, 'unthrottled container accumulated throttling',
                       rates=rates, stat_delta=delta)
    if not agree:
        return _result(INDETERMINATE, 'counters disagree before any limit was applied',
                       rates=rates, tolerance=tol, stat_delta=delta)
    if min(rates.values()) < expected - REF_SLACK_CORES:
        return _result(INDETERMINATE, 'offered load below available guest CPUs',
                       expected=expected, rates=rates, stat_delta=delta)
    return _result(PASS, 'unthrottled busy threads consume the available CPUs',
                   expected=expected, rates=rates, stat_delta=delta,
                   steal_s=summary.get('steal_s'))


def classify_quota(summary, expected, period_s, reference=None):
    """Enforcement verdict for one fixed --cpus/--cpu-quota run."""
    if not controller_present(summary):
        return _result(FAIL, 'cpu.stat controller absent in container')
    wall = summary['wall_s']
    lower, upper = cfs_band(expected, period_s, wall)
    # Capture-window uncertainty: the most permissive upper bound uses the
    # shortest plausible counter interval, the strictest lower bound the
    # longest one. Zero-duration captures (normal case) collapse to nominal.
    _, upper_narrow = cfs_band(expected, period_s, summary['elapsed_narrow_s'])
    lower_wide, _ = cfs_band(expected, period_s, summary['elapsed_wide_s'])
    rates = summary['rates']
    ri = summary['rates_interval']
    delta = summary['stat_delta']
    observed = quota_setting(summary)
    if (observed is None or abs(observed - expected) > 1e-6
            or summary['end']['cgroup']['cpu.max']['period'] != round(period_s * 1e6)):
        return _result(FAIL, 'container cpu.max does not match requested quota',
                       requested=expected, observed=observed)
    agree, tol = rates_agree(rates, expected)
    common = {'expected': expected, 'period_s': period_s, 'wall_s': wall,
              'lower': lower, 'upper': upper,
              'lower_wide': lower_wide, 'upper_narrow': upper_narrow,
              'rates': rates, 'rates_interval': {k: {'low': v[0], 'high': v[1]}
                                                 for k, v in ri.items()},
              'tolerance': tol,
              'stat_delta': delta, 'steal_s': summary.get('steal_s'),
              'unaccounted_thread_cpu_cores': rates['proc'] - rates['thread'],
              'worker_cpu_cores': summary['worker_cpu_cores'],
              'sampler_cpu_cores': summary['sampler_cpu_cores'],
              'other_process_cpu_cores': rates['stat'] - rates['proc']}
    if max(rates['thread'], ri['proc'][1], ri['stat'][1]) > upper_narrow:
        return _result(FAIL, 'consumption exceeds the CFS quota+period bound', **common)
    if delta['nr_throttled'] < 1 or delta['throttled_usec'] <= 0:
        return _result(INDETERMINATE,
                       'no throttling counters; quota effect cannot be demonstrated', **common)
    if not agree:
        return _result(INDETERMINATE, 'independent CPU counters disagree', **common)
    if min(ri['proc'][0], ri['stat'][0]) < lower_wide:
        return _result(INDETERMINATE, 'consumption below quota delivery bound; host starvation or noise', **common)
    if reference is not None:
        ref_rates = reference['rates']
        if min(ref_rates.values()) <= upper_narrow + SEPARATION_CORES:
            return _result(INDETERMINATE, 'reference load does not clear the enforcement band',
                           reference_rates=ref_rates, **common)
    return _result(PASS, 'consumption bounded by quota with throttling evidence', **common)


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


def _cpuset_expected(summary):
    cg = summary['end'].get('cgroup', {})
    raw = cg.get('cpuset.cpus.effective', {}).get('raw', '')
    return parse_cpu_list(raw), raw


def classify_cpuset(summary, expected_cpus, expected_quota=None, period_s=0.1, reference=None):
    expected_set = set(expected_cpus)
    actual_set, raw = _cpuset_expected(summary)
    if raw == '':
        return _result(FAIL, 'cpuset.cpus.effective unreadable or empty', raw=raw)
    if actual_set != expected_set:
        return _result(FAIL, 'effective cpuset differs from --cpuset-cpus',
                       requested=sorted(expected_set), observed=sorted(actual_set), raw=raw)
    for w in summary['workers']:
        for key in ('affinity_start', 'affinity_end'):
            if set(w.get(key, [])) != expected_set:
                return _result(FAIL, 'worker affinity was not restricted to the requested cpuset',
                               worker=w['idx'], key=key, observed=w.get(key),
                               expected=sorted(expected_set))
        if not set(w['cpus_seen']) <= expected_set:
            return _result(FAIL, 'worker ran on a CPU outside the requested cpuset',
                           worker=w['idx'], seen=w['cpus_seen'], expected=sorted(expected_set))
    if expected_quota is not None:
        result = classify_quota(summary, expected_quota, period_s, reference)
        result['evidence']['cpuset'] = sorted(actual_set)
        return result
    if quota_setting(summary) is not None:
        return _result(FAIL, 'cpuset-only run unexpectedly has a CPU quota')
    threads = summary['start']['threads']
    expected = min(threads, len(expected_set)) * summary['active_wall_s'] / summary['wall_s']
    rates = summary['rates']
    agree, tol = rates_agree(rates, expected)
    delta = summary['stat_delta']
    common = {'expected': expected, 'cpuset': sorted(actual_set), 'rates': rates,
              'tolerance': tol, 'stat_delta': delta, 'steal_s': summary.get('steal_s')}
    if delta['nr_throttled']:
        return _result(FAIL, 'unquotaed cpuset container accumulated throttling', **common)
    if not agree:
        return _result(INDETERMINATE, 'counters disagree on cpuset run', **common)
    if not (expected - REF_SLACK_CORES <= min(rates.values())
            and max(rates.values()) <= expected + REF_SLACK_CORES):
        return _result(INDETERMINATE, 'cpuset-local consumption outside expected per-CPU band', **common)
    return _result(PASS, 'all busy threads ran only on the requested CPUs at per-CPU capacity', **common)


def _key_of(s):
    return s.get('quota_max'), s.get('quota'), s.get('period')


def normalize_update_samples(samples):
    """Validate raw update-run samples.

    Returns (normalized, failure). failure is a _result(FAIL) dict when the
    stream cannot support any window verdict; normalized preserves every
    observation including unstable and slow captures."""
    norm = []
    for i, s in enumerate(samples):
        missing = [k for k in SAMPLE_REQUIRED if k not in s]
        if missing:
            return None, _result(FAIL, 'sampler output is missing required capture fields',
                                 missing=missing, index=i)
        if s['max_errno'] != 0:
            return None, _result(FAIL, 'sampler could not read cpu.max during update run',
                                 index=i, errno=s['max_errno'])
        if s['max2_errno'] != 0:
            return None, _result(FAIL, 'sampler could not re-read cpu.max during update run',
                                 index=i, errno=s['max2_errno'])
        if s['stat_errno'] != 0:
            return None, _result(FAIL, 'sampler could not read cpu.stat during update run',
                                 index=i, errno=s['stat_errno'])
        if s['quota_max'] not in (0, 1) or s['q2_quota_max'] not in (0, 1):
            return None, _result(FAIL, 'invalid parsed cpu.max setting', index=i)
        if not isinstance(s['max_stable'], int) or s['max_stable'] not in (0, 1):
            return None, _result(FAIL, 'invalid max_stable marker', index=i)
        if s['cap_end_ns'] < s['wall_ns']:
            return None, _result(FAIL, 'sampler capture clock reversed', index=i,
                                 wall_ns=s['wall_ns'], cap_end_ns=s['cap_end_ns'])
        if 'cap_dur_ns' in s and s['cap_dur_ns'] != s['cap_end_ns'] - s['wall_ns']:
            return None, _result(FAIL, 'sampler capture duration inconsistent', index=i)
        norm.append({
            'index': i, 'wall': s['wall_ns'], 'cap_end': s['cap_end_ns'],
            'key': _key_of(s),
            'key2': (s['q2_quota_max'], s['q2_quota'], s['q2_period']),
            'stable': s['max_stable'] == 1,
            'slow': (s['cap_end_ns'] - s['wall_ns']) / 1e9 > CAPTURE_MAX_S,
            'proc_ns': s['proc_cpu_ns'], 'usage_usec': s['usage_usec'],
            'nr_periods': s['nr_periods'], 'nr_throttled': s['nr_throttled'],
            'throttled_usec': s['throttled_usec'],
            'steal_ns': s.get('steal_ns') if s.get('steal_errno', 0) == 0 else None,
        })
    for a, b in zip(norm, norm[1:]):
        if b['wall'] <= a['wall']:
            return None, _result(FAIL, 'sampler timeline moved backwards',
                                 index=b['index'])
        if any(b[k] < a[k] for k in ('proc_ns', 'usage_usec', 'nr_periods',
                                     'nr_throttled', 'throttled_usec')):
            return None, _result(FAIL, 'sampler CPU counters moved backwards',
                                 index=b['index'])
        if a['steal_ns'] is not None and b['steal_ns'] is not None \
                and b['steal_ns'] < a['steal_ns']:
            return None, _result(FAIL, 'steal counter moved backwards', index=b['index'])
    return norm, None


def _collapse(keys):
    out = []
    for key in keys:
        if not out or out[-1] != key:
            out.append(key)
    return out


def stable_timeline(samples, schedule):
    """Status of the observed cpu.max timeline against the update schedule.

    Stable captures collapse consecutively; captures whose two cpu.max reads
    disagree (unstable) may only sit on a scheduled transition. Slow captures
    never affect timeline grouping."""
    expected_keys = [(0, int(round(cores * period_us)), period_us)
                     for cores, period_us in schedule]
    norm, failure = normalize_update_samples(samples)
    if failure:
        return failure['status'], {'reason': failure['reason'], **failure['evidence'],
                                   'expected': expected_keys}
    stable_keys = _collapse([s['key'] for s in norm if s['stable']])
    adjacency = set(zip(expected_keys, expected_keys[1:]))
    unstable_evidence = []
    for s in norm:
        if s['stable']:
            continue
        before = next((x['key'] for x in reversed(norm[:s['index']]) if x['stable']), None)
        after = next((x['key'] for x in norm[s['index'] + 1:] if x['stable']), None)
        unstable_evidence.append({'index': s['index'], 'before': before, 'after': after,
                                  'read_a': s['key'], 'read_b': s['key2']})
        if before is None or after is None or before == after or (before, after) not in adjacency:
            return FAIL, {'reason': 'cpu.max changed during a capture outside the scheduled transition',
                          'expected': expected_keys, 'stable_observed': stable_keys,
                          'unstable': unstable_evidence}
    if stable_keys != expected_keys:
        return FAIL, {'reason': 'observed cpu.max timeline does not match docker update schedule',
                      'expected': expected_keys, 'observed': stable_keys,
                      'unstable': unstable_evidence,
                      'segment_lengths': [sum(1 for s in norm if s['stable'] and s['key'] == k)
                                          for k in stable_keys]}
    return PASS, {'reason': 'running probe observes each docker update through its own cpu.max',
                  'expected': expected_keys, 'observed': stable_keys,
                  'unstable': unstable_evidence}


def _interior_groups(norm, lo_ns, hi_ns, segment_indexes):
    """Stable, fast captures fully inside the guarded time range, split into
    contiguous groups so no rate delta crosses an unstable capture."""
    blockers = {s['index'] for s in norm if not s['stable']}
    groups, current = [], []
    for s in norm:
        if s['index'] not in segment_indexes or not s['stable'] or s['slow']:
            continue
        if s['wall'] < lo_ns or s['cap_end'] > hi_ns:
            continue
        if current and any(bi in blockers for bi in range(current[-1]['index'] + 1, s['index'])):
            groups.append(current)
            current = []
        current.append(s)
    if current:
        groups.append(current)
    return groups


def _window_result(i, cores, period_us, a, b, reference, counts, spans):
    period_s = period_us / 1e6
    wide_s = (b['cap_end'] - a['wall']) / 1e9
    narrow_s = (b['wall'] - a['cap_end']) / 1e9
    mid_s = (b['wall'] - a['wall']) / 1e9
    if wide_s < MIN_INTERIOR_S or narrow_s <= 0:
        return _result(INDETERMINATE, 'window too short after time-based transition guards',
                       segment=i, interior_samples=counts['used'],
                       wall_wide_s=wide_s, wall_narrow_s=narrow_s, **spans, **counts)
    proc_d = (b['proc_ns'] - a['proc_ns']) / 1e9
    stat_d = (b['usage_usec'] - a['usage_usec']) / 1e6
    proc_lo, proc_hi = proc_d / wide_s, proc_d / narrow_s
    stat_lo, stat_hi = stat_d / wide_s, stat_d / narrow_s
    _, upper_narrow = cfs_band(cores, period_s, narrow_s)
    lower_wide, _ = cfs_band(cores, period_s, wide_s)
    lower, upper = cfs_band(cores, period_s, mid_s)
    nr_throttled = b['nr_throttled'] - a['nr_throttled']
    throttled_us = b['throttled_usec'] - a['throttled_usec']
    steal_s = None
    if a['steal_ns'] is not None and b['steal_ns'] is not None:
        steal_s = (b['steal_ns'] - a['steal_ns']) / 1e9 / wide_s
    ev = {'segment': i, 'expected': cores, 'period_s': period_s,
          'wall_s': mid_s, 'wall_wide_s': wide_s, 'wall_narrow_s': narrow_s,
          'lower': lower, 'upper': upper, 'lower_wide': lower_wide,
          'upper_narrow': upper_narrow,
          'rates': {'proc': proc_d / mid_s, 'stat': stat_d / mid_s},
          'rates_interval': {'proc': {'low': proc_lo, 'high': proc_hi},
                             'stat': {'low': stat_lo, 'high': stat_hi}},
          'nr_throttled': nr_throttled, 'throttled_usec': throttled_us,
          'steal_cores': steal_s, **spans}
    ev.update(counts)
    if max(proc_hi, stat_hi) > upper_narrow:
        r = _result(FAIL, 'post-update consumption exceeds quota+period bound', **ev)
    elif nr_throttled < 1 or throttled_us <= 0:
        r = _result(INDETERMINATE, 'no throttling counters in update window', **ev)
    elif min(proc_lo, stat_lo) < lower_wide:
        r = _result(INDETERMINATE, 'post-update delivery below quota bound; starvation or noise', **ev)
    else:
        tol = max(AGREE_MIN, AGREE_REL * cores)
        disagreement = max(0.0, stat_hi - proc_lo, proc_hi - stat_lo)
        if disagreement > tol:
            r = _result(INDETERMINATE, 'post-update CPU counters disagree', **ev)
        elif reference is not None and min(reference['rates'].values()) <= upper_narrow + SEPARATION_CORES:
            r = _result(INDETERMINATE, 'reference load does not clear the update-window band',
                        reference_rates=reference['rates'], **ev)
        else:
            r = _result(PASS, 'live docker update enforced within the unchanged container', **ev)
    return r


def classify_update_run(summary, schedule, reference=None):
    """Verify a single container whose --cpus value changes via docker update.

    schedule: ordered [(cores, period_us), ...] the container must show, in
    order, in its own stable cpu.max observations.
    """
    samples = summary['samples'] if isinstance(summary, dict) else summary
    expected_keys = [(0, int(round(cores * period_us)), period_us)
                     for cores, period_us in schedule]
    norm, failure = normalize_update_samples(samples)
    if failure:
        return [failure]
    timeline_status, timeline_ev = stable_timeline(samples, schedule)
    if timeline_status == FAIL:
        return [_result(FAIL, timeline_ev['reason'],
                        expected=timeline_ev.get('expected'),
                        observed=timeline_ev.get('stable_observed')
                                 or timeline_ev.get('observed'),
                        unstable=timeline_ev.get('unstable'))]
    results = []
    # Same key can recur (2.0 -> 0.5 -> 2.0), so segment membership is the
    # ordered run of stable samples, not the key value.
    run_id = -1
    prev_key = None
    for s in norm:
        if s['stable']:
            if run_id < 0 or s['key'] != prev_key:
                run_id += 1
            s['run'] = run_id
            prev_key = s['key']
    end_wall_ns = summary.get('end', {}).get('wall_ns') if isinstance(summary, dict) else None
    if end_wall_ns is None:
        end_wall_ns = norm[-1]['cap_end']
    for i, (cores, period_us) in enumerate(schedule):
        seg = [s for s in norm if s.get('run') == i]
        if not seg:
            results.append(_result(INDETERMINATE, 'no stable sample in scheduled segment', segment=i))
            continue
        first, last = seg[0], seg[-1]
        # Transition happened somewhere inside (last old capture end, first new
        # capture start); guards are measured from the fully-known stable edge.
        lo_ns = first['wall'] + int(HEAD_GUARD_S * 1e9)
        hi_ns = (end_wall_ns if i == len(schedule) - 1 else last['cap_end']) \
            - int(TAIL_GUARD_S * 1e9)
        groups = _interior_groups(norm, lo_ns, hi_ns, {s['index'] for s in seg})
        counts = {'samples_total': len(norm), 'used': 0,
                  'stable_total': sum(1 for s in norm if s['stable']),
                  'unstable_total': sum(1 for s in norm if not s['stable']),
                  'unstable_indexes': [s['index'] for s in norm if not s['stable']],
                  'slow_indexes': [s['index'] for s in norm if s['slow']],
                  'candidate_groups': [len(g) for g in groups]}
        # Longest contiguous group maximizes the stable interior.
        group = max(groups, key=lambda g: (g[-1]['cap_end'] - g[0]['wall']), default=[])
        if len(group) < 2:
            counts['used'] = len(group)
            results.append(_result(INDETERMINATE,
                                   'window too short after time-based transition guards',
                                   segment=i, head_guard_s=HEAD_GUARD_S,
                                   tail_guard_s=TAIL_GUARD_S,
                                   capture_max_s=CAPTURE_MAX_S, **counts))
            continue
        a, b = group[0], group[-1]
        counts['used'] = len(group)
        counts['endpoint_indexes'] = [a['index'], b['index']]
        spans = {'transition_guard_s': {'head': HEAD_GUARD_S, 'tail': TAIL_GUARD_S},
                 'first_stable_wall_s': (first['wall'] - norm[0]['wall']) / 1e9,
                 'last_stable_cap_end_s': (last['cap_end'] - norm[0]['wall']) / 1e9}
        results.append(_window_result(i, cores, period_us, a, b, reference, counts, spans))
    return results
