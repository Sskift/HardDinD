"""Parser/evidence tests for the CUDA lifecycle checker.

No GPU is used.  The model emits the exact JSONL shape of the probe with
the pinned 1 MiB / 8 iterations / 4 workers workload; mutations model
each regression.  The real binary is exercised only on GPU-free argv
paths (usage and child gate EOF, reached before any cuInit).
"""
import copy
import json
import os
import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import cuda_lifecycle_checks as checks

BIN = REPO / 'build' / 'cuda-lifecycle-probe'
NGPU = 4
UUIDS = [f'GPU-aa90a0c1-f2dc-5c66-bf77-bec9f70c7ff{x}' for x in '0123']
EL = 1 << 18
ITER = 8
TCYC = 4
CCYC = 2
THREADS = CHILDREN = 4
PID = 4242


class Model:
    def __init__(self, peer_accessible=True):
        self.peer_accessible = peer_accessible
        self.r = []

    def meta(self):
        return {'type': 'meta', 'schema': 1, 'init_rc': 0,
                'iterations': ITER, 'thread_cycles': TCYC,
                'child_cycles': CCYC, 'threads': THREADS,
                'children': CHILDREN, 'elements': EL,
                'bytes': EL * 4, 'pid': PID}

    def inventory(self):
        return [{'type': 'inventory', 'ordinal': i, 'uuid': UUIDS[i],
                 'uuid_valid': True, 'name': 'NVIDIA A100-PCIE-40GB',
                 'total_mem': 42285268992} for i in range(NGPU)]

    def solo(self):
        out = []
        for i in range(NGPU):
            out.append({'type': 'worker', 'phase': 'solo', 'gpu': i,
                        'ordinal': i, 'uuid': UUIDS[i], 'pid': PID,
                        'iterations': ITER, 'elements': EL,
                        'allocs': ITER * 3, 'frees': ITER * 3,
                        'bytes_checked': ITER * EL * 4,
                        'mismatches': 0, 'create_rc': 0, 'unload_rc': 0,
                        'destroy_rc': 0, 'info_enter_rc': 0,
                        'info_exit_rc': 0, 'free_enter': 14000000000,
                        'free_exit': 14000000000,
                        'start_ns': 1000 + i, 'end_ns': 9000 + i})
        return out

    def threads(self):
        out = []
        for i in range(NGPU):
            out.append({'type': 'worker', 'phase': 'thread', 'worker': i,
                        'ordinal': i, 'uuid': UUIDS[i], 'pid': PID,
                        'tid': 5000 + i, 'cycles': TCYC, 'elements': EL,
                        'allocs': TCYC * 3, 'frees': TCYC * 3,
                        'bytes_checked': TCYC * EL * 4,
                        'mismatches': 0, 'create_rc': 0, 'load_rc': 0,
                        'unload_rc': 0, 'destroy_rc': 0,
                        'ready_ns': 10000 + i,
                        'start_ns': 11000 + i, 'end_ns': 50000 + i})
        return out

    def _cyc(self, role, proc, idx, base=20000):
        return {'cycle': idx, 'seed': 100 + idx + role,
                'role': role, 'proc': proc, 'worker': 0, 'round': 0,
                'alloc_rc': [0, 0, 0], 'htod_rc': 0, 'launch_rc': 0,
                'sync_rc': 0, 'dtoh_rc': 0, 'free_rc': [0, 0, 0],
                'mismatches': 0, 'csum': 12345 + idx,
                'start_ns': base + idx * 100,
                'end_ns': base + idx * 100 + 90}

    def children(self):
        out = []
        rel = 30000
        for i in range(NGPU):
            s, e = 31000 + i, 48000 + i
            out.append({
                'type': 'worker', 'phase': 'child', 'worker': i,
                'ordinal': i, 'uuid': UUIDS[i],
                'child_pid': 6000 + i, 'reported_pid': 6000 + i,
                'reported_ppid': PID, 'received': True, 'exec_errno': 0,
                'child_exit': 0, 'child_signal': 0, 'killed': False,
                'elements': EL, 'cycles': CCYC,
                'allocs': CCYC * 3, 'frees': CCYC * 3,
                'mismatches': 0, 'create_rc': 0, 'load_rc': 0,
                'unload_rc': 0, 'destroy_rc': 0,
                'fork_ns': 20000 + i, 'ready_ns': 25000 + i,
                'release_ns': rel, 'firstbyte_ns': e + 100,
                'wait_ns': 60000 + i, 'start_ns': s, 'end_ns': e,
                'cycles_detail': [self._cyc(3, i + 1, 0, s),
                                  self._cyc(3, i + 1, 1, s + 200)]})
        return out

    def peers(self):
        out = []
        for i in range(NGPU):
            for j in range(NGPU):
                if i == j:
                    continue
                can = self.peer_accessible
                rec = {'type': 'peer', 'from': i, 'to': j,
                       'from_ordinal': i, 'to_ordinal': j,
                       'from_uuid': UUIDS[i], 'to_uuid': UUIDS[j],
                       'can_rc': 0, 'can_access': can,
                       'supported': can,
                       'create_a_rc': 0, 'create_b_rc': 0,
                       'destroy_a_rc': 0, 'destroy_b_rc': 0,
                       'start_ns': 70000 + i * 10 + j,
                       'end_ns': 70900 + i * 10 + j}
                if can:
                    rec.update({'enable_rc': 0, 'htod_rc': 0,
                                'dto_d_rc': 0, 'dtoh_rc': 0,
                                'elements': EL, 'bytes': EL * 4,
                                'mismatches': 0, 'csum': 999})
                else:
                    rec.update({'enable_rc': None, 'htod_rc': None,
                                'dto_d_rc': None, 'dtoh_rc': None,
                                'elements': EL, 'bytes': 0,
                                'mismatches': 0, 'csum': 0})
                out.append(rec)
        return out

    def summary(self):
        return {'type': 'summary', 'schema': 1, 'status': 'pass',
                'device_count': NGPU, 'iterations': ITER,
                'thread_cycles': TCYC, 'child_cycles': CCYC,
                'threads': THREADS, 'children': CHILDREN,
                'elements': EL, 'start_ns': 0, 'end_ns': 99999,
                'pid': PID}

    def build(self):
        return ([self.meta()] + self.inventory() + self.solo()
                + self.threads() + self.children() + self.peers()
                + [self.summary()])

    def text(self, records=None):
        return ''.join(json.dumps(x) + '\n'
                       for x in (records if records is not None
                                 else self.build()))


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.m = Model()

    def verdict(self, records=None, **kw):
        req = {'uuids': UUIDS, 'iterations': ITER, 'thread_cycles': TCYC,
               'child_cycles': CCYC, 'threads': THREADS,
               'children': CHILDREN, 'elements': EL}
        req.update(kw)
        return checks.analyze(self.m.text(records), req)

    def reject(self, records=None, **kw):
        v = self.verdict(records, **kw)
        self.assertFalse(v['ok'], msg=v['reasons'])
        return v['reasons']

    def test_clean_passes(self):
        self.assertTrue(self.verdict()['ok'])

    def test_workload_is_exactly_one_mib(self):
        r = self.m.build()
        r[0]['bytes'] = 100
        self.reject(r)

    # inventory
    def test_inventory_uuid_order(self):
        r = self.m.build()
        r[1]['uuid'], r[2]['uuid'] = r[2]['uuid'], r[1]['uuid']
        self.reject(r)

    def test_inventory_invalid_uuid_marker_fails(self):
        r = self.m.build()
        r[1]['uuid_valid'] = False
        self.reject(r)

    def test_missing_inventory_fails(self):
        r = [x for x in self.m.build()
             if not (x.get('type') == 'inventory' and x.get('ordinal') == 0)]
        self.reject(r)

    # solo
    def test_solo_alloc_free_balance(self):
        r = self.m.build()
        s = [x for x in r if x.get('phase') == 'solo'][0]
        s['frees'] -= 1
        self.reject(r)

    def test_solo_bytes_checked(self):
        r = self.m.build()
        s = [x for x in r if x.get('phase') == 'solo'][1]
        s['bytes_checked'] -= 4
        self.reject(r)

    def test_solo_destroy_cleanup_required(self):
        r = self.m.build()
        s = [x for x in r if x.get('phase') == 'solo'][0]
        s['destroy_rc'] = 700
        self.reject(r)

    # threads
    def test_threads_must_overlap(self):
        r = self.m.build()
        ts = [x for x in r if x.get('phase') == 'thread']
        for k, x in enumerate(ts):
            x['start_ns'] = 70000 + k * 1000
            x['end_ns'] = 70500 + k * 1000
        self.reject(r)

    def test_thread_distinct_tids_and_cards(self):
        r = self.m.build()
        ts = [x for x in r if x.get('phase') == 'thread']
        ts[1]['tid'] = ts[0]['tid']
        self.reject(r)
        r = self.m.build()
        ts = [x for x in r if x.get('phase') == 'thread']
        ts[1]['ordinal'] = 0
        ts[1]['uuid'] = UUIDS[0]
        self.reject(r)

    def test_thread_independent_context_rcs(self):
        r = self.m.build()
        t = [x for x in r if x.get('phase') == 'thread'][0]
        t['create_rc'] = 201
        self.reject(r)

    # children
    def test_child_exit_nonzero(self):
        r = self.m.build()
        c = [x for x in r if x.get('phase') == 'child'][0]
        c['child_exit'] = 1
        self.reject(r)

    def test_child_distinct_pids(self):
        r = self.m.build()
        cs = [x for x in r if x.get('phase') == 'child']
        cs[1]['child_pid'] = cs[0]['child_pid']
        cs[1]['reported_pid'] = cs[0]['reported_pid']
        self.reject(r)

    def test_child_handshake_order(self):
        r = self.m.build()
        c = [x for x in r if x.get('phase') == 'child'][0]
        c['ready_ns'] = c['release_ns'] + 1
        self.reject(r)
        r = self.m.build()
        c = [x for x in r if x.get('phase') == 'child'][0]
        c['cycles_detail'][0]['start_ns'] = c['release_ns'] - 5
        self.reject(r)

    def test_child_overlap(self):
        r = self.m.build()
        cs = [x for x in r if x.get('phase') == 'child']
        for k, c in enumerate(cs):
            c['start_ns'] = 80000 + k * 20000
            c['end_ns'] = 80500 + k * 20000
            c['firstbyte_ns'] = c['end_ns'] + 1
        self.reject(r)

    def test_child_cycles_detail(self):
        r = self.m.build()
        c = [x for x in r if x.get('phase') == 'child'][0]
        c['cycles_detail'][0]['dtoh_rc'] = 1
        self.reject(r)
        r = self.m.build()
        c = [x for x in r if x.get('phase') == 'child'][0]
        c['cycles_detail'][0]['role'] = 1
        self.reject(r)

    def test_child_missing_report(self):
        r = self.m.build()
        c = [x for x in r if x.get('phase') == 'child'][0]
        c['received'] = False
        self.reject(r)

    # peer
    def test_peer_matrix_complete(self):
        r = self.m.build()
        r = [x for x in r if not (x.get('type') == 'peer'
                                  and x.get('from') == 0 and x.get('to') == 1)]
        self.reject(r)

    def test_peer_matrix_no_duplicates(self):
        r = self.m.build()
        p = [x for x in r if x.get('type') == 'peer']
        r.insert(r.index(p[0]), copy.deepcopy(p[0]))
        self.reject(r)

    def test_accessible_pair_requires_successful_transfer(self):
        r = self.m.build()
        p = [x for x in r if x.get('type') == 'peer'][0]
        p['dto_d_rc'] = 1
        self.reject(r)
        r = self.m.build()
        p = [x for x in r if x.get('type') == 'peer'][0]
        p['mismatches'] = 3
        self.reject(r)

    def test_accessible_pair_enable_required(self):
        r = self.m.build()
        p = [x for x in r if x.get('type') == 'peer'][0]
        p['enable_rc'] = 101
        self.reject(r)

    def test_unsupported_pair_honestly_recorded_passes(self):
        # One pair inaccessible: null transfer fields, supported=false is
        # honest evidence, NOT skip-as-pass. Must still pass.
        m2 = Model(peer_accessible=True)
        r = m2.build()
        p = [x for x in r if x.get('type') == 'peer'
             and x.get('from') == 0 and x.get('to') == 1][0]
        p.update({'can_access': False, 'supported': False,
                  'enable_rc': None, 'htod_rc': None, 'dto_d_rc': None,
                  'dtoh_rc': None, 'bytes': 0, 'csum': 0})
        v = checks.analyze(m2.text(r),
                          {'uuids': UUIDS, 'iterations': ITER,
                           'thread_cycles': TCYC, 'child_cycles': CCYC,
                           'threads': THREADS, 'children': CHILDREN,
                           'elements': EL})
        self.assertTrue(v['ok'], msg=v['reasons'])
        self.assertEqual(v['peer_accessible_pairs'], 11)

    def test_unsupported_pair_must_not_fake_transfer(self):
        m2 = Model(peer_accessible=False)
        r = m2.build()
        p = [x for x in r if x.get('type') == 'peer'][0]
        p['enable_rc'] = 0          # looks attempted though unsupported
        self.assertFalse(checks.analyze(
            m2.text(r), {'uuids': UUIDS, 'iterations': ITER,
                         'thread_cycles': TCYC, 'child_cycles': CCYC,
                         'threads': THREADS, 'children': CHILDREN,
                         'elements': EL})['ok'])

    def test_supported_must_equal_can(self):
        r = self.m.build()
        p = [x for x in r if x.get('type') == 'peer'][0]
        p['supported'] = False
        self.reject(r)

    def test_peer_context_cleanup(self):
        r = self.m.build()
        p = [x for x in r if x.get('type') == 'peer'][0]
        p['destroy_b_rc'] = 700
        self.reject(r)

    # misc
    def test_error_record_fails(self):
        r = self.m.build()
        r.insert(-1, {'type': 'error', 'stage': 'x'})
        self.reject(r)

    def test_summary_cannot_forge_pass(self):
        r = self.m.build()
        r[-1]['status'] = 'fail'
        self.reject(r)

    def test_invalid_json(self):
        v = checks.analyze(self.m.text() + '{nope\n',
                           {'uuids': UUIDS})
        self.assertFalse(v['ok'])

    def test_cli_exit_codes(self):
        good = self.m.text()
        bad = good.replace('"can_access": true',
                           '"can_access": false', 1)
        base = [sys.executable,
                str(REPO / 'scripts' / 'cuda_lifecycle_checks.py'),
                '/dev/stdin', '--uuids', ','.join(UUIDS)]
        g = subprocess.run(base, input=good, stdout=subprocess.PIPE,
                           text=True)
        b = subprocess.run(base, input=bad, stdout=subprocess.PIPE,
                           text=True)
        self.assertEqual(g.returncode, 0, msg=g.stdout)
        self.assertEqual(b.returncode, 1)


