#!/usr/bin/env python3
"""host_correlate v3 - round12 read-only host-side correlator for the guest
cpu_capture_witness. Supersedes frozen v2 (strict fail-open review fixes):
both start+end bracketed syncs required; strict witness schema/summary/count;
strict positive-integer PIDs. v2 ancestry: supersedes rejected-v1 (correct
status-mask parsing incl. ShdPnd;
id@starttime entity identity with PID-reuse/regression rejection on UML
threads and runners; bracketed clock-offset intervals with drift consistency
and explicit unavailable state; full-bracket coverage requirements;
per-entity acquisition intervals and generation checks; schedstat
availability evidence; strict CLI bounds and fail-closed reporting.

  sample --uml-pid PID --out DIR [--duration-s S] [--interval-ms N]
  analyze --samples DIR --witness FILE --out DIR

Read-only /proc access. No privileges, ftrace, signals, cgroup or affinity
changes. Self-bounded; writes only its output directory.
"""
import argparse
import datetime
import json
import math
import os
import pathlib
import signal
import sys
import time

STATE, UTIME, STIME, STARTTIME, PROCESSOR = 3, 14, 15, 22, 39


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested with fixtures)
# ---------------------------------------------------------------------------

def parse_stat_text(data):
    """Parse /proc/<pid>[/task/<tid>]/stat into 1-based proc(5) fields."""
    if not data:
        return None
    lp = data.find('(')
    rp = data.rfind(')')
    if lp < 0 or rp < 0 or rp <= lp:
        return None
    fields = data[rp + 2:].split()
    out = {1: int(data[:lp].strip()), 2: data[lp + 1:rp]}
    for i, v in enumerate(fields, start=3):
        try:
            out[i] = int(v)
        except ValueError:
            out[i] = v
    return out


def parse_status_masks(data):
    """SigBlk/SigIgn/SigCgt/SigPnd/ShdPnd hex masks from /proc/<tid>/status."""
    masks = {}
    wanted = {'sigblk': 'SigBlk:', 'sigpnd': 'SigPnd:', 'shdpnd': 'ShdPnd:'}
    for line in (data or '').splitlines():
        for key, prefix in wanted.items():
            if line.startswith(prefix):
                parts = line.split()
                if len(parts) >= 2:
                    masks[key] = parts[1]
    return masks


def parse_schedstat_text(data):
    if not data:
        return None
    parts = data.split()
    if len(parts) < 3:
        return None
    try:
        return {'exec_ns': int(parts[0]), 'wait_ns': int(parts[1]),
                'switches': int(parts[2])}
    except ValueError:
        return None


def entity_key(ident, starttime):
    return '%d@%d' % (ident, starttime)


# ---------------------------------------------------------------------------
# /proc IO (read-only)
# ---------------------------------------------------------------------------

def read_text(path, limit=None):
    try:
        with open(path, 'r') as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return None


def mono_ns():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def clock_pair_ns():
    # Both reads back to back; callers bracket this with tick timestamps.
    return time.clock_gettime_ns(time.CLOCK_REALTIME), mono_ns()


