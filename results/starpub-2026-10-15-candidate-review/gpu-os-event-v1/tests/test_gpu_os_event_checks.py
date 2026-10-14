"""Parser/evidence tests for the OS-event ABI checker.

No GPU is used.  The native model is a JSONL log shaped exactly like the
probe's real output with the pinned 550.120 embedded statuses; mutations
represent every regression the probe is meant to expose, including
confusion between the syscall and embedded-RM-status channels.  The real
binary is only invoked in --layout-selfcheck mode (device-free).
"""
import copy
import json
import os
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gpu_os_event_checks as checks

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / 'build' / 'gpu-os-event-probe'

ST = {'OK': 0, 'RESOURCES': 0x1A, 'EVENT': 0x28, 'OS': 0x59}
POLL_MS = 30


class EventModel:
    """Builds the exact JSONL the probe emits on a healthy host."""

    def __init__(self):
        self.records = []
        self.allocations = self.frees = 0
        self.clients = 0

    def meta(self):
        return {'type': 'meta', 'schema': 1, 'mode': 'native_abi',
                'device': '/dev/nvidia0',
                'gpu_char_fd_tag_semantics': 'opaque_numeric',
                'driver': '550.120', 'poll_ms': POLL_MS,
                'sources': {}}

    def tags(self):
        return {'type': 'tags', 'gpu_char_fd': 3,
                'opaque': [0x4f530001, 0x4f530002, 0x4f530003],
                'opaque_are_open_fds': [False, False, False]}

    def event(self, name, op, client, status, hdevice=0, tag=3,
              tag_open=False):
        nr = 206 if op == 'alloc' else 207
        if op == 'alloc' and status == ST['OK']:
            self.allocations += 1
        if op == 'free' and status == ST['OK']:
            self.frees += 1
        return {'type': 'event', 'name': name, 'op': op,
                'request': {'nr': nr, 'size': 16, 'hClient': client,
                            'hDevice': hdevice, 'tag': tag,
                            'tag_is_open_fd': tag_open},
                'syscall': {'rc': 0, 'errno': 0},
                'embeddedStatus': status,
                'expectedEmbedded': status,
                'match': True}

    def case(self, name, ops, clients, **kw):
        tag_open = name not in ('opaque_tag_not_fd',
                                'two_independent_clients')
        kw.setdefault('tag_open', tag_open)
        for i, (op, st) in enumerate(ops):
            self.records.append(
                self.event(name, op, clients, st, **kw))

    def build(self):
        r = [self.meta(), self.tags()]
        clients = {n: 100 + i for i, n in enumerate(
            ['alloc_free', 'duplicate_alloc', 'missing_free', 'double_free',
             'opaque_tag_not_fd', 'two_independent_clients',
             'hdevice_ignored', 'ged'])}
        self.clients = len(set(clients.values())) + 1
        self.case('alloc_free',
                  [('alloc', ST['OK']), ('free', ST['OK'])],
                  clients['alloc_free'])
        self.case('duplicate_alloc',
                  [('alloc', ST['OK']), ('alloc', ST['RESOURCES']),
                   ('free', ST['OK'])],
                  clients['duplicate_alloc'])
        self.case('missing_free', [('free', ST['EVENT'])],
                  clients['missing_free'])
        self.case('double_free',
                  [('alloc', ST['OK']), ('free', ST['OK']),
                   ('free', ST['EVENT'])],
                  clients['double_free'])
        self.case('opaque_tag_not_fd',
                  [('alloc', ST['OK']), ('free', ST['OK'])],
                  clients['opaque_tag_not_fd'],
                  tag=0x4f530001, tag_open=False)
        # two independent clients: same tag, two distinct clients on two
        # opens, four records (fd1 client first and last, fd2 client middle)
        tic_a = clients['two_independent_clients']
        tic_b = tic_a + 1
        for op, st, c in [('alloc', ST['OK'], tic_a),
                          ('alloc', ST['OK'], tic_b),
                          ('free', ST['OK'], tic_a),
                          ('free', ST['OK'], tic_b)]:
            self.records.append(self.event('two_independent_clients', op,
                                           c, st, tag=0x4f530002,
                                           tag_open=False))
        self.case('hdevice_ignored',
                  [('alloc', ST['OK']), ('free', ST['OK'])],
                  clients['hdevice_ignored'], hdevice=0xDEADBEEF,
                  tag=0x4f530003, tag_open=False)

        r += self.records
        r.append({'type': 'get_event_data',
                  'name': 'get_event_data_empty',
                  'request': {'nr': 82, 'size': 16,
                              'hClient': clients['ged'],
                              'pEvent_valid': True},
                  'syscall': {'rc': 0, 'errno': 0},
                  'embeddedStatus': ST['OS'], 'expectedEmbedded': 89,
                  'moreEvents': 0, 'output_buffer_touched': False,
                  'match': True})
        r.append({'type': 'poll', 'name': 'poll_blocking_empty',
                  'nonblock': False, 'timeout_ms': POLL_MS,
                  'syscall': {'rc': 0, 'errno': 0},
                  'revents': 0,
                  'elapsed_ns': POLL_MS * 1_000_000 + 200_000,
                  'empty': True})
        r.append({'type': 'poll', 'name': 'poll_nonblock_empty',
                  'nonblock': True, 'timeout_ms': 0,
                  'syscall': {'rc': 0, 'errno': 0},
                  'revents': 0, 'elapsed_ns': 3000, 'empty': True})
        r.append({'type': 'wrong_size',
                  'name': 'wrong_size_channel',
                  'request': {'nr': 206, 'size': 15,
                              'hClient': clients['ged'], 'tag': 1},
                  'syscall': {'rc': -1, 'errno': 22,
                              'errno_name': 'EINVAL'},
                  'embeddedStatus': None,
                  'expect_syscall_errno': 22,
                  'embedded_channel_absent': True, 'match': True})
        created = 8
        r.append({'type': 'summary', 'schema': 1,
                  'allocations_ok': self.allocations,
                  'frees_ok': self.frees,
                  'clients_created': created, 'clients_freed': created,
                  'fds_opened': 3, 'fds_closed': 3, 'errors': 0,
                  'nv0005_notification': 'not_tested', 'internal_cuda': 0,
                  'status': 'pass'})
        return r

    def text(self, records=None):
        return ''.join(json.dumps(x) + '\n' for x in
                       (records if records is not None else self.build()))


