"""Independent strict checker for gpu_os_event_probe v2 JSONL output.

Two failure channels are kept distinct on every record:

  * syscall channel: ioctl(2) rc / errno (e.g. wrong payload size ->
    -1/EINVAL, NV01_ROOT alloc on the wrong device node);
  * embedded channel: ioctl rc 0 with the RM Status inside the payload.

The verdict is derived from individual records; the summary status is
never trusted.  Layout mode validates --layout-selfcheck output.
"""
import argparse
import json
import sys

ST_OK = 0
ST_INSUFFICIENT_RESOURCES = 0x1A
ST_INVALID_EVENT = 0x28
ST_OPERATING_SYSTEM = 0x59

PINNED_LAYOUT = {
    'nv_os_event': {'size': 16, 'offsets': {'hClient': 0, 'hDevice': 4,
                                            'tag': 8, 'Status': 12}},
    'nv_os41': {'size': 16, 'offsets': {'pEvent': 0, 'MoreEvents': 8,
                                        'status': 12}},
    'nv64': 48,
    'nv00': 16,
    'escapes': {'ALLOC_ROOT': 43, 'FREE': 41,
                'ALLOC_OS_EVENT': 206, 'FREE_OS_EVENT': 207,
                'RM_GET_EVENT_DATA': 82},
    'device_chain': {
        'root_alloc': '/dev/nvidiactl NV_CTL_DEVICE_ONLY',
        'os_event': 'gpu char node, opaque numeric tag'},
    'status': {'OK': 0, 'INSUFFICIENT_RESOURCES': 0x1A,
               'INVALID_ARGUMENT': 0x1F, 'INVALID_EVENT': 0x28,
               'OPERATING_SYSTEM': 0x59},
}

# Per-event-case expectations: (op, embedded status, hDevice)
EVENT_EXPECT = {
    'alloc_free': [('alloc', ST_OK, 0), ('free', ST_OK, 0)],
    'duplicate_alloc': [('alloc', ST_OK, 0),
                        ('alloc', ST_INSUFFICIENT_RESOURCES, 0),
                        ('free', ST_OK, 0)],
    'missing_free': [('free', ST_INVALID_EVENT, 0)],
    'double_free': [('alloc', ST_OK, 0), ('free', ST_OK, 0),
                    ('free', ST_INVALID_EVENT, 0)],
    'opaque_tag_not_fd': [('alloc', ST_OK, 0), ('free', ST_OK, 0)],
    # The two allocations come from distinct root clients via separate
    # GPU nvfp opens; the free intentionally reports hDevice 0 (ignored).
    'two_independent_clients': [('alloc', ST_OK, 0), ('alloc', ST_OK, 0),
                                ('free', ST_OK, 0), ('free', ST_OK, 0)],
    # Alloc deliberately sends a bogus hDevice; free sends 0: both ignored.
    'hdevice_ignored': [('alloc', ST_OK, 0xDEADBEEF),
                        ('free', ST_OK, 0x00000000)],
}
ROOT_CLIENTS = 9
EXPECTED_OPENS = {'ctl', 'gpu_blocking', 'gpu_nonblock'}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def analyze_layout(content):
    reasons = []
    records = [json.loads(l) for l in content.splitlines() if l.strip()]
    if len(records) != 2 or records[0].get('mode') != 'layout_selfcheck':
        return {'ok': False, 'mode': 'layout',
                'reasons': ['expected meta(layout) + single layout record']}
    layout = records[1]
    if layout.get('type') != 'layout':
        reasons.append('second record is not layout')
    for key, want in PINNED_LAYOUT.items():
        if layout.get(key) != want:
            reasons.append(f'layout {key} mismatch')
    return {'ok': not reasons, 'mode': 'layout', 'reasons': reasons}


