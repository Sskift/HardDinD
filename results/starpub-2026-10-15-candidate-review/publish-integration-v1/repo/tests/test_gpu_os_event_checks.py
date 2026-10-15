"""Tests for the OS-event ABI checker v2.

No GPU is used.  The native model emits the exact JSONL shape of the
v2 probe with pinned 550.120 statuses; mutations model every regression.
The frozen actual-native v1 log (client alloc on the GPU node ->
hClient 0) is included as a fixture and must be rejected.  The real
binary is only run in device-free --layout-selfcheck mode.
"""
import copy
import json
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gpu_os_event_checks as checks

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / 'build' / 'gpu-os-event-probe'
V1_FIXTURE = REPO / 'tests' / 'fixtures' / 'os-event-v1-native-0.jsonl'

ST = {'OK': 0, 'RESOURCES': 0x1A, 'EVENT': 0x28, 'OS': 0x59}
POLL_MS = 30
CTL = '/dev/nvidiactl'
DEV = '/dev/nvidia0'
CASE_NAMES = ['alloc_free', 'duplicate_alloc', 'missing_free',
              'double_free', 'opaque_tag_not_fd',
              'two_independent_clients_a', 'two_independent_clients_b',
              'hdevice_ignored', 'reserved']
TIC_TAG = 0x4F530002
OPAQUE = {'opaque_tag_not_fd': 0x4F530001,
          'two_independent_clients': TIC_TAG,
          'hdevice_ignored': 0x4F530003}


