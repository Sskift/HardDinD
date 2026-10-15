"""Independent strict checker for cuda_cuda_lifecycle_probe JSONL.

Verdict is derived from individual records, never the summary.

Parity mode: pass --native-reference <native.jsonl> when checking a guest
log (owner always runs native-before and native-after).  The reference
inventory must match the requested UUIDs and every ordered peer pair's
can_access must agree; a guest false against a native true fails the run.

It covers:

  * inventory equals the requested GPU set (uuids, ordinal order);
  * solo: repeated alloc/HtoD/kernel/DtoH/free iterations per card with
    bytes-checked accounting and exact context create/destroy cleanup;
  * thread: four INDEPENDENT contexts across cards with positive
    measured overlap and balanced allocations;
  * child: four fresh-exec processes with complete handshake timing,
    distinct pids, positive overlap, exact per-cycle results;
  * peer: the FULL ordered (i,j), i!=j matrix of cuDeviceCanAccessPeer;
    accessible pairs must complete an HtoD/DtoD/DtoH byte-checked
    transfer; inaccessible pairs are recorded as supported=false
    (honest unsupported evidence, never skipped as pass);
  * syscall/CUDA status fields are all zero on success paths.

Run:
  python3 cuda_lifecycle_checks.py <probe.stdout> \
      [--uuids GPU-a,GPU-b,...] [--poll-ms N]
"""
import argparse
import json
import pathlib
import sys

DEFAULT = {'iterations': 8, 'thread_cycles': 4, 'child_cycles': 2,
           'threads': 4, 'children': 4, 'elements': 1 << 18}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _cycle(reasons, where, cyc, role, proc, worker, seed_ns=True,
           expect_idx=None):
    if cyc.get('role') != role:
        reasons.append(f'{where}: role {cyc.get("role")} != {role}')
    if cyc.get('proc') != proc:
        reasons.append(f'{where}: proc {cyc.get("proc")} != {proc}')
    if cyc.get('worker') != worker:
        reasons.append(f'{where}: worker mismatch')
    if expect_idx is not None and cyc.get('cycle') != expect_idx:
        reasons.append(f'{where}: cycle index mismatch')
    for key in ('htod_rc', 'launch_rc', 'sync_rc', 'dtoh_rc'):
        if cyc.get(key) != 0:
            reasons.append(f'{where}: {key}={cyc.get(key)}')
    al = cyc.get('alloc_rc', [])
    fr = cyc.get('free_rc', [])
    if al != [0, 0, 0] or fr != [0, 0, 0]:
        reasons.append(f'{where}: alloc/free rc nonzero')
    if cyc.get('mismatches') != 0:
        reasons.append(f'{where}: mismatches {cyc.get("mismatches")}')
    if not _is_int(cyc.get('csum')):
        reasons.append(f'{where}: bad csum')
    if seed_ns:
        s, e = cyc.get('start_ns'), cyc.get('end_ns')
        if not _is_int(s) or not _is_int(e) or not (0 <= s < e):
            reasons.append(f'{where}: non-monotonic cycle interval')


def _overlap(reasons, where, intervals, minimum):
    if len(intervals) != minimum:
        reasons.append(f'{where}: expected {minimum} intervals, got '
                       f'{len(intervals)}')
        return
    window = min(e for s, e in intervals) - max(s for s, e in intervals)
    if window <= 0:
        reasons.append(f'{where}: no positive coexistence '
                       f'(overlap_ns={window})')


