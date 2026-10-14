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
* if the unthrottled reference does not clearly exceed the upper enforcement
  band, offered load was insufficient and the result is indeterminate --
  never pass.
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
UPDATE_HEAD_SAMPLES = 5   # guard samples dropped after a docker update
UPDATE_TAIL_SAMPLES = 2


def parse_events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


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
    rates = {}
    rates['thread'] = (end['worker_cpu_ns'] / 1e9) / wall_s if wall_s else 0.0
    rates['proc'] = ((end['proc_cpu_ns'] - start['proc_cpu_ns']) / 1e9) / wall_s if wall_s else 0.0
    d0, d1 = end['stat_start'], end['stat_end']
    if d0.get('present') != 1 or d1.get('present') != 1:
        raise ValueError('missing start/end cpu.stat counters')
    stat_delta = {k: d1[k] - d0[k]
                  for k in ('usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec',
                            'nr_bursts', 'burst_usec')}
    if any(v < 0 for v in stat_delta.values()) or end['proc_cpu_ns'] < start['proc_cpu_ns']:
        raise ValueError('CPU counters moved backwards')
    rates['stat'] = (stat_delta['usage_usec'] / 1e6) / wall_s if wall_s else 0.0
    return {'start': start, 'end': end, 'samples': samples, 'wall_s': wall_s,
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
    expected = min(threads, ncpu)
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
                   expected=expected, rates=rates, stat_delta=delta)


def classify_quota(summary, expected, period_s, reference=None):
    """Enforcement verdict for one fixed --cpus/--cpu-quota run."""
    if not controller_present(summary):
        return _result(FAIL, 'cpu.stat controller absent in container')
    wall = summary['wall_s']
    lower, upper = cfs_band(expected, period_s, wall)
    rates = summary['rates']
    delta = summary['stat_delta']
    observed = quota_setting(summary)
    if (observed is None or abs(observed - expected) > 1e-6
            or summary['end']['cgroup']['cpu.max']['period'] != round(period_s * 1e6)):
        return _result(FAIL, 'container cpu.max does not match requested quota',
                       requested=expected, observed=observed)
    agree, tol = rates_agree(rates, expected)
    common = {'expected': expected, 'period_s': period_s, 'wall_s': wall,
              'lower': lower, 'upper': upper, 'rates': rates, 'tolerance': tol,
              'stat_delta': delta}
    if max(rates.values()) > upper:
        return _result(FAIL, 'consumption exceeds the CFS quota+period bound', **common)
    if delta['nr_throttled'] < 1 or delta['throttled_usec'] <= 0:
        return _result(INDETERMINATE,
                       'no throttling counters; quota effect cannot be demonstrated', **common)
    if not agree:
        return _result(INDETERMINATE, 'independent CPU counters disagree', **common)
    if min(rates.values()) < lower:
        return _result(INDETERMINATE, 'consumption below quota delivery bound; host starvation or noise', **common)
    if reference is not None:
        ref_rates = reference['rates']
        if min(ref_rates.values()) <= upper + SEPARATION_CORES:
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
    expected = min(threads, len(expected_set))
    rates = summary['rates']
    agree, tol = rates_agree(rates, expected)
    delta = summary['stat_delta']
    common = {'expected': expected, 'cpuset': sorted(actual_set), 'rates': rates,
              'tolerance': tol, 'stat_delta': delta}
    if delta['nr_throttled']:
        return _result(FAIL, 'unquotaed cpuset container accumulated throttling', **common)
    if not agree:
        return _result(INDETERMINATE, 'counters disagree on cpuset run', **common)
    if not (expected - REF_SLACK_CORES <= min(rates.values())
            and max(rates.values()) <= expected + REF_SLACK_CORES):
        return _result(INDETERMINATE, 'cpuset-local consumption outside expected per-CPU band', **common)
    return _result(PASS, 'all busy threads ran only on the requested CPUs at per-CPU capacity', **common)


