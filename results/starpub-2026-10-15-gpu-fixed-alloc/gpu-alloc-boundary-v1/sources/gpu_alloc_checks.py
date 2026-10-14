"""Compare the fixed-class allocation probe against actual native observations."""
import collections
import json
import re
import struct

_CASES = ['normal', 'auto_handle', 'size_4', 'size_1', 'size_max',
          'child_zero', 'child_other_client', 'child_unknown', 'root_zero',
          'parent_zero', 'parent_other', 'child_null', 'child_low',
          'child_noncanonical', 'child_wrap', 'child_readonly', 'child_none',
          'cross_rw', 'cross_ro', 'cross_none', 'header_readonly', 'header_none',
          'root_zero_bad_pointer']
_EXTRA = ['rights_zero', 'rights_all', 'rights_debug', 'rights_low',
          'rights_noncanonical', 'rights_readonly', 'rights_none', 'rights_cross',
          'flags_2', 'flags_high', 'flags_finn']
EXPECTED_CASES = [f'v{n}_{c}' for n in (21, 64) for c in _CASES] + ['v64_' + c for c in _EXTRA]
EXPECTED_CHECKS = ['open_control', 'clients', 'v21_normal', 'v21_auto_handle',
                   'v64_normal', 'v64_auto_handle', 'four_threads_alloc_free',
                   'free_client_b', 'free_client_a'] + [f'independent_process_{i}' for i in range(4)]


def _complete(value):
    ids = value.get('identities', [])
    return (len(ids) == 1 and 0 < ids[0].get('client_a', 0) < 2**32
            and 0 < ids[0].get('client_b', 0) < 2**32
            and ids[0]['client_a'] != ids[0]['client_b']
            and collections.Counter(n for n, _ in value.get('checks', [])) == collections.Counter(EXPECTED_CHECKS)
            and [r.get('case') for r in value.get('results', [])] == EXPECTED_CASES
            and value.get('ends') in (['0'], ['1']))


def parse(content):
    value = {'checks': re.findall(r'^GPU_ALLOC_CHECK (\w+) (pass|fail)$', content, re.M),
             'results': [json.loads(x) for x in re.findall(r'^GPU_ALLOC_RESULT (.+)$', content, re.M)],
             'identities': [json.loads(x) for x in re.findall(r'^GPU_ALLOC_IDENTITIES (.+)$', content, re.M)],
             'ends': re.findall(r'^GPU_ALLOC_END status=([01]) internal_cuda_verified=0$', content, re.M)}
    valid = _complete(value)
    try:
        if valid:
            for record in value['results']:
                contract(record, value['identities'])
    except (ValueError, KeyError, struct.error, TypeError):
        valid = False
    value['observation_complete'] = valid
    value['probe_checks_pass'] = valid and value['ends'] == ['0'] and all(s == 'pass' for _, s in value['checks'])
    return value


def contract(record, identities):
    ids = identities[0]
    def handle(n):
        return 'client_a' if n == ids['client_a'] else 'client_b' if n == ids['client_b'] else n
    def ptr(n):
        return n if n in (0, 1, 2**63, 2**64 - 1) else 'user_buffer'
    layout = record['layout']
    if layout not in (21, 64) or not record['case'].startswith(f'v{layout}_'):
        raise ValueError('incorrect allocation layout')
    fmt = '<IIIIQII' if layout == 21 else '<IIIIQQIII4s'
    before, after = [list(struct.unpack(fmt, bytes.fromhex(record[k]))) for k in ('header_before', 'header_after')]
    value = dict(record)
    value['alloc_pointer_preserved'] = before[4] == after[4]
    if layout == 64:
        value['rights_pointer_preserved'] = before[5] == after[5]
    if before[2] == 0 and after[2] != 0 and record['rc'] == 0 and after[-1 if layout == 21 else -2] == 0:
        after[2] = 'allocated_object'
    for name, row in (('header_before', before), ('header_after', after)):
        row[0], row[1], row[4] = handle(row[0]), handle(row[1]), ptr(row[4])
        if layout == 64:
            row[5], row[-1] = ptr(row[5]), row[-1].hex()
        value[name] = row
    for name in ('child_before', 'child_after'):
        if value[name] != 'n/a':
            value[name] = handle(struct.unpack('<I', bytes.fromhex(value[name]))[0])
    for name in ('rights_before', 'rights_after'):
        if value[name] != 'n/a' and len(bytes.fromhex(value[name])) != 4:
            raise ValueError('invalid rights mask length')
    return value


def compare(baseline, observed):
    total = len(baseline.get('results', []))
    if not (_complete(baseline) and _complete(observed)
            and baseline.get('observation_complete') and observed.get('observation_complete')):
        return {'matches': False, 'matched': 0, 'total': total,
                'differences': [{'error': 'missing, extra, or malformed observations'}]}
    differences = []
    for i, (a, b) in enumerate(zip(baseline['results'], observed['results'])):
        a, b = contract(a, baseline['identities']), contract(b, observed['identities'])
        fields = {k: {'native': a.get(k), 'observed': b.get(k)} for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
        if fields:
            differences.append({'index': i, 'case': a['case'], 'fields': fields})
    return {'matches': not differences, 'matched': total - len(differences), 'total': total, 'differences': differences}
