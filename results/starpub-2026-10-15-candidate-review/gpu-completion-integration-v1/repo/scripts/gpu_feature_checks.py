"""Exact system-feature control comparisons, with native error semantics."""
import collections
import json
import re
import struct

EXPECTED_CASES = ['normal', 'zeroed', 'client_zero', 'object_zero', 'object_unknown',
    'child_null', 'child_low', 'child_noncanonical', 'child_wrap', 'child_readonly',
    'child_none', 'cross_rw', 'cross_ro', 'cross_none', 'header_readonly', 'header_none',
    'size_zero', 'size_short', 'size_long', 'size_max', 'null_size_zero', 'flags_1',
    'flags_2', 'flags_4', 'flags_8', 'flags_high', 'flags_all', 'client_zero_bad_size',
    'client_zero_bad_pointer', 'finn_readonly', 'finn_bad_size']
EXPECTED_CHECKS = ['open_control', 'client', 'initial_query', 'normal', 'zeroed',
    'four_threads', 'release_client'] + [f'independent_process_{i}' for i in range(4)]


def _complete(v):
    ids = v.get('identities', [])
    return (len(ids) == 1 and 0 < ids[0].get('client', 0) < 2**32
            and collections.Counter(n for n, _ in v.get('checks', [])) == collections.Counter(EXPECTED_CHECKS)
            and [r.get('case') for r in v.get('results', [])] == EXPECTED_CASES
            and v.get('ends') in (['0'], ['1']))


def contract(row, identities):
    value = dict(row)
    client = identities[0]['client']
    headers = [list(struct.unpack('<IIIIQII', bytes.fromhex(row[k]))) for k in ('header_before', 'header_after')]
    value['raw_pointer_preserved'] = headers[0][4] == headers[1][4]
    for key, h in zip(('header_before', 'header_after'), headers):
        h[0], h[1] = [('client' if n == client else n) for n in h[:2]]
        h[4] = h[4] if h[4] in (0, 1, 2**63, 2**64 - 1) else 'user_buffer'
        value[key] = h
    for k in ('child_before', 'child_after'):
        if value[k] != 'n/a' and len(bytes.fromhex(value[k])) != 4:
            raise ValueError('incorrect feature parameter length')
    return value


def parse(content):
    value = {'checks': re.findall(r'^GPU_FEATURE_CHECK (\w+) (pass|fail)$', content, re.M),
             'results': [json.loads(x) for x in re.findall(r'^GPU_FEATURE_RESULT (.+)$', content, re.M)],
             'identities': [json.loads(x) for x in re.findall(r'^GPU_FEATURE_IDENTITIES (.+)$', content, re.M)],
             'ends': re.findall(r'^GPU_FEATURE_END status=([01]) internal_cuda_verified=0$', content, re.M)}
    complete = _complete(value)
    try:
        if complete:
            for row in value['results']:
                contract(row, value['identities'])
    except (ValueError, KeyError, struct.error, TypeError):
        complete = False
    value['observation_complete'] = complete
    value['probe_checks_pass'] = complete and value['ends'] == ['0'] and all(s == 'pass' for _, s in value['checks'])
    return value


def compare(a, b):
    total = len(a.get('results', []))
    if not (_complete(a) and _complete(b) and a.get('observation_complete') and b.get('observation_complete')):
        return {'matches': False, 'matched': 0, 'total': total,
                'differences': [{'error': 'missing, extra, or malformed observations'}]}
    differences = []
    for i, (native, observed) in enumerate(zip(a['results'], b['results'])):
        x, y = contract(native, a['identities']), contract(observed, b['identities'])
        fields = {k: {'native': x.get(k), 'observed': y.get(k)} for k in x.keys() | y.keys() if x.get(k) != y.get(k)}
        if fields:
            differences.append({'index': i, 'case': x['case'], 'fields': fields})
    return {'matches': not differences, 'matched': total - len(differences), 'total': total, 'differences': differences}