def snapshot_entity(ident, is_thread):
    """One entity, with stat read before AND after the other files, and an
    acquisition bracket. A starttime mismatch across the snapshot means the
    PID/TID was replaced mid-read: generation_consistent=0."""
    base = ('/proc/%d/task/%d' % ident) if is_thread else ('/proc/%d' % ident)
    acq_begin = mono_ns()
    pre_raw = read_text(base + '/stat', 4096)
    pre = parse_stat_text(pre_raw)
    mid_mono = mono_ns()
    if pre is None:
        return None
    wchan = (read_text(base + '/wchan', 64) or '').strip() or '0'
    ss_text = read_text(base + '/schedstat', 256)
    ss = parse_schedstat_text(ss_text)
    masks = parse_status_masks(read_text(base + '/status', 4096)) if is_thread else {}
    cgroup = None
    if not is_thread:
        cg_raw = read_text(base + '/cgroup', 4096)
        if cg_raw:
            tails = [ln.split(':', 2)[2] for ln in cg_raw.splitlines()
                     if ln.count(':') >= 2]
            cgroup = tails[-1].strip() if tails else None
    post_raw = read_text(base + '/stat', 4096)
    post = parse_stat_text(post_raw)
    acq_end = mono_ns()
    if post is None:
        return None
    consistent = post[STARTTIME] == pre[STARTTIME]
    return {
        'id': ident[1] if is_thread else ident,
        'key': entity_key(ident[1] if is_thread else ident, pre[STARTTIME]),
        'starttime': pre[STARTTIME],
        'comm': pre[2],
        'generation_consistent': int(consistent),
        'acq_begin_ns': acq_begin,
        'acq_mid_ns': mid_mono,
        'acq_end_ns': acq_end,
        'stat_pre': {'mono_ns': mid_mono, 'state': pre[STATE],
                     'utime': pre[UTIME], 'stime': pre[STIME],
                     'starttime': pre[STARTTIME], 'processor': pre.get(PROCESSOR)},
        'stat_post': {'state': post[STATE], 'utime': post[UTIME],
                      'stime': post[STIME], 'starttime': post[STARTTIME],
                      'processor': post.get(PROCESSOR)},
        'wchan': wchan,
        'schedstat': ss,            # None => unavailable for this entity
        'sigblk': masks.get('sigblk'),
        'sigpnd': masks.get('sigpnd'),
        'shdpnd': masks.get('shdpnd'),
        'cgroup': cgroup,
    }


def list_children(uml_pid):
    """PIDs reported as children by any UML thread (and one level deeper)."""
    pids = set()
    sources = ['/proc/%d/task' % uml_pid]
    try:
        tids = list(pathlib.Path(sources[0]).iterdir())
    except OSError:
        return pids
    for tidp in tids:
        raw = read_text(str(tidp / 'children'), 4096)
        for tok in (raw or '').split():
            try:
                pids.add(int(tok))
            except ValueError:
                pass
    for pid in list(pids):
        raw = read_text('/proc/%d/task/%d/children' % (pid, pid), 4096)
        for tok in (raw or '').split():
            try:
                pids.add(int(tok))
            except ValueError:
                pass
    return pids


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def schedstat_evidence():
    setting = read_text('/proc/sys/kernel/sched_schedstat', 32)
    own = parse_schedstat_text(read_text('/proc/self/schedstat', 128))
    return {
        'sched_schedstat_sysctl': setting.strip() if setting else None,
        'self_schedstat_present': own is not None,
        'host_clk_tck': os.sysconf('SC_CLK_TCK'),
        'note': 'schedstat run-wait is cumulative runqueue latency; absence is '
                'unknown, never evidence of zero wait',
    }


