"""Independent strict checker for gpu_os_event_probe JSONL output.

It validates the pinned 550.120 OS-event ABI semantics, keeping the two
distinct failure channels apart:

  * syscall channel: ioctl(2) returns -1 with errno (wrong payload size);
  * embedded channel: ioctl returns 0 and the driver writes an RM status
    into the 16-byte payload's Status field.

The checker never trusts a summary flag: it derives the verdict from the
individual meta/event/poll/get_event_data/wrong_size records and checks
exact owned-resource accounting in the summary.  Layout records from
--layout-selfcheck are validated against compile-time pinned offsets.

Run:
  python3 gpu_os_event_checks.py <probe.stdout> [--device /dev/nvidia0]
"""
import argparse
import json
import sys

# Pinned nvstatuscodes.h (550.120)
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
    'escapes': {'ALLOC_OS_EVENT': 206, 'FREE_OS_EVENT': 207,
                'RM_GET_EVENT_DATA': 82, 'ALLOC_ROOT': 43, 'FREE': 41},
    'status': {'OK': 0, 'INSUFFICIENT_RESOURCES': 0x1A,
               'INVALID_ARGUMENT': 0x1F, 'INVALID_EVENT': 0x28,
               'OPERATING_SYSTEM': 0x59},
}

# name -> (op alloc/free/all, expected embedded Status)
EVENT_EXPECT = {
    'alloc_free': [('alloc', ST_OK), ('free', ST_OK)],
    'duplicate_alloc': [('alloc', ST_OK),
                        ('alloc', ST_INSUFFICIENT_RESOURCES),
                        ('free', ST_OK)],
    'missing_free': [('free', ST_INVALID_EVENT)],
    'double_free': [('alloc', ST_OK), ('free', ST_OK),
                    ('free', ST_INVALID_EVENT)],
    'opaque_tag_not_fd': [('alloc', ST_OK), ('free', ST_OK)],
    'two_independent_clients': [('alloc', ST_OK), ('alloc', ST_OK),
                                ('free', ST_OK), ('free', ST_OK)],
    'hdevice_ignored': [('alloc', ST_OK), ('free', ST_OK)],
}


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
            reasons.append(f'layout {key} mismatch: '
                           f'{layout.get(key)!r} != {want!r}')
    return {'ok': not reasons, 'mode': 'layout', 'reasons': reasons}