class NativeReferenceParityTests(unittest.TestCase):
    """--native-reference: guest peer parity against native baseline."""

    def req(self, ref=None):
        return {'uuids': UUIDS, 'iterations': ITER,
                'thread_cycles': TCYC, 'child_cycles': CCYC,
                'threads': THREADS, 'children': CHILDREN,
                'elements': EL, 'native_reference': ref}

    def test_identical_native_guest_passes(self):
        text = Model().text()
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl',
                                         delete=False) as f:
            f.write(text)
            ref = f.name
        try:
            v = checks.analyze(text, self.req(ref))
            self.assertTrue(v['ok'], msg=v['reasons'])
        finally:
            os.unlink(ref)

    def _ref_file(self, text):
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_guest_false_when_native_true_fails(self):
        ref = self._ref_file(Model().text())
        guest = Model().build()
        p = [x for x in guest if x.get('type') == 'peer'
             and x.get('from') == 0 and x.get('to') == 1][0]
        p.update({'can_access': False, 'supported': False,
                  'enable_rc': None, 'htod_rc': None, 'dto_d_rc': None,
                  'dtoh_rc': None, 'bytes': 0, 'csum': 0})
        v = checks.analyze(Model().text(guest), self.req(ref))
        self.assertFalse(v['ok'])
        self.assertTrue(any('parity regression' in r for r in v['reasons']))

    def test_guest_true_when_native_false_fails(self):
        native = Model(peer_accessible=False).build()
        ref = self._ref_file(Model().text(native))
        guest = Model(peer_accessible=False).build()
        p = [x for x in guest if x.get('type') == 'peer'
             and x.get('from') == 2 and x.get('to') == 3][0]
        p.update({'can_access': True, 'supported': True, 'enable_rc': 0,
                  'htod_rc': 0, 'dto_d_rc': 0, 'dtoh_rc': 0,
                  'bytes': EL * 4, 'mismatches': 0, 'csum': 7})
        v = checks.analyze(Model().text(guest), self.req(ref))
        self.assertFalse(v['ok'])
        self.assertTrue(any('does not provide' in r for r in v['reasons']))

    def test_reference_uuid_mismatch_fails(self):
        ref_text = Model().text().replace(UUIDS[0], UUIDS[0][:-1] + '9')
        ref = self._ref_file(ref_text)
        v = checks.analyze(Model().text(), self.req(ref))
        self.assertFalse(v['ok'])
        self.assertTrue(any('inventory' in r for r in v['reasons']))

    def test_reference_incomplete_matrix_fails(self):
        native = [x for x in Model().build()
                  if not (x.get('type') == 'peer'
                          and x.get('from') == 1 and x.get('to') == 0)]
        ref = self._ref_file(Model().text(native))
        v = checks.analyze(Model().text(), self.req(ref))
        self.assertFalse(v['ok'])
        self.assertTrue(any('reference peer matrix incomplete' in r
                            for r in v['reasons']))

    def test_reference_missing_transfer_on_accessible_fails(self):
        native = Model().build()
        p = [x for x in native if x.get('type') == 'peer'][0]
        p['dto_d_rc'] = 1
        ref = self._ref_file(Model().text(native))
        v = checks.analyze(Model().text(), self.req(ref))
        self.assertFalse(v['ok'])
        self.assertTrue(any('native accessible pair' in r
                            for r in v['reasons']))

    def test_unreadable_reference_fails(self):
        v = checks.analyze(Model().text(),
                           self.req('/no/such/reference.jsonl'))
        self.assertFalse(v['ok'])
        self.assertTrue(any('unreadable' in r for r in v['reasons']))

    def test_honest_all_false_native_and_guest_passes(self):
        # Local all-false query diagnostic, identical both sides: honest
        # unsupported everywhere, null transfer fields on both.
        ref = self._ref_file(Model(peer_accessible=False).text())
        v = checks.analyze(Model(peer_accessible=False).text(),
                           self.req(ref))
        self.assertTrue(v['ok'], msg=v['reasons'])
        self.assertEqual(v['peer_accessible_pairs'], 0)

    def test_no_reference_still_accepts_honest_local_matrix(self):
        v = checks.analyze(Model(peer_accessible=False).text(),
                           self.req(None))
        self.assertTrue(v['ok'], msg=v['reasons'])


