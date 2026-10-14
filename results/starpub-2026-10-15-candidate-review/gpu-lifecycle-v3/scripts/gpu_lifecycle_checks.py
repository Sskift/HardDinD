"""Independent validator for src/cuda_lifecycle_probe.c JSONL output.

The probe prints one JSON object per line:

    meta, inventory*N, solo workers, shared ctx + shared workers,
    multicard workers, child workers, summary

Pass/fail is re-derived here from the individual records.  The summary
record's status is only checked for consistency; it can never turn an
incomplete or inconsistent log into a pass.

This is a parser/validator, not a GPU emulator: it accepts structural
evidence produced by a real run and rejects forged, truncated, duplicated,
reordered or serial-execution fixtures.
"""
import argparse
import json
import pathlib
import sys

PHASE_RANK = {'solo': 0, 'shared': 1, 'multicard': 2, 'child': 3}

# Cycle identity roles, mirrored from src/cuda_lifecycle_probe.c.  The
# parent and every fork+exec child derive identical seeds from this
# explicit namespace instead of a process-local counter, so cross-process
# stale/misrouted data cannot hide behind a repeated input pattern.
ROLE_SOLO = 1
ROLE_SHARED = 2
ROLE_MULTICARD = 3
ROLE_CHILD = 4
_MASK = 0xFFFFFFFF


def cycle_seed(role, proc, worker, round_, cycle):
    """Deterministic uint32 pattern seed for one (role,proc,worker,round,
    cycle) tuple; must match the C cycle_seed() bit for bit."""
    h = (0x9E3779B9 + role * 0x85EBCA77 + proc * 0xC2B2AE3D
         + worker * 0x27D4EB2F + round_ * 0x165667B1
         + cycle * 0xD1B54A31) & _MASK
    h ^= h >> 16
    h = (h * 0x7FEB352D) & _MASK
    h ^= h >> 15
    h = (h * 0x846CA68B) & _MASK
    h ^= h >> 16
    return h if h else 1

# Keep in sync with the defaults compiled into src/cuda_lifecycle_probe.c.
DEFAULT_REQUEST = {
    'cycles': 8,
    'child_cycles': 2,
    'ctx_rounds': 2,
    'threads': 4,
    'elements': 262144,
}


def inventory_uuids(content):
    """Ordered UUID list from a probe log's inventory records."""
    uuids, ordinals = [], []
    for line in content.splitlines():
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict) and rec.get('type') == 'inventory':
            uuids.append(rec['uuid'])
            ordinals.append(rec['ordinal'])
    if len(set(uuids)) != len(uuids) or ordinals != sorted(ordinals):
        raise ValueError('invalid inventory in reference log')
    return uuids


def _parse_lines(content):
    records, reasons = [], []
    for lineno, raw in enumerate(content.splitlines(), 1):
        if not raw.strip():
            reasons.append(f'line {lineno}: blank line')
            continue
        try:
            rec = json.loads(raw)
        except ValueError as error:
            reasons.append(f'line {lineno}: invalid JSON: {error}')
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get('type'), str):
            reasons.append(f'line {lineno}: record without string type')
            continue
        records.append((lineno, rec))
    return records, reasons


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_rc_list(reasons, where, rec, key, count):
    value = rec.get(key)
    if not (isinstance(value, list) and len(value) == count
            and all(_is_int(v) for v in value)):
        reasons.append(f'{where}: {key} must be {count} integers')
        return
    if any(v != 0 for v in value):
        reasons.append(f'{where}: {key} has non-zero driver rc {value}')


