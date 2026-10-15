"""Functional evaluation for the two-container --cpu-shares contention run (v3).

v3 replaces the v2 "single timestamp + continuous weight timeline" model with
the frozen v4 *bracketed capture* contract, applied to weight contention:

* Every periodic observation is one capture: wall_ns taken before the first
  syscall, cap_end_ns after the last; cpu.weight and cpuset.cpus.effective are
  read BEFORE and AFTER cpu.stat and cpu.max is read twice around it. A weight
  change landing anywhere inside a (possibly multi-second) capture is reported
  weight_stable=0; the checker then quarantines the whole capture span.
* Rate deltas between two captures a,b elapsed over some wall interval
  w in [b.wall_ns - a.cap_end_ns (narrow), b.cap_end_ns - a.wall_ns (wide)].
  Every per-container rate, and therefore the joint allocation fraction, is an
  INTERVAL. A phase PASSES only when every plausible value satisfies the
  functional band; it FAILS only when every plausible value violates; a
  straddling boundary is INDETERMINATE. Slow/valid captures shrink the joint
  intervals (never silently pass, never throw evidence away).
* Joint phases are the intersection of BOTH containers' stable-fast sample
  segments at the planned weights. Startup stagger, updates, unstable/error
  captures, slow captures and coverage gaps are all quarantine intervals that
  no rate delta may cross.

Expected fractions derive from the OBSERVED cgroup cpu.weight values, never
from raw Docker shares. The pinned daemon's shares->weight conversion
(moby-port-pristine containerd/cgroups/v3 log2 fit) is recorded only as a
computed label pending a real run.

v2's fail-closed missing-evidence machinery is retained unchanged in spirit:
checker-owned mandatory labels/roles, no producer allowlists, mandatory
snapshot fields, present leftovers inventory, strict fail/indeterminate/
not_tested separation.
"""
import json
import re

PASS = 'pass'
FAIL = 'fail'
INDETERMINATE = 'indeterminate'
NOT_TESTED = 'not_tested'

CONTAINERS = ('a', 'b')
STAGES = 3

PLAN = ({'a': 1024, 'b': 512},
        {'a': 512, 'b': 1024},
        {'a': 512, 'b': 512})
PLANNED_UPDATES = ((1, 'a', 1024, 512),
                   (1, 'b', 512, 1024),
                   (2, 'b', 1024, 512))

MANDATORY_LABELS_V3 = frozenset((
    'build-share-probe', 'gcc-version', 'probe-static-check', 'probe-selftest',
    'instance-start', 'daemon-info', 'image-import', 'start-a', 'start-b',
    'snapshot-a-0', 'snapshot-b-0', 'inspect-a-0', 'inspect-b-0',
    'preupd-a-1', 'update-a-1', 'postupd-a-1',
    'preupd-b-1', 'update-b-1', 'postupd-b-1',
    'snapshot-a-1', 'snapshot-b-1', 'inspect-a-1', 'inspect-b-1',
    'preupd-b-2', 'update-b-2', 'postupd-b-2',
    'snapshot-a-2', 'snapshot-b-2', 'inspect-a-2', 'inspect-b-2',
    'wait-a', 'wait-b', 'logs-a', 'logs-b', 'final-a', 'final-b',
    'rm-a', 'rm-b', 'ps-leftovers', 'instance-stop'))
# v4 adds explicit bounded graceful completion: after the final stable phase
# the runner SIGTERMs both probes and they emit graceful end events.
MANDATORY_LABELS = MANDATORY_LABELS_V3 | frozenset(('stop-a', 'stop-b'))
OPTIONAL_LABELS = frozenset(('prerm-a', 'prerm-b', 'cleanup-a', 'cleanup-b', 'final-stop'))
OPTIONAL_PREFIXES = ('poll-',)

GRACE_STOP_LATENCY_S = 2.0    # SIGTERM receipt -> flushed end event
STABLE_WINDOW_MIN_S = 4.0     # the runner's bounded completion window floor

INPUT_ROLES = ('kernel', 'disk', 'probe_source', 'probe_binary', 'runner',
               'checker', 'harddind', 'guest_cleanup')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

# Functional tolerance (unchanged since v1; scheduler-granularity derived).
START_TRIM_S = 0.5
MIN_WINDOW_S = 4.0
MIN_SAMPLES = 20
MAX_GAP_S = 0.5
ENDPOINT_SKEW_S = 0.5
BRACKET_SKEW_S = 0.4
CAPTURE_FAST_S = 0.05        # slower bracketed captures are treated as uncertain
MIN_PHASE_COVERAGE = 0.7     # measured joint intervals must span >=70% of run bounds
# The 100 ms sample cadence can stall under guest descheduling; stalls above
# MAX_GAP_S split runs (not widened, not called normal). v4 observed 1.89 s.

FRAC_TOL = 0.09
EQUAL_TOL = 0.08
FRAC_AGREE = 0.05
ORDER_MARGIN = 0.05
AGREE_REL = 0.05
AGREE_MIN = 0.03
WHOLE_RUN_AGREE = 0.08
SAT_LOW = 0.85
SAT_HIGH = 1.15
STARVATION_FRAC = 0.12
STARVATION_CORES = 0.10

PHASE_LABELS = ('phase_2to1', 'phase_reversed_1to2', 'phase_equal_control')

# Pinned primary source for the *computed* shares->weight label (NOT observed):
# moby-port-pristine/vendor/github.com/containerd/cgroups/v3/cgroup2/utils.go
# ConvertCPUSharesToCgroupV2Value, a log2 quadratic fit (ceil(10**exponent)).
PINNED_MAPPING_SOURCE = (
    'moby-port-pristine/vendor/github.com/containerd/cgroups/v3/cgroup2/utils.go '
    '(also opencontainers/cgroups/utils.go); label=computed_pending_runtime')

REQUIRED_CHECK_NAMES = (
    'guest_topology', 'container_identity', 'probe_streams', 'weight_mapping',
    'phase_2to1', 'phase_reversed_1to2', 'phase_equal_control', 'phase_ordering',
    'capture_protocol', 'weight_timeline', 'residency_affinity',
    'whole_run_accounting', 'update_ledger', 'command_ledger',
    'input_provenance', 'leftovers', 'lifecycle')

SAMPLE_REQUIRED = (
    'wall_ns', 'cap_end_ns', 'cap_dur_ns', 'proc_cpu_ns', 'sampler_cpu_ns',
    'weight1', 'weight2', 'weight1_errno', 'weight2_errno',
    'weight1_invalid', 'weight2_invalid', 'weight_stable',
    'cpuset1_errno', 'cpuset2_errno', 'cpuset_stable',
    'cpuset1_raw', 'cpuset2_raw',
    'quota_max', 'quota', 'period', 'max_errno',
    'q2_quota_max', 'q2_quota', 'q2_period', 'max2_errno', 'max_stable',
    'share_valid', 'stat_errno', 'usage_usec', 'nr_periods',
    'nr_throttled', 'throttled_usec')


def _result(status, reason, **evidence):
    return {'status': status, 'reason': reason, 'evidence': _native(evidence)}