class EventCheckerTests(unittest.TestCase):
    def setUp(self):
        self.model = EventModel()

    def verdict(self, records=None):
        return checks.analyze(self.model.text(records),
                             {'device': '/dev/nvidia0', 'poll_ms': POLL_MS})

    def assertRejected(self, records):
        v = self.verdict(records)
        self.assertFalse(v['ok'], msg=v['reasons'])
        return v['reasons']

    def test_clean_model_passes(self):
        v = self.verdict()
        self.assertTrue(v['ok'], msg=v['reasons'])

    def test_duplicate_alloc_must_be_embedded_resources_not_syscall(self):
        # The v10 mapping patch guessed 0x1F; pinned 550.120 wraps the
        # duplicate to 0x1A via RmAllocOsEvent, ioctl rc 0.
        records = self.model.build()
        dup = [r for r in records if r.get('type') == 'event'
               and r.get('name') == 'duplicate_alloc']
        dup[1]['embeddedStatus'] = 0x1F
        dup[1]['match'] = False
        self.assertRejected(records)

    def test_duplicate_as_syscall_failure_is_rejected(self):
        records = self.model.build()
        dup = [r for r in records if r.get('type') == 'event'
               and r.get('name') == 'duplicate_alloc']
        dup[1]['syscall'] = {'rc': -1, 'errno': 22}
        dup[1]['match'] = False
        self.assertRejected(records)

    def test_missing_free_embedded_invalid_event(self):
        records = self.model.build()
        rec = [r for r in records if r.get('name') == 'missing_free'][0]
        rec['embeddedStatus'] = ST['OK']
        rec['match'] = False
        self.assertRejected(records)

    def test_missing_free_via_syscall_channel_rejected(self):
        records = self.model.build()
        rec = [r for r in records if r.get('name') == 'missing_free'][0]
        rec['syscall'] = {'rc': -1, 'errno': 2}
        rec['match'] = False
        self.assertRejected(records)

    def test_opaque_tag_must_not_be_an_open_fd(self):
        records = self.model.build()
        rec = [r for r in records if r.get('name') == 'opaque_tag_not_fd'][0]
        rec['request']['tag_is_open_fd'] = True
        self.assertRejected(records)

    def test_opaque_tags_record_must_report_non_open(self):
        records = self.model.build()
        tags = [r for r in records if r.get('type') == 'tags'][0]
        tags['opaque_are_open_fds'][0] = True
        self.assertRejected(records)

    def test_two_clients_same_tag_both_succeed(self):
        records = self.model.build()
        recs = [r for r in records
                if r.get('name') == 'two_independent_clients'
                and r.get('op') == 'alloc']
        # One client rejected would be a per-client registration regression.
        recs[1]['embeddedStatus'] = ST['RESOURCES']
        recs[1]['match'] = False
        self.assertRejected(records)

    def test_two_clients_must_use_distinct_handles(self):
        records = self.model.build()
        recs = [r for r in records
                if r.get('name') == 'two_independent_clients'
                and r.get('op') == 'alloc']
        # Same hClient twice would not prove independent registrations.
        recs[1]['request']['hClient'] = recs[0]['request']['hClient']
        self.assertRejected(records)

    def test_two_clients_must_share_one_tag(self):
        records = self.model.build()
        recs = [r for r in records
                if r.get('name') == 'two_independent_clients'
                and r.get('op') == 'alloc']
        recs[1]['request']['tag'] += 1
        # Different tags do not test the cross-client same-tag semantics.
        self.assertRejected(records)

    def test_hdevice_field_is_ignored(self):
        records = self.model.build()
        rec = [r for r in records if r.get('name') == 'hdevice_ignored'][0]
        rec['request']['hDevice'] = 0
        self.assertRejected(records)

    def test_wrong_size_is_syscall_channel_without_embedded(self):
        records = self.model.build()
        w = [r for r in records if r.get('type') == 'wrong_size'][0]
        # Fabricating an embedded success on a short payload is invalid.
        w['embeddedStatus'] = 0
        w['match'] = False
        self.assertRejected(records)

    def test_wrong_size_requires_einval(self):
        records = self.model.build()
        w = [r for r in records if r.get('type') == 'wrong_size'][0]
        w['syscall'] = {'rc': -1, 'errno': 9, 'errno_name': 'EBADF'}
        w['match'] = False
        self.assertRejected(records)

    def test_get_event_data_empty_status(self):
        records = self.model.build()
        g = [r for r in records
             if r.get('type') == 'get_event_data'][0]
        g['embeddedStatus'] = ST['OK']      # would claim a pending event
        g['match'] = False
        self.assertRejected(records)

    def test_get_event_data_with_more_events_rejected(self):
        records = self.model.build()
        g = [r for r in records
             if r.get('type') == 'get_event_data'][0]
        g['moreEvents'] = 1
        self.assertRejected(records)

    def test_poll_spurious_readiness_is_a_fabricated_event(self):
        records = self.model.build()
        p = [r for r in records if r.get('name') == 'poll_blocking_empty'][0]
        p['revents'] = 1  # POLLIN with nothing queued
        p['empty'] = False
        self.assertRejected(records)

    def test_poll_blocking_must_wait_timeout(self):
        records = self.model.build()
        p = [r for r in records if r.get('name') == 'poll_blocking_empty'][0]
        p['elapsed_ns'] = 1000  # returned far too early
        self.assertRejected(records)

    def test_missing_case_fails(self):
        records = [r for r in self.model.build()
                   if not (r.get('type') == 'event'
                           and r.get('name') == 'opaque_tag_not_fd')]
        self.assertRejected(records)

    def test_extra_duplicate_event_record_fails(self):
        records = self.model.build()
        rec = [r for r in records if r.get('name') == 'alloc_free'][0]
        records.insert(records.index(rec), copy.deepcopy(rec))
        self.assertRejected(records)

    def test_unbalanced_alloc_free_summary_fails(self):
        records = self.model.build()
        s = [r for r in records if r.get('type') == 'summary'][0]
        s['frees_ok'] -= 1
        self.assertRejected(records)

    def test_unbalanced_clients_or_fds_fail(self):
        records = self.model.build()
        s = [r for r in records if r.get('type') == 'summary'][0]
        s['clients_freed'] -= 1
        self.assertRejected(records)
        records = self.model.build()
        s = [r for r in records if r.get('type') == 'summary'][0]
        s['fds_closed'] -= 1
        self.assertRejected(records)

    def test_summary_status_cannot_forge_pass(self):
        records = self.model.build()
        s = [r for r in records if r.get('type') == 'summary'][0]
        s['status'] = 'fail'
        self.assertRejected(records)
        s['errors'] = 0  # status fail with zero errors still fails
        self.assertRejected(records)

    def test_nv0005_must_remain_not_tested(self):
        records = self.model.build()
        s = [r for r in records if r.get('type') == 'summary'][0]
        s['nv0005_notification'] = 'pass'
        self.assertRejected(records)

    def test_meta_driver_and_device_required(self):
        records = self.model.build()
        m = records[0]
        m['driver'] = '535.183'
        self.assertRejected(records)
        records = self.model.build()
        records[0]['device'] = '/dev/nvidia1'
        self.assertRejected(records)

    def test_invalid_json_line_fails(self):
        text = self.model.text() + '{not json\n'
        v = checks.analyze(text, {'device': '/dev/nvidia0',
                                  'poll_ms': POLL_MS})
        self.assertFalse(v['ok'])

    def test_cli_exit_codes(self):
        good = self.model.text()
        bad = good.replace('"embeddedStatus": 26',
                           '"embeddedStatus": 31', 1)
        gp = subprocess.run(
            [sys.executable,
             str(REPO / 'scripts' / 'gpu_os_event_checks.py'),
             '/dev/stdin'], input=good, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        bp = subprocess.run(
            [sys.executable,
             str(REPO / 'scripts' / 'gpu_os_event_checks.py'),
             '/dev/stdin'], input=bad, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.assertEqual(gp.returncode, 0)
        self.assertEqual(bp.returncode, 1)
        self.assertIn('"ok": false', bp.stdout)

    def test_pinned_layout_constants(self):
        self.assertEqual(checks.PINNED_LAYOUT['nv_os_event']['size'], 16)
        self.assertEqual(checks.PINNED_LAYOUT['escapes']['ALLOC_OS_EVENT'],
                         206)
        self.assertEqual(checks.ST_INSUFFICIENT_RESOURCES, 0x1A)
        self.assertEqual(checks.ST_INVALID_EVENT, 0x28)
        self.assertEqual(checks.ST_OPERATING_SYSTEM, 0x59)


class RealBinaryLayoutTests(unittest.TestCase):
    """Runs the real probe binary in device-free --layout-selfcheck."""

    def test_layout_selfcheck_matches_pinned_layout(self):
        if not BIN.exists():
            self.skipTest('event probe binary not built')
        proc = subprocess.run([str(BIN), '--layout-selfcheck'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(proc.returncode, 0,
                         msg=proc.stderr.decode(errors='replace'))
        v = checks.analyze_layout(proc.stdout.decode())
        self.assertTrue(v['ok'], msg=v['reasons'])

    def test_layout_selfcheck_rejects_args(self):
        if not BIN.exists():
            self.skipTest('event probe binary not built')
        proc = subprocess.run([str(BIN), '--layout-selfcheck', '--device',
                               '/dev/nvidia0'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(proc.returncode, 2)

    def test_bad_args_usage_without_device_access(self):
        if not BIN.exists():
            self.skipTest('event probe binary not built')
        proc = subprocess.run([str(BIN), '--bogus'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()