def _check_cycle(where, cyc, idx, elements, identity, all_seeds):
    """Validate one cycle record against its explicit identity.  The seed
    is re-derived from (role, proc, worker, round, cycle) and collected for
    run-wide uniqueness, which also spans the separate exec children."""
    reasons = []
    role, proc, worker, round_ = identity
    if not isinstance(cyc, dict):
        return [f'{where}: cycle record is not an object']
    if cyc.get('cycle') != idx:
        reasons.append(f'{where}: cycle index {cyc.get("cycle")} != {idx}')
    for key, value in (('role', role), ('proc', proc),
                       ('worker', worker), ('round', round_)):
        if cyc.get(key) != value:
            reasons.append(f'{where}: identity {key}={cyc.get(key)} != '
                           f'{value}')
    expected_seed = cycle_seed(role, proc, worker, round_, idx)
    seed = cyc.get('seed')
    if not _is_int(seed) or seed == 0:
        reasons.append(f'{where}: bad cycle seed')
    elif not 0 < seed <= _MASK:
        reasons.append(f'{where}: seed out of uint32 range')
    else:
        if seed != expected_seed:
            reasons.append(f'{where}: seed {seed} does not derive from '
                           f'role={role} proc={proc} worker={worker} '
                           f'round={round_} cycle={idx}')
        if seed in all_seeds:
            reasons.append(f'{where}: seed {seed} reused across cycles or '
                           'processes')
        all_seeds[seed] = all_seeds.get(seed, 0) + 1
    for key in ('htod_rc', 'launch_rc', 'sync_rc', 'dtoh_rc'):
        if not _is_int(cyc.get(key)):
            reasons.append(f'{where}: {key} missing or non-integer')
        elif cyc[key] != 0:
            reasons.append(f'{where}: {key}={cyc[key]}')
    _check_rc_list(reasons, where, cyc, 'alloc_rc', 3)
    _check_rc_list(reasons, where, cyc, 'free_rc', 3)
    if cyc.get('mismatches') != 0:
        reasons.append(f'{where}: mismatches={cyc.get("mismatches")}')
    if not _is_int(cyc.get('csum')):
        reasons.append(f'{where}: missing checksum')
    start, end = cyc.get('start_ns'), cyc.get('end_ns')
    if not all(_is_int(v) for v in (start, end)) or not start < end:
        reasons.append(f'{where}: non-monotonic cycle interval')
    if elements is not None and not _is_int(elements):
        reasons.append(f'{where}: elements field invalid')
    return reasons


def _check_worker_common(where, rec, request, ncycles, role, proc, all_seeds):
    reasons = []
    if rec.get('elements') != request['elements']:
        reasons.append(f'{where}: elements {rec.get("elements")} != '
                       f'{request["elements"]}')
    if not _is_int(rec.get('allocs')) or not _is_int(rec.get('frees')):
        reasons.append(f'{where}: alloc/free counters missing')
    detail = rec.get('cycles_detail')
    if detail is None:
        reasons.append(f'{where}: cycles_detail missing')
        return reasons
    if not isinstance(detail, list):
        reasons.append(f'{where}: cycles_detail not a list')
        return reasons
    if len(detail) != ncycles:
        reasons.append(f'{where}: {len(detail)} cycles_detail records, '
                       f'expected {ncycles}')
    for i, cyc in enumerate(detail):
        reasons.extend(_check_cycle(f'{where}.cycle{i}', cyc, i,
                                    rec.get('elements'),
                                    (role, proc, rec.get('worker'), 0),
                                    all_seeds))
    return reasons

def _overlap(reasons, where, intervals, minimum):
    """All [start,end] intervals must overlap by a positive window."""
    if len(intervals) != minimum:
        reasons.append(f'{where}: expected {minimum} intervals, got '
                       f'{len(intervals)}')
        return None
    starts = [s for s, _ in intervals]
    ends = [e for _, e in intervals]
    if any(not s < e for s, e in intervals):
        reasons.append(f'{where}: non-monotonic worker interval')
        return None
    window = min(ends) - max(starts)
    if window <= 0:
        reasons.append(f'{where}: workers did not coexist (overlap_ns='
                       f'{window})')
    return window


