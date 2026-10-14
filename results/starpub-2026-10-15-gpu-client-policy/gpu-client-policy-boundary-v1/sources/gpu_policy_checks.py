"""Exact 0xd04 observation comparison. Differences remain failures, including
known size/flags and inherited-FD gaps. This is not GPU compute acceptance.
"""
import collections
import json
import re
import struct

EXPECTED_CHECKS = ['open_control', 'alloc_client_a', 'alloc_client_b', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'compose_all_debug', 'compose_all_debug_pointer_preserved', 'require_pid_dup', 'require_pid_dup_pointer_preserved', 'allow_pid_dup', 'allow_pid_dup_pointer_preserved', 'allow_token_dup', 'allow_token_dup_pointer_preserved', 'client_self_dup', 'client_self_dup_pointer_preserved', 'client_existing_dup', 'client_existing_dup_pointer_preserved', 'client_existing_revoke', 'client_existing_revoke_pointer_preserved', 'client_unknown_dup', 'client_unknown_dup_pointer_preserved', 'revoke_all_dup', 'revoke_all_dup_pointer_preserved', 'revoke_pid_nonexistent', 'revoke_pid_nonexistent_pointer_preserved', 'restore_all_dup', 'restore_all_dup_pointer_preserved', 'type_0', 'type_0_pointer_preserved', 'type_1', 'type_1_pointer_preserved', 'type_2', 'type_2_pointer_preserved', 'type_3', 'type_3_pointer_preserved', 'type_4', 'type_4_pointer_preserved', 'type_5', 'type_5_pointer_preserved', 'type_6', 'type_6_pointer_preserved', 'type_7', 'type_7_pointer_preserved', 'type_8', 'type_8_pointer_preserved', 'type_9', 'type_9_pointer_preserved', 'type_65535', 'type_65535_pointer_preserved', 'mask_dup_only', 'mask_dup_only_pointer_preserved', 'mask_debug_only', 'mask_debug_only_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'zero_client_object', 'zero_client_object_pointer_preserved', 'zero_object', 'zero_object_pointer_preserved', 'unknown_client', 'unknown_client_pointer_preserved', 'object_other_client', 'object_other_client_pointer_preserved', 'client_object_swapped', 'client_object_swapped_pointer_preserved', 'child_null', 'child_null_pointer_preserved', 'child_low_1', 'child_low_1_pointer_preserved', 'child_noncanonical_2_63', 'child_noncanonical_2_63_pointer_preserved', 'child_all_ones', 'child_all_ones_pointer_preserved', 'child_protnone', 'child_protnone_pointer_preserved', 'child_crosspage_rw_rw', 'child_crosspage_rw_rw_pointer_preserved', 'child_crosspage_rw_none', 'child_crosspage_rw_none_pointer_preserved', 'header_readonly', 'header_protnone', 'replace_all_dup', 'replace_all_dup_pointer_preserved', 'compose_all_debug', 'compose_all_debug_pointer_preserved', 'require_pid_dup', 'require_pid_dup_pointer_preserved', 'allow_pid_dup', 'allow_pid_dup_pointer_preserved', 'allow_token_dup', 'allow_token_dup_pointer_preserved', 'client_self_dup', 'client_self_dup_pointer_preserved', 'client_existing_dup', 'client_existing_dup_pointer_preserved', 'client_existing_revoke', 'client_existing_revoke_pointer_preserved', 'client_unknown_dup', 'client_unknown_dup_pointer_preserved', 'revoke_all_dup', 'revoke_all_dup_pointer_preserved', 'revoke_pid_nonexistent', 'revoke_pid_nonexistent_pointer_preserved', 'restore_all_dup', 'restore_all_dup_pointer_preserved', 'post_fault_control', 'post_fault_control_pointer_preserved', 'four_threads_shared_client', 'four_threads_own_clients', 'free_client_b', 'free_client_a', 'independent_process_0', 'independent_process_1', 'independent_process_2', 'independent_process_3', 'inherited_fd_cross_client']

EXPECTED_CASES = ['replace_all_dup', 'compose_all_debug', 'require_pid_dup', 'allow_pid_dup', 'allow_token_dup', 'client_self_dup', 'client_existing_dup', 'client_existing_revoke', 'client_unknown_dup', 'revoke_all_dup', 'revoke_pid_nonexistent', 'restore_all_dup', 'type_0', 'type_1', 'type_2', 'type_3', 'type_4', 'type_5', 'type_6', 'type_7', 'type_8', 'type_9', 'type_65535', 'mask_dup_only', 'mask_debug_only', 'mask_nice_only', 'mask_zero', 'mask_unknown_bits', 'action_0', 'replace_all_dup', 'action_1', 'replace_all_dup', 'action_2', 'replace_all_dup', 'action_3', 'replace_all_dup', 'action_4', 'replace_all_dup', 'action_6', 'replace_all_dup', 'action_5', 'replace_all_dup', 'action_7', 'replace_all_dup', 'action_255', 'replace_all_dup', 'zero_client_object', 'zero_object', 'unknown_client', 'object_other_client', 'client_object_swapped', 'unknown_command_d0e', 'child_null', 'child_low_1', 'child_noncanonical_2_63', 'child_all_ones', 'child_readonly', 'child_protnone', 'child_crosspage_rw_rw', 'child_crosspage_rw_ro', 'child_crosspage_rw_none', 'child_crosspage_ro_rw', 'header_readonly', 'header_protnone', 'size_0', 'size_4', 'size_8', 'size_11', 'size_13', 'size_16', 'size_32', 'size_4096', 'flags_0x1', 'flags_0x2', 'flags_0x80000000', 'flags_0xffffffff', 'replace_all_dup', 'compose_all_debug', 'require_pid_dup', 'allow_pid_dup', 'allow_token_dup', 'client_self_dup', 'client_existing_dup', 'client_existing_revoke', 'client_unknown_dup', 'revoke_all_dup', 'revoke_pid_nonexistent', 'restore_all_dup', 'post_fault_control', 'independent_process_0', 'independent_process_1', 'independent_process_2', 'independent_process_3', 'inherited_fd_cross_client']


def parse(content):
    checks = re.findall(r'^GPU_SHARE_CHECK (\w+) (pass|fail)$', content, re.M)
    rows = [json.loads(x) for x in re.findall(r'^GPU_SHARE_RESULT (.+)$', content, re.M)]
    ids = [json.loads(x) for x in re.findall(r'^GPU_SHARE_IDENTITIES (.+)$', content, re.M)]
    valid_identity = len(ids) == 1 and ids[0]['client_a'] != 0 and ids[0]['client_b'] != 0 and ids[0]['client_a'] != ids[0]['client_b']
    complete = (collections.Counter(n for n, _ in checks) == collections.Counter(EXPECTED_CHECKS)
                and [r['case'] for r in rows] == EXPECTED_CASES and valid_identity
                and re.search(r'^GPU_SHARE_END status=[01] internal_cuda_verified=0$', content, re.M) is not None)
    return {'checks': checks, 'results': rows, 'identities': ids,
            'observation_complete': bool(complete),
            'probe_checks_pass': complete and all(s == 'pass' for _, s in checks)}


def contract(record, identities):
    ids = identities[0]
    def handle(n):
        return 'client_a' if n == ids['client_a'] else 'client_b' if n == ids['client_b'] else n
    def target(n):
        return 'self_pid' if n == ids['pid'] else handle(n)
    def pointer(n):
        return n if n in (0, 1, 1 << 63, (1 << 64) - 1) else 'user_buffer'
    def header(value):
        a, b, cmd, flags, ptr, size, status = struct.unpack('<IIIIQII', bytes.fromhex(value))
        return [handle(a), handle(b), cmd, flags, pointer(ptr), size, status]
    def child(value):
        if value == 'n/a': return value
        t, mask, typ, action, padding = struct.unpack('<IIHBB', bytes.fromhex(value))
        return [target(t), mask, typ, action, padding]
    result = dict(record)
    if 'header_before' in result:
        result['client'] = handle(int(result['client'], 16))
        result['object'] = handle(int(result['object'], 16))
        result['params'] = pointer(int(result['params'], 16))
        result['policy'] = dict(result['policy'], target=target(int(result['policy']['target'], 16)))
        for key in ('header_before', 'header_after'): result[key] = header(result[key])
        for key in ('child_before', 'child_after'): result[key] = child(result[key])
    return result


def compare(baseline, observed):
    if not baseline.get('observation_complete') or not observed.get('observation_complete'):
        return {'matches': False, 'matched': 0, 'total': len(EXPECTED_CASES),
                'differences': [{'error': 'missing, extra, or malformed observations'}]}
    differences = []
    for index, (a, b) in enumerate(zip(baseline['results'], observed['results'])):
        a, b = contract(a, baseline['identities']), contract(b, observed['identities'])
        fields = {k: {'native': a.get(k), 'observed': b.get(k)} for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
        if fields: differences.append({'index': index, 'case': a['case'], 'fields': fields})
    return {'matches': not differences, 'matched': len(EXPECTED_CASES) - len(differences),
            'total': len(EXPECTED_CASES), 'differences': differences}