def quota_segments(samples):
    """Group consecutive samples sharing one observed cpu.max setting."""
    segments = []
    for s in samples:
        key = (s.get('quota_max'), s.get('quota'), s.get('period'))
        if segments and segments[-1]['key'] == key:
            segments[-1]['samples'].append(s)
        else:
            segments.append({'key': key, 'samples': [s]})
    return segments


def _segment_rates(samples):
    if len(samples) < 2:
        return None
    a, b = samples[0], samples[-1]
    wall = (b['wall_ns'] - a['wall_ns']) / 1e9
    if wall <= 0:
        return None
    proc = ((b['proc_cpu_ns'] - a['proc_cpu_ns']) / 1e9) / wall
    stat = ((b['usage_usec'] - a['usage_usec']) / 1e6) / wall
    throttled = b['nr_throttled'] - a['nr_throttled']
    throttled_us = b['throttled_usec'] - a['throttled_usec']
    return {'wall_s': wall, 'proc': proc, 'stat': stat,
            'nr_throttled': throttled, 'throttled_usec': throttled_us}


def classify_update_run(summary, schedule, reference=None):
    """Verify a single container whose --cpus value changes via docker update.

    schedule: ordered [(cores, period_us), ...] the container must show, in
    order, in its own sampled cpu.max observations.
    """
    samples = summary['samples']
    if any(s.get('stat_errno') != 0 or s.get('max_errno') != 0 for s in samples):
        return [_result(FAIL, 'sampler could not read cpu.stat or cpu.max during update run')]
    for a, b in zip(samples, samples[1:]):
        if (b['wall_ns'] <= a['wall_ns'] or any(b[k] < a[k] for k in
                ('proc_cpu_ns', 'usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec'))):
            return [_result(FAIL, 'sampler timeline or counters moved backwards')]
    segments = quota_segments(samples)
    expected_keys = [(0, int(round(cores * period_us)), period_us)
                     for cores, period_us in schedule]
    actual_keys = [seg['key'] for seg in segments]
    if actual_keys != expected_keys:
        return [_result(FAIL, 'observed cpu.max timeline does not match docker update schedule',
                        expected=expected_keys, observed=actual_keys,
                        segment_lengths=[len(seg['samples']) for seg in segments])]
    results = []
    for i, (seg, (cores, period_us)) in enumerate(zip(segments, schedule)):
        interior = seg['samples'][UPDATE_HEAD_SAMPLES:len(seg['samples']) - UPDATE_TAIL_SAMPLES]
        rates = _segment_rates(interior)
        if rates is None or rates['wall_s'] < 1.0:
            results.append(_result(INDETERMINATE, 'window too short after transition guards',
                                   segment=i, interior_samples=len(interior)))
            continue
        period_s = period_us / 1e6
        lower, upper = cfs_band(cores, period_s, rates['wall_s'])
        ev = {'segment': i, 'expected': cores, 'period_s': period_s, 'wall_s': rates['wall_s'],
              'lower': lower, 'upper': upper, 'rates': {k: rates[k] for k in ('proc', 'stat')},
              'nr_throttled': rates['nr_throttled'], 'throttled_usec': rates['throttled_usec']}
        if max(rates['proc'], rates['stat']) > upper:
            r = _result(FAIL, 'post-update consumption exceeds quota+period bound', **ev)
        elif rates['nr_throttled'] < 1 or rates['throttled_usec'] <= 0:
            r = _result(INDETERMINATE, 'no throttling counters in update window', **ev)
        elif min(rates['proc'], rates['stat']) < lower:
            r = _result(INDETERMINATE, 'post-update delivery below quota bound; starvation or noise', **ev)
        elif abs(rates['proc'] - rates['stat']) > max(AGREE_MIN, AGREE_REL * cores):
            r = _result(INDETERMINATE, 'post-update CPU counters disagree', **ev)
        elif reference is not None and min(reference['rates'].values()) <= upper + SEPARATION_CORES:
            r = _result(INDETERMINATE, 'reference load does not clear the update-window band',
                        reference_rates=reference['rates'], **ev)
        else:
            r = _result(PASS, 'live docker update enforced within the unchanged container', **ev)
        results.append(r)
    return results