class EventModel:
    """Exact JSONL the healthy v2 probe produces."""

    def __init__(self):
        self.records = []
        self.allocations = self.frees = 0
        # Deterministic distinct nonzero root handles per case index.
        self.clients = {n: 0x1010 + i for i, n in enumerate(CASE_NAMES)}
        self.fd_of = {'ctl': 4, 'gpu_blocking': 3, 'gpu_nonblock': 5}

    def meta(self):
        return {'type': 'meta', 'schema': 1, 'mode': 'native_abi',
                'device': DEV, 'ctl_device': CTL,
                'gpu_char_fd_tag_semantics': 'opaque_numeric',
                'driver': '550.120', 'poll_ms': POLL_MS,
                'device_chain': 'NV01_ROOT alloc/free on ctl '
                                '(NV_CTL_DEVICE_ONLY); OS events on gpu '
                                'char nvfp with opaque numeric tag',
                'sources': {}}

    def opens(self):
        out = []
        for role, nonblock in (('ctl', False), ('gpu_blocking', False),
                               ('gpu_nonblock', True)):
            out.append({'type': 'open', 'role': role, 'node':
                        CTL if role == 'ctl' else DEV,
                        'nonblock': nonblock,
                        'request_flags': 2 | (2048 if nonblock else 0),
                        'syscall': {'rc': 0, 'errno': 0},
                        'fd': self.fd_of[role]})
        return out

    def closes(self):
        return [{'type': 'close', 'role': role, 'fd': fd,
                 'syscall': {'errno': 0}}
                for role, fd in (('ctl', self.fd_of['ctl']),
                                 ('gpu_blocking', self.fd_of['gpu_blocking']),
                                 ('gpu_nonblock',
                                  self.fd_of['gpu_nonblock']))]

    def polls(self):
        return [
            {'type': 'poll', 'name': 'poll_blocking_30',
             'timeout_ms': POLL_MS, 'fd_nonblock': False, 'fd_flags': 524290,
             'syscall': {'rc': 0, 'errno': 0}, 'revents': 0,
             'elapsed_ns': 30_070_000, 'empty': True},
            {'type': 'poll', 'name': 'poll_blocking_0',
             'timeout_ms': 0, 'fd_nonblock': False, 'fd_flags': 524290,
             'syscall': {'rc': 0, 'errno': 0}, 'revents': 0,
             'elapsed_ns': 4_000, 'empty': True},
            {'type': 'poll', 'name': 'poll_nonblock_30',
             'timeout_ms': POLL_MS, 'fd_nonblock': True,
             'fd_flags': 526338,
             'syscall': {'rc': 0, 'errno': 0}, 'revents': 0,
             'elapsed_ns': 30_090_000, 'empty': True},
            {'type': 'poll', 'name': 'poll_nonblock_0',
             'timeout_ms': 0, 'fd_nonblock': True, 'fd_flags': 526338,
             'syscall': {'rc': 0, 'errno': 0}, 'revents': 0,
             'elapsed_ns': 3_500, 'empty': True},
        ]

    def ged(self):
        return {'type': 'get_event_data',
                'name': 'get_event_data_empty',
                'request': {'nr': 82, 'size': 16, 'pEvent_valid': True,
                            'output_sentinel': 'a5'},
                'syscall': {'rc': 0, 'errno': 0},
                'embeddedStatus': ST['OS'], 'moreEvents': 0,
                'output_buffer_touched': False, 'match': True}

    def wrong_size(self):
        return {'type': 'wrong_size', 'name': 'wrong_size_channel',
                'request': {'nr': 206, 'size': 15,
                            'hClient': self.clients['reserved'],
                            'tag': 0x4F530003},
                'syscall': {'rc': -1, 'errno': 22, 'errno_name': 'EINVAL'},
                'embeddedStatus': None, 'expect_syscall_errno': 22,
                'embedded_channel_absent': True, 'match': True}

    def root_records(self):
        out = []
        for i, name in enumerate(CASE_NAMES):
            h = self.clients[name]
            out.append({'type': 'root_alloc', 'case': name, 'index': i,
                        'node': CTL,
                        'syscall': {'rc': 0, 'errno': 0},
                        'embeddedStatus': 0,
                        'returned': {'root': 0, 'parent': 0, 'object': h,
                                     'cls': 2, 'allocParms': 0, 'rights': 0,
                                     'paramsSize': 0, 'flags': 0},
                        'client_handle_field': 'object',
                        'client_handle': h, 'match': True})
        return out

    def root_frees(self):
        return [{'type': 'root_free', 'case': name,
                 'client': self.clients[name], 'node': CTL,
                 'syscall': {'rc': 0, 'errno': 0}, 'embeddedStatus': 0,
                 'returned': {'root': self.clients[name],
                              'parent': self.clients[name],
                              'object': self.clients[name]},
                 'match': True} for name in CASE_NAMES]

    def event(self, name, op, case_for_client, tag, status, hdevice,
              tag_open, fd_role):
        nr = 206 if op == 'alloc' else 207
        if op == 'alloc' and status == ST['OK']:
            self.allocations += 1
        if op == 'free' and status == ST['OK']:
            self.frees += 1
        return {'type': 'event', 'name': name, 'op': op,
                'request': {'nr': nr, 'size': 16,
                            'hClient': self.clients[case_for_client],
                            'hDevice': hdevice, 'tag': tag,
                            'tag_is_open_fd': tag_open,
                            'fd_role': fd_role},
                'syscall': {'rc': 0, 'errno': 0},
                'embeddedStatus': status, 'expectedEmbedded': status,
                'match': True}

    def events(self):
        r = []
        gpu_tag = self.fd_of['gpu_blocking']
        e = lambda *a: r.append(self.event(*a))
        e('alloc_free', 'alloc', 'alloc_free', gpu_tag, ST['OK'], 0,
          True, 'gpu_blocking')
        e('alloc_free', 'free', 'alloc_free', gpu_tag, ST['OK'], 0,
          True, 'gpu_blocking')
        e('duplicate_alloc', 'alloc', 'duplicate_alloc', gpu_tag, ST['OK'],
          0, True, 'gpu_blocking')
        e('duplicate_alloc', 'alloc', 'duplicate_alloc', gpu_tag,
          ST['RESOURCES'], 0, True, 'gpu_blocking')
        e('duplicate_alloc', 'free', 'duplicate_alloc', gpu_tag, ST['OK'],
          0, True, 'gpu_blocking')
        e('missing_free', 'free', 'missing_free', gpu_tag, ST['EVENT'],
          0, True, 'gpu_blocking')
        e('double_free', 'alloc', 'double_free', gpu_tag, ST['OK'], 0,
          True, 'gpu_blocking')
        e('double_free', 'free', 'double_free', gpu_tag, ST['OK'], 0,
          True, 'gpu_blocking')
        e('double_free', 'free', 'double_free', gpu_tag, ST['EVENT'], 0,
          True, 'gpu_blocking')
        e('opaque_tag_not_fd', 'alloc', 'opaque_tag_not_fd',
          OPAQUE['opaque_tag_not_fd'], ST['OK'], 0, False, 'gpu_blocking')
        e('opaque_tag_not_fd', 'free', 'opaque_tag_not_fd',
          OPAQUE['opaque_tag_not_fd'], ST['OK'], 0, False, 'gpu_blocking')
        e('two_independent_clients', 'alloc',
          'two_independent_clients_a', TIC_TAG, ST['OK'], 0, False,
          'gpu_blocking')
        e('two_independent_clients', 'alloc',
          'two_independent_clients_b', TIC_TAG, ST['OK'], 0, False,
          'gpu_nonblock')
        e('two_independent_clients', 'free',
          'two_independent_clients_a', TIC_TAG, ST['OK'], 0, False,
          'gpu_blocking')
        e('two_independent_clients', 'free',
          'two_independent_clients_b', TIC_TAG, ST['OK'], 0, False,
          'gpu_nonblock')
        e('hdevice_ignored', 'alloc', 'hdevice_ignored',
          OPAQUE['hdevice_ignored'], ST['OK'], 0xDEADBEEF, False,
          'gpu_blocking')
        e('hdevice_ignored', 'free', 'hdevice_ignored',
          OPAQUE['hdevice_ignored'], ST['OK'], 0, False, 'gpu_blocking')
        return r

    def summary(self):
        return {'type': 'summary', 'schema': 1,
                'allocations_ok': self.allocations,
                'frees_ok': self.frees,
                'clients_created': 9, 'clients_freed': 9,
                'fds_opened': 3, 'fds_closed': 3, 'cases_skipped': 0,
                'errors': 0, 'nv0005_notification': 'not_tested',
                'internal_cuda': 0, 'status': 'pass'}

    def build(self):
        r = [self.meta()]
        r += self.opens()
        r += self.polls()
        r.append(self.ged())
        r.append(self.wrong_size())
        r += self.root_records()
        r += self.events()
        r += self.root_frees()
        r += self.closes()
        r.append(self.summary())
        return r

    def text(self, records=None):
        return ''.join(json.dumps(x) + '\n'
                       for x in (records if records is not None
                                 else self.build()))