class ArgsFixtureBinaryTests(unittest.TestCase):
    """Direct-function args fixture build artifact."""

    FIX = REPO / 'build' / 'cuda-lifecycle-args-fixture'

    def test_fixture_binary_passes(self):
        if not self.FIX.exists():
            self.skipTest('args fixture binary not built')
        p = subprocess.run([str(self.FIX)], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(p.returncode, 0,
                         msg=p.stdout.decode(errors='replace'))
        rows = [json.loads(l) for l in
                p.stdout.decode().splitlines() if l.strip()]
        self.assertEqual(rows[-1]['failures'], 0)
        self.assertEqual(rows[-1]['checks'], 45)


class RealBinaryTests(unittest.TestCase):
    def run_probe(self, argv, timeout=20):
        return subprocess.run([str(BIN)] + argv,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)

    def test_bad_args_usage(self):
        if not BIN.exists():
            self.skipTest('binary not built')
        self.assertEqual(self.run_probe(['--bogus']).returncode, 2)

    def test_parent_malformed_numeric_args_rejected_before_gpu(self):
        # All rejected during parse/bounds validation, before load_api or
        # any GPU call -- safe GPU-free.
        if not BIN.exists():
            self.skipTest('binary not built')
        cases = [
            ['--iterations', '0'], ['--iterations', '33'],
            ['--iterations', 'abc'], ['--iterations', '1x'],
            ['--iterations', ''], ['--iterations', '+1'],
            ['--iterations', ' 1'],
            ['--cycles', 'abc'],          # alias is also strict
            ['--cycles', '0'],
            ['--thread-cycles', 'x'], ['--thread-cycles', '33'],
            ['--child-cycles', '0'], ['--children', '0'],
            ['--children', '33'], ['--children', '2j'],
            ['--threads', '3'], ['--threads', 'abc'],
            ['--elements', '4095'], ['--elements', '1048577'],
            ['--elements', '1m'], ['--elements', '-1'],
            ['--deadline-ms', '499'], ['--deadline-ms', '60001'],
            ['--deadline-ms', ''],
        ]
        for argv in cases:
            p = self.run_probe(argv)
            self.assertEqual(p.returncode, 2, msg=f'{argv} rc={p.returncode}')

    def test_iterations_alias_documentation_in_usage(self):
        if not BIN.exists():
            self.skipTest('binary not built')
        p = self.run_probe(['--bogus'])
        err = p.stderr.decode(errors='replace')
        self.assertIn('--iterations 1..32', err)
        self.assertIn('deprecated alias', err)

    def _pipes(self):
        return {n: os.pipe() for n in ('gate', 'out', 'rdy', 'rel')}

    def _child_argv(self, pipes, cycles=CCYC, elements=EL, ordinal=0,
                    seq=0):
        return [str(BIN), '--child', '--ordinal', str(ordinal),
                '--seq', str(seq),
                '--gate-fd', str(pipes['gate'][0]),
                '--out-fd', str(pipes['out'][1]),
                '--rdy-fd', str(pipes['rdy'][1]),
                '--rel-fd', str(pipes['rel'][1]),
                '--elements', str(elements), '--cycles', str(cycles)]

    def _run_child(self, pipes, argv):
        keep = sorted({fd for pair in pipes.values() for fd in pair
                       if fd >= 0})
        return subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, pass_fds=keep,
                              timeout=20)

    def test_child_full_argv_gate_eof_returns_3(self):
        # Gate EOF happens before cuInit; full new handshake argv must be
        # accepted by top-level main (regression on v4 whitelist bug).
        if not BIN.exists():
            self.skipTest('binary not built')
        pipes = self._pipes()
        try:
            os.close(pipes['gate'][1])
            p = self._run_child(pipes, self._child_argv(pipes))
            self.assertEqual(p.returncode, 3,
                             msg=p.stderr.decode(errors='replace'))
        finally:
            for a, b in pipes.values():
                for fd in (a, b):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_child_boundary_values_reach_gate_eof(self):
        # Valid boundaries 1/32 cycles, 4096/1MiB elements must parse
        # strictly and reach the gate (rc 3), not be rejected as usage (2).
        if not BIN.exists():
            self.skipTest('binary not built')
        for cycles, elements in ((1, 4096), (32, 1 << 20)):
            pipes = self._pipes()
            try:
                os.close(pipes['gate'][1])
                p = self._run_child(
                    pipes, self._child_argv(pipes, cycles=cycles,
                                            elements=elements))
                self.assertEqual(p.returncode, 3,
                                 msg=f'{cycles}/{elements}: '
                                     f'{p.stderr.decode(errors="replace")}')
            finally:
                for a, b in pipes.values():
                    for fd in (a, b):
                        try:
                            os.close(fd)
                        except OSError:
                            pass

    def test_child_malformed_values_return_2(self):
        if not BIN.exists():
            self.skipTest('binary not built')
        pipes = self._pipes()
        bad_cases = [
            lambda: self._child_argv(pipes, cycles=0),
            lambda: self._child_argv(pipes, cycles=33),
            lambda: self._child_argv(pipes, cycles=0, elements=4095),
            lambda: self._child_argv(pipes, cycles=1,
                                     elements=(1 << 20) + 1),
            lambda: self._child_argv(pipes, cycles=1, ordinal=-1),
        ]
        try:
            # No gate manipulation needed: malformed argv fails before the
            # gate is read; keep all pipe ends open anyway.
            for make_argv in bad_cases:
                p = self._run_child(pipes, make_argv())
                self.assertEqual(p.returncode, 2,
                                 msg=p.stderr.decode(errors='replace'))
        finally:
            for a, b in pipes.values():
                for fd in (a, b):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_child_junk_trailing_chars_return_2(self):
        if not BIN.exists():
            self.skipTest('binary not built')
        pipes = self._pipes()
        try:
            argv = self._child_argv(pipes)
            for idx, bad in (
                    (argv.index('--cycles') + 1, '2x'),
                    (argv.index('--elements') + 1, '262144j')):
                a2 = list(argv)
                a2[idx] = bad
                p = self._run_child(pipes, a2)
                self.assertEqual(p.returncode, 2)
        finally:
            for a, b in pipes.values():
                for fd in (a, b):
                    try:
                        os.close(fd)
                    except OSError:
                        pass


if __name__ == '__main__':
    unittest.main()