def analyze(content, request=None):
    request = request or {}
    expected_uuids = request.get('uuids') or []
    defaults = dict(DEFAULT)
    defaults.update({k: v for k, v in request.items()
                     if k in defaults})
    iterations = defaults['iterations']
    thread_cycles = defaults['thread_cycles']
    child_cycles = defaults['child_cycles']
    threads = defaults['threads']
    children = defaults['children']
    elements = defaults['elements']
    reasons = []
    verdict = {'ok': False, 'reasons': reasons}

    records = []
    for n, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            reasons.append(f'line {n}: invalid JSON')
            continue
        r['_line'] = n
        records.append(r)

    by = {}
    for r in records:
        by.setdefault(r.get('type'), []).append(r)

    for r in by.get('error', []):
        reasons.append(f'line {r["_line"]}: error {r.get("stage")} '
                       f'{r.get("message", "")}')

    ngpu = len(expected_uuids)

    # ---- meta ----
    metas = by.get('meta', [])
    if len(metas) != 1:
        reasons.append(f'one meta record, got {len(metas)}')
    else:
        m = metas[0]
        for k, v in (('init_rc', 0), ('iterations', iterations),
                     ('thread_cycles', thread_cycles),
                     ('child_cycles', child_cycles),
                     ('threads', threads), ('children', children),
                     ('elements', elements), ('bytes', elements * 4)):
            if m.get(k) != v:
                reasons.append(f'meta {k}={m.get(k)} != {v}')
        if m.get('bytes') != 1 << 20:
            reasons.append('meta workload must be exactly 1 MiB')

    # ---- inventory ----
    inv = by.get('inventory', [])
    if ngpu:
        if len(inv) != ngpu:
            reasons.append(f'inventory records {len(inv)} != {ngpu}')
        seen = []
        for i, r in enumerate(inv):
            where = f'inventory[{i}]'
            if r.get('ordinal') != i:
                reasons.append(f'{where}: ordinal order mismatch')
            if r.get('uuid_valid') is not True:
                reasons.append(f'{where}: uuid_valid not true')
            if not isinstance(r.get('name'), str) or not r['name']:
                reasons.append(f'{where}: name missing')
            if not _is_int(r.get('total_mem')) or r['total_mem'] <= 0:
                reasons.append(f'{where}: total_mem invalid')
            seen.append(r.get('uuid'))
        if seen != list(expected_uuids):
            reasons.append('inventory uuid set/order != requested set')
        if len(set(seen)) != len(seen):
            reasons.append('duplicate inventory UUID')

    # ---- solo ----
    solo = [r for r in by.get('worker', []) if r.get('phase') == 'solo']
    solo_uuids = set()
    if len(solo) != ngpu:
        reasons.append(f'solo workers {len(solo)} != {ngpu}')
    for i, r in enumerate(solo):
        where = f'solo[{i}]'
        for k in ('create_rc', 'unload_rc', 'destroy_rc'):
            if r.get(k) != 0:
                reasons.append(f'{where}: {k}={r.get(k)}')
        if r.get('iterations') != iterations:
            reasons.append(f'{where}: iterations mismatch')
        if r.get('elements') != elements:
            reasons.append(f'{where}: elements mismatch')
        ops = iterations * 3
        if r.get('allocs') != ops or r.get('frees') != ops:
            reasons.append(f'{where}: alloc/free counts must be '
                           f'{ops}/{ops}')
        if r.get('bytes_checked') != iterations * elements * 4:
            reasons.append(f'{where}: bytes_checked mismatch')
        if r.get('mismatches') != 0:
            reasons.append(f'{where}: mismatches {r.get("mismatches")}')
        if r.get('info_enter_rc') != 0 or r.get('info_exit_rc') != 0:
            reasons.append(f'{where}: MemGetInfo rc nonzero')
        s, e = r.get('start_ns'), r.get('end_ns')
        if not _is_int(s) or not _is_int(e) or not (0 <= s < e):
            reasons.append(f'{where}: interval invalid')
        solo_uuids.add(r.get('uuid'))
    if ngpu and solo_uuids != set(expected_uuids):
        reasons.append('solo did not cover every requested GPU')

    # ---- thread: independent contexts, one card each ----
    th = [r for r in by.get('worker', []) if r.get('phase') == 'thread']
    tintervals = []
    if len(th) != threads:
        reasons.append(f'thread workers {len(th)} != {threads}')
    tids, tords, tuuids = set(), set(), set()
    for i, r in enumerate(th):
        where = f'thread[{i}]'
        for k in ('create_rc', 'load_rc', 'unload_rc', 'destroy_rc'):
            if r.get(k) != 0:
                reasons.append(f'{where}: {k}={r.get(k)}')
        if r.get('cycles') != thread_cycles:
            reasons.append(f'{where}: cycles mismatch')
        ops = thread_cycles * 3
        if r.get('allocs') != ops or r.get('frees') != ops:
            reasons.append(f'{where}: alloc/free counts mismatch')
        if r.get('bytes_checked') != thread_cycles * elements * 4:
            reasons.append(f'{where}: bytes_checked mismatch')
        if r.get('mismatches') != 0:
            reasons.append(f'{where}: mismatches')
        if r.get('tid') in tids or not _is_int(r.get('tid')):
            reasons.append(f'{where}: duplicate/invalid tid')
        tids.add(r.get('tid'))
        tords.add(r.get('ordinal'))
        tuuids.add(r.get('uuid'))
        rd, s, e = r.get('ready_ns'), r.get('start_ns'), r.get('end_ns')
        if not all(_is_int(x) for x in (rd, s, e)) or not (rd <= s < e):
            reasons.append(f'{where}: timing order broken')
        tintervals.append((s, e))
    if th:
        _overlap(reasons, 'thread', tintervals, threads)
        if len(tords) != min(threads, ngpu) or tords != set(range(ngpu)) \
                and ngpu:
            # threads must cover each card once when threads == ngpu
            if threads == ngpu and tords != set(range(ngpu)):
                reasons.append('thread workers must cover every card once')
        if ngpu and tuuids != set(expected_uuids):
            reasons.append('thread phase did not cover every GPU UUID')

    # ---- child ----
    ch = [r for r in by.get('worker', []) if r.get('phase') == 'child']
    cint = []
    if len(ch) != children:
        reasons.append(f'child workers {len(ch)} != {children}')
    pids = set()
    chuuids = set()
    for i, r in enumerate(ch):
        where = f'child[{i}]'
        if r.get('worker') != i:
            reasons.append(f'{where}: worker id mismatch')
        if r.get('received') is not True:
            reasons.append(f'{where}: report not received')
        if r.get('exec_errno') != 0:
            reasons.append(f'{where}: exec error')
        if r.get('child_exit') != 0:
            reasons.append(f'{where}: exit {r.get("child_exit")}')
        if r.get('child_signal') not in (0, None):
            reasons.append(f'{where}: killed by signal')
        if r.get('killed') is True:
            reasons.append(f'{where}: parent had to SIGKILL')
        pid = r.get('child_pid')
        if not _is_int(pid) or pid <= 0 or pid in pids:
            reasons.append(f'{where}: duplicate/invalid pid')
        pids.add(pid)
        if r.get('reported_pid') != pid or r.get('reported_ppid') != \
                (ch and metas[0].get('pid')):
            reasons.append(f'{where}: reported identity mismatch')
        for k in ('create_rc', 'load_rc', 'unload_rc', 'destroy_rc'):
            if r.get(k) != 0:
                reasons.append(f'{where}: {k}={r.get(k)}')
        if r.get('cycles') != child_cycles or \
                r.get('elements') != elements:
            reasons.append(f'{where}: cycles/elements mismatch')
        if r.get('allocs') != child_cycles * 3 or \
                r.get('frees') != child_cycles * 3:
            reasons.append(f'{where}: alloc/free counts mismatch')
        if r.get('mismatches') != 0:
            reasons.append(f'{where}: mismatches')
        t = (r.get('fork_ns'), r.get('ready_ns'), r.get('release_ns'),
             r.get('start_ns'), r.get('end_ns'), r.get('firstbyte_ns'),
             r.get('wait_ns'))
        if not all(_is_int(x) for x in t):
            reasons.append(f'{where}: timing fields incomplete')
        else:
            f0, rd, rl, s, e, fb, w = t
            if not (0 < f0 <= rd <= rl <= s < e <= fb <= w):
                reasons.append(f'{where}: handshake timing order broken')
            cint.append((s, e))
        detail = r.get('cycles_detail')
        if not isinstance(detail, list) or len(detail) != child_cycles:
            reasons.append(f'{where}: cycles_detail incomplete')
        else:
            for c, cyc in enumerate(detail):
                _cycle(reasons, f'{where}.cycle{c}', cyc, 3, i + 1, 0,
                       expect_idx=c)
            if detail[0].get('start_ns', 0) < r.get('release_ns', 0):
                reasons.append(f'{where}: compute started pre-release')
        chuuids.add(r.get('uuid'))
    if ch:
        _overlap(reasons, 'child', cint, children)
        if ngpu and chuuids != set(expected_uuids):
            reasons.append('child phase did not cover every GPU UUID')

    # ---- peer matrix ----
    peers = by.get('peer', [])
    expected_pairs = ngpu * (ngpu - 1) if ngpu else None
    if expected_pairs is not None:
        if len(peers) != expected_pairs:
            reasons.append(f'peer records {len(peers)} != '
                           f'{expected_pairs} ordered pairs')
        pairset = set()
        accessible = 0
        for r in peers:
            a, b = r.get('from'), r.get('to')
            where = f'peer[{a}->{b}]'
            if not (_is_int(a) and _is_int(b) and a != b
                    and 0 <= a < ngpu and 0 <= b < ngpu):
                reasons.append(f'{where}: bad pair indices')
                continue
            if (a, b) in pairset:
                reasons.append(f'{where}: duplicate pair')
            pairset.add((a, b))
            if r.get('can_rc') != 0:
                reasons.append(f'{where}: can_access query failed')
            can = r.get('can_access')
            if not isinstance(can, bool):
                reasons.append(f'{where}: can_access must be bool')
                continue
            supported = r.get('supported')
            if supported is not can:
                reasons.append(f'{where}: supported must equal can_access')
            if r.get('create_a_rc') != 0 or r.get('create_b_rc') != 0:
                reasons.append(f'{where}: context create rc nonzero')
            if r.get('destroy_a_rc') != 0 or r.get('destroy_b_rc') != 0:
                reasons.append(f'{where}: context destroy rc nonzero')
            if can:
                accessible += 1
                if r.get('enable_rc') != 0:
                    reasons.append(f'{where}: enable peer access rc '
                                   f'{r.get("enable_rc")}')
                for k in ('htod_rc', 'dto_d_rc', 'dtoh_rc'):
                    if r.get(k) != 0:
                        reasons.append(f'{where}: {k}={r.get(k)}')
                if r.get('mismatches') != 0:
                    reasons.append(f'{where}: transfer mismatches')
                if r.get('bytes') != elements * 4:
                    reasons.append(f'{where}: transferred bytes mismatch')
                if not _is_int(r.get('csum')):
                    reasons.append(f'{where}: csum invalid')
            else:
                # Honest unsupported: enable/transfer fields must be null,
                # never a zero that could fake a skipped-as-pass success.
                for k in ('enable_rc', 'htod_rc', 'dto_d_rc', 'dtoh_rc'):
                    if r.get(k) is not None:
                        reasons.append(f'{where}: unsupported pair must '
                                       f'carry null {k}')
                if r.get('mismatches') != 0 or r.get('bytes') != 0:
                    reasons.append(f'{where}: unsupported pair must report '
                                   'no transfer work')
            s, e = r.get('start_ns'), r.get('end_ns')
            if not _is_int(s) or not _is_int(e) or not (s < e):
                reasons.append(f'{where}: interval invalid')
        if expected_pairs and pairset and \
                pairset != {(i, j) for i in range(ngpu)
                            for j in range(ngpu) if i != j}:
            reasons.append('peer matrix incomplete')
        verdict['peer_accessible_pairs'] = accessible
        verdict['peer_total_pairs'] = expected_pairs

    # ---- native reference parity (owner always supplies native-before) ----
    ref_path = request.get('native_reference')
    if ref_path:
        try:
            ref_text = pathlib.Path(ref_path).read_text()
        except OSError:
            reasons.append(f'native reference unreadable: {ref_path}')
            ref_text = ''
        ref_recs = []
        for ln, line in enumerate(ref_text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                ref_recs.append(json.loads(line))
            except ValueError:
                reasons.append(f'native reference line {ln}: invalid JSON')
        ref_inv = [r.get('uuid') for r in ref_recs
                   if r.get('type') == 'inventory']
        ref_peers = {}
        for r in ref_recs:
            if r.get('type') == 'peer':
                ref_peers[(r.get('from'), r.get('to'))] = r
        # Strict UUID equality with the reference inventory.
        if ngpu:
            if ref_inv != list(expected_uuids):
                reasons.append('native reference inventory does not match '
                               'the requested UUID set/order')
            expected_pairset = {(i, j) for i in range(ngpu)
                                for j in range(ngpu) if i != j}
            if set(ref_peers) != expected_pairset:
                reasons.append('native reference peer matrix incomplete')
            cur = {(r.get('from'), r.get('to')): r for r in peers}
            for pair in sorted(expected_pairset):
                rr = ref_peers.get(pair)
                gg = cur.get(pair)
                where = f'peer-parity[{pair[0]}->{pair[1]}]'
                if rr is None or gg is None:
                    reasons.append(f'{where}: missing pair in a phase')
                    continue
                if rr.get('can_rc') != 0:
                    reasons.append(f'{where}: native can_access query '
                                   'failed')
                ncan, gcan = rr.get('can_access'), gg.get('can_access')
                if not isinstance(ncan, bool) or not isinstance(gcan, bool):
                    reasons.append(f'{where}: can_access must be bool in '
                                   'both logs')
                    continue
                if ncan and not gcan:
                    reasons.append(f'{where}: native can access peer but '
                                   'guest cannot (parity regression)')
                if gcan and not ncan:
                    reasons.append(f'{where}: guest claims peer access the '
                                   'native host does not provide')
                # An accessible pair must actually complete a transfer in
                # BOTH logs; unsupported pairs never count as transfers.
                for phase, rec in (('native', rr), ('guest', gg)):
                    if rec.get('can_access'):
                        for k in ('htod_rc', 'dto_d_rc', 'dtoh_rc'):
                            if rec.get(k) != 0:
                                reasons.append(
                                    f'{where}: {phase} accessible pair '
                                    f'missing successful {k}')
                        if rec.get('mismatches') != 0 or \
                                rec.get('bytes') != elements * 4:
                            reasons.append(
                                f'{where}: {phase} accessible pair has no '
                                'byte-checked transfer')
                    else:
                        for k in ('htod_rc', 'dto_d_rc', 'dtoh_rc',
                                  'enable_rc'):
                            if rec.get(k) is not None:
                                reasons.append(
                                    f'{where}: {phase} unsupported pair '
                                    f'must carry null {k}')

    # ---- summary ----
    summaries = by.get('summary', [])
    if len(summaries) != 1:
        reasons.append(f'one summary, got {len(summaries)}')
    elif records[-1].get('type') != 'summary':
        reasons.append('summary must be last')
    else:
        s = summaries[0]
        if s.get('status') != 'pass' or s.get('device_count') != ngpu:
            reasons.append('summary not clean')

    verdict['ok'] = not reasons
    return verdict


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('log', type=argparse.FileType('r'))
    p.add_argument('--uuids', default='',
                   help='comma-separated expected GPU UUIDs in ordinal order')
    p.add_argument('--iterations', type=int, default=DEFAULT['iterations'])
    p.add_argument('--thread-cycles', type=int,
                   default=DEFAULT['thread_cycles'])
    p.add_argument('--child-cycles', type=int,
                   default=DEFAULT['child_cycles'])
    p.add_argument('--threads', type=int, default=DEFAULT['threads'])
    p.add_argument('--children', type=int, default=DEFAULT['children'])
    p.add_argument('--elements', type=int, default=DEFAULT['elements'])
    p.add_argument('--native-reference', default=None,
                   help='native-before JSONL; enforces peer parity')
    a = p.parse_args(argv)
    uuids = [u for u in a.uuids.split(',') if u]
    v = analyze(a.log.read(), {
        'uuids': uuids, 'iterations': a.iterations,
        'thread_cycles': a.thread_cycles,
        'child_cycles': a.child_cycles, 'threads': a.threads,
        'children': a.children, 'elements': a.elements,
        'native_reference': a.native_reference})
    print(json.dumps(v, indent=2))
    return 0 if v['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
