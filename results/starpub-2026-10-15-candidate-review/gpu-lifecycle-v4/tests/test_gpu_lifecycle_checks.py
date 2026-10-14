"""Parser/validator tests for the CUDA lifecycle probe JSONL contract.

These tests build small structural models of probe output: one complete,
internally consistent model that must pass, and targeted mutations
(omitted GPU/worker/cycle, forged summary, failed cleanup, duplicate
identity, serial timing) that each must be rejected.  No CUDA calls are
made anywhere; acceptance comes entirely from parsing real evidence.
"""
import copy
import json
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import gpu_lifecycle_checks as checks

REPO = pathlib.Path(__file__).resolve().parents[1]
PID = 4242


def uuid(i):
    return f'GPU-{i + 1:08x}-0000-0000-0000-000000000000'


class Model:
    def __init__(self, ngpu=4, threads=4, children=4, cycles=8,
                 child_cycles=2, ctx_rounds=2, elements=262144):
        self.ngpu = ngpu
        self.threads = threads
        self.children = children
        self.cycles = cycles
        self.child_cycles = child_cycles
        self.ctx_rounds = ctx_rounds
        self.elements = elements
        self.uuids = [uuid(i) for i in range(ngpu)]

    def request(self, **over):
        req = {'uuids': self.uuids, 'threads': self.threads,
               'children': self.children, 'cycles': self.cycles,
               'child_cycles': self.child_cycles,
               'elements': self.elements, 'ctx_rounds': self.ctx_rounds}
        req.update(over)
        return req

    def cycle(self, idx, role, proc, worker, round_, start=1000, end=2000):
        seed = checks.cycle_seed(role, proc, worker, round_, idx)
        return {'cycle': idx, 'seed': seed, 'role': role, 'proc': proc,
                'worker': worker, 'round': round_,
                'alloc_rc': [0, 0, 0], 'htod_rc': 0, 'launch_rc': 0,
                'sync_rc': 0, 'dtoh_rc': 0, 'free_rc': [0, 0, 0],
                'mismatches': 0, 'csum': 12345,
                'start_ns': start + idx, 'end_ns': end + idx}

    def cycle_list(self, role, proc, worker, round_, n):
        return [self.cycle(i, role, proc, worker, round_) for i in range(n)]

    def meta(self):
        return {'type': 'meta', 'schema': 1, 'init_rc': 0,
                'cycles': self.cycles, 'child_cycles': self.child_cycles,
                'ctx_rounds': self.ctx_rounds, 'threads': self.threads,
                'children': self.children, 'elements': self.elements,
                'pid': PID}

    def inventory(self):
        return [{'type': 'inventory', 'ordinal': i, 'uuid': self.uuids[i],
                 'uuid_valid': True, 'name': f'ModelGPU{i}',
                 'total_mem': 40_000_000_000}
                for i in range(self.ngpu)]

    def solo_worker(self, i):
        rounds = []
        for r in range(self.ctx_rounds):
            rounds.append({'round': r, 'create_rc': 0, 'load_rc': 0,
                           'unload_rc': 0, 'destroy_rc': 0,
                           'info_enter_rc': 0, 'info_exit_rc': 0,
                           'free_enter': 30_000_000_000,
                           'free_exit': 30_000_000_000,
                           'cycles': self.cycle_list(
                               checks.ROLE_SOLO, 0, i, r, self.cycles)})
        return {'type': 'worker', 'phase': 'solo', 'worker': i,
                'ordinal': i, 'uuid': self.uuids[i], 'pid': PID,
                'tid': PID, 'elements': self.elements,
                'ctx_rounds': self.ctx_rounds, 'cycles': self.cycles,
                'allocs': self.ctx_rounds * self.cycles * 3,
                'frees': self.ctx_rounds * self.cycles * 3, 'rounds': rounds,
                'start_ns': 100, 'end_ns': 900_000}

    def shared_ctx(self):
        return {'type': 'ctx', 'phase': 'shared', 'ordinal': 0,
                'uuid': self.uuids[0], 'threads': self.threads,
                'create_rc': 0, 'load_rc': 0, 'unload_rc': 0,
                'destroy_rc': 0}

    def shared_worker(self, i):
        return {'type': 'worker', 'phase': 'shared', 'worker': i,
                'ordinal': 0, 'uuid': self.uuids[0], 'pid': PID,
                'tid': 500 + i, 'elements': self.elements,
                'cycles': self.cycles, 'allocs': self.cycles * 3,
                'frees': self.cycles * 3, 'enter_rc': 0, 'attach_rc': 0,
                'current_match': True, 'unset_rc': 0, 'detach_rc': 0,
                'ready_ns': 190 + i, 'start_ns': 200 + i,
                'end_ns': 90_000 + i,
                'cycles_detail': self.cycle_list(
                    checks.ROLE_SHARED, 0, i, 0, self.cycles)}

    def multi_worker(self, i):
        return {'type': 'worker', 'phase': 'multicard', 'worker': i,
                'ordinal': i, 'uuid': self.uuids[i], 'pid': PID,
                'tid': 700 + i, 'elements': self.elements,
                'cycles': self.cycles, 'allocs': self.cycles * 3,
                'frees': self.cycles * 3, 'create_rc': 0, 'load_rc': 0,
                'unload_rc': 0, 'destroy_rc': 0, 'ready_ns': 190 + i,
                'start_ns': 200 + i, 'end_ns': 90_000 + i,
                'cycles_detail': self.cycle_list(
                    checks.ROLE_MULTICARD, 0, i, 0, self.cycles)}

    def child_worker(self, i, ordinal=None, start=300, end=90_500,
                     fork=100, ready=200, release=250, wait=120_000):
        ordinal = i % self.ngpu if ordinal is None else ordinal
        cycles = self.cycle_list(
            checks.ROLE_CHILD, i + 1, 0, 0, self.child_cycles)
        return {'type': 'worker', 'phase': 'child', 'worker': i,
                'ordinal': ordinal, 'uuid': self.uuids[ordinal],
                'child_pid': 5000 + i, 'reported_pid': 5000 + i,
                'reported_ppid': PID, 'received': True, 'exec_errno': 0,
                'child_exit': 0, 'child_signal': 0, 'killed': False,
                'elements': self.elements, 'cycles': self.child_cycles,
                'allocs': self.child_cycles * 3,
                'frees': self.child_cycles * 3, 'mismatches': 0,
                'create_rc': 0, 'load_rc': 0, 'unload_rc': 0,
                'destroy_rc': 0, 'fork_ns': fork + i,
                'ready_ns': ready + i, 'release_ns': release,
                'firstbyte_ns': 91_000 + i, 'wait_ns': wait + i,
                'start_ns': start + i, 'end_ns': end + i,
                'cycles_detail': cycles}

    def records(self):
        out = [self.meta()]
        out += self.inventory()
        out += [self.solo_worker(i) for i in range(self.ngpu)]
        out.append(self.shared_ctx())
        out += [self.shared_worker(i) for i in range(self.threads)]
        out += [self.multi_worker(i) for i in range(self.ngpu)]
        out += [self.child_worker(i) for i in range(self.children)]
        out.append({'type': 'summary', 'schema': 1, 'status': 'pass',
                    'device_count': self.ngpu, 'cycles': self.cycles,
                    'child_cycles': self.child_cycles,
                    'ctx_rounds': self.ctx_rounds, 'threads': self.threads,
                    'children': self.children, 'elements': self.elements,
                    'start_ns': 100, 'end_ns': 200_000, 'pid': PID})
        return out

    def text(self, records=None):
        return ''.join(json.dumps(r, separators=(',', ':')) + '\n'
                       for r in (records or self.records()))