def cmd_sample(args):
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    uml = args.uml_pid
    anchor = snapshot_entity(uml, is_thread=False)
    if anchor is None:
        print(json.dumps({'ok': False, 'fatal': 'uml pid not readable', 'pid': uml}))
        return 2
    evidence = schedstat_evidence()
    if not evidence['self_schedstat_present']:
        print(json.dumps({'ok': False,
                          'fatal': 'host /proc/self/schedstat unreadable'}))
        return 2
    interval_ns = args.interval_ms * 1_000_000
    stop_mono = mono_ns() + int(args.duration_s * 1e9)
    manifest = {
        'kind': 'host_correlate_samples_v2',
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'uml_pid': uml,
        'uml_starttime': anchor['starttime'],
        'uml_comm': anchor['comm'],
        'uml_cgroup': anchor['cgroup'],
        'duration_s': args.duration_s,
        'interval_ms': args.interval_ms,
        'schedstat': evidence,
        'termination_reason': None,
        'samples': 0,
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    samples_f = (out / 'samples.jsonl').open('w', buffering=1)
    stopping = {'flag': False, 'reason': 'duration'}

    def handler(signum, frame):
        stopping['flag'] = True
        stopping['reason'] = 'signal'
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    known = {}  # key -> id
    n = 0
    errors = []
    try:
        while not stopping['flag'] and mono_ns() < stop_mono:
            tick_begin = mono_ns()
            rt, rt_mono = clock_pair_ns()
            tick_errors = []
            leader = snapshot_entity(uml, is_thread=False)
            if leader is None:
                # /proc/<pid> vanished: distinguish a clean target exit from a
                # live PID being replaced (which would still be readable).
                gone = not pathlib.Path('/proc/%d' % uml).exists()
                reason = 'target_exited' if gone else 'leader_unreadable'
                tick_errors.append(reason)
                row = {'seq': n, 'rt_ns': rt, 'rt_mono_ns': rt_mono,
                       'tick_begin_ns': tick_begin, 'tick_end_ns': mono_ns(),
                       'leader_alive': False, 'threads': {}, 'runners': {},
                       'events': [], 'errors': tick_errors}
                samples_f.write(json.dumps(row) + '\n')
                stopping['flag'] = True
                stopping['reason'] = reason
                n += 1
                break
            if leader['starttime'] != manifest['uml_starttime']:
                tick_errors.append('uml_leader_replaced')
                row = {'seq': n, 'rt_ns': rt, 'rt_mono_ns': rt_mono,
                       'tick_begin_ns': tick_begin, 'tick_end_ns': mono_ns(),
                       'leader_alive': True, 'leader_generation_consistent': False,
                       'threads': {}, 'runners': {},
                       'events': [], 'errors': tick_errors}
                samples_f.write(json.dumps(row) + '\n')
                stopping['flag'] = True
                stopping['reason'] = 'leader_replaced'
                n += 1
                break

            threads, runners = {}, {}
            try:
                tids = sorted(int(p.name)
                              for p in pathlib.Path('/proc/%d/task' % uml).iterdir()
                              if p.name.isdigit())
            except OSError:
                tids = []
            for tid in tids:
                ent = snapshot_entity((uml, tid), is_thread=True)
                if ent:
                    threads[ent['key']] = ent

            events = []
            for pid in list_children(uml):
                ent = snapshot_entity(pid, is_thread=False)
                if ent is None or ent['cgroup'] != manifest['uml_cgroup']:
                    continue
                if ent['comm'] == 'uml-userspace':
                    runners[ent['key']] = ent
                    if ent['key'] not in known:
                        known[ent['key']] = ent['id']
                        events.append({'kind': 'runner_birth', 'id': ent['id'],
                                       'starttime': ent['starttime'],
                                       'key': ent['key']})
            for key, pid in list(known.items()):
                if key not in runners:
                    # Distinguish "gone" from "replaced by a new starttime":
                    # replaced keys re-enter as births above.
                    events.append({'kind': 'runner_gone', 'id': pid,
                                   'starttime': int(key.split('@', 1)[1]),
                                   'key': key})
                    del known[key]

            tick_end = mono_ns()
            samples_f.write(json.dumps({
                'seq': n, 'rt_ns': rt, 'rt_mono_ns': rt_mono,
                'tick_begin_ns': tick_begin, 'tick_end_ns': tick_end,
                'leader_alive': True,
                'leader_starttime_pre': leader['stat_pre']['starttime'],
                'leader_starttime_post': leader['stat_post']['starttime'],
                'leader_generation_consistent': leader['generation_consistent'],
                'threads': threads, 'runners': runners,
                'events': events, 'errors': tick_errors}) + '\n')
            n += 1
            target = rt_mono + interval_ns
            while not stopping['flag'] and mono_ns() < stop_mono:
                now = mono_ns()
                if now >= target:
                    break
                time.sleep(max(0.0, min(0.05, (target - now) / 1e9)))
    except Exception as exc:  # never report ok on unexpected failure
        errors.append('%s: %s' % (type(exc).__name__, exc))
        stopping['reason'] = 'error'
    finally:
        samples_f.close()
        manifest['samples'] = n
        manifest['termination_reason'] = stopping['reason']
        manifest['errors'] = errors
        manifest['clock_end'] = {'rt_ns': clock_pair_ns()[0],
                                 'mono_ns': mono_ns()}
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    ok = stopping['reason'] in ('duration', 'signal', 'target_exited') and not errors
    print(json.dumps({'ok': ok, 'samples': n,
                      'termination_reason': stopping['reason'],
                      'errors': errors, 'out': str(out)}))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Analysis: clock alignment, coverage, identity-checked deltas
# ---------------------------------------------------------------------------

REQUIRED_SYNC_TAGS = ('start', 'end')


def build_alignment(syncs, rows):
    """Offset interval(s) mapping guest MONOTONIC -> host MONOTONIC.

    Each witness sync brackets its REALTIME read with two MONOTONIC reads;
    each host row brackets its REALTIME read with tick_begin/tick_end.
    Matching by REALTIME order yields an explicit offset interval per tag.
    Strict (v3): exactly one 'start' and one 'end' sync, BOTH bracketed, and
    their intervals must intersect. Missing/duplicate/unknown tags, missing
    pairs or skew => status unavailable; a single good tag is never enough.
    """
    result = {'status': 'unavailable', 'per_tag': [], 'offset_lo_ns': None,
              'offset_hi_ns': None, 'reason': None}
    tags = [s for s in syncs if s.get('tag') in REQUIRED_SYNC_TAGS]
    unknown = sorted({s.get('tag') for s in syncs
                      if s.get('tag') not in REQUIRED_SYNC_TAGS})
    if unknown:
        result['reason'] = 'unexpected sync tags: %s' % ','.join(map(str, unknown))
        return result
    counts = {}
    for s in tags:
        counts[s['tag']] = counts.get(s['tag'], 0) + 1
    if not rows:
        result['reason'] = 'missing host rows'
        return result
    if any(counts.get(t, 0) != 1 for t in REQUIRED_SYNC_TAGS):
        result['reason'] = 'sync tags must be exactly one start and one end'
        return result
    for s in tags:
        rt_g = s['rt_ns']
        g0, g1 = s['mono_before_ns'], s['mono_after_ns']
        i0 = i1 = None
        for i, r in enumerate(rows):
            if r['rt_ns'] <= rt_g:
                i0 = i
            if r['rt_ns'] >= rt_g and i1 is None:
                i1 = i
        if i0 is None or i1 is None:
            result['per_tag'].append({'tag': s['tag'], 'available': False,
                                      'reason': 'rt outside host capture span'})
            continue
        # Host mono at rt_g is within [begin(row i0), end(row i1)].
        host_lo = rows[i0]['tick_begin_ns']
        host_hi = rows[i1]['tick_end_ns']
        off_lo = host_lo - g1
        off_hi = host_hi - g0
        if off_hi < off_lo:
            result['per_tag'].append({'tag': s['tag'], 'available': False,
                                      'reason': 'negative offset interval'})
            continue
        result['per_tag'].append({'tag': s['tag'], 'available': True,
                                  'offset_lo_ns': off_lo,
                                  'offset_hi_ns': off_hi,
                                  'width_ns': off_hi - off_lo})
    avail = [t for t in result['per_tag'] if t.get('available')]
    if len(avail) != len(REQUIRED_SYNC_TAGS):
        result['reason'] = 'both start and end syncs must bracket host capture'
        return result
    lo = max(t['offset_lo_ns'] for t in avail)
    hi = min(t['offset_hi_ns'] for t in avail)
    if hi < lo:
        result['reason'] = 'per-tag offset intervals do not intersect (skew)'
        return result
    # Drift consistency: tag midpoints must agree within combined uncertainty.
    mids = sorted(((t['tag'],
                    (t['offset_lo_ns'] + t['offset_hi_ns']) // 2)
                   for t in avail), key=lambda x: x[1])
    result.update({'status': 'available', 'offset_lo_ns': lo,
                   'offset_hi_ns': hi, 'width_ns': hi - lo,
                   'midpoint_span_ns': (mids[-1][1] - mids[0][1]) if len(mids) > 1 else 0,
                   'reason': None})
    return result


def _in_window_entities(rows, group, i, j, key):
    states, wchans, cpus, sigblks, gens = set(), set(), set(), set(), set()
    for r in rows[i:j + 1]:
        e = r[group].get(key)
        if not e:
            continue
        states.add(e['stat_pre']['state'])
        states.add(e['stat_post']['state'])
        wchans.add(e['wchan'])
        cpus.add(e['stat_pre']['processor'])
        gens.add(e['generation_consistent'])
        for m in ('sigblk', 'sigpnd', 'shdpnd'):
            if e.get(m):
                sigblks.add('%s=%s' % (m, e[m]))
    return {'states': sorted(states), 'wchans': sorted(wchans),
            'cpus': sorted(c for c in cpus if c is not None),
            'sig_masks_seen': sorted(sigblks),
            'mixed_generations_in_window': int(0 in gens)}


def correlate_stage(stage, cap, rows, align):
    """Identity-checked, fully-bracketed delta attribution."""
    out = {'k': cap['k'], 'stage': stage['name'],
           'guest_wall_ns': stage['end_ns'] - stage['begin_ns'],
           'guest_thread_cpu_ns': stage['tcpu_end_ns'] - stage['tcpu_begin_ns']}
    if align['status'] != 'available':
        out.update({'coverage': 'unattributed',
                    'reason': 'clock alignment unavailable: %s' % align['reason']})
        return out
    begin_t = stage['begin_ns'] + align['offset_lo_ns']
    end_t = stage['end_ns'] + align['offset_hi_ns']
    out.update({'host_begin_ns_lo': begin_t, 'host_end_ns_hi': end_t,
                'alignment_width_ns': align['width_ns']})

    # Last row fully ACQUIRED before the interval start; first row whose
    # acquisition began after the interval end. Partial overlaps never count.
    i = None
    for idx, r in enumerate(rows):
        if r['tick_end_ns'] <= begin_t:
            i = idx
    j = None
    for idx, r in enumerate(rows):
        if r['tick_begin_ns'] >= end_t:
            j = idx
            break
    if i is None:
        out.update({'coverage': 'uncovered_pre',
                    'reason': 'no host sample fully before interval start'})
        return out
    if j is None:
        out.update({'coverage': 'uncovered_post',
                    'reason': 'no host sample fully after interval end'})
        return out
    if j <= i:
        out.update({'coverage': 'uncovered',
                    'reason': 'host sampling coarser than interval+uncertainty'})
        return out

    entities = {}
    for group, label in (('threads', 'thread'), ('runners', 'runner')):
        keys = set(rows[i][group]) | set(rows[j][group])
        for key in sorted(keys):
            a, b = rows[i][group].get(key), rows[j][group].get(key)
            ent = {'label': label, 'key': key}
            if a is None or b is None:
                ent.update({'identity': 'missing',
                            'detail': 'entity not present at both bracket ticks'})
                entities['%s:%s' % (label, key)] = ent
                continue
            if not (a['generation_consistent'] and b['generation_consistent']):
                ent.update({'identity': 'indeterminate',
                            'detail': 'snapshot straddled a PID/TID generation'})
                entities['%s:%s' % (label, key)] = ent
                continue
            regressions = []
            deltas = {}
            for field in ('utime', 'stime'):
                d = b['stat_pre'][field] - a['stat_pre'][field]
                deltas[field + '_ticks'] = d   # raw; never clamped
                if d < 0:
                    regressions.append(field)
            if a['schedstat'] and b['schedstat']:
                for field in ('exec_ns', 'wait_ns', 'switches'):
                    d = b['schedstat'][field] - a['schedstat'][field]
                    deltas[field] = d
                    if d < 0:
                        regressions.append('schedstat.' + field)
                wait_available = True
            else:
                wait_available = False
            ent.update({
                'identity': 'matched' if not regressions else 'counter_regression',
                'bracket_pre_acq_end_ns': a['acq_end_ns'],
                'bracket_post_acq_begin_ns': b['acq_begin_ns'],
                'host_bracket_width_ns': b['acq_begin_ns'] - a['acq_end_ns'],
                'deltas': deltas,
                'run_wait_available': wait_available,
                'regressions': regressions,
            })
            ent.update(_in_window_entities(rows, group, i, j, key))
            entities['%s:%s' % (label, key)] = ent

    births, deaths = [], []
    for r in rows[i:j + 1]:
        for ev in r.get('events', []):
            births.append(ev) if ev['kind'] == 'runner_birth' else deaths.append(ev)
    out.update({'coverage': 'covered', 'pre_seq': rows[i]['seq'],
                'post_seq': rows[j]['seq'], 'entities': entities,
                'runner_births': births, 'runner_gone': deaths})
    return out


STAGE_NAMES = ('cpu.max.first', 'cpu.stat', 'cpu.max.second', '/proc/stat',
               'process_clock')
CAP_GROUPS = ('max1', 'stat', 'max2', 'proc')
KNOWN_SUMMARY_STATUS = ('complete', 'deadline', 'clock_error')


def validate_witness(syncs, caps, summary, expect_count):
    """Strict witness schema. Returns a list of problems (empty == valid).

    Required: exactly one start/end sync; the witness summary line (captured
    separately on stderr by the harness) with requested==completed==count of
    caps, status 'complete'; caps numbered 0..n-1 with five named stages and
    the raw cgroup groups. Nothing is inferred from an incomplete witness.
    """
    problems = []
    tag_counts = {}
    for s in syncs:
        tag_counts[s.get('tag')] = tag_counts.get(s.get('tag'), 0) + 1
        if (s.get('tag') not in REQUIRED_SYNC_TAGS
                or not isinstance(s.get('rt_ns'), int)
                or not isinstance(s.get('mono_before_ns'), int)
                or not isinstance(s.get('mono_after_ns'), int)
                or s['mono_after_ns'] < s['mono_before_ns']):
            problems.append('malformed sync event: %r' % s.get('tag'))
    for tag in REQUIRED_SYNC_TAGS:
        if tag_counts.get(tag, 0) != 1:
            problems.append('sync tag %r count=%d (need exactly 1)'
                            % (tag, tag_counts.get(tag, 0)))
    if not isinstance(summary, dict):
        problems.append('missing witness summary (stderr)')
        summary = {}
    requested = summary.get('requested')
    completed = summary.get('completed')
    status = summary.get('status')
    if not isinstance(requested, int) or not isinstance(completed, int) \
            or requested <= 0 or completed < 0 or completed > requested:
        problems.append('summary requested/completed invalid: %r' % summary)
    if status not in KNOWN_SUMMARY_STATUS:
        problems.append('summary status invalid: %r' % (status,))
    if status == 'complete' and requested != completed:
        problems.append('summary status complete but completed != requested')
    if expect_count is not None:
        if requested != expect_count or completed != expect_count:
            problems.append('summary counts %s/%s != requested --expect-count %d'
                            % (requested, completed, expect_count))
    if not caps:
        problems.append('zero cap events')
    if isinstance(completed, int) and completed != len(caps):
        problems.append('cap event count %d != summary completed %d'
                        % (len(caps), completed))
    for idx, cap in enumerate(caps):
        pfx = 'cap[%d] ' % idx
        if cap.get('k') != idx:
            problems.append(pfx + 'k out of sequence: %r' % (cap.get('k'),))
        stages = cap.get('stages')
        if not isinstance(stages, list) or len(stages) != len(STAGE_NAMES):
            problems.append(pfx + 'stages missing/wrong length')
            continue
        for j, st in enumerate(stages):
            if st.get('name') != STAGE_NAMES[j] \
                    or not all(isinstance(st.get(k), int) for k in
                               ('begin_ns', 'end_ns', 'tcpu_begin_ns',
                                'tcpu_end_ns')) \
                    or st['end_ns'] < st['begin_ns'] \
                    or st['tcpu_end_ns'] < st['tcpu_begin_ns']:
                problems.append(pfx + 'malformed stage %r' % st.get('name'))
        for grp in CAP_GROUPS:
            g = cap.get(grp)
            if not isinstance(g, dict) or not isinstance(g.get('errno'), int):
                problems.append(pfx + 'missing/malformed group %r' % grp)
    return problems


def analyze_capture(manifest, rows, witness, summary, expect_count):
    syncs = [w for w in witness if w.get('event') == 'sync']
    caps = [w for w in witness if w.get('event') == 'cap']
    unknown_events = sorted({w.get('event') for w in witness
                             if w.get('event') not in ('sync', 'cap')})
    schema_problems = validate_witness(syncs, caps, summary, expect_count)
    if unknown_events:
        schema_problems.append('unexpected witness events: %s'
                               % ','.join(map(str, unknown_events)))
    align = build_alignment(syncs, rows)
    stages = []
    for cap in caps:
        for st in cap.get('stages', []):
            stages.append(correlate_stage(st, cap, rows, align))
    stages.sort(key=lambda s: s['guest_wall_ns'], reverse=True)
    leader_ok = (manifest.get('termination_reason')
                 in ('duration', 'signal', 'target_exited')
                 and all(r.get('leader_generation_consistent', True) for r in rows))
    coverage_counts = {}
    for s in stages:
        coverage_counts[s.get('coverage')] = coverage_counts.get(
            s.get('coverage'), 0) + 1
    witness_ok = not schema_problems
    alignment_ok = align['status'] == 'available'
    integrity_ok = leader_ok and len(rows) >= 2
    return {
        'kind': 'host_correlate_report_v3',
        'schema': {'witness_complete': witness_ok,
                   'expected_count': expect_count,
                   'cap_events': len(caps),
                   'summary': summary if isinstance(summary, dict) else None,
                   'problems': schema_problems},
        'run_integrity': {
            'termination_reason': manifest.get('termination_reason'),
            'samples': manifest.get('samples'),
            'leader_consistent': leader_ok,
            'enough_rows': len(rows) >= 2,
            'ok': integrity_ok,
            'schedstat': manifest.get('schedstat'),
        },
        'clock_alignment': align,
        'coverage_counts': coverage_counts,
        # Attribution exists only when all three are good; uncovered stages
        # are still listed verbatim and never carry invented deltas.
        'attribution_valid': integrity_ok and witness_ok and alignment_ok,
        'stages': stages,
    }


def _load_json_lines(path, what):
    try:
        return [json.loads(l)
                for l in pathlib.Path(path).read_text().splitlines() if l.strip()]
    except (OSError, ValueError) as exc:
        raise ValueError('%s parse failure: %s' % (what, exc))


def cmd_analyze(args):
    sdir, out = pathlib.Path(args.samples), pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        manifest = json.loads((sdir / 'manifest.json').read_text())
        rows = _load_json_lines(sdir / 'samples.jsonl', 'samples')
        witness = _load_json_lines(args.witness, 'witness')
        summary_lines = _load_json_lines(args.summary, 'summary')
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'fatal': str(exc)}))
        return 2
    summaries = [s for s in summary_lines if s.get('event') == 'summary']
    if len(summaries) != 1:
        print(json.dumps({'ok': False,
                          'fatal': 'exactly one witness summary line required, '
                                   'found %d' % len(summaries)}))
        return 2
    result = analyze_capture(manifest, rows, witness, summaries[0],
                             args.expect_count)
    (out / 'correlation.json').write_text(json.dumps(result, indent=2) + '\n')
    ok = (result['run_integrity']['ok']
          and result['schema']['witness_complete']
          and result['clock_alignment']['status'] == 'available')
    print(json.dumps({'ok': ok, 'stages': len(result['stages']),
                      'coverage': result['coverage_counts'],
                      'witness_complete': result['schema']['witness_complete'],
                      'schema_problems': result['schema']['problems'],
                      'alignment': result['clock_alignment']['status'],
                      'integrity': result['run_integrity']['ok'],
                      'out': str(out / 'correlation.json')}, indent=2))
    return 0 if ok else 1