def analyze(content, request=None):
    request = request or {}
    device = request.get('device', '/dev/nvidia0')
    ctl_device = request.get('ctl_device', '/dev/nvidiactl')
    poll_ms = int(request.get('poll_ms', 30))
    reasons = []
    verdict = {'ok': False, 'mode': 'native_abi', 'reasons': reasons,
               'cases_seen': []}

    records = []
    for n, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            reasons.append(f'line {n}: invalid JSON')
            continue
        rec['_line'] = n
        records.append(rec)

    by_type = {}
    for r in records:
        by_type.setdefault(r.get('type'), []).append(r)

    for r in by_type.get('fatal', []):
        reasons.append(f'line {r["_line"]}: fatal {r.get("stage", "")}')
    for r in by_type.get('error', []):
        reasons.append(f'line {r["_line"]}: error {r.get("stage", "")}')
    for r in by_type.get('skip', []):
        reasons.append(f'line {r["_line"]}: skipped case '
                       f'{r.get("name")} ({r.get("reason")})')

    # ---- meta ----
    metas = by_type.get('meta', [])
    if len(metas) != 1:
        reasons.append(f'exactly one meta record, got {len(metas)}')
    else:
        m = metas[0]
        if m.get('schema') != 1 or m.get('mode') != 'native_abi':
            reasons.append('meta schema/mode invalid')
        if m.get('device') != device:
            reasons.append(f'meta device {m.get("device")} != {device}')
        if m.get('ctl_device') != ctl_device:
            reasons.append('meta ctl_device mismatch')
        if m.get('driver') != '550.120':
            reasons.append('meta driver is not 550.120')
        if 'NV_CTL_DEVICE_ONLY' not in str(m.get('device_chain', '')):
            reasons.append('meta device_chain must document ctl-only root')

    # ---- opens / closes ----
    opens = by_type.get('open', [])
    roles = {o.get('role') for o in opens}
    if roles != EXPECTED_OPENS or len(opens) != 3:
        reasons.append(f'expected opens for {sorted(EXPECTED_OPENS)}, '
                       f'got {sorted(roles)}')
    for o in opens:
        if o.get('syscall', {}).get('rc') != 0 or not _is_int(o.get('fd')) \
                or o.get('fd') < 0:
            reasons.append(f'open {o.get("role")} failed')
    gpu_fds = {o['role']: o['fd'] for o in opens
               if o.get('role') in EXPECTED_OPENS}
    closes = by_type.get('close', [])
    if len(closes) != 3:
        reasons.append(f'expected 3 closes, got {len(closes)}')
    else:
        if {c.get('fd') for c in closes} != set(gpu_fds.values()):
            reasons.append('closed fds do not match opened fds')

    # ---- tags ----
    tags = by_type.get('tags', [])
    if tags:
        reasons.append('legacy v1 tags record present; v2 uses open records')
    # Opaque tags are declared by their cases only; the non-fd property is
    # checked per event record below.

    # ---- root clients: raw NVOS64 alloc on ctl, distinct nonzero ----
    allocs = by_type.get('root_alloc', [])
    frees = by_type.get('root_free', [])
    client_of_case = {}
    if len(allocs) != ROOT_CLIENTS:
        reasons.append(f'expected {ROOT_CLIENTS} root_alloc records, got '
                       f'{len(allocs)}')
    for a in allocs:
        where = f'root_alloc[{a.get("index")}]'
        sysc = a.get('syscall', {})
        if a.get('node') != ctl_device:
            reasons.append(f'{where}: root alloc must use {ctl_device}')
        if sysc.get('rc') != 0 or sysc.get('errno') != 0:
            reasons.append(f'{where}: syscall failure {sysc}')
        if a.get('embeddedStatus') != ST_OK:
            reasons.append(f'{where}: embedded status not OK')
        ret = a.get('returned', {})
        handle = ret.get('object')
        if not _is_int(handle) or handle == 0:
            reasons.append(f'{where}: returned object handle must be '
                           'nonzero (NV01_ROOT handle comes back in '
                           'hObjectNew, not hRoot)')
        if a.get('client_handle_field') != 'object':
            reasons.append(f'{where}: client_handle_field must be object')
        if a.get('client_handle') != handle:
            reasons.append(f'{where}: client_handle != returned.object')
        if a.get('match') is not True:
            reasons.append(f'{where}: probe did not self-match')
        name = a.get('case')
        if name in client_of_case:
            reasons.append(f'{where}: duplicate case name {name}')
        client_of_case[name] = handle
    handles = [h for h in client_of_case.values() if _is_int(h)]
    if len(set(handles)) != len(handles):
        reasons.append('root clients must be distinct handles')
    if handles and min(handles) <= 0:
        reasons.append('root client handles must be nonzero')

    if len(frees) != ROOT_CLIENTS:
        reasons.append(f'expected {ROOT_CLIENTS} root_free records, got '
                       f'{len(frees)}')
    freed = set()
    for f in frees:
        where = f'root_free {f.get("case")}'
        if f.get('node') != ctl_device:
            reasons.append(f'{where}: root free must use {ctl_device}')
        if f.get('syscall', {}).get('rc') != 0 or \
                f.get('syscall', {}).get('errno') != 0:
            reasons.append(f'{where}: syscall failure')
        if f.get('embeddedStatus') != ST_OK or f.get('match') is not True:
            reasons.append(f'{where}: embedded status not OK')
        c = f.get('client')
        if not _is_int(c) or c <= 0:
            reasons.append(f'{where}: bad client')
        freed.add(c)
    if freed != set(handles):
        reasons.append('freed root clients do not match allocated set')

    # ---- event channel records ----
    events = by_type.get('event', [])
    grouped = {}
    for r in events:
        grouped.setdefault(r.get('name'), []).append(r)
    verdict['cases_seen'] = sorted(grouped)

    for name, expected in EVENT_EXPECT.items():
        got = grouped.get(name)
        if got is None:
            reasons.append(f'missing case {name}')
            continue
        if len(got) != len(expected):
            reasons.append(f'{name}: {len(got)} records, expected '
                           f'{len(expected)}')
            continue
        for i, (rec, (op, st, hdev)) in enumerate(zip(got, expected)):
            where = f'{name}[{i}]'
            sysc = rec.get('syscall', {})
            if rec.get('op') != op:
                reasons.append(f'{where}: op mismatch')
            if sysc.get('rc') != 0 or sysc.get('errno') != 0:
                reasons.append(f'{where}: syscall failure {sysc}')
            if rec.get('embeddedStatus') != st:
                reasons.append(f'{where}: embeddedStatus '
                               f'{rec.get("embeddedStatus")} != {st}')
            if rec.get('match') is not True:
                reasons.append(f'{where}: probe did not self-match')
            req = rec.get('request', {})
            if req.get('nr') != (206 if op == 'alloc' else 207):
                reasons.append(f'{where}: wrong ioctl nr')
            if req.get('size') != 16:
                reasons.append(f'{where}: wrong size')
            if req.get('hDevice') != hdev:
                reasons.append(f'{where}: hDevice {req.get("hDevice")} != '
                               f'{hdev} (intended field value)')
            if not _is_int(req.get('hClient')) or req.get('hClient') <= 0:
                reasons.append(f'{where}: hClient must be a nonzero root '
                               'client handle')
            expected_client = client_of_case.get(
                'two_independent_clients_a' if
                (name == 'two_independent_clients' and i in (0, 2)) else
                'two_independent_clients_b' if
                (name == 'two_independent_clients' and i in (1, 3)) else
                name)
            if expected_client and req.get('hClient') != expected_client:
                reasons.append(f'{where}: hClient does not match its '
                               'allocated root client')

    # Opaque tags must never be open descriptors.
    for name in ('opaque_tag_not_fd', 'two_independent_clients',
                 'hdevice_ignored'):
        for rec in grouped.get(name, []):
            if rec['request'].get('tag_is_open_fd') is not False:
                reasons.append(f'{name}: opaque tag resolved to an open fd '
                               'or lookup failed (null)')
    # The gpu-char-fd tag case must genuinely reference an open fd.
    af = grouped.get('alloc_free', [])
    if af and af[0]['request'].get('tag_is_open_fd') is not True:
        reasons.append('alloc_free tag must be the open GPU char fd number')

    # Two independent clients: distinct handles, one shared tag, both nvfp.
    tic = grouped.get('two_independent_clients', [])
    if tic:
        allocs_t = [r for r in tic if r.get('op') == 'alloc']
        hclients = [r['request'].get('hClient') for r in allocs_t]
        tags = {r['request'].get('tag') for r in allocs_t}
        if len(allocs_t) == 2:
            if len(set(hclients)) != 2:
                reasons.append('two_independent_clients needs distinct '
                               'hClient values')
            if tags != {0x4F530002}:
                reasons.append('two_independent_clients must share one tag')

    # ---- GET_EVENT_DATA ----
    ged = by_type.get('get_event_data', [])
    if len(ged) != 1:
        reasons.append(f'exactly one get_event_data, got {len(ged)}')
    else:
        g = ged[0]
        if g.get('syscall', {}).get('rc') != 0:
            reasons.append('get_event_data syscall failure')
        if g.get('embeddedStatus') != ST_OPERATING_SYSTEM:
            reasons.append('empty-queue embeddedStatus must be 0x59')
        if g.get('moreEvents') != 0:
            reasons.append('MoreEvents must be 0')
        if g.get('output_buffer_touched') is not False:
            reasons.append('sentinel output buffer was touched')
        if g.get('request', {}).get('output_sentinel') != 'a5':
            reasons.append('output buffer must use the nonzero a5 sentinel')
        if g.get('match') is not True:
            reasons.append('get_event_data did not self-match')

    # ---- poll matrix: timeout is honored regardless of O_NONBLOCK ----
    polls = {p.get('name'): p for p in by_type.get('poll', [])}
    matrix = (('poll_blocking_30', False, poll_ms, True),
              ('poll_blocking_0', False, 0, False),
              ('poll_nonblock_30', True, poll_ms, True),
              ('poll_nonblock_0', True, 0, False))
    for name, want_nb, want_to, must_wait in matrix:
        p = polls.get(name)
        if p is None:
            reasons.append(f'missing {name}')
            continue
        if p.get('fd_nonblock') != want_nb:
            reasons.append(f'{name}: fd_nonblock mismatch')
        if p.get('timeout_ms') != want_to:
            reasons.append(f'{name}: timeout_ms mismatch')
        if p.get('syscall', {}).get('rc') != 0:
            reasons.append(f'{name}: poll failed')
        if p.get('revents') != 0 or p.get('empty') is not True:
            reasons.append(f'{name}: spurious readiness')
        el = p.get('elapsed_ns')
        if not _is_int(el) or el < 0:
            reasons.append(f'{name}: elapsed_ns invalid')
            continue
        if must_wait:
            lo = int(want_to * 0.8 * 1_000_000)
            hi = int((want_to + 1000) * 1_000_000)
            if not (lo <= el <= hi):
                reasons.append(f'{name}: {want_to}ms poll did not wait '
                               '(O_NONBLOCK must not shorten poll timeout)')
        else:
            if el >= 5_000_000:
                reasons.append(f'{name}: timeout=0 poll must return '
                               'immediately on both fd types')

    # ---- wrong size syscall channel ----
    wrong = by_type.get('wrong_size', [])
    if len(wrong) != 1:
        reasons.append(f'exactly one wrong_size, got {len(wrong)}')
    else:
        w = wrong[0]
        if w.get('syscall', {}).get('rc') != -1 or \
                w.get('syscall', {}).get('errno') != 22:
            reasons.append('wrong_size must be ioctl -1/EINVAL')
        if w.get('embeddedStatus') is not None:
            reasons.append('wrong_size must carry no embedded Status')
        if w.get('request', {}).get('size') != 15 or \
                w.get('match') is not True:
            reasons.append('wrong_size payload/self-match invalid')

    # ---- summary ----
    summaries = by_type.get('summary', [])
    if len(summaries) != 1:
        reasons.append(f'exactly one summary, got {len(summaries)}')
    else:
        s = summaries[0]
        if records[-1].get('type') != 'summary':
            reasons.append('summary must be last')
        if s.get('nv0005_notification') != 'not_tested':
            reasons.append('NV0005 must be not_tested')
        if s.get('internal_cuda') != 0:
            reasons.append('internal_cuda must be 0')
        if s.get('clients_created') != ROOT_CLIENTS or \
                s.get('clients_freed') != ROOT_CLIENTS:
            reasons.append('summary must show 9 nonzero root clients '
                           'created and freed')
        if s.get('fds_opened') != 3 or s.get('fds_closed') != 3:
            reasons.append('summary fds must balance at 3')
        if s.get('allocations_ok') != s.get('frees_ok'):
            reasons.append('allocations and frees must balance')
        if s.get('cases_skipped') != 0:
            reasons.append('cases_skipped must be 0')
        if s.get('errors') != 0 or s.get('status') != 'pass':
            reasons.append(f"summary not clean: errors={s.get('errors')}")

    if set(verdict['cases_seen']) != set(EVENT_EXPECT):
        reasons.append('event case set mismatch')

    verdict['ok'] = not reasons
    return verdict


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('log', type=argparse.FileType('r'))
    p.add_argument('--device', default='/dev/nvidia0')
    p.add_argument('--ctl-device', default='/dev/nvidiactl')
    p.add_argument('--poll-ms', type=int, default=30)
    p.add_argument('--layout', action='store_true')
    a = p.parse_args(argv)
    content = a.log.read()
    v = analyze_layout(content) if a.layout else analyze(
        content, {'device': a.device, 'ctl_device': a.ctl_device,
                  'poll_ms': a.poll_ms})
    print(json.dumps(v, indent=2))
    return 0 if v['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