def _native(obj):
    if isinstance(obj, dict):
        return {str(k): _native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        out = [_native(v) for v in obj]
        try:
            return sorted(out, key=lambda v: json.dumps(v, sort_keys=True))
        except TypeError:
            return out
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    raise TypeError('non-JSON-native evidence: %r' % (type(obj),))


def parse_events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


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


def computed_weight(shares):
    """The PINNED daemon's conversion, replicated for a computed-only label."""
    import math
    if shares == 0:
        return 0
    if shares <= 2:
        return 1
    if shares >= 262144:
        return 10000
    l = math.log2(float(shares))
    exponent = (l * l + 125 * l) / 612.0 - 7.0 / 34.0
    return math.ceil(10 ** exponent)


def _as_int(event, key, where):
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s: missing integer %s' % (where, key))
    return value


def _weight_of_event(event):
    field = (event.get('cgroup') or {}).get('cpu.weight')
    if not isinstance(field, dict) or field.get('errno') != 0 or field.get('invalid'):
        raise ValueError('cpu.weight endpoint evidence missing/invalid')
    raw = field.get('raw')
    m = re.fullmatch(r'[0-9]+', str(raw).strip()) if raw is not None else None
    if not m:
        raise ValueError('cpu.weight endpoint not numeric: %r' % (raw,))
    value = int(raw)
    if not 1 <= value <= 10000:
        raise ValueError('cpu.weight endpoint out of range: %d' % value)
    return value


def _endpoint_cpuset(event, shared_cpu, side=1):
    cg = event.get('cgroup') or {}
    key = 'cpuset.cpus.effective' if side == 1 else 'cpuset.cpus.effective2'
    field = cg.get(key)
    if not isinstance(field, dict) or field.get('errno') != 0:
        raise ValueError('%s missing or error' % key)
    if parse_cpu_list(field.get('raw', '')) != {shared_cpu}:
        raise ValueError('%s is not the shared CPU %d' % (key, shared_cpu))
    return field.get('raw')


def _endpoint_max(event):
    cmax = (event.get('cgroup') or {}).get('cpu.max')
    if not isinstance(cmax, dict) or cmax.get('errno') != 0 or cmax.get('quota_max') != 1:
        raise ValueError('endpoint cpu.max missing, errored, or finite quota')


def _endpoint_stat(event):
    st = (event.get('cgroup') or {}).get('cpu.stat')
    if not isinstance(st, dict) or st.get('errno', 0) or st.get('present') != 1 or not st.get('raw'):
        raise ValueError('endpoint cpu.stat missing or invalid')
    for key in ('usage_usec', 'nr_periods', 'nr_throttled', 'throttled_usec'):
        _as_int(st, key, 'cpu.stat')


# --------------------------------------------------------------------------- #
# Stream validation
# --------------------------------------------------------------------------- #

def _classify_sample(s, shared_cpu):
    """Return ('stable'|'unstable'|'error', detail). Schema assumed valid."""
    read_err = (s['max_errno'] != 0 or s['max2_errno'] != 0 or s['stat_errno'] != 0
                or s['weight1_errno'] != 0 or s['weight2_errno'] != 0
                or s['cpuset1_errno'] != 0 or s['cpuset2_errno'] != 0)
    hard_err = (s['max_errno'] != 0 or s['max2_errno'] != 0 or s['stat_errno'] != 0
                or s['cpuset1_errno'] != 0 or s['cpuset2_errno'] != 0)
    if hard_err:
        # cpu.max/cpu.stat/cpuset are invariant during a shares run; a failed
        # read breaks the strict capture contract rather than quarantining.
        return 'fatal', {'max': s['max_errno'], 'max2': s['max2_errno'],
                         'stat': s['stat_errno'],
                         'c1': s['cpuset1_errno'], 'c2': s['cpuset2_errno']}
    if s['weight1_errno'] != 0 or s['weight2_errno'] != 0:
        # The file under experiment may transiently be unreadable; the failed
        # capture is preserved as quarantine evidence and counted.
        return 'error', {'w1': s['weight1_errno'], 'w2': s['weight2_errno']}
    # All reads succeeded: strict protocol invariants.
    if s['quota_max'] != 1 or s['q2_quota_max'] != 1:
        return 'error', {'protocol': 'quota observed inside shares experiment'}
    if parse_cpu_list(s['cpuset1_raw']) != {shared_cpu} \
            or parse_cpu_list(s['cpuset2_raw']) != {shared_cpu}:
        return 'error', {'protocol': 'cpuset escaped shared cpu within capture'}
    if not s['max_stable'] or not s['cpuset_stable']:
        return 'unstable', {'reason': 'max_or_cpuset_changed'}
    if not s['weight_stable']:
        return 'unstable', {'reason': 'weight_changed',
                            'w1': s['weight1'], 'w2': s['weight2']}
    return 'stable', {}


def validate_contend_stream(events, shared_cpu):
    if not events:
        raise ValueError('contend stream is empty')
    starts = [e for e in events if e.get('event') == 'start']
    ends = [e for e in events if e.get('event') == 'end']
    samples = [e for e in events if e.get('event') == 'sample']
    if len(starts) != 1 or len(ends) != 1 or not samples:
        raise ValueError('stream needs one start, one end and samples')
    start, end = starts[0], ends[0]
    if events[0] is not start or events[-1] is not end:
        raise ValueError('stream is not start..sample*..end')
    if start.get('mode') != 'contend':
        raise ValueError('share protocol requires the dedicated contend mode stream')
    _endpoint_max(start)
    _endpoint_max(end)
    _endpoint_cpuset(start, shared_cpu, 1)
    _endpoint_cpuset(end, shared_cpu, 1)
    _weight_of_event(start)
    _weight_of_event(end)
    _endpoint_stat(start)
    _endpoint_stat(end)
    if set(start.get('affinity') or []) != {shared_cpu}:
        raise ValueError('process affinity at start escapes the shared CPU')
    if int(start.get('nproc', 0)) <= shared_cpu:
        raise ValueError('guest reports fewer online CPUs than the shared CPU index')
    deadline = _as_int(start, 'load_deadline_ns', 'start')
    if deadline <= start['wall_ns']:
        raise ValueError('load_deadline_ns does not exceed start wall time')
    sampler0 = _as_int(start, 'sampler_cpu_ns', 'start')

    classified = []
    prev = None
    for i, s in enumerate(samples):
        where = 'sample %d' % i
        missing = [k for k in SAMPLE_REQUIRED if k not in s]
        if missing:
            raise ValueError('%s missing fields %r' % (where, missing))
        for k in SAMPLE_REQUIRED:
            if k.endswith('_raw'):
                if not isinstance(s[k], str):
                    raise ValueError('%s %s must be a string' % (where, k))
            elif k in ('weight_stable', 'max_stable', 'cpuset_stable', 'share_valid'):
                if s[k] not in (0, 1):
                    raise ValueError('%s invalid marker %s' % (where, k))
            elif not isinstance(s[k], int) or isinstance(s[k], bool):
                raise ValueError('%s %s must be integer' % (where, k))
        # v4 graceful-completion markers (absent == 0 on frozen v3/v4-era logs).
        for k in ('terminal', 'graceful'):
            if k in s and s[k] not in (0, 1):
                raise ValueError('%s invalid %s marker' % (where, k))
        if s.get('terminal') == 1 and not s.get('graceful'):
            raise ValueError('%s terminal capture outside graceful shutdown' % where)
        if s['cap_end_ns'] < s['wall_ns']:
            raise ValueError('%s capture clock reversed' % where)
        if s['cap_dur_ns'] != s['cap_end_ns'] - s['wall_ns']:
            raise ValueError('%s cap_dur_ns inconsistent' % where)
        if not 1 <= s['weight1'] <= 10000 and s['weight1_errno'] == 0:
            raise ValueError('%s weight1 out of range' % where)
        if not 1 <= s['weight2'] <= 10000 and s['weight2_errno'] == 0:
            raise ValueError('%s weight2 out of range' % where)
        kind, detail = _classify_sample(s, shared_cpu)
        if kind == 'fatal':
            raise ValueError('%s: invariant capture file read error %r' % (where, detail))
        if kind == 'error' and detail.get('protocol'):
            raise ValueError('%s: %s' % (where, detail['protocol']))
        # A capture spanning SIGTERM-driven worker shutdown is terminal: its
        # counters straddle busy->idle and must never anchor a rate window.
        if s.get('terminal') == 1:
            kind, detail = 'terminal', {'reason': 'graceful_stop_span'}
        if prev is not None:
            if s['wall_ns'] <= prev['wall_ns']:
                raise ValueError('capture wall clock moved backwards')
            for key in ('proc_cpu_ns', 'sampler_cpu_ns', 'usage_usec', 'nr_periods',
                        'nr_throttled', 'throttled_usec'):
                if s[key] < prev[key]:
                    raise ValueError('counter %s moved backwards' % key)
        row = dict(s)
        row['kind'] = kind
        row['detail'] = detail
        row['slow'] = (s['cap_end_ns'] - s['wall_ns']) / 1e9 > CAPTURE_FAST_S
        classified.append(row)
        prev = s

    workers = end.get('workers') or []
    if len(workers) != start.get('threads') or not workers:
        raise ValueError('worker accounting does not match announced thread count')
    if sorted(w['idx'] for w in workers) != list(range(start['threads'])):
        raise ValueError('worker indices incomplete')
    if sum(w['cpu_ns'] for w in workers) != end.get('worker_cpu_ns'):
        raise ValueError('per-worker CPU clocks do not match worker_cpu_ns total')
    for w in workers:
        if w.get('cpu_ns', 0) <= 0:
            raise ValueError('worker %d recorded no CPU time' % w['idx'])
        if any(w.get(k) != 0 for k in
               ('getcpu_errors', 'getcpu_oor', 'affinity_start_errno', 'affinity_end_errno')):
            raise ValueError('worker %d has failed affinity/getcpu observations' % w['idx'])
        for key in ('affinity_start', 'affinity_end'):
            if set(w.get(key) or []) != {shared_cpu}:
                raise ValueError('worker %d %s is not pinned to CPU %d' % (w['idx'], key, shared_cpu))
        seen = set(w.get('cpus_seen') or [])
        if not seen or not seen <= {shared_cpu}:
            raise ValueError('worker %d ran outside the shared CPU: %r' % (w['idx'], sorted(seen)))

    sampler1 = _as_int(end, 'sampler_cpu_ns', 'end')
    if sampler1 < sampler0:
        raise ValueError('sampler CPU clock moved backwards')
    graceful = int(end.get('graceful_stop', 0))
    if graceful not in (0, 1):
        raise ValueError('end graceful_stop marker invalid')
    stop_ns_v = end.get('stop_ns', 0)
    if not isinstance(stop_ns_v, int) or isinstance(stop_ns_v, bool) or stop_ns_v < 0:
        raise ValueError('end stop_ns invalid')
    if graceful:
        if stop_ns_v == 0:
            raise ValueError('graceful end without stop_ns')
        if not (start['wall_ns'] <= stop_ns_v <= end['cap_end_ns']):
            raise ValueError('stop_ns outside the bracketed stream interval')
        if (end['cap_end_ns'] - stop_ns_v) / 1e9 > GRACE_STOP_LATENCY_S:
            raise ValueError('graceful shutdown to end-event flush exceeded '
                             '%.1f s' % GRACE_STOP_LATENCY_S)
    if 'hard_deadline_ns' in end and end['hard_deadline_ns'] != deadline:
        raise ValueError('end hard_deadline_ns disagrees with start load_deadline_ns')
    wall_ns = end['wall_ns'] - start['wall_ns']
    if wall_ns <= 0:
        raise ValueError('spin wall duration non-positive')
    proc_ns = end['proc_cpu_ns'] - start['proc_cpu_ns']
    stat_us = end['stat_end']['usage_usec'] - end['stat_start']['usage_usec']
    if proc_ns <= 0 or stat_us < 0:
        raise ValueError('process/stat counters invalid over the run')
    busy = [s for s in classified if s['wall_ns'] <= deadline]
    if len(busy) < 2:
        raise ValueError('no fully-busy captures at or before load_deadline_ns')
    wall_s = wall_ns / 1e9
    sampler_ns = sampler1 - sampler0
    return {
        'start': start, 'end': end, 'samples': busy, 'tail_samples': classified[len(busy):],
        'wall_s': wall_s, 'deadline_ns': deadline, 'sampler_ns': sampler_ns,
        'rates': {'thread': (end['worker_cpu_ns'] + sampler_ns) / 1e9 / wall_s,
                  'proc': proc_ns / 1e9 / wall_s,
                  'stat': stat_us / 1e6 / wall_s},
        'wide_s': (end['cap_end_ns'] - start['wall_ns']) / 1e9,
        'narrow_s': max(0, end['wall_ns'] - start['cap_end_ns']) / 1e9,
        'workers': workers, 'shared_cpu': shared_cpu,
        'graceful': bool(graceful), 'stop_ns': stop_ns_v,
        'weight_start': _weight_of_event(start), 'weight_end': _weight_of_event(end),
        'start_weight_stable': bool((start.get('cgroup') or {}).get('weight_stable')),
        'end_weight_stable': bool((end.get('cgroup') or {}).get('weight_stable'))}


def validate_stage_snapshot(event, shared_cpu, where):
    """One docker exec snapshot (unbracketed corroboration only)."""
    if not isinstance(event, dict) or event.get('event') != 'snapshot':
        raise ValueError('%s is not a snapshot event' % where)
    _as_int(event, 'wall_ns', where)
    cg = event.get('cgroup') or {}
    cmax = cg.get('cpu.max')
    if not isinstance(cmax, dict) or cmax.get('errno') != 0 or cmax.get('quota_max') != 1:
        raise ValueError('%s cpu.max missing/error/quota' % where)
    eff = cg.get('cpuset.cpus.effective')
    if not isinstance(eff, dict) or eff.get('errno') != 0 \
            or parse_cpu_list(eff.get('raw', '')) != {shared_cpu}:
        raise ValueError('%s cpuset.cpus.effective invalid' % where)
    st = cg.get('cpu.stat')
    if not isinstance(st, dict) or st.get('errno', 0) or st.get('present') != 1:
        raise ValueError('%s cpu.stat invalid' % where)
    weight = _weight_of_event(event)
    return {'wall_ns': event['wall_ns'], 'weight': weight, 'cpuset_eff': eff.get('raw')}


# --------------------------------------------------------------------------- #
# Timeline: stable segments, weight runs, quarantine
# --------------------------------------------------------------------------- #

def reconstruct(samples, max_gap_ns):
    """Split the capture stream into stable-fast chains separated by weight
    changes, unstable/error captures, coverage gaps or slow stable captures.

    A slow STABLE capture is not discarded: it may bracket a chain on either
    side (head/tail anchor). A rate delta ending/starting at that anchor is
    honestly spread over its whole [wall_ns, cap_end_ns] by the joint-window
    interval math, which is what lets a multi-second valid capture make a
    phase INDETERMINATE instead of either passing or vanishing. Unstable and
    error captures can never anchor anything: their spans are quarantine."""
    fast_chains, quarantine = [], []

    def flush(chain):
        if chain:
            fast_chains.append(chain)

    def q(lo, hi, kind):
        return {'lo_ns': lo, 'hi_ns': hi, 'kind': kind}

    chain, prev = [], None
    for s in samples:
        if prev is not None and s['wall_ns'] - prev['cap_end_ns'] > max_gap_ns:
            quarantine.append(q(prev['cap_end_ns'], s['wall_ns'], 'coverage_gap'))
            flush(chain)
            chain = []
        if s['kind'] != 'stable':
            label = 'graceful_stop_span' if s['kind'] == 'terminal' else s['kind']
            quarantine.append(q(s['wall_ns'], s['cap_end_ns'], label))
            flush(chain)
            chain = []
        elif s['slow']:
            flush(chain)
            chain = []
        else:
            if chain and s['weight1'] != chain[0]['weight1']:
                quarantine.append(q(prev['cap_end_ns'], s['wall_ns'], 'weight_change'))
                flush(chain)
                chain = []
            chain.append(s)
        prev = s
    flush(chain)

    # Attach at most one directly adjacent slow, stable, same-weight capture
    # to each chain edge; consecutive slow captures anchor nothing across them.
    segments, slow = [], []
    for i, ch in enumerate(fast_chains):
        first_idx = samples.index(ch[0])
        last_idx = samples.index(ch[-1])
        head = tail = None
        h = samples[first_idx - 1] if first_idx > 0 else None
        if h is not None and h['kind'] == 'stable' and h['slow'] \
                and h['weight1'] == ch[0]['weight1']:
            head = h
        t = samples[last_idx + 1] if last_idx + 1 < len(samples) else None
        if t is not None and t['kind'] == 'stable' and t['slow'] \
                and t['weight1'] == ch[-1]['weight1']:
            tail = t
        seg_samples = ([head] if head else []) + ch + ([tail] if tail else [])
        segments.append({
            'weight': ch[0]['weight1'], 'fast_samples': ch,
            'head': head, 'tail': tail, 'samples': seg_samples,
            'first_ns': seg_samples[0]['wall_ns'],
            'last_t1': seg_samples[-1]['cap_end_ns'],
            'has_slow_anchor': bool(head or tail)})
    for s in samples:
        if s['kind'] == 'stable' and s['slow']:
            slow.append({'t0_ns': s['wall_ns'], 't1_ns': s['cap_end_ns'],
                         'weight': s['weight1'], 'stable': True})

    quarantine.sort(key=lambda z: z['lo_ns'])
    merged = []
    for part in quarantine:
        lo, hi, kind = part['lo_ns'], part['hi_ns'], part['kind']
        if hi < lo:
            hi = lo
        if merged and lo <= merged[-1]['hi_ns']:
            merged[-1]['hi_ns'] = max(merged[-1]['hi_ns'], hi)
            if kind not in merged[-1]['kind']:
                merged[-1]['kind'] = merged[-1]['kind'] + '+' + kind
        else:
            merged.append(q(lo, hi, kind))

    weights, run_index = [], []
    idx = -1
    prev_weight = None
    for seg in segments:
        if seg['weight'] != prev_weight:
            idx += 1
            weights.append(seg['weight'])
            prev_weight = seg['weight']
        seg['run_index'] = idx
        run_index.append(idx)
    return {'segments': segments, 'weights': weights,
            'quarantine': merged, 'slow_anchors': slow, 'samples': samples}


PLANNED_WEIGHT_SEQUENCE = {'a': ('high', 'low'), 'b': ('low', 'high', 'low')}


def validate_timeline(recs, params):
    """Strict weight-timeline verification for both containers."""
    max_gap_ns = int(float(params.get('max_gap_s', MAX_GAP_S)) * 1e9)
    built = {}
    problems = []
    observed = set()
    for c in CONTAINERS:
        if c not in recs:
            problems.append('%s timeline absent' % c)
            continue
        r = reconstruct(recs[c]['samples'], max_gap_ns)
        built[c] = r
        observed.update(r['weights'])
    if problems:
        return None, {'error': '; '.join(problems)}
    if len(observed) != 2:
        return built, {'error': 'timeline must contain exactly two distinct weights',
                       'observed': sorted(observed)}
    whi, wlo = max(observed), min(observed)
    expected_seq = {'a': [whi, wlo], 'b': [wlo, whi, wlo]}
    for c in CONTAINERS:
        if built[c]['weights'] != expected_seq[c]:
            return built, {'error': '%s stable weight sequence %r does not match plan %r'
                           % (c, built[c]['weights'], expected_seq[c]),
                           'whi': whi, 'wlo': wlo}

    # Every unstable capture must straddle exactly one planned weight
    # transition: nearest stable captures on each side must differ, and the
    # two bracket reads have to be the old then the new value in capture
    # order (A high->low; B low->high then high->low).
    allowed_transitions = {'a': {(whi, wlo)}, 'b': {(wlo, whi), (whi, wlo)}}
    for c in CONTAINERS:
        sample_list = recs[c]['samples']
        for i, s in enumerate(sample_list):
            if s['kind'] != 'unstable':
                continue
            d = s.get('detail') or {}
            if d.get('reason') != 'weight_changed':
                return built, {'error': '%s unstable capture with no planned weight change: %r'
                               % (c, d), 'at_ns': s['wall_ns']}
            old, new = s['weight1'], s['weight2']
            before = next((x for x in reversed(sample_list[:i])
                           if x['kind'] == 'stable'), None)
            after = next((x for x in sample_list[i + 1:]
                          if x['kind'] == 'stable'), None)
            if before is None or after is None:
                return built, {'error': '%s unstable weight capture at timeline edge' % c,
                               'at_ns': s['wall_ns']}
            if before['weight1'] == after['weight1']:
                return built, {'error': '%s weight flipped inside one capture and reverted; '
                               'not a planned transition' % c, 'at_ns': s['wall_ns'],
                               'w1': old, 'w2': new}
            if (old, new) != (before['weight1'], after['weight1']):
                return built, {'error': '%s bracket reads %r->%r do not match surrounding '
                               'stable weights %r->%r'
                               % (c, old, new, before['weight1'], after['weight1']),
                               'at_ns': s['wall_ns']}
            if (old, new) not in allowed_transitions[c]:
                return built, {'error': '%s unstable capture %r->%r is not a planned reversal'
                               % (c, old, new), 'at_ns': s['wall_ns']}
            if not (before['cap_end_ns'] <= s['cap_end_ns']
                    and after['wall_ns'] >= s['wall_ns']):
                return built, {'error': '%s unstable capture is not bracketed in time' % c}
    built['whi'] = whi
    built['wlo'] = wlo
    return built, {}


# --------------------------------------------------------------------------- #
# Joint windows with capture-duration uncertainty intervals
# --------------------------------------------------------------------------- #

def _segment_intervals(segments):
    """Coverage intervals of stable-fast segments: [first_t0, last_t1]."""
    return [[seg['first_ns'], seg['last_t1']] for seg in segments]


def _union(intervals):
    intervals = sorted(([lo, hi] for lo, hi in intervals if hi > lo), key=lambda x: x[0])
    out = []
    for lo, hi in intervals:
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _intersect_unions(u1, u2):
    out = []
    i = j = 0
    while i < len(u1) and j < len(u2):
        lo = max(u1[i][0], u2[j][0])
        hi = min(u1[i][1], u2[j][1])
        if hi > lo:
            out.append([lo, hi])
        if u1[i][1] < u2[j][1]:
            i += 1
        else:
            j += 1
    return out


def _rate_interval(first, last):
    wide = (last['cap_end_ns'] - first['wall_ns']) / 1e9
    narrow = max(1e-9, (last['wall_ns'] - first['cap_end_ns']) / 1e9)
    dp = (last['proc_cpu_ns'] - first['proc_cpu_ns']) / 1e9
    ds = (last['usage_usec'] - first['usage_usec']) / 1e6
    return {'wide_s': wide, 'narrow_s': narrow,
            'proc_lo': dp / wide, 'proc_hi': dp / narrow,
            'stat_lo': ds / wide, 'stat_hi': ds / narrow,
            'first_ns': first['wall_ns'], 'last_t1': last['cap_end_ns'],
            'first_t1': first['cap_end_ns'], 'last_t0': last['wall_ns']}


def build_joint_windows(built, summaries, params):
    """For each phase, pair every stable-fast chain (each with optional slow
    capture anchors at its edges) of A with those of B and intersect coverages.
    Endpoint rate intervals spread over anchor capture spans, so slow valid
    reads propagate uncertainty instead of disappearing."""
    whi, wlo = built['whi'], built['wlo']
    trim_ns = int(float(params.get('start_trim_s', START_TRIM_S)) * 1e9)
    min_ns = int(float(params.get('min_window_s', MIN_WINDOW_S)) * 1e9)
    skew_ns = int(float(params.get('endpoint_skew_s', ENDPOINT_SKEW_S)) * 1e9)
    min_samples = int(params.get('min_samples', MIN_SAMPLES))
    # (A run index, B run index) per phase: A runs [hi0, lo1],
    # B runs [lo0, hi1, lo2]; A's low run spans B phases 1 and 2.
    phase_runs = [(0, 0), (1, 1), (1, 2)]
    phase_weights = [{'a': whi, 'b': wlo}, {'a': wlo, 'b': whi},
                     {'a': wlo, 'b': wlo}]

    phases = []
    for k, (run_a, run_b) in enumerate(phase_runs):
        wpair = phase_weights[k]
        segs = {c: [g for g in built[c]['segments']
                    if g['run_index'] == (run_a if c == 'a' else run_b)]
                for c in CONTAINERS}
        intervals, too_short = [], []
        joint_index = 0
        for ga in segs['a']:
            for gb in segs['b']:
                lo0 = max(ga['first_ns'], gb['first_ns'])
                hi0 = min(ga['last_t1'], gb['last_t1'])
                if hi0 <= lo0:
                    continue
                lo = lo0 + (trim_ns if k == 0 and joint_index == 0 else 0)
                joint_index += 1
                per = {}
                for c, g in (('a', ga), ('b', gb)):
                    fs = [s for s in g['samples']
                          if s['wall_ns'] >= lo and s['cap_end_ns'] <= hi0]
                    if len(fs) < 2:
                        per[c] = {'samples': len(fs)}
                        continue
                    ri = _rate_interval(fs[0], fs[-1])
                    ri['samples'] = len(fs)
                    ri['fast_samples'] = len(g['fast_samples'])
                    ri['slow_anchor_lo'] = bool(g.get('head'))
                    ri['slow_anchor_hi'] = bool(g.get('tail'))
                    per[c] = ri
                problems = []
                if any('wide_s' not in per[c] for c in CONTAINERS):
                    problems.append('under-sampled joint interval')
                else:
                    for c in CONTAINERS:
                        if per[c]['samples'] < min_samples:
                            problems.append('%s only %d captures' % (c, per[c]['samples']))
                        if per[c]['wide_s'] * 1e9 < min_ns:
                            problems.append('%s wide interval %.2fs < %.1fs'
                                            % (c, per[c]['wide_s'], min_ns / 1e9))
                    skew_lo = abs(per['a']['first_ns'] - per['b']['first_ns'])
                    skew_hi = abs(per['a']['last_t1'] - per['b']['last_t1'])
                    if max(skew_lo, skew_hi) > skew_ns:
                        problems.append('endpoint skew %.2fs > %.2fs'
                                        % (max(skew_lo, skew_hi) / 1e9, skew_ns / 1e9))
                entry = {'lo_ns': lo, 'hi_ns': hi0, 'containers': per}
                (too_short if problems else intervals).append(entry)
        # Joint run bounds as a union (a run weight can recur in disjoint
        # chains, e.g. B's light weight in stages 0 and 2).
        run_union = {c: _union(_segment_intervals(segs[c])) for c in CONTAINERS}
        bounds_union = _intersect_unions(run_union['a'], run_union['b'])
        # Quarantine intersecting any joint run bounds is an in-phase gap.
        q_inside = []
        run_span = [max(min(g['first_ns'] for g in segs[c]) for c in CONTAINERS),
                    min(max(g['last_t1'] for g in segs[c]) for c in CONTAINERS)]
        for c in CONTAINERS:
            for part in built[c]['quarantine']:
                qlo, qhi, kind = part['lo_ns'], part['hi_ns'], part['kind']
                if kind == 'weight_change':
                    continue   # planned weight seam, already outside scored runs
                if qhi > run_span[0] and qlo < run_span[1]:
                    q_inside.append({'container': c,
                                     'lo_ns': max(qlo, run_span[0]),
                                     'hi_ns': min(qhi, run_span[1]), 'kind': kind})
        # Measured intervals must span most of the joint run bounds; one clean
        # remnant after a long hole must not PASS.
        total_bounds = sum(hi - lo for lo, hi in bounds_union)
        measured_span = sum(e['hi_ns'] - e['lo_ns'] for e in intervals)
        coverage = (measured_span / total_bounds) if total_bounds > 0 else 0.0
        phases.append({'stage': k, 'expected_fraction_a':
                       wpair['a'] / (wpair['a'] + wpair['b']),
                       'weights': wpair, 'intervals': intervals,
                       'too_short': too_short, 'quarantine': q_inside,
                       'coverage_fraction': coverage,
                       'joint_bounds': bounds_union,
                       'run_span': run_span,
                       'slow_anchors': {c: [
                           {'lo_ns': s['t0_ns'], 'hi_ns': s['t1_ns'],
                            'dur_s': (s['t1_ns'] - s['t0_ns']) / 1e9}
                           for s in built[c]['slow_anchors']] for c in CONTAINERS}})
    return phases


def _interval_decision(interval, weights_pair, expected):
    """Three-valued verdict for one joint captured interval."""
    per = interval['containers']
    tol = EQUAL_TOL if weights_pair['a'] == weights_pair['b'] else FRAC_TOL
    ev = {'expected_fraction_a_from_weight': expected, 'tolerance': tol,
          'observed_weights': weights_pair, 'lo_ns': interval['lo_ns'],
          'hi_ns': interval['hi_ns']}
    ev['rates'] = {c: {k: per[c][k] for k in
                       ('proc_lo', 'proc_hi', 'stat_lo', 'stat_hi', 'wide_s', 'narrow_s',
                        'samples')} for c in CONTAINERS}

    def frac(fa, fb):
        return fa / (fa + fb) if fa + fb > 0 else 0.0

    # Allocation fraction extrema across every plausible per-container rate.
    fp = (frac(per['a']['proc_lo'], per['b']['proc_hi']),
          frac(per['a']['proc_hi'], per['b']['proc_lo']))
    fs = (frac(per['a']['stat_lo'], per['b']['stat_hi']),
          frac(per['a']['stat_hi'], per['b']['stat_lo']))
    f_lo, f_hi = min(fp[0], fs[0]), max(fp[1], fs[1])
    ev.update({'fraction_interval': [f_lo, f_hi],
               'fraction_a_proc_interval': list(fp), 'fraction_a_stat_interval': list(fs),
               'total_proc_interval': [per['a']['proc_lo'] + per['b']['proc_lo'],
                                       per['a']['proc_hi'] + per['b']['proc_hi']],
               'total_stat_interval': [per['a']['stat_lo'] + per['b']['stat_lo'],
                                       per['a']['stat_hi'] + per['b']['stat_hi']]})
    # 1. Independent counter agreement: the two plausibility INTERVALS must
    # come within tolerance (interval width itself is propagated uncertainty,
    # not disagreement).
    for c in CONTAINERS:
        gap = max(0.0, per[c]['stat_lo'] - per[c]['proc_hi'],
                  per[c]['proc_lo'] - per[c]['stat_hi'])
        if gap > max(AGREE_MIN, AGREE_REL):
            return _result(INDETERMINATE, 'container %s proc/stat rate intervals are '
                           'disjoint beyond tolerance' % c, **ev)
    frac_gap = max(0.0, fp[0] - fs[1], fs[0] - fp[1])
    if frac_gap > FRAC_AGREE:
        return _result(INDETERMINATE, 'proc and stat fraction intervals are disjoint', **ev)
    # 2. Saturation over the whole plausible box.
    t_lo = min(ev['total_proc_interval'][0], ev['total_stat_interval'][0])
    t_hi = max(ev['total_proc_interval'][1], ev['total_stat_interval'][1])
    if t_lo > SAT_HIGH:
        return _result(FAIL, 'even the smallest plausible total exceeds 1 core through '
                       'a one-CPU cpuset; counters contradictory', **ev)
    if t_hi < SAT_LOW:
        return _result(INDETERMINATE, 'combined load under-saturates the shared CPU under '
                       'every plausible capture duration', **ev)
    if t_lo < SAT_LOW or t_hi > SAT_HIGH:
        return _result(INDETERMINATE, 'saturation band only straddled once capture-duration '
                       'uncertainty is propagated', **ev)
    # 3. Starvation under every plausible value -> FAIL; straddled -> INDET.
    for c in CONTAINERS:
        lo_rate = min(per[c]['proc_lo'], per[c]['stat_lo'])
        hi_rate = max(per[c]['proc_hi'], per[c]['stat_hi'])
        lo_frac = f_lo if c == 'a' else 1 - f_hi
        hi_frac = f_hi if c == 'a' else 1 - f_lo
        if hi_rate < STARVATION_CORES or hi_frac < STARVATION_FRAC:
            return _result(FAIL, 'container %s starved under every plausible value' % c, **ev)
        if lo_rate < STARVATION_CORES or lo_frac < STARVATION_FRAC:
            return _result(INDETERMINATE, 'starvation threshold straddled by capture '
                           'uncertainty for container %s' % c, **ev)
    # 4. Functional band: all plausible valid -> PASS; all invalid -> FAIL.
    if f_hi < expected - tol or f_lo > expected + tol:
        return _result(FAIL, 'all plausible allocations contradict the observed weight '
                       'ratio band', observed_band=(f_lo, f_hi), **ev)
    if f_lo < expected - tol or f_hi > expected + tol:
        return _result(INDETERMINATE, 'capture-duration uncertainty straddles the weight '
                       'band edge; cannot pass or prove failure',
                       observed_band=(f_lo, f_hi), **ev)
    return _result(PASS, 'every plausible allocation follows the observed cpu.weight ratio', **ev)


def classify_phase(phase):
    # Any read error, unstable straddle or unobserved coverage gap INSIDE the
    # run bounds leaves the phase doubtfully attributed: indeterminate, never
    # passed around. Planned-transition seams sit between run weights and are
    # by construction outside these bounds.
    inner_gaps = [q for q in phase.get('quarantine', [])
                  if any(k in q['kind'] for k in
                         ('error', 'unstable', 'coverage_gap', 'weight_change'))]
    if phase['intervals'] and inner_gaps:
        return _result(INDETERMINATE,
                       'observational gap inside the phase bounds '
                       '(read error/unstable capture/coverage hole)',
                       gaps=inner_gaps, weights=phase['weights'],
                       measured_intervals=len(phase['intervals']))
    if phase['intervals']:
        verdicts = [_interval_decision(i, phase['weights'], phase['expected_fraction_a'])
                    for i in phase['intervals']]
        statuses = [v['status'] for v in verdicts]
        coverage = phase.get('coverage_fraction', 1.0)
        coverage_note = {'measured_intervals': len(verdicts),
                         'coverage_fraction': round(coverage, 4),
                         'min_coverage': MIN_PHASE_COVERAGE,
                         'too_short_intervals': len(phase['too_short']),
                         'quarantine': phase['quarantine'],
                         'slow_anchors': phase.get('slow_anchors', {})}
        if coverage < MIN_PHASE_COVERAGE:
            return _result(INDETERMINATE,
                           'only %.0f%% of the joint run bounds is measurable after '
                           'quarantine/slow-capture exclusion' % (coverage * 100),
                           weights=phase['weights'], **coverage_note)
        if FAIL in statuses:
            r = next(v for v in verdicts if v['status'] == FAIL)
            r['evidence'].update(coverage_note)
            return r
        if INDETERMINATE in statuses:
            r = next(v for v in verdicts if v['status'] == INDETERMINATE)
            r['evidence'].update(coverage_note)
            return r
        base = verdicts[0]
        base['evidence'].update(coverage_note)
        return base
    reason = 'no joint interval survived quarantine and minimum-window requirements'
    if phase['too_short']:
        reason += '; %d interval(s) too short' % len(phase['too_short'])
    return _result(INDETERMINATE, reason, weights=phase['weights'],
                   too_short=phase['too_short'], quarantine=phase['quarantine'],
                   joint_bounds=phase.get('joint_bounds'))


def classify_ordering(phase_results, phase_data):
    if any(r['status'] != PASS for r in phase_results):
        return _result(NOT_TESTED, 'ordering needs all three phase PASS verdicts')
    ext = []
    for r in phase_results:
        lo, hi = r['evidence']['fraction_interval']
        ext.append((lo, hi))
    (p0_lo, p0_hi), (p1_lo, p1_hi), (eq_lo, eq_hi) = ext
    ev = {'intervals': {'2to1': ext[0], 'reversed': ext[1], 'equal': ext[2]},
          'required_margin': ORDER_MARGIN}
    if p0_lo > eq_hi + ORDER_MARGIN and eq_lo > p1_hi + ORDER_MARGIN:
        return _result(PASS, 'all plausible allocations reverse order with the live '
                       'weight reversal and separate at equal weight', **ev)
    if p0_hi + ORDER_MARGIN <= p1_lo:
        return _result(FAIL, 'the weight reversal did not reverse allocation under any '
                       'plausible value', **ev)
    return _result(INDETERMINATE, 'capture uncertainty makes the ordering intervals overlap', **ev)


# --------------------------------------------------------------------------- #
# Configuration / identity / protocol ledgers (v2 fail-closed, retained)
# --------------------------------------------------------------------------- #

def classify_weight_mapping(containers, built, summaries, shared_cpu, daemon, params):
    mapping = {}
    pairs = []
    for c in CONTAINERS:
        data = containers.get(c) or {}
        snaps = data.get('stage_snapshots') or []
        insps = [r for r in (data.get('inspections') or []) if r.get('stage') in (0, 1, 2)]
        if len(snaps) != STAGES or len(insps) != STAGES:
            return _result(FAIL, 'need one stage snapshot and inspect per container per stage',
                           container=c, snapshots=len(snaps), inspections=len(insps))
        for k in range(STAGES):
            snap, insp = snaps[k], insps[k]
            shares = insp.get('shares')
            if shares != PLAN[k][c]:
                return _result(FAIL, 'inspect CpuShares does not match plan',
                               container=c, stage=k, observed=shares, expected=PLAN[k][c])
            if str(insp.get('cpuset')) != str(shared_cpu):
                return _result(FAIL, 'inspect cpuset drifted off the shared CPU',
                               container=c, stage=k, observed=insp.get('cpuset'))
            if insp.get('quota') not in (0, None):
                return _result(FAIL, 'CpuQuota set; it would dominate --cpu-shares',
                               container=c, stage=k, quota=insp.get('quota'))
            if insp.get('running') is not True:
                return _result(FAIL, 'container not running at stage inspect',
                               container=c, stage=k)
            pairs.append({'container': c, 'stage': k, 'shares': shares,
                          'observed_weight': snap['weight'], 'snapshot_ns': snap['wall_ns']})
            old = mapping.setdefault(int(shares), int(snap['weight']))
            if old != snap['weight']:
                return _result(FAIL, 'same --cpu-shares maps to different cpu.weight values',
                               shares=shares, weights=(old, snap['weight']),
                               container=c, stage=k)
    if set(mapping) != {512, 1024}:
        return _result(FAIL, 'mapping must cover exactly shares 512 and 1024', mapping=mapping)
    if mapping[1024] <= mapping[512]:
        return _result(FAIL, 'mapping is not monotonic for 512 vs 1024', mapping=mapping)
    if built:
        if built.get('whi') != mapping[1024] or built.get('wlo') != mapping[512]:
            return _result(FAIL, 'continuous timeline weights disagree with stage snapshots',
                           timeline={'high': built.get('whi'), 'low': built.get('wlo')},
                           mapping=mapping)
    # Endpoint weights only assert when the endpoint capture itself was stable.
    for c, stage0_weight, stage2_weight, stable0, stable2 in (
            ('a', mapping[1024], mapping[512],
             summaries['a']['start_weight_stable'], summaries['a']['end_weight_stable']),
            ('b', mapping[512], mapping[512],
             summaries['b']['start_weight_stable'], summaries['b']['end_weight_stable'])):
        if stable0 and summaries[c]['weight_start'] != stage0_weight:
            return _result(FAIL, 'stable start capture disagrees with planned weight',
                           container=c, observed=summaries[c]['weight_start'],
                           expected=stage0_weight)
        if stable2 and summaries[c]['weight_end'] != stage2_weight:
            return _result(FAIL, 'stable end capture disagrees with planned weight',
                           container=c, observed=summaries[c]['weight_end'],
                           expected=stage2_weight)
    computed = {512: computed_weight(512), 1024: computed_weight(1024)}
    return _result(PASS,
                   'observed shares->weight mapping consistent; fractions derive from '
                   'observed weights; pinned-formula values are computed labels only',
                   mapping={str(k): v for k, v in sorted(mapping.items())}, pairs=pairs,
                   computed_weights_pending_runtime={str(k): v for k, v in computed.items()},
                   observed_matches_pinned_formula=mapping == computed,
                   computed_source=PINNED_MAPPING_SOURCE,
                   server_version=daemon.get('server_version'),
                   cgroup_version=daemon.get('cgroup_version'))


def classify_capture_protocol(built, summaries, containers, updates, params, schema=4):
    if not built or 'whi' not in built:
        return _result(NOT_TESTED, 'depends on valid timelines')
    detail = {'containers': {}}
    bracket_bound_ns = int(float(params.get('bracket_skew_s', BRACKET_SKEW_S)) * 1e9)
    sample_ns = int((params.get('sample_ms') or 100) * 1e6)
    bound = bracket_bound_ns + sample_ns
    problems = []
    bracket_rows = []
    by_key = {(u.get('stage'), u.get('container')): u for u in (updates or [])}
    expected_dir = {(1, 'a'): (built['whi'], built['wlo']),
                    (1, 'b'): (built['wlo'], built['whi']),
                    (2, 'b'): (built['whi'], built['wlo'])}
    for (stage, c), (old_w, new_w) in expected_dir.items():
        u = by_key.get((stage, c))
        if not isinstance(u, dict):
            problems.append('missing update record %s stage %d' % (c, stage))
            continue
        pre, post = u.get('pre'), u.get('post')
        if not isinstance(pre, dict) or not isinstance(post, dict):
            problems.append('update %s stage %d lacks pre/post brackets' % (c, stage))
            continue
        if pre.get('weight') != old_w or post.get('weight') != new_w:
            problems.append('bracket weights %r->%r do not match %r->%r: %s stage %d'
                            % (pre.get('weight'), post.get('weight'), old_w, new_w, c, stage))
        if not isinstance(pre.get('wall_ns'), int) or not isinstance(post.get('wall_ns'), int) \
                or pre['wall_ns'] >= post['wall_ns']:
            problems.append('bracket timestamps missing or out of order: %s stage %d' % (c, stage))
        bracket_rows.append({'stage': stage, 'container': c,
                             'pre_ns': pre.get('wall_ns'), 'pre_weight': pre.get('weight'),
                             'post_ns': post.get('wall_ns'), 'post_weight': post.get('weight')})
    for c in CONTAINERS:
        rec = built[c]
        slow = [{'t0_ns': s['wall_ns'], 't1_ns': s['cap_end_ns'],
                 'dur_s': (s['cap_end_ns'] - s['wall_ns']) / 1e9,
                 'weight_stable': s['weight_stable'], 'share_valid': s['share_valid']}
                for s in rec['samples'] if s['slow']]
        errors = [{'t0_ns': s['wall_ns'], 't1_ns': s['cap_end_ns'],
                   'detail': s.get('detail'), 'weight1_errno': s['weight1_errno'],
                   'weight2_errno': s['weight2_errno'], 'stat_errno': s['stat_errno']}
                  for s in rec['samples'] if s['kind'] == 'error']
        unstable = [{'t0_ns': s['wall_ns'], 't1_ns': s['cap_end_ns'],
                     'w1': s['weight1'], 'w2': s['weight2']}
                    for s in rec['samples'] if s['kind'] == 'unstable']
        detail['containers'][c] = {
            'segments': [{'weight': g['weight'],
                          'fast_samples': len(g['fast_samples']),
                          'slow_head': g['head'] is not None,
                          'slow_tail': g['tail'] is not None,
                          'first_ns': g['first_ns'], 'last_t1': g['last_t1']}
                         for g in rec['segments']],
            'quarantine': rec['quarantine'], 'slow_captures': slow,
            'error_captures': errors, 'unstable_captures': unstable,
            'max_capture_s': max((s['cap_end_ns'] - s['wall_ns']) / 1e9
                                 for s in rec['samples']),
            'error_count': len(errors)}
        if errors and all(len(g['fast_samples']) < 2 for g in rec['segments']):
            problems.append('%s has read errors and no usable stable segment' % c)
    detail['brackets'] = bracket_rows
    # Completion-mode integrity: both containers must finish the same way.
    modes = {c: ('graceful' if summaries[c].get('graceful') else 'hard_deadline')
             for c in CONTAINERS}
    detail['completion_modes'] = modes
    if modes['a'] != modes['b']:
        problems.append('mixed completion modes %r: one graceful stop, one hard deadline'
                        % modes)
    if schema >= 4 and modes['a'] != 'graceful':
        # Schema-4 protocol promises bounded explicit graceful completion; a
        # hard-ceiling exit means the required final window was not provided
        # by the runner (or a stop was lost), regardless of residual windows.
        problems.append('schema-4 run ended on the hard duration ceiling '
                        'instead of graceful completion: %r' % modes)
    if modes['a'] == 'graceful':
        for c in CONTAINERS:
            lat = (summaries[c]['samples'][-1]['cap_end_ns'] - summaries[c]['stop_ns']) \
                if summaries[c]['samples'] else None
            detail['containers'][c]['graceful'] = {
                'stop_ns': summaries[c]['stop_ns'],
                'last_sample_cap_end_ns': summaries[c]['samples'][-1]['cap_end_ns']
                if summaries[c]['samples'] else None,
                'flush_latency_s': (lat / 1e9) if lat is not None else None}
        # The stable joint run must actually have >= MIN_WINDOW_S before the
        # earlier stop signal; phases themselves enforce this, but record it.
    if problems:
        return _result(FAIL, 'capture protocol violated: ' + '; '.join(problems), **detail)
    mode_note = ('graceful SIGTERM completion after final stable phase'
                 if modes['a'] == 'graceful' else
                 'hard-deadline completion (legacy v3 protocol)')
    return _result(PASS,
                   'all updates bracketed; unstable/error/slow captures quarantined; '
                   'rate deltas span only stable-fast joint intervals; ' + mode_note, **detail)


def classify_residency(summaries, shared_cpu):
    detail = {'shared_cpu': shared_cpu, 'containers': {}}
    for c in CONTAINERS:
        s = summaries[c]
        seen = sorted({cpu for w in s['workers'] for cpu in w['cpus_seen']})
        affs = sorted({tuple(w[k]) for w in s['workers']
                       for k in ('affinity_start', 'affinity_end')})
        detail['containers'][c] = {'residency_union': seen,
                                   'affinity_masks': [list(a) for a in affs]}
        if seen != [shared_cpu] or [list(a) for a in affs] != [[shared_cpu]]:
            return _result(FAIL, 'container %s residency/affinity escaped CPU %d'
                           % (c, shared_cpu), **detail)
    return _result(PASS, 'every busy thread of both containers stayed on the shared CPU', **detail)


def classify_whole_run_accounting(summaries):
    rows = {}
    for c in CONTAINERS:
        s = summaries[c]
        rates = s['rates']
        spread = max(rates.values()) - min(rates.values())
        tol = max(AGREE_MIN, WHOLE_RUN_AGREE)
        rows[c] = {'rates': rates, 'spread': spread, 'tolerance': tol,
                   'sampler_cpu_cores': s['sampler_ns'] / 1e9 / s['wall_s'],
                   'thread_basis': 'workers_plus_sampler',
                   'capture_interval_s': {'narrow': s['narrow_s'], 'wide': s['wide_s']}}
        if spread > tol:
            return _result(INDETERMINATE,
                           'container %s whole-run thread/proc/stat counters disagree even '
                           'over the widest bracketed interval' % c, containers=rows)
        if min(rates.values()) < STARVATION_CORES:
            return _result(FAIL, 'container %s consumed almost no CPU over the whole run' % c,
                           containers=rows)
    return _result(PASS, 'per-thread(+sampler), process and cgroup counters agree; '
                   'bracketed start/end intervals recorded', containers=rows)


def classify_identity(containers):
    rows = {}
    for c in CONTAINERS:
        data = containers.get(c) or {}
        created = data.get('created_id')
        insps = [r for r in (data.get('inspections') or []) if isinstance(r, dict)]
        if not created or not insps:
            return _result(FAIL, 'container %s lacks identity evidence' % c)
        ids = {created} | {r.get('id') for r in insps}
        if len(ids) != 1 or None in ids:
            return _result(FAIL, 'container %s changed identity across live updates' % c,
                           created_id=created, inspect_ids=[r.get('id') for r in insps])
        final = next((r for r in insps if r.get('stage') == STAGES), None)
        if final is None:
            return _result(FAIL, 'container %s lacks post-wait final inspect' % c)
        if final.get('running') or final.get('exit_code') != 0 or str(data.get('probe_exit')) != '0':
            return _result(FAIL, 'container %s did not finish cleanly' % c,
                           running=final.get('running'), exit_code=final.get('exit_code'),
                           probe_exit=data.get('probe_exit'))
        rows[c] = {'id': created, 'wait_exit': data.get('wait_exit'),
                   'probe_exit': data.get('probe_exit')}
    if rows['a']['id'] == rows['b']['id']:
        return _result(FAIL, 'the two contention containers share one ID', containers=rows)
    return _result(PASS, 'one identity per container through every live update; exits 0',
                   containers=rows)


def classify_update_ledger(updates):
    seen, malformed = {}, []
    for u in updates or []:
        if not isinstance(u, dict):
            malformed.append(repr(u)); continue
        key = (u.get('stage'), u.get('container'))
        if key in seen:
            malformed.append('duplicate %r' % (key,))
        seen[key] = u
        if not isinstance(u.get('exit_code'), int) or isinstance(u.get('exit_code'), bool):
            malformed.append('exit_code %r' % (u.get('exit_code'),))
        if not isinstance(u.get('timed_out'), bool):
            malformed.append('timed_out %r' % (u.get('timed_out'),))
    if malformed:
        return _result(FAIL, 'malformed update records: ' + '; '.join(malformed), updates=updates or [])
    missing, mismatch, failed = [], [], []
    for stage, c, old, new in PLANNED_UPDATES:
        u = seen.get((stage, c))
        if u is None:
            missing.append({'stage': stage, 'container': c, 'from': old, 'to': new}); continue
        if u.get('from_shares') != old or u.get('to_shares') != new:
            mismatch.append({'recorded': (u.get('stage'), u.get('container'),
                                          u.get('from_shares'), u.get('to_shares')),
                             'expected': (stage, c, old, new)})
        elif u['exit_code'] != 0 or u['timed_out']:
            failed.append({'stage': stage, 'container': c, 'exit_code': u['exit_code'],
                           'timed_out': u['timed_out']})
    known = {(s, c) for s, c, _, _ in PLANNED_UPDATES}
    extra = sorted(str(k) for k in seen if k not in known)
    if missing:
        return _result(FAIL, 'planned live updates were never issued', missing=missing)
    if mismatch:
        return _result(FAIL, 'update record does not match the canonical plan', mismatch=mismatch)
    if extra:
        return _result(FAIL, 'unknown update records', extra=extra)
    if failed:
        return _result(FAIL, 'live update command failed or timed out', failed=failed)
    return _result(PASS, 'all canonical live weight updates issued successfully',
                   updates=[{'stage': s, 'container': c, 'from': o, 'to': n}
                            for s, c, o, n in PLANNED_UPDATES])


def classify_command_ledger(commands, required=MANDATORY_LABELS):
    required = frozenset(required)
    if not isinstance(commands, list) or not commands:
        return _result(FAIL, 'command ledger is absent or empty')
    seen, malformed, unknown, duplicates = {}, [], [], []
    for row in commands:
        if not isinstance(row, dict):
            malformed.append('row is not an object'); continue
        label = row.get('label')
        if not isinstance(label, str) or not label:
            malformed.append('label %r' % (label,)); continue
        is_poll = label.startswith(OPTIONAL_PREFIXES)
        if label not in required and label not in OPTIONAL_LABELS and not is_poll:
            unknown.append(label); continue
        if not isinstance(row.get('exit_code'), int) or isinstance(row.get('exit_code'), bool):
            malformed.append('%s: exit_code %r' % (label, row.get('exit_code')))
        if not isinstance(row.get('timed_out'), bool):
            malformed.append('%s: timed_out %r' % (label, row.get('timed_out')))
        audit = row.get('cleanup')
        if not isinstance(audit, dict) or not isinstance(audit.get('clean_shutdown'), bool):
            malformed.append('%s: cleanup audit missing' % label)
        if label in seen:
            if not is_poll:
                duplicates.append(label)
        seen.setdefault(label, []).append(row)
    problems = []
    if malformed:
        problems.append('malformed: ' + '; '.join(sorted(set(malformed))))
    if unknown:
        problems.append('unknown labels: ' + ', '.join(sorted(set(unknown))))
    if duplicates:
        problems.append('duplicate labels: ' + ', '.join(sorted(set(duplicates))))
    missing = sorted(required - set(seen))
    if missing:
        problems.append('missing mandatory labels: ' + ', '.join(missing))
    failed = []
    for label in sorted(required & set(seen)):
        for row in seen[label]:
            if row['exit_code'] != 0 or row['timed_out'] or not row['cleanup']['clean_shutdown']:
                failed.append({'label': label, 'exit_code': row['exit_code'],
                               'timed_out': row['timed_out'],
                               'remaining_live_pids': row['cleanup'].get('remaining_live_pids'),
                               'remaining_zombie_pids': row['cleanup'].get('remaining_zombie_pids')})
    auditable = OPTIONAL_LABELS | {l for l in seen if l.startswith(OPTIONAL_PREFIXES)}
    for label in sorted(auditable & set(seen)):
        for row in seen[label]:
            if row['timed_out'] or not row['cleanup']['clean_shutdown']:
                failed.append({'label': label, 'optional_audit_failed': True,
                               'timed_out': row['timed_out']})
    if problems or failed:
        return _result(FAIL, 'command ledger fails closed: ' + '; '.join(problems),
                       failed=failed, executed_count=len(commands))
    return _result(PASS, 'every checker-mandated command exited 0 exactly once with clean groups',
                   mandatory_count=len(required), executed_count=len(commands))


def classify_inputs(inputs):
    if not isinstance(inputs, list) or not inputs:
        return _result(FAIL, 'input provenance records are absent')
    by_role, malformed = {}, []
    for row in inputs:
        if not isinstance(row, dict):
            malformed.append('row not object'); continue
        role = row.get('role')
        if role in by_role:
            malformed.append('duplicate role %r' % (role,))
        if not isinstance(role, str) or not role:
            malformed.append('role %r' % (role,))
        if not isinstance(row.get('path'), str) or not row['path']:
            malformed.append('%s: path' % role)
        for key in ('sha256_before', 'sha256_after'):
            if not isinstance(row.get(key), str) or not SHA256_RE.fullmatch(row[key]):
                malformed.append('%s: %s invalid/empty' % (role, key))
        by_role[role] = row
    problems = list(malformed)
    unknown = sorted(set(by_role) - set(INPUT_ROLES))
    missing = sorted(set(INPUT_ROLES) - set(by_role))
    if unknown:
        problems.append('unknown roles: ' + ', '.join(unknown))
    if missing:
        problems.append('missing roles: ' + ', '.join(missing))
    changed = [r for r in INPUT_ROLES if r in by_role
               and by_role[r]['sha256_before'] != by_role[r]['sha256_after']]
    if changed:
        problems.append('changed during run: ' + ', '.join(changed))
    if problems:
        return _result(FAIL, 'input provenance fails closed: ' + '; '.join(problems),
                       roles=sorted(by_role))
    return _result(PASS, 'all mandatory inputs hashed non-empty before and after, unchanged',
                   inputs={r: by_role[r]['path'] for r in INPUT_ROLES})


def classify_leftovers(value):
    if not isinstance(value, str):
        return _result(FAIL, 'post-cleanup container inventory was never collected')
    found = [line for line in value.splitlines() if line.strip()]
    if found:
        return _result(FAIL, 'experiment containers remain after cleanup', leftover_ids=found)
    return _result(PASS, 'executed leftovers inventory is empty')


def classify_lifecycle(final_state):
    if not isinstance(final_state, dict) or not final_state:
        return _result(FAIL, 'guest instance final state is missing')
    ok = (final_state.get('phase') == 'stopped'
          and not final_state.get('forced_shutdown')
          and (final_state.get('guest_cleanup') or {}).get('clean_shutdown')
          and (final_state.get('network_cleanup') or {}).get('clean_shutdown'))
    if not ok:
        return _result(FAIL, 'guest lifecycle was not clean',
                       phase=final_state.get('phase'),
                       forced_shutdown=final_state.get('forced_shutdown'),
                       error=final_state.get('error'))
    return _result(PASS, 'guest stopped cleanly without forced shutdown',
                   phase=final_state.get('phase'))


def classify_topology(daemon, shared_cpu):
    if not isinstance(daemon, dict) or daemon.get('ncpu') != 4:
        return _result(FAIL, 'protocol requires a 4-vCPU guest; daemon evidence %r'
                       % (daemon.get('ncpu') if isinstance(daemon, dict) else None))
    if str(daemon.get('cgroup_version')) != '2':
        return _result(FAIL, 'cpu.weight protocol needs cgroup v2',
                       cgroup_version=daemon.get('cgroup_version'))
    if not isinstance(shared_cpu, int) or not 0 <= shared_cpu < 4:
        return _result(FAIL, 'shared cpu index invalid: %r' % (shared_cpu,))
    return _result(PASS, '4-vCPU cgroup-v2 guest; contention pinned to one shared CPU',
                   ncpu=4, shared_cpu=shared_cpu, server_version=daemon.get('server_version'))


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as error:
        return _result(FAIL, '%s: %s' % (type(error).__name__, error))


def evaluate(evidence):
    if not isinstance(evidence, dict):
        return {name: _result(FAIL, 'evidence is not an object') for name in REQUIRED_CHECK_NAMES}
    # PURITY: evaluate is a pure function. It must never mutate the caller's
    # evidence (the v6 defect: normalized pre/post snapshots and an added
    # stage_snapshots key were written back, so a serialized report would not
    # re-evaluate to its own verdict). All mutable working structures are
    # private deep copies; the input is byte-identical before and after.
    def _private(value):
        # Evidence is JSON-native; round-trip gives a deep private copy.
        return json.loads(json.dumps(value))
    params = _private(evidence.get('params')) if isinstance(evidence.get('params'), dict) else {}
    shared_cpu = params.get('shared_cpu')
    daemon = _private(evidence.get('daemon')) if isinstance(evidence.get('daemon'), dict) else {}
    containers = (_private(evidence.get('containers'))
                  if isinstance(evidence.get('containers'), dict) else {})
    updates = _private(evidence.get('updates')) if isinstance(evidence.get('updates'), list) else []
    # Protocol generation: v3 had no explicit graceful stop; v4 requires it.
    schema = int(evidence.get('schema_version', 4) or 4)
    checks = {}

    checks['guest_topology'] = _safe(classify_topology, daemon, shared_cpu)
    checks['container_identity'] = _safe(classify_identity, containers)

    summaries, snapshots, errors = {}, {}, []
    cpu0 = shared_cpu if isinstance(shared_cpu, int) else 0
    for c in CONTAINERS:
        data = containers.get(c) if isinstance(containers.get(c), dict) else {}
        try:
            if data.get('spin_error'):
                raise ValueError(data['spin_error'])
            summaries[c] = validate_contend_stream(data.get('spin_events') or [], cpu0)
        except Exception as error:
            errors.append('%s stream: %s: %s' % (c, type(error).__name__, error))
        try:
            raw_snaps = data.get('snapshots') or []
            if len(raw_snaps) != STAGES:
                raise ValueError('need %d stage snapshots, got %d' % (STAGES, len(raw_snaps)))
            normalized = [validate_stage_snapshot(e, cpu0, '%s stage %d' % (c, k))
                          for k, e in enumerate(raw_snaps)]
            times = [s['wall_ns'] for s in normalized]
            if any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError('stage snapshot timestamps not strictly ordered')
            if c in summaries and not (summaries[c]['start']['wall_ns'] <= times[0]
                                       and times[-1] <= summaries[c]['end']['cap_end_ns']):
                raise ValueError('stage snapshots fall outside the bracketed spin interval')
            snapshots[c] = normalized
        except Exception as error:
            errors.append('%s snapshots: %s: %s' % (c, type(error).__name__, error))
        try:
            for u in updates:
                if not isinstance(u, dict) or u.get('container') != c:
                    continue
                for side in ('pre', 'post'):
                    if u.get(side) is not None:
                        u[side] = validate_stage_snapshot(u[side], cpu0,
                                                          '%s %s bracket stage %s'
                                                          % (c, side, u.get('stage')))
        except Exception as error:
            errors.append('%s update brackets: %s: %s' % (c, type(error).__name__, error))

    for c in CONTAINERS:
        if c in snapshots and isinstance(containers.get(c), dict):
            containers[c]['stage_snapshots'] = snapshots[c]

    observed = any((containers.get(c) or {}).get('spin_events')
                   or (containers.get(c) or {}).get('spin_error')
                   or (containers.get(c) or {}).get('snapshots') for c in CONTAINERS)
    if errors:
        status = INDETERMINATE if not observed else FAIL
        checks['probe_streams'] = _result(status,
            'probe/snapshot evidence missing or invalid: ' + '; '.join(errors),
            samples={c: len(summaries[c]['samples']) for c in summaries})
    else:
        checks['probe_streams'] = _result(
            PASS, 'both contend streams complete with bracketed weight/cpuset captures',
            samples={c: len(summaries[c]['samples']) for c in CONTAINERS},
            max_capture_s={c: max((s['cap_end_ns'] - s['wall_ns']) / 1e9
                                  for s in summaries[c]['samples']) for c in CONTAINERS},
            wall_s={c: round(summaries[c]['wall_s'], 3) for c in CONTAINERS})

    built = None
    timeline_built = None
    phase_data = []
    phase_results = []
    timeline_err = None
    if len(summaries) == 2:
        try:
            timeline_built, timeline_err = validate_timeline(summaries, params)
        except Exception as error:
            timeline_built, timeline_err = None, {'error': '%s: %s'
                                                  % (type(error).__name__, error)}
        if not timeline_err and isinstance(timeline_built, dict) and 'whi' in timeline_built:
            built = timeline_built
            try:
                phase_data = build_joint_windows(built, summaries, params)
            except Exception as error:
                phase_data = []
                timeline_err = {'error': 'joint window construction: %s: %s'
                                % (type(error).__name__, error)}
            if len(phase_data) == STAGES and not timeline_err:
                phase_results = [_safe(classify_phase, p) for p in phase_data]
            else:
                phase_results = [_result(FAIL, 'joint window construction failed')] * STAGES
        else:
            phase_results = [_result(NOT_TESTED, 'no valid joint weight timeline')] * STAGES
    for i, label in enumerate(PHASE_LABELS):
        checks[label] = phase_results[i] if phase_results else \
            _result(NOT_TESTED, 'no valid joint weight timeline')

    checks['phase_ordering'] = (
        _safe(classify_ordering, phase_results, phase_data) if built else
        _result(NOT_TESTED, 'depends on phase windows'))
    checks['weight_mapping'] = (
        _safe(classify_weight_mapping, containers, built, summaries, cpu0, daemon, params)
        if built and len(summaries) == 2 else
        _result(NOT_TESTED, 'depends on valid timelines'))

    def _timeline_rows():
        if not built:
            return None
        return {c: {'weights': built[c]['weights'],
                    'segments': [{'weight': g['weight'], 'samples': len(g['samples'])}
                                 for g in built[c]['segments']],
                    'quarantine': built[c]['quarantine']} for c in CONTAINERS}
    if timeline_err:
        checks['weight_timeline'] = _result(FAIL, timeline_err['error'],
                                            rows=_timeline_rows()
                                            if isinstance(timeline_built, dict) else None)
    elif built:
        checks['weight_timeline'] = _result(
            PASS, 'stable weight runs match A:[high,low] B:[low,high,low]; '
            'only planned transitions straddle captures', rows=_timeline_rows(),
            weight_high=built['whi'], weight_low=built['wlo'])
    else:
        checks['weight_timeline'] = _result(NOT_TESTED, 'depends on valid timelines')
    checks['capture_protocol'] = (
        _safe(classify_capture_protocol, built, summaries, containers, updates, params, schema)
        if built else _result(NOT_TESTED, 'depends on valid timelines'))
    checks['residency_affinity'] = (
        _safe(classify_residency, summaries, cpu0) if len(summaries) == 2
        else _result(NOT_TESTED, 'depends on probe streams'))
    checks['whole_run_accounting'] = (
        _safe(classify_whole_run_accounting, summaries) if len(summaries) == 2
        else _result(NOT_TESTED, 'depends on probe streams'))

    required_labels = MANDATORY_LABELS if schema >= 4 else MANDATORY_LABELS_V3
    checks['update_ledger'] = _safe(classify_update_ledger, updates)
    checks['command_ledger'] = _safe(
        classify_command_ledger,
        evidence.get('commands') if isinstance(evidence.get('commands'), list) else None,
        required_labels)
    checks['input_provenance'] = _safe(
        classify_inputs,
        evidence.get('inputs') if isinstance(evidence.get('inputs'), list) else None)
    checks['leftovers'] = _safe(classify_leftovers, evidence.get('leftovers_stdout'))
    checks['lifecycle'] = _safe(classify_lifecycle, evidence.get('final_state'))
    return checks


def overall_verdict(checks, errors=(), required=REQUIRED_CHECK_NAMES):
    if not isinstance(checks, dict):
        return FAIL
    statuses = [checks.get(name, {}).get('status') if isinstance(checks.get(name), dict) else None
                for name in required]
    missing = [name for name in required if not isinstance(checks.get(name), dict)
               or checks[name].get('status') is None]
    if FAIL in statuses or missing or errors:
        return FAIL
    if INDETERMINATE in statuses:
        return INDETERMINATE
    if NOT_TESTED in statuses:
        return NOT_TESTED
    return PASS