# ---------------------------------------------------------------------------

def finite_positive(s, lo, hi):
    try:
        v = float(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('not a number')
    if not math.isfinite(v) or not lo <= v <= hi:
        raise argparse.ArgumentTypeError('out of range')
    return v


def strict_int(s, lo, hi):
    s = str(s)
    if not s or not s.lstrip('+').isdigit():
        raise argparse.ArgumentTypeError('not an integer')
    v = int(s)
    if not lo <= v <= hi:
        raise argparse.ArgumentTypeError('out of range')
    return v


def strict_pid(s):
    """Positive integer PIDs only: digits, no sign, no fraction, no exponent."""
    s = str(s)
    if not s or not s.isdigit():
        raise argparse.ArgumentTypeError('pid must be a positive integer')
    v = int(s)
    if not 1 <= v <= 2 ** 22:
        raise argparse.ArgumentTypeError('pid out of range')
    return v


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('sample')
    s.add_argument('--uml-pid', type=strict_pid, required=True)
    s.add_argument('--out', required=True)
    s.add_argument('--duration-s', type=lambda x: finite_positive(x, 1.0, 300.0),
                   default=30.0)
    s.add_argument('--interval-ms', type=lambda x: strict_int(x, 2, 1000),
                   default=10)
    s.set_defaults(func=cmd_sample)
    a = sub.add_parser('analyze')
    a.add_argument('--samples', required=True)
    a.add_argument('--witness', required=True)
    a.add_argument('--summary', required=True,
                   help='witness stderr file containing its summary event')
    a.add_argument('--expect-count', type=lambda x: strict_int(x, 1, 100000),
                   required=True, help='required completed cap count')
    a.add_argument('--out', required=True)
    a.set_defaults(func=cmd_analyze)
    args = p.parse_args()
    raise SystemExit(args.func(args))


if __name__ == '__main__':
    main()