class EventCheckerTests(unittest.TestCase):
    def setUp(self):
        self.model = EventModel()

    def verdict(self, records=None):
        return checks.analyze(
            self.model.text(records),
            {'device': DEV, 'ctl_device': CTL, 'poll_ms': POLL_MS})

    def assertRejected(self, records):
        v = self.verdict(records)
        self.assertFalse(v['ok'], msg=v['reasons'])
        return v['reasons']

    def find(self, records, typ, **kw):
        return [r for r in records if r.get('type') == typ
                and all(r.get(k) == v for k, v in kw.items())]

    def test_clean_model_passes(self):
        self.assertTrue(self.verdict()['ok'])

    # ---- root client chain ----
    def test_root_alloc_must_be_on_ctl(self):
        records = self.model.build()
        a = self.find(records, 'root_alloc')[0]
        a['node'] = DEV
        self.assertRejected(records)

    def test_root_alloc_syscall_failure_fails(self):
        records = self.model.build()
        a = self.find(records, 'root_alloc')[0]
        a['syscall'] = {'rc': -1, 'errno': 22}
        a['match'] = False
        self.assertRejected(records)

    def test_zero_root_handle_fails(self):
        records = self.model.build()
        a = self.find(records, 'root_alloc', case='alloc_free')[0]
        a['returned']['object'] = 0
        a['client_handle'] = 0
        a['match'] = False
        self.assertRejected(records)

    def test_client_handle_is_hObjectNew_not_hRoot(self):
        records = self.model.build()
        a = self.find(records, 'root_alloc', case='alloc_free')[0]
        a['client_handle_field'] = 'root'
        a['match'] = False
        self.assertRejected(records)

    def test_root_clients_must_be_distinct_and_nonzero(self):
        records = self.model.build()
        allocs = self.find(records, 'root_alloc')
        allocs[1]['returned']['object'] = allocs[0]['returned']['object']
        allocs[1]['client_handle'] = allocs[0]['client_handle']
        allocs[1]['match'] = False
        self.assertRejected(records)

    def test_root_free_set_must_match_allocs(self):
        records = self.model.build()
        f = self.find(records, 'root_free', case='reserved')[0]
        f['client'] += 1
        f['match'] = False
        self.assertRejected(records)

    # ---- opens / fds ----
    def test_missing_or_failed_open_fails(self):
        records = self.model.build()
        opens = self.find(records, 'open')
        opens[0]['syscall'] = {'rc': -1, 'errno': 13}
        opens[0]['fd'] = -1
        self.assertRejected(records)
        records = self.model.build()
        records = [r for r in records if not (r.get('type') == 'open'
                                              and r.get('role') == 'ctl')]
        self.assertRejected(records)

    def test_close_fd_must_match_open(self):
        records = self.model.build()
        c = self.find(records, 'close', role='ctl')[0]
        c['fd'] = 99
        self.assertRejected(records)

    # ---- event status channels ----
    def test_duplicate_embedded_resources_not_argument(self):
        records = self.model.build()
        recs = self.find(records, 'event', name='duplicate_alloc')
        recs[1]['embeddedStatus'] = 0x1F
        recs[1]['expectedEmbedded'] = 0x1F
        recs[1]['match'] = False
        self.assertRejected(records)

    def test_duplicate_as_syscall_fails(self):
        records = self.model.build()
        recs = self.find(records, 'event', name='duplicate_alloc')
        recs[1]['syscall'] = {'rc': -1, 'errno': 22}
        recs[1]['match'] = False
        self.assertRejected(records)

    def test_missing_free_embedded_event(self):
        records = self.model.build()
        rec = self.find(records, 'event', name='missing_free')[0]
        rec['embeddedStatus'] = ST['OK']
        rec['match'] = False
        self.assertRejected(records)

    def test_opaque_tag_lookup_null_fails(self):
        records = self.model.build()
        rec = self.find(records, 'event', name='opaque_tag_not_fd')[0]
        rec['request']['tag_is_open_fd'] = None
        rec['match'] = False
        self.assertRejected(records)

    def test_gpu_tag_must_be_open_fd(self):
        records = self.model.build()
        rec = self.find(records, 'event', name='alloc_free')[0]
        rec['request']['tag_is_open_fd'] = False
        rec['match'] = False
        self.assertRejected(records)

    def test_two_clients_distinct_handles_shared_tag(self):
        records = self.model.build()
        recs = self.find(records, 'event', name='two_independent_clients',
                         op='alloc')
        recs[1]['request']['hClient'] = recs[0]['request']['hClient']
        recs[1]['match'] = False
        self.assertRejected(records)
        records = self.model.build()
        recs = self.find(records, 'event', name='two_independent_clients',
                         op='alloc')
        recs[1]['request']['tag'] += 1
        recs[1]['match'] = False
        self.assertRejected(records)

    def test_hdevice_free_sends_zero_alloc_sends_deadbeef(self):
        records = self.model.build()
        recs = self.find(records, 'event', name='hdevice_ignored')
        # alloc must carry DEADBEEF
        recs[0]['request']['hDevice'] = 0
        self.assertRejected(records)
        records = self.model.build()
        recs = self.find(records, 'event', name='hdevice_ignored')
        # free intentionally sends zero, not DEADBEEF
        recs[1]['request']['hDevice'] = 0xDEADBEEF
        self.assertRejected(records)

    def test_event_hclient_must_match_root_alloc(self):
        records = self.model.build()
        rec = self.find(records, 'event', name='alloc_free')[0]
        rec['request']['hClient'] = 0
        rec['match'] = False
        self.assertRejected(records)

    def test_event_with_zero_hclient_rejected(self):
        records = self.model.build()
        rec = self.find(records, 'event', name='alloc_free')[0]
        rec['request']['hClient'] = 0
        self.assertRejected(records)

    # ---- GED / poll ----
    def test_get_event_data_sentinel_and_status(self):
        records = self.model.build()
        g = self.find(records, 'get_event_data')[0]
        g['embeddedStatus'] = ST['OK']
        g['match'] = False
        self.assertRejected(records)
        records = self.model.build()
        g = self.find(records, 'get_event_data')[0]
        g['output_buffer_touched'] = True
        g['match'] = False
        self.assertRejected(records)
        records = self.model.build()
        g = self.find(records, 'get_event_data')[0]
        g['request']['output_sentinel'] = '00'
        self.assertRejected(records)

    def test_poll_spurious_readiness_fails(self):
        records = self.model.build()
        p = self.find(records, 'poll', name='poll_blocking_30')[0]
        p['revents'] = 1
        p['empty'] = False
        self.assertRejected(records)

    def test_nonblock_must_not_shorten_poll_timeout(self):
        # nonblock fd + 30 ms poll must still wait ~30 ms.
        records = self.model.build()
        p = self.find(records, 'poll', name='poll_nonblock_30')[0]
        p['elapsed_ns'] = 4_000
        self.assertRejected(records)

    def test_timeout_zero_immediate_on_both_fd_types(self):
        for name in ('poll_blocking_0', 'poll_nonblock_0'):
            records = self.model.build()
            p = self.find(records, 'poll', name=name)[0]
            p['elapsed_ns'] = 10_000_000
            self.assertRejected(records)

    def test_poll_fd_nonblock_flag_must_match(self):
        records = self.model.build()
        p = self.find(records, 'poll', name='poll_nonblock_0')[0]
        p['fd_nonblock'] = False
        self.assertRejected(records)

    def test_wrong_size_syscall_channel_only(self):
        records = self.model.build()
        w = self.find(records, 'wrong_size')[0]
        w['syscall']['errno'] = 9
        w['match'] = False
        self.assertRejected(records)
        records = self.model.build()
        w = self.find(records, 'wrong_size')[0]
        w['embeddedStatus'] = 0
        w['match'] = False
        self.assertRejected(records)

    def test_skip_record_fails(self):
        records = self.model.build()
        records.insert(-1, {'type': 'skip', 'name': 'alloc_free',
                            'reason': 'root_client_alloc_failed'})
        s = self.find(records, 'summary')[0]
        s['cases_skipped'] = 1
        s['errors'] = 1
        s['status'] = 'fail'
        self.assertRejected(records)

    def test_summary_accounting_nonzero_distinct(self):
        records = self.model.build()
        s = self.find(records, 'summary')[0]
        s['clients_created'] = 0
        self.assertRejected(records)
        records = self.model.build()
        s = self.find(records, 'summary')[0]
        s['clients_freed'] = 8
        self.assertRejected(records)

    def test_nv0005_stays_not_tested(self):
        records = self.model.build()
        self.find(records, 'summary')[0]['nv0005_notification'] = 'pass'
        self.assertRejected(records)

    def test_missing_case_and_extra_record(self):
        records = [r for r in self.model.build()
                   if not (r.get('type') == 'event'
                           and r.get('name') == 'opaque_tag_not_fd')]
        self.assertRejected(records)
        records = self.model.build()
        rec = self.find(records, 'event', name='alloc_free')[0]
        records.insert(records.index(rec), copy.deepcopy(rec))
        self.assertRejected(records)

    def test_meta_chain_required(self):
        records = self.model.build()
        records[0]['ctl_device'] = DEV
        self.assertRejected(records)
        records = self.model.build()
        records[0]['device_chain'] = 'unknown'
        self.assertRejected(records)

    def test_invalid_json(self):
        v = checks.analyze(self.model.text() + '{bad\n',
                           {'device': DEV, 'ctl_device': CTL,
                            'poll_ms': POLL_MS})
        self.assertFalse(v['ok'])

    def test_cli_exit_codes(self):
        good = self.model.text()
        bad = good.replace('"embeddedStatus": 26',
                           '"embeddedStatus": 31', 1)
        base = [sys.executable,
                str(REPO / 'scripts' / 'gpu_os_event_checks.py'),
                '/dev/stdin']
        gp = subprocess.run(base, input=good, stdout=subprocess.PIPE,
                            text=True)
        bp = subprocess.run(base, input=bad, stdout=subprocess.PIPE,
                            text=True)
        self.assertEqual(gp.returncode, 0)
        self.assertEqual(bp.returncode, 1)
        self.assertIn('"ok": false', bp.stdout)

    # ---- frozen actual-native v1 log must fail ----
    def test_actual_native_v1_log_is_rejected(self):
        self.assertTrue(V1_FIXTURE.exists())
        text = V1_FIXTURE.read_text()
        v = checks.analyze(text, {'device': DEV, 'ctl_device': CTL,
                                  'poll_ms': POLL_MS})
        self.assertFalse(v['ok'])
        joined = ' | '.join(v['reasons'])
        # The concrete v1 defects must be named, not just generic fail.
        self.assertIn('error new_client', joined)
        self.assertIn('two_independent_clients', joined)
        self.assertIn('hdevice_ignored', joined)
        self.assertIn('root_alloc', joined)

    def test_pinned_constants(self):
        self.assertEqual(checks.ST_INSUFFICIENT_RESOURCES, 0x1A)
        self.assertEqual(checks.ST_INVALID_EVENT, 0x28)
        self.assertEqual(checks.ST_OPERATING_SYSTEM, 0x59)
        self.assertEqual(checks.PINNED_LAYOUT['nv_os_event']['size'], 16)


class RealBinaryTests(unittest.TestCase):
    def test_layout_selfcheck(self):
        if not BIN.exists():
            self.skipTest('event binary not built')
        proc = subprocess.run([str(BIN), '--layout-selfcheck'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(proc.returncode, 0,
                         msg=proc.stderr.decode(errors='replace'))
        self.assertTrue(checks.analyze_layout(
            proc.stdout.decode())['ok'])

    def test_bad_args(self):
        if not BIN.exists():
            self.skipTest('event binary not built')
        proc = subprocess.run([str(BIN), '--bogus'],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(proc.returncode, 2)

    def test_layout_rejects_extra_args(self):
        if not BIN.exists():
            self.skipTest('event binary not built')
        proc = subprocess.run([str(BIN), '--layout-selfcheck', '--device',
                               DEV], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()