def analyze(content, request=None):
    request = request or {}
    device = request.get('device', '/dev/nvidia0')
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
        if m.get('driver') != '550.120':
            reasons.append('meta driver is not 550.120')

    for bad in by_type.get('fatal', []) + by_type.get('error', []):
        reasons.append(f'line {bad["_line"]}: {bad.get("type")} '
                       f'{bad.get("stage", "")}')

    # ---- tags record: opaque tags must not be open descriptors ----
    tags = by_type.get('tags', [])
    if len(tags) != 1:
        reasons.append(f'exactly one tags record, got {len(tags)}')
    else:
        t = tags[0]
        if not _is_int(t.get('gpu_char_fd')) or t['gpu_char_fd'] < 0:
            reasons.append('tags.gpu_char_fd invalid')
        flags = t.get('opaque_are_open_fds')
        if flags != [False, False, False]:
            reasons.append(f'opaque tags must not be open fds: {flags}')

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
        for i, (rec, (op, st)) in enumerate(zip(got, expected)):
            where = f'{name}[{i}]'
            sysc = rec.get('syscall', {})
            if rec.get('op') != op:
                reasons.append(f'{where}: op {rec.get("op")} != {op}')
            if sysc.get('rc') != 0 or sysc.get('errno') != 0:
                reasons.append(f'{where}: syscall failure '
                               f'rc={sysc.get("rc")} errno={sysc.get("errno")}')
            if rec.get('embeddedStatus') != st:
                reasons.append(f'{where}: embeddedStatus '
                               f'{rec.get("embeddedStatus")} != {st}')
            if rec.get('match') is not True:
                reasons.append(f'{where}: probe did not self-match')
            req = rec.get('request', {})
            if not _is_int(req.get('nr')) or req.get('nr') != \
                    (206 if op == 'alloc' else 207):
                reasons.append(f'{where}: wrong ioctl nr')
            if req.get('size') != 16 or not _is_int(req.get('hClient')):
                reasons.append(f'{where}: request size/hClient invalid')

    tic = grouped.get('two_independent_clients', [])
    if tic:
        allocs = [r for r in tic if r.get('op') == 'alloc']
        hclients = {r.get('request', {}).get('hClient') for r in allocs}
        tags = {r.get('request', {}).get('tag') for r in allocs}
        if len(allocs) != 2:
            reasons.append('two_independent_clients needs two allocs')
        elif len(hclients) != 2:
            reasons.append('two_independent_clients allocs must use two '
                           'distinct hClient values')
        if len(tags) != 1:
            reasons.append('two_independent_clients must share one tag '
                           'value across the clients')

    # Opaque-tag and hDevice evidence
    for name, want_opaque in (('opaque_tag_not_fd', True),
                              ('two_independent_clients', True)):
        for rec in grouped.get(name, []):
            if rec['request'].get('tag_is_open_fd') is not False:
                reasons.append(f'{name}: tag must be a non-fd opaque value')
    hdev = grouped.get('hdevice_ignored', [])
    if hdev and any(r['request'].get('hDevice') != 0xDEADBEEF
                    for r in hdev):
        reasons.append('hdevice_ignored must send hDevice=0xDEADBEEF')

    # ---- wrong-size: SYSCALL channel, embedded channel absent ----
    wrong = by_type.get('wrong_size', [])
    if len(wrong) != 1:
        reasons.append(f'exactly one wrong_size record, got {len(wrong)}')
    else:
        w = wrong[0]
        sysc = w.get('syscall', {})
        if sysc.get('rc') != -1 or sysc.get('errno') != 22:
            reasons.append(f'wrong_size must be ioctl -1/EINVAL, got '
                           f'{sysc}')
        if w.get('embeddedStatus') is not None:
            reasons.append('wrong_size must carry no embedded Status')
        if w.get('request', {}).get('size') != 15:
            reasons.append('wrong_size must use a 15-byte payload')
        if w.get('match') is not True:
            reasons.append('wrong_size probe did not self-match')

    # ---- GET_EVENT_DATA empty queue: embedded 0x59, ioctl rc 0 ----
    ged = by_type.get('get_event_data', [])
    if len(ged) != 1:
        reasons.append(f'exactly one get_event_data record, got {len(ged)}')
    else:
        g = ged[0]
        sysc = g.get('syscall', {})
        if sysc.get('rc') != 0 or sysc.get('errno') != 0:
            reasons.append(f'get_event_data syscall failure: {sysc}')
        if g.get('embeddedStatus') != ST_OPERATING_SYSTEM:
            reasons.append('empty-queue GET_EVENT_DATA embeddedStatus '
                           'must be 0x59')
        if g.get('moreEvents') != 0:
            reasons.append('empty-queue MoreEvents must stay 0')
        if g.get('output_buffer_touched') is not False:
            reasons.append('empty queue must not copy event data out')
        if g.get('match') is not True:
            reasons.append('get_event_data probe did not self-match')

    # ---- poll: empty queue must not signal readiness ----
    polls = {p.get('name'): p for p in by_type.get('poll', [])}
    for name in ('poll_blocking_empty', 'poll_nonblock_empty'):
        p = polls.get(name)
        if p is None:
            reasons.append(f'missing {name}')
            continue
        if p.get('syscall', {}).get('rc') != 0:
            reasons.append(f'{name}: poll failed')
        if p.get('revents') != 0:
            reasons.append(f'{name}: unexpected readiness revents='
                           f'{p.get("revents")} (would be a fabricated/'
                           'leaked event)')
        if p.get('empty') is not True:
            reasons.append(f'{name}: empty flag not set')
        elapsed = p.get('elapsed_ns')
        if not _is_int(elapsed) or elapsed < 0:
            reasons.append(f'{name}: elapsed_ns invalid')
            continue
        if name == 'poll_blocking_empty':
            lo = int(poll_ms * 0.8 * 1_000_000)
            hi = int((poll_ms + 1000) * 1_000_000)
            if not (lo <= elapsed <= hi):
                reasons.append(f'{name}: blocking empty poll must wait '
                               f'~{poll_ms}ms, elapsed_ns={elapsed}')
        else:
            if elapsed >= poll_ms * 1_000_000:
                reasons.append('nonblock empty poll must return before '
                               'the blocking timeout')

    # ---- summary: exact owned accounting, no claimed notifications ----
    summaries = by_type.get('summary', [])
    if len(summaries) != 1:
        reasons.append(f'exactly one summary record, got {len(summaries)}')
    else:
        s = summaries[0]
        if records[-1].get('type') != 'summary':
            reasons.append('summary must be the final record')
        if s.get('nv0005_notification') != 'not_tested':
            reasons.append('NV0005 must be explicitly not_tested')
        if s.get('internal_cuda') != 0:
            reasons.append('internal_cuda must be 0 (no CUDA claimed)')
        if s.get('allocations_ok') != s.get('frees_ok'):
            reasons.append('successful allocations and frees must balance')
        if s.get('clients_created') != s.get('clients_freed'):
            reasons.append('created and freed NV01_ROOT clients must balance')
        if s.get('fds_opened') != s.get('fds_closed'):
            reasons.append('opened and closed fds must balance')
        if s.get('errors') != 0 or s.get('status') != 'pass':
            reasons.append(f'summary not clean: errors='
                           f'{s.get("errors")} status={s.get("status")}')

    # Required cases completeness
    required = set(EVENT_EXPECT) | {'wrong_size_channel'}
    if set(verdict['cases_seen']) != set(EVENT_EXPECT):
        missing = set(EVENT_EXPECT) - set(verdict['cases_seen'])
        extra = set(verdict['cases_seen']) - set(EVENT_EXPECT)
        if missing:
            reasons.append(f'missing event cases: {sorted(missing)}')
        if extra:
            reasons.append(f'unexpected event cases: {sorted(extra)}')
    if 'wrong_size' in by_type:
        verdict['cases_seen'].append('wrong_size_channel')

    verdict['ok'] = not reasons
    return verdict


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('log', type=argparse.FileType('r'))
    p.add_argument('--device', default='/dev/nvidia0')
    p.add_argument('--poll-ms', type=int, default=30)
    p.add_argument('--layout', action='store_true',
                   help='validate a --layout-selfcheck log')
    a = p.parse_args(argv)
    content = a.log.read()
    v = analyze_layout(content) if a.layout else analyze(
        content, {'device': a.device, 'poll_ms': a.poll_ms})
    print(json.dumps(v, indent=2))
    return 0 if v['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