def analyze(content, request):
    """Return a verdict dict; verdict['ok'] is the independently derived
    result and never comes from the probe's summary."""
    request = dict(request)
    request.setdefault('cycles', DEFAULT_REQUEST['cycles'])
    request.setdefault('child_cycles', DEFAULT_REQUEST['child_cycles'])
    request.setdefault('ctx_rounds', DEFAULT_REQUEST['ctx_rounds'])
    request.setdefault('threads', DEFAULT_REQUEST['threads'])
    request.setdefault('elements', DEFAULT_REQUEST['elements'])
    expected_uuids = list(request['uuids'])
    ngpu = len(expected_uuids)
    if 'children' not in request:
        request['children'] = max(DEFAULT_REQUEST['threads'], ngpu)
    ordinals = request.get('ordinals') or list(range(ngpu))
    reasons = []
    all_seeds = {}
    verdict = {'ok': False, 'reasons': reasons, 'overlap_ns': {},
               'gpu_count': ngpu}
    if not expected_uuids:
        reasons.append('request contains no expected GPU UUIDs')
    if len(set(expected_uuids)) != len(expected_uuids):
        reasons.append('expected GPU UUID list contains duplicates')

    records, parse_reasons = _parse_lines(content)
    reasons.extend(parse_reasons)
    by_type = {}
    for _, rec in records:
        by_type.setdefault(rec['type'], []).append(rec)

    # ---- meta (exactly one, first record) ------------------------------
    metas = by_type.get('meta', [])
    if len(metas) != 1:
        reasons.append(f'meta records: expected 1, got {len(metas)}')
        meta = {}
    else:
        meta = metas[0]
        if records[0][1] is not meta:
            reasons.append('meta is not the first record')
        if meta.get('schema') != 1 or meta.get('init_rc') != 0:
            reasons.append('meta schema/init_rc invalid')
        for key in ('cycles', 'child_cycles', 'ctx_rounds', 'threads',
                    'children', 'elements'):
            if meta.get(key) != request[key]:
                reasons.append(f'meta {key}={meta.get(key)} does not match '
                               f'requested {request[key]}')
        if not _is_int(meta.get('pid')) or meta['pid'] <= 0:
            reasons.append('meta pid invalid')
    parent_pid = meta.get('pid')

    # ---- errors abort acceptance immediately ---------------------------
    if by_type.get('error'):
        reasons.append(f"{len(by_type['error'])} error record(s) present")

    # ---- inventory ------------------------------------------------------
    inv = by_type.get('inventory', [])
    if len(inv) != ngpu:
        reasons.append(f'inventory records: expected {ngpu}, got {len(inv)}')
    seen_ordinals, seen_uuids = [], []
    uuid_by_ordinal = {}
    for i, rec in enumerate(inv):
        where = f'inventory[{i}]'
        if i >= len(ordinals) or rec.get('ordinal') != ordinals[i]:
            reasons.append(f'{where}: unexpected ordinal {rec.get("ordinal")}')
        if not _is_int(rec.get('total_mem')) or rec['total_mem'] <= 0:
            reasons.append(f'{where}: total_mem invalid')
        if not isinstance(rec.get('name'), str) or not rec['name']:
            reasons.append(f'{where}: name missing')
        if not isinstance(rec.get('uuid'), str) or not rec['uuid']:
            reasons.append(f'{where}: uuid missing')
        if rec.get('uuid_valid') is not True:
            reasons.append(f'{where}: uuid_valid is not true (driver query '
                           'failed; all-zero marker must not pass)')
        seen_ordinals.append(rec.get('ordinal'))
        seen_uuids.append(rec.get('uuid'))
        uuid_by_ordinal[rec.get('ordinal')] = rec.get('uuid')
    if len(seen_ordinals) != len(set(seen_ordinals)):
        reasons.append('duplicate inventory ordinal')
    if len(seen_uuids) != len(set(seen_uuids)):
        reasons.append('duplicate inventory UUID')
    if seen_uuids and seen_uuids != expected_uuids:
        reasons.append('inventory UUID set/order does not match the '
                       'requested GPU set')

    # ---- canonical record order (type and phase grouping) --------------
    middle = [(rec['type'], rec.get('phase'))
              for _, rec in records
              if rec['type'] not in ('meta', 'inventory', 'summary', 'error')]
    expected_middle = [('worker', 'solo')] * ngpu
    expected_middle.append(('ctx', 'shared'))
    expected_middle += [('worker', 'shared')] * request['threads']
    expected_middle += [('worker', 'multicard')] * ngpu
    expected_middle += [('worker', 'child')] * request['children']
    if middle != expected_middle:
        reasons.append('phase records are missing, duplicated, reordered '
                       'or from an unexpected phase')

    workers = by_type.get('worker', [])
    phase_workers = {p: [] for p in PHASE_RANK}
    for rec in workers:
        phase_workers.setdefault(rec.get('phase'), []).append(rec)

    # ---- solo phase -----------------------------------------------------
    solo = phase_workers.get('solo', [])
    if len(solo) != ngpu:
        reasons.append(f'solo workers: expected {ngpu}, got {len(solo)}')
    for i, rec in enumerate(solo):
        where = f'solo[{i}]'
        if rec.get('worker') != i:
            reasons.append(f'{where}: worker id {rec.get("worker")} != {i}')
        if i < ngpu:
            if rec.get('ordinal') != ordinals[i]:
                reasons.append(f'{where}: wrong ordinal')
            if rec.get('uuid') != expected_uuids[i]:
                reasons.append(f'{where}: wrong uuid')
        rounds = rec.get('rounds')
        if not isinstance(rounds, list) or len(rounds) != request['ctx_rounds']:
            reasons.append(f'{where}: expected {request["ctx_rounds"]} '
                           f'context rounds')
            rounds = []
        for r, roundrec in enumerate(rounds):
            rwhere = f'{where}.round{r}'
            if not isinstance(roundrec, dict) or roundrec.get('round') != r:
                reasons.append(f'{rwhere}: bad round record')
                continue
            for key in ('create_rc', 'load_rc', 'unload_rc', 'destroy_rc'):
                if roundrec.get(key) != 0:
                    reasons.append(f'{rwhere}: {key}='
                                   f'{roundrec.get(key)}')
            for key in ('info_enter_rc', 'info_exit_rc'):
                if roundrec.get(key) != 0:
                    reasons.append(f'{rwhere}: {key}='
                                   f'{roundrec.get(key)}')
            for key in ('free_enter', 'free_exit'):
                if not _is_int(roundrec.get(key)):
                    reasons.append(f'{rwhere}: {key} missing')
            cycles = roundrec.get('cycles')
            if not isinstance(cycles, list) or len(cycles) != request['cycles']:
                reasons.append(f'{rwhere}: expected {request["cycles"]} '
                               'cycles')
                continue
            for c, cyc in enumerate(cycles):
                reasons.extend(_check_cycle(f'{rwhere}.cycle{c}', cyc, c,
                             rec.get('elements'),
                             (ROLE_SOLO, 0, i, r), all_seeds))
        expected_ops = request['ctx_rounds'] * request['cycles'] * 3
        if rec.get('allocs') != expected_ops or rec.get('frees') != expected_ops:
            reasons.append(f'{where}: alloc/free counts '
                           f'{rec.get("allocs")}/{rec.get("frees")} != '
                           f'{expected_ops}')
        if rec.get('cycles') != request['cycles']:
            reasons.append(f'{where}: cycles count mismatch')
        if not _is_int(rec.get('start_ns')) or not _is_int(rec.get('end_ns')) \
                or rec['start_ns'] >= rec['end_ns']:
            reasons.append(f'{where}: bad interval')

    # ---- shared phase ---------------------------------------------------
    ctxs = by_type.get('ctx', [])
    if len(ctxs) != 1:
        reasons.append(f'shared ctx records: expected 1, got {len(ctxs)}')
        ctx = {}
    else:
        ctx = ctxs[0]
        if ctx.get('phase') != 'shared':
            reasons.append('ctx record is not phase=shared')
        for key in ('create_rc', 'load_rc', 'unload_rc', 'destroy_rc'):
            if ctx.get(key) != 0:
                reasons.append(f'shared ctx {key}={ctx.get(key)}')
        if ctx.get('threads') != request['threads']:
            reasons.append('shared ctx thread count mismatch')
        if ngpu and ctx.get('uuid') != expected_uuids[0]:
            reasons.append('shared ctx bound to the wrong GPU UUID')
    shared = phase_workers.get('shared', [])
    if len(shared) != request['threads']:
        reasons.append(f'shared workers: expected {request["threads"]}, '
                       f'got {len(shared)}')
    shared_intervals, shared_tids = [], []
    for i, rec in enumerate(shared):
        where = f'shared[{i}]'
        if rec.get('worker') != i:
            reasons.append(f'{where}: bad worker id')
        if ngpu and rec.get('uuid') != expected_uuids[0]:
            reasons.append(f'{where}: wrong shared-context UUID')
        if rec.get('attach_rc') != 0 or rec.get('detach_rc') != 0:
            reasons.append(f'{where}: attach/detach rc '
                           f'{rec.get("attach_rc")}/{rec.get("detach_rc")}')
        if rec.get('pid') != parent_pid:
            reasons.append(f'{where}: pid is not the probe process')
        reasons.extend(_check_worker_common(where, rec, request,
                                            request['cycles'], ROLE_SHARED,
                                            0, all_seeds))
        if rec.get('cycles') != request['cycles']:
            reasons.append(f'{where}: cycles count mismatch')
        expected_ops = request['cycles'] * 3
        if rec.get('allocs') != expected_ops or rec.get('frees') != expected_ops:
            reasons.append(f'{where}: alloc/free counts mismatch')
        for key in ('ready_ns', 'start_ns', 'end_ns'):
            if not _is_int(rec.get(key)):
                reasons.append(f'{where}: {key} missing')
        if _is_int(rec.get('tid')):
            if rec['tid'] in shared_tids:
                reasons.append(f'{where}: duplicate thread identity')
            shared_tids.append(rec['tid'])
        if all(_is_int(rec.get(k)) for k in ('ready_ns', 'start_ns', 'end_ns')):
            if not rec['ready_ns'] <= rec['start_ns'] < rec['end_ns']:
                reasons.append(f'{where}: barrier/interval ordering broken')
            shared_intervals.append((rec['start_ns'], rec['end_ns']))
    if len(shared_tids) != request['threads']:
        reasons.append('shared phase thread identities incomplete')
    window = _overlap(reasons, 'shared', shared_intervals, request['threads'])
    verdict['overlap_ns']['shared'] = window

    # ---- multicard phase ------------------------------------------------
    multi = phase_workers.get('multicard', [])
    if len(multi) != ngpu:
        reasons.append(f'multicard workers: expected {ngpu}, got {len(multi)}')
    multi_intervals, multi_tids = [], []
    for i, rec in enumerate(multi):
        where = f'multicard[{i}]'
        if rec.get('worker') != i:
            reasons.append(f'{where}: bad worker id')
        if i < ngpu:
            if rec.get('ordinal') != ordinals[i]:
                reasons.append(f'{where}: wrong ordinal')
            if rec.get('uuid') != expected_uuids[i]:
                reasons.append(f'{where}: wrong uuid')
        for key in ('create_rc', 'load_rc', 'unload_rc', 'destroy_rc'):
            if rec.get(key) != 0:
                reasons.append(f'{where}: {key}={rec.get(key)}')
        if rec.get('pid') != parent_pid:
            reasons.append(f'{where}: pid is not the probe process')
        reasons.extend(_check_worker_common(where, rec, request,
                                            request['cycles'],
                                            ROLE_MULTICARD, 0, all_seeds))
        if rec.get('cycles') != request['cycles']:
            reasons.append(f'{where}: cycles count mismatch')
        expected_ops = request['cycles'] * 3
        if rec.get('allocs') != expected_ops or rec.get('frees') != expected_ops:
            reasons.append(f'{where}: alloc/free counts mismatch')
        if _is_int(rec.get('tid')):
            if rec['tid'] in multi_tids:
                reasons.append(f'{where}: duplicate thread identity')
            if rec['tid'] in shared_tids:
                reasons.append(f'{where}: multicard tid reused from shared '
                               'group')
            multi_tids.append(rec['tid'])
        for key in ('ready_ns', 'start_ns', 'end_ns'):
            if not _is_int(rec.get(key)):
                reasons.append(f'{where}: {key} missing')
        if all(_is_int(rec.get(k)) for k in ('ready_ns', 'start_ns', 'end_ns')):
            if not rec['ready_ns'] <= rec['start_ns'] < rec['end_ns']:
                reasons.append(f'{where}: barrier/interval ordering broken')
            multi_intervals.append((rec['start_ns'], rec['end_ns']))
    window = _overlap(reasons, 'multicard', multi_intervals, ngpu)
    verdict['overlap_ns']['multicard'] = window

    # ---- child phase ----------------------------------------------------
    children = phase_workers.get('child', [])
    if len(children) != request['children']:
        reasons.append(f'child workers: expected {request["children"]}, '
                       f'got {len(children)}')
    child_intervals, child_pids = [], []
    child_uuid_seen = set()
    for i, rec in enumerate(children):
        where = f'child[{i}]'
        if rec.get('worker') != i:
            reasons.append(f'{where}: bad worker seq')
        if rec.get('ordinal') not in ordinals:
            reasons.append(f'{where}: ordinal not in inventory')
        else:
            expected = uuid_by_ordinal.get(rec['ordinal'])
            if rec.get('uuid') != expected:
                reasons.append(f'{where}: uuid does not match inventory')
            child_uuid_seen.add(rec.get('uuid'))
        if rec.get('received') is not True:
            reasons.append(f'{where}: child report not received')
        if rec.get('exec_errno') != 0:
            reasons.append(f'{where}: exec errno {rec.get("exec_errno")}')
        if rec.get('child_exit') != 0:
            reasons.append(f'{where}: child exit {rec.get("child_exit")}')
        if rec.get('child_signal') not in (0, None):
            reasons.append(f'{where}: child killed by signal '
                           f'{rec.get("child_signal")}')
        if rec.get('killed') is True:
            reasons.append(f'{where}: parent had to kill the child')
        pid = rec.get('child_pid')
        if not _is_int(pid) or pid <= 0:
            reasons.append(f'{where}: bad child pid')
        elif pid in child_pids:
            reasons.append(f'{where}: duplicate child pid')
        else:
            child_pids.append(pid)
        if rec.get('reported_pid') != pid:
            reasons.append(f'{where}: reported pid identity mismatch')
        if rec.get('reported_ppid') != parent_pid:
            reasons.append(f'{where}: child ppid is not the probe process')
        if rec.get('elements') != request['elements']:
            reasons.append(f'{where}: elements mismatch')
        for key in ('create_rc', 'load_rc', 'unload_rc', 'destroy_rc'):
            if rec.get(key) != 0:
                reasons.append(f'{where}: {key}={rec.get(key)}')
        if rec.get('cycles') != request['child_cycles']:
            reasons.append(f'{where}: child cycles mismatch')
        detail = rec.get('cycles_detail')
        if not isinstance(detail, list) or len(detail) != request['child_cycles']:
            reasons.append(f'{where}: child cycles_detail incomplete')
        else:
            for c, cyc in enumerate(detail):
                reasons.extend(_check_cycle(f'{where}.cycle{c}', cyc, c,
                             rec.get('elements'),
                             (ROLE_CHILD, i + 1, 0, 0), all_seeds))
        expected_ops = request['child_cycles'] * 3
        if rec.get('allocs') != expected_ops or rec.get('frees') != expected_ops:
            reasons.append(f'{where}: child alloc/free counts mismatch')
        if rec.get('mismatches') != 0:
            reasons.append(f'{where}: child mismatches={rec.get("mismatches")}')
        keys = ('fork_ns', 'release_ns', 'firstbyte_ns', 'wait_ns',
                'start_ns', 'end_ns')
        nums = {k: rec.get(k) for k in keys}
        if not all(_is_int(v) for v in nums.values()):
            reasons.append(f'{where}: timing/identity fields incomplete')
        else:
            if not (0 < nums['fork_ns'] <= nums['release_ns']
                    <= nums['start_ns'] < nums['end_ns']
                    <= nums['wait_ns']):
                reasons.append(f'{where}: child timing order broken')
            if not (nums['start_ns'] <= nums['firstbyte_ns']
                    <= nums['wait_ns']):
                reasons.append(f'{where}: first-byte timing implausible')
            child_intervals.append((nums['start_ns'], nums['end_ns']))
    if request['children'] >= ngpu and child_uuid_seen != set(expected_uuids):
        reasons.append('child phase did not cover every requested GPU UUID')
    window = _overlap(reasons, 'child', child_intervals, request['children'])
    verdict['overlap_ns']['child'] = window
    if children and all(_is_int(r.get('fork_ns')) and _is_int(r.get('wait_ns'))
                        for r in children) and len(children) == request['children']:
        if max(r['fork_ns'] for r in children) >= \
                min(r['wait_ns'] for r in children):
            reasons.append('children were reaped before every launch: '
                           'serial execution cannot prove coexistence')

    # ---- run-wide seed namespace completeness (covers the exec children
    # whose process-local counters reset to zero) ------------------------
    expected_seed_records = (
        ngpu * request['ctx_rounds'] * request['cycles']
        + request['threads'] * request['cycles']
        + ngpu * request['cycles']
        + request['children'] * request['child_cycles'])
    if len(all_seeds) != expected_seed_records:
        reasons.append(f'distinct derived seeds {len(all_seeds)} != '
                       f'expected {expected_seed_records}')
    reused = sorted(s for s, n in all_seeds.items() if n > 1)
    if reused:
        reasons.append(f'{len(reused)} seed(s) reused across phases/'
                       'processes/rounds')

    # ---- summary: consistency only, never authority --------------------
    summaries = by_type.get('summary', [])
    if len(summaries) != 1:
        reasons.append(f'summary records: expected 1, got {len(summaries)}')
    else:
        if records[-1][1] is not summaries[0]:
            reasons.append('summary is not the final record')
        s = summaries[0]
        if s.get('status') != 'pass':
            reasons.append('summary status is not pass')
        if s.get('schema') != 1 or s.get('device_count') != ngpu:
            reasons.append('summary schema/device_count mismatch')
        for key in ('cycles', 'child_cycles', 'ctx_rounds', 'threads',
                    'children', 'elements'):
            if s.get(key) != request[key]:
                reasons.append(f'summary {key}={s.get(key)} does not match '
                               f'request {request[key]}')
        if s.get('pid') != parent_pid:
            reasons.append('summary pid does not match meta pid')

    if not reasons:
        verdict['ok'] = True
    return verdict


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('log', type=pathlib.Path)
    p.add_argument('--expect-uuid', action='append', default=[],
                   help='expected GPU UUID (repeat or comma separated)')
    p.add_argument('--expect-uuid-from-log', type=pathlib.Path,
                   help='take the expected UUID set from another probe log '
                        '(e.g. the native-before inventory)')
    p.add_argument('--threads', type=int, default=DEFAULT_REQUEST['threads'])
    p.add_argument('--children', type=int)
    p.add_argument('--cycles', type=int, default=DEFAULT_REQUEST['cycles'])
    p.add_argument('--child-cycles', type=int,
                   default=DEFAULT_REQUEST['child_cycles'])
    p.add_argument('--elements', type=int, default=DEFAULT_REQUEST['elements'])
    p.add_argument('--ctx-rounds', type=int,
                   default=DEFAULT_REQUEST['ctx_rounds'])
    a = p.parse_args(argv)
    uuids = []
    for item in a.expect_uuid:
        uuids.extend(u for u in item.split(',') if u)
    if a.expect_uuid_from_log:
        uuids = inventory_uuids(a.expect_uuid_from_log.read_text())
    request = {
        'uuids': uuids,
        'threads': a.threads,
        'cycles': a.cycles,
        'child_cycles': a.child_cycles,
        'elements': a.elements,
        'ctx_rounds': a.ctx_rounds,
    }
    if a.children is not None:
        request['children'] = a.children
    verdict = analyze(a.log.read_text(), request)
    print(json.dumps(verdict, indent=2))
    return 0 if verdict['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