class LifecycleValidatorTests(unittest.TestCase):
    def setUp(self):
        self.model = Model()

    def verdict(self, records=None, model=None, **req_over):
        m = model or self.model
        return checks.analyze(m.text(records), m.request(**req_over))

    def assertRejected(self, records=None, model=None, **req_over):
        v = self.verdict(records, model, **req_over)
        self.assertFalse(v['ok'], msg=f'unexpected pass: {v["reasons"]}')
        return v['reasons']

    def test_complete_model_passes_and_proves_overlap(self):
        v = self.verdict()
        self.assertTrue(v['ok'], msg=v['reasons'])
        for phase in ('shared', 'multicard', 'child'):
            self.assertIsNotNone(v['overlap_ns'][phase])
            self.assertGreater(v['overlap_ns'][phase], 0)

    def test_summary_flag_alone_is_ignored(self):
        records = [self.model.meta(),
                   {'type': 'summary', 'schema': 1, 'status': 'pass',
                    'device_count': 4, 'cycles': 8, 'child_cycles': 2,
                    'ctx_rounds': 2, 'threads': 4, 'children': 4,
                    'elements': 262144, 'pid': PID}]
        reasons = self.assertRejected(records)
        self.assertTrue(any('inventory' in r for r in reasons))

    def test_tampered_summary_status_fails(self):
        records = self.model.records()
        records[-1]['status'] = 'fail'
        self.assertRejected(records)

    def test_missing_gpu_uuid_set_fails(self):
        # Request four GPUs, hand in a run that only covered three.
        small = Model(ngpu=3, children=4)
        v = checks.analyze(small.text(), self.model.request())
        self.assertFalse(v['ok'])

    def test_omitted_inventory_card_fails(self):
        records = [r for r in self.model.records()
                   if not (r.get('type') == 'inventory' and r['ordinal'] == 3)]
        self.assertRejected(records)

    def test_omitted_shared_worker_fails(self):
        records = [r for r in self.model.records()
                   if not (r.get('phase') == 'shared'
                           and r.get('type') == 'worker'
                           and r.get('worker') == 3)]
        self.assertRejected(records)

    def test_omitted_multicard_worker_fails(self):
        records = [r for r in self.model.records()
                   if not (r.get('phase') == 'multicard'
                           and r.get('worker') == 2)]
        self.assertRejected(records)

    def test_omitted_child_worker_fails(self):
        records = [r for r in self.model.records()
                   if not (r.get('phase') == 'child'
                           and r.get('worker') == 1)]
        self.assertRejected(records)

    def test_missing_phase_fails(self):
        records = [r for r in self.model.records()
                   if r.get('phase') != 'multicard']
        self.assertRejected(records)

    def test_missing_cycle_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'shared' and r.get('worker') == 0:
                r['cycles_detail'].pop()
        self.assertRejected(records)

    def test_missing_solo_cycle_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'solo' and r.get('worker') == 1:
                r['rounds'][0]['cycles'].pop()
        self.assertRejected(records)

    def test_failed_device_free_is_not_accepted(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'solo' and r.get('worker') == 0:
                r['rounds'][0]['cycles'][3]['free_rc'][2] = 700
                break
        reasons = self.assertRejected(records)
        self.assertTrue(any('free_rc' in r for r in reasons))

    def test_failed_context_destroy_is_not_accepted(self):
        records = self.model.records()
        for r in records:
            if r.get('type') == 'ctx':
                r['destroy_rc'] = 705
        self.assertRejected(records)

    def test_failed_module_unload_in_child_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 0:
                r['unload_rc'] = 401
        self.assertRejected(records)

    def test_duplicate_inventory_uuid_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('type') == 'inventory' and r['ordinal'] == 2:
                r['uuid'] = self.model.uuids[0]
        self.assertRejected(records)

    def test_duplicate_worker_id_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'shared' and r.get('worker') == 1:
                r['worker'] = 0
        self.assertRejected(records)

    def test_duplicate_thread_identity_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'shared' and r.get('worker') == 2:
                r['tid'] = 500
        self.assertRejected(records)

    def test_serial_threads_cannot_pass(self):
        # Intervals laid end-to-start: barrier-free serial execution.
        records = self.model.records()
        for r in records:
            if r.get('type') == 'worker' and r.get('phase') == 'shared':
                i = r['worker']
                r['ready_ns'] = 100 + i * 1000
                r['start_ns'] = 200 + i * 1000
                r['end_ns'] = 300 + i * 1000
        reasons = self.assertRejected(records)
        self.assertTrue(any('coexist' in r for r in reasons))

    def test_serial_children_cannot_pass(self):
        m = self.model
        records = []
        for r in m.records():
            if r.get('phase') == 'child':
                i = r['worker']
                r['fork_ns'] = 100 + i * 1000
                r['release_ns'] = 150 + i * 1000
                r['start_ns'] = 200 + i * 1000
                r['end_ns'] = 300 + i * 1000
                r['firstbyte_ns'] = 310 + i * 1000
                r['wait_ns'] = 400 + i * 1000
            records.append(r)
        self.assertRejected(records)

    def test_reap_before_all_launches_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 3:
                r['wait_ns'] = 50            # before the last fork at 103
        reasons = self.assertRejected(records)
        self.assertTrue(any('reaped' in r or 'timing order' in r
                            for r in reasons))

    def test_child_nonzero_exit_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 2:
                r['child_exit'] = 1
        self.assertRejected(records)

    def test_child_signal_or_kill_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 2:
                r['killed'] = True
                r['child_signal'] = 9
                r['child_exit'] = 0
        self.assertRejected(records)

    def test_child_ppid_mismatch_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 1:
                r['reported_ppid'] = 999
        self.assertRejected(records)

    def test_child_missing_card_coverage_fails(self):
        # Four children but only card 0 is touched.
        m = self.model
        records = []
        for r in m.records():
            if r.get('phase') == 'child':
                r['ordinal'] = 0
                r['uuid'] = m.uuids[0]
            records.append(r)
        reasons = self.assertRejected(records)
        self.assertTrue(any('cover every requested GPU' in r
                            for r in reasons))

    def test_error_record_fails(self):
        records = self.model.records()
        records.insert(5, {'type': 'error', 'stage': 'solo_load', 'errno': 3})
        self.assertRejected(records)

    def test_reordered_records_fail(self):
        records = self.model.records()
        a = next(i for i, r in enumerate(records)
                 if r.get('phase') == 'multicard' and r.get('worker') == 0)
        b = next(i for i, r in enumerate(records)
                 if r.get('phase') == 'multicard' and r.get('worker') == 1)
        records[a], records[b] = records[b], records[a]
        self.assertRejected(records)

    def test_malformed_json_line_fails(self):
        text = self.model.text() + '{"type": "worker", broken\n'
        v = checks.analyze(text, self.model.request())
        self.assertFalse(v['ok'])

    def test_seed_repetition_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'shared' and r.get('worker') == 0:
                r['cycles_detail'][2]['seed'] = r['cycles_detail'][0]['seed']
        self.assertRejected(records)

    def test_wrong_elements_fails(self):
        m = Model(elements=131072)
        self.assertFalse(checks.analyze(m.text(), self.model.request())['ok'])

    def test_duplicate_seed_across_solo_rounds_within_round_fails(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'solo' and r.get('worker') == 0:
                r['rounds'][1]['cycles'][1]['seed'] = \
                    r['rounds'][1]['cycles'][0]['seed']
        self.assertRejected(records)

    def test_mismatched_parameters_fail(self):
        m = self.model
        self.assertFalse(checks.analyze(
            m.text(), m.request(cycles=4))['ok'])
        self.assertFalse(checks.analyze(
            m.text(), m.request(threads=8))['ok'])
        self.assertFalse(checks.analyze(
            m.text(), m.request(ctx_rounds=3))['ok'])
        self.assertFalse(checks.analyze(
            m.text(), m.request(child_cycles=3))['ok'])
        self.assertFalse(checks.analyze(
            m.text(), m.request(children=8))['ok'])

    def test_blank_log_and_empty_request_fail(self):
        self.assertFalse(checks.analyze('', {'uuids': self.model.uuids})['ok'])
        self.assertFalse(checks.analyze(self.model.text(),
                                        {'uuids': []})['ok'])

    def test_inventory_uuids_helper(self):
        uuids = checks.inventory_uuids(self.model.text())
        self.assertEqual(uuids, self.model.uuids)
        text = self.model.text()
        # Duplicate UUID inventory must not be usable as a reference.
        bad = text.replace(self.model.uuids[2], self.model.uuids[0], 1)
        with self.assertRaises(ValueError):
            checks.inventory_uuids(bad)

    def test_cli_exit_codes(self):
        checker = REPO / 'scripts' / 'gpu_lifecycle_checks.py'
        good = pathlib.Path('/tmp/lcv-good.jsonl')
        good.write_text(self.model.text())
        args = [sys.executable, str(checker), str(good)]
        for u in self.model.uuids:
            args += ['--expect-uuid', u]
        rc = subprocess.run(args, stdout=subprocess.PIPE).returncode
        self.assertEqual(rc, 0)
        bad = pathlib.Path('/tmp/lcv-bad.jsonl')
        records = self.model.records()
        records[-1]['status'] = 'fail'
        bad.write_text(self.model.text(records))
        bad_args = [sys.executable, str(checker), str(bad)]
        for u in self.model.uuids:
            bad_args += ['--expect-uuid', u]
        rc = subprocess.run(bad_args, stdout=subprocess.PIPE).returncode
        self.assertEqual(rc, 1)

    def test_runner_preserves_compute_default_and_old_hashes(self):
        runner = (REPO / 'scripts' / 'run-gpu-cuda.py').read_text()
        self.assertIn("--probe", runner)
        self.assertIn("default='compute'", runner)
        # The accepted cuda_probe inputs stay in the hash coverage list.
        self.assertIn("repo/'src/cuda_probe.c'", runner)
        self.assertIn("repo/'src/cuda_ioctl_trace.c'", runner)
        self.assertIn('cuda_lifecycle_probe.c', runner)
        self.assertIn('gpu_lifecycle_checks.py', runner)

    def _all_cycles(self, records):
        out = []
        for r in records:
            if r.get('type') == 'worker' and r.get('phase') == 'solo':
                for roundrec in r.get('rounds', []):
                    out.extend(roundrec.get('cycles', []))
            elif r.get('type') == 'worker':
                out.extend(r.get('cycles_detail', []))
        return out

    def test_seed_namespace_is_global_and_cross_process(self):
        # 4 gpus * 2 rounds * 8 + 4 shared * 8 + 4 multicard * 8
        # + 4 children * 2 = 136 distinct, derivation-correct cycles.
        records = self.model.records()
        cycles = self._all_cycles(records)
        self.assertEqual(len(cycles), 136)
        seeds = [c['seed'] for c in cycles]
        self.assertEqual(len(set(seeds)), 136)
        v = self.verdict(records)
        self.assertTrue(v['ok'], msg=v['reasons'])

    def test_seed_must_derive_from_identity(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'solo' and r.get('worker') == 0:
                r['rounds'][0]['cycles'][0]['seed'] = 12345
        self.assertRejected(records)

    def test_cycle_identity_fields_are_validated(self):
        for field, value in (('role', checks.ROLE_CHILD), ('proc', 99),
                             ('worker', 7), ('round', 3)):
            records = self.model.records()
            for r in records:
                if r.get('phase') == 'shared' and r.get('worker') == 1:
                    r['cycles_detail'][0][field] = value
                    break
            self.assertRejected(records)

    def test_child_cycle_identity_is_per_process(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 1:
                # A post-fork child pretending to be proc 0 (the parent) or
                # child seq 0 must not be accepted.
                r['cycles_detail'][0]['proc'] = 0
        self.assertRejected(records)

    def test_cross_process_seed_reuse_cannot_pass(self):
        # Simulates the v1 bug: fork+exec resets a process-local counter,
        # so child seq 2 repeats child seq 0's pattern.  The run must fail
        # even though allocations and exit statuses all look clean.
        records = self.model.records()
        children = [r for r in records if r.get('phase') == 'child']
        children[2]['cycles_detail'][0]['seed'] = \
            children[0]['cycles_detail'][0]['seed']
        self.assertRejected(records)

    def test_seed_distinctness_holds_between_every_phase_pair(self):
        # Solo round 1 must not reuse round 0, and phases must not overlap.
        records = self.model.records()
        groups = {}
        for c in self._all_cycles(records):
            key = (c['role'], c['proc'], c['worker'], c['round'])
            groups.setdefault(key, set()).add(c['seed'])
        keys = list(groups)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                self.assertFalse(groups[a] & groups[b])

    def test_shared_requires_enter_attach_identity_unset_detach(self):
        for field, value in (('enter_rc', 201), ('attach_rc', 201),
                             ('unset_rc', 1), ('detach_rc', 1)):
            records = self.model.records()
            for r in records:
                if r.get('type') == 'worker' and r.get('phase') == 'shared' \
                        and r.get('worker') == 0:
                    r[field] = value
            self.assertRejected(records)
        records = self.model.records()
        for r in records:
            if r.get('type') == 'worker' and r.get('phase') == 'shared' \
                    and r.get('worker') == 2:
                r['current_match'] = False
        self.assertRejected(records)

    def test_shared_missing_transition_fields_fail(self):
        records = self.model.records()
        for r in records:
            if r.get('type') == 'worker' and r.get('phase') == 'shared' \
                    and r.get('worker') == 0:
                del r['current_match']
        self.assertRejected(records)

    def test_child_requires_ready_handshake(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 1:
                r['ready_ns'] = 0
        self.assertRejected(records)

    def test_child_ready_must_follow_fork_and_precede_release(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 1:
                r['ready_ns'] = r['release_ns'] + 1
        self.assertRejected(records)
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 1:
                r['ready_ns'] = r['fork_ns'] - 1
        self.assertRejected(records)

    def test_child_compute_must_start_after_postsetup_release(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 0:
                r['cycles_detail'][0]['start_ns'] = r['release_ns'] - 5
                r['cycles_detail'][0]['end_ns'] = r['release_ns'] - 1
        self.assertRejected(records)

    def test_child_cycle_interval_must_stay_in_worker_interval(self):
        records = self.model.records()
        for r in records:
            if r.get('phase') == 'child' and r.get('worker') == 0:
                r['cycles_detail'][-1]['end_ns'] = r['end_ns'] + 1
        self.assertRejected(records)

    def test_invalid_inventory_uuid_is_marked_and_rejected(self):
        records = self.model.records()
        for r in records:
            if r.get('type') == 'inventory' and r['ordinal'] == 1:
                r['uuid'] = 'GPU-00000000-0000-0000-0000-000000000000'
                r['uuid_valid'] = False
        self.assertRejected(records)

    def test_missing_uuid_valid_flag_cannot_pass(self):
        records = self.model.records()
        for r in records:
            if r.get('type') == 'inventory':
                del r['uuid_valid']
        self.assertRejected(records)

    def test_grid_divisor_matches_block_width(self):
        src = (REPO / 'src' / 'cuda_lifecycle_probe.c').read_text()
        self.assertIn('(elements + 255u) / 256u', src)
        self.assertNotIn('(elements + 255u) / 255u', src)

    def test_child_line_bounds_are_static_and_fail_closed(self):
        src = (REPO / 'src' / 'cuda_lifecycle_probe.c').read_text()
        self.assertIn('#define CHILD_LINE_MAX 7240', src)
        self.assertIn('#define CHILD_CYCLE_FIELDS 16', src)
        self.assertIn('_Static_assert(CHILD_LINE_MAX', src)
        self.assertIn('child report too large', src)

    def test_seed_derivation_is_deterministic_and_matches_constants(self):
        self.assertEqual(checks.cycle_seed(checks.ROLE_CHILD, 1, 0, 0, 0),
                         2537644613)   # C fixture seed_table row 5
        self.assertEqual(checks.cycle_seed(checks.ROLE_CHILD, 2, 0, 0, 0),
                         2078791858)   # row 6
        self.assertEqual(checks.cycle_seed(checks.ROLE_CHILD, 5, 0, 0, 1),
                         973914739)    # row 7
        # Pure function, reproducible across runs (no pid/time entropy).
        self.assertEqual(checks.cycle_seed(1, 0, 3, 1, 7),
                         checks.cycle_seed(1, 0, 3, 1, 7))

    def test_duplicate_gpu_arg_is_rejected_at_parser_level(self):
        src = (REPO / 'src' / 'cuda_lifecycle_probe.c').read_text()
        self.assertIn('duplicate', src)   # parse_ordinals rejects repeats

    def test_host_fixture_log_evidence_cross_checks_seeds(self):
        log = REPO / 'build' / 'host-fixture.log'
        if not log.exists():
            self.skipTest('host fixture log not built in this checkout')
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        cases = {r['case']: r for r in rows}
        expected = [
            checks.cycle_seed(*t) for t in (
                (checks.ROLE_SOLO, 0, 0, 0, 0),
                (checks.ROLE_SOLO, 0, 3, 1, 7),
                (checks.ROLE_SHARED, 0, 0, 0, 0),
                (checks.ROLE_SHARED, 0, 3, 0, 7),
                (checks.ROLE_MULTICARD, 0, 2, 0, 4),
                (checks.ROLE_CHILD, 1, 0, 0, 0),
                (checks.ROLE_CHILD, 2, 0, 0, 0),
                (checks.ROLE_CHILD, 5, 0, 0, 1))]
        self.assertEqual(cases['seed_table']['seeds'], expected)
        self.assertIs(cases['seed_matrix']['unique'], True)
        self.assertEqual(cases['fixture_summary']['failures'], 0)
        for name in ('nan', 'plus_inf', 'minus_inf', 'negative',
                     'oob_large', 'oob_above_uint32'):
            row = cases[f'validate_{name}']
            self.assertEqual(row['bad_rc'], 1)
            self.assertEqual(row['mismatches'], 1)
        self.assertIs(row['csum_excludes_corrupt'], True)
        self.assertEqual(cases['validate_all_nan']['csum'], 0)
        b = cases['report_bounds']
        self.assertIs(b['fits'], True)
        self.assertIs(b['tiny_buffer_rejected'], True)
        self.assertIs(b['prefix_within_margin'], True)
        self.assertIs(b['token_within_margin'], True)
        self.assertLessEqual(b['full_len'], b['CHILD_LINE_MAX'])
        self.assertEqual(cases['parse_truncated']['rc'], -1)
        self.assertEqual(cases['parse_count_mismatch']['rc'], -1)
        self.assertEqual(cases['parse_garbage']['rc'], -1)
        self.assertEqual(cases['parse_max_cycles']['rc'], 0)
        self.assertIs(cases['parse_max_cycles']['identity_restored'], True)
        for name in ('parse_missing_newline', 'parse_trailing_junk',
                     'parse_wrong_seq', 'parse_unknown_key',
                     'parse_duplicate_key', 'parse_bad_uuid'):
            self.assertEqual(cases[name]['rc'], -1)
        for name in (k for k in cases if k.startswith('parse_bad_cycle_')):
            self.assertEqual(cases[name]['rc'], -1)
        proto_log = REPO / 'build' / 'protocol-regression.log'
        if not proto_log.exists():
            self.skipTest('protocol regression log not built')
        prow = [json.loads(l) for l in proto_log.read_text().splitlines()]
        pcases = {r['case']: r for r in prow}
        self.assertIs(pcases['max_token']['preserved'], True)
        self.assertLessEqual(pcases['max_token']['bytes'], 216)
        self.assertIs(pcases['numeric_and_field_bounds']['all_rejected'],
                      True)
        self.assertEqual(pcases['protocol_regression_summary']['failures'], 0)

    def test_protocol_regression_binary_executes_clean_when_built(self):
        binpath = REPO / 'build' / 'owner-protocol-regression'
        if not binpath.exists():
            self.skipTest('protocol regression binary not built')
        proc = subprocess.run([str(binpath)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(proc.returncode, 0,
                         msg=proc.stderr.decode(errors='replace'))

    def test_host_fixture_binary_executes_clean_when_built(self):
        binpath = REPO / 'build' / 'cuda-lifecycle-host-fixture'
        if not binpath.exists():
            self.skipTest('host fixture binary not built in this checkout')
        proc = subprocess.run([str(binpath)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)
        # Non-zero covers fixture assertions and ASan/UBSan aborts.
        self.assertEqual(proc.returncode, 0,
                         msg=proc.stderr.decode(errors='replace'))
        rows = [json.loads(l) for l in
                proc.stdout.decode().splitlines()]
        summary = [r for r in rows if r['case'] == 'fixture_summary']
        self.assertEqual(summary[0]['failures'], 0)

    def test_pinned_ptx_and_doc_references_in_source(self):
        src = (REPO / 'src' / 'cuda_lifecycle_probe.c').read_text()
        self.assertIn('.version 7.0', src)
        self.assertIn('.target sm_70', src)
        self.assertIn('12.4.0', src)
        self.assertIn('group__CUDA__CTX__DEPRECATED', src)
        self.assertIn('group__CUDA__DEVICE', src)
        self.assertIn('550.120', src)
        # The accepted probe must remain untouched by this round.
        old = (REPO / 'src' / 'cuda_probe.c').read_text()
        self.assertIn('"add"', old)


if __name__ == '__main__':
    unittest.main()
