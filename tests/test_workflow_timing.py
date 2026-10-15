"""Pure-Python verifiers for the Docker workflow timing runner.

These tests never boot a guest: they feed crafted raw evidence through the
runner's acceptance logic so that missing, failed, timed-out or empty output
can never be reported as a pass, and they pin the exact-object cleanup plan
and the fixed payloads. Real timing failures are recorded, not inferred.
"""
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "workflow_timing",
    pathlib.Path(__file__).resolve().parents[1] / "scripts/run-workflow-timing.py")
workflow_timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow_timing)

analyzer_spec = importlib.util.spec_from_file_location(
    "workflow_timing_analyzer",
    pathlib.Path(__file__).resolve().parents[1] / "scripts/analyze-workflow-timing.py")
analyzer = importlib.util.module_from_spec(analyzer_spec)
analyzer_spec.loader.exec_module(analyzer)

PAYLOAD_1M_SHA = '324f2e874228934ded6ec26efc322f964bc4566db42279a94267a2b86f5cbce3'
PAYLOAD_64K_SHA = '81fea5e75847997c40c0134b143f25a0469ae7102771f7a8f09a74ba95d2d52f'


def component(stage, rc=0, stdout='ok', stderr='', timed_out=False, expect=(0,),
              tolerated=False, synthetic=False):
    status = 'ok' if not timed_out and rc in expect else ('timeout' if timed_out else 'fail')
    return {'stage': stage, 'docker_args': ['x'], 'exit_code': rc, 'timed_out': timed_out,
            'seconds': 0.01, 'stdout': stdout, 'stderr': stderr,
            'stdout_total_bytes': len(stdout), 'stderr_total_bytes': len(stderr),
            'truncated': False, 'expect': list(expect), 'tolerated': tolerated,
            'status': status, 'synthetic': synthetic, 'overlaps': []}


def passing_observation(workflow, rep=0):
    obs = {'rep': rep,
           'components': [component(stage, stdout='out')
                          for stage in workflow_timing.REQUIRED_STAGES[workflow]],
           'checks': [{'name': name, 'ok': True, 'detail': ''}
                      for name in workflow_timing.REQUIRED_CHECKS[workflow]],
           'objects': [], 'cleanup_components': [], 'cleanup_errors': [], 'error': None,
           'status': 'pending', 'body_seconds': 0.1, 'cleanup_seconds': 0.01}
    return workflow_timing.finalize_observation(obs, workflow)


class FinalizeObservationTests(unittest.TestCase):
    def test_every_workflow_baseline_passes(self):
        for workflow in workflow_timing.WORKFLOWS:
            obs = passing_observation(workflow)
            self.assertEqual(obs['status'], 'pass', workflow)
            self.assertTrue(obs['reaped_cleanly'])

    def test_nonzero_exit_with_real_stderr_is_fail(self):
        obs = passing_observation('lifecycle')
        obs['status'] = 'pending'
        for index, comp in enumerate(obs['components']):
            if comp['stage'] == 'exec_marker':
                obs['components'][index] = component(
                    'exec_marker', rc=126, stderr='OCI runtime exec failed: ...')
                break
        workflow_timing.finalize_observation(obs, 'lifecycle')
        self.assertEqual(obs['status'], 'fail')
        problems = workflow_timing.list_problems({'lifecycle': {'observations': [obs]}})
        self.assertTrue(any(p['kind'] == 'fail' for p in problems))

    def test_command_timeout_is_timeout_not_fail_or_pass(self):
        obs = passing_observation('lifecycle')
        obs['status'] = 'pending'
        obs['components'][3] = component('wait', timed_out=True, expect=(0,))
        obs['error'] = 'component wait status=timeout'
        workflow_timing.finalize_observation(obs, 'lifecycle')
        self.assertEqual(obs['status'], 'timeout')
        problems = workflow_timing.list_problems({'lifecycle': {'observations': [obs]}})
        self.assertEqual([p['kind'] for p in problems].count('timeout'), 1)

    def test_empty_required_output_cannot_pass(self):
        comp = component('run', stdout='   ')
        self.assertFalse(workflow_timing.component_ok(comp, need_output=True))
        comp = component('run', stdout='real output')
        self.assertTrue(workflow_timing.component_ok(comp, need_output=True))

    def test_missing_check_is_incomplete_not_pass(self):
        obs = passing_observation('volume')
        obs['status'] = 'pending'
        obs['checks'] = [c for c in obs['checks'] if c['name'] != 'host_roundtrip_digest']
        workflow_timing.finalize_observation(obs, 'volume')
        self.assertEqual(obs['status'], 'incomplete_fail')
        self.assertIn('host_roundtrip_digest', obs['missing_checks'])

    def test_missing_stage_is_incomplete_not_pass(self):
        obs = passing_observation('image')
        obs['status'] = 'pending'
        obs['components'] = [c for c in obs['components'] if c['stage'] != 'load']
        workflow_timing.finalize_observation(obs, 'image')
        self.assertEqual(obs['status'], 'incomplete_fail')
        self.assertEqual(obs['missing_stages'], ['load'])

    def test_false_check_is_fail(self):
        obs = passing_observation('build')
        obs['status'] = 'pending'
        for check in obs['checks']:
            if check['name'] == 'warm_cache_marker':
                check['ok'] = False
                check['detail'] = 'no CACHED marker in warm build'
        workflow_timing.finalize_observation(obs, 'build')
        self.assertEqual(obs['status'], 'fail')
        problems = workflow_timing.list_problems({'build': {'observations': [obs]}})
        self.assertTrue(any(p['kind'] == 'check_failed' and 'warm_cache_marker' in p['detail']
                            for p in problems))

    def test_not_run_observation_is_listed_not_discarded(self):
        obs = workflow_timing.not_run_observation('net', 2, 'budget gone')
        problems = workflow_timing.list_problems({'net': {'observations': [obs]}})
        self.assertEqual([p['kind'] for p in problems], ['not_run_wall_budget'])
        self.assertEqual(problems[0]['rep'], 2)
        # The never-run observation itself still enumerates what was skipped.
        self.assertTrue(obs['missing_checks'])
        self.assertTrue(obs['missing_stages'])

    def test_cleanup_timeout_leaves_not_clean(self):
        obs = passing_observation('lifecycle')
        obs['cleanup_errors'] = ['container x timed out']
        workflow_timing.finalize_observation(obs, 'lifecycle')
        self.assertFalse(obs['reaped_cleanly'])
        problems = workflow_timing.list_problems({'lifecycle': {'observations': [obs]}})
        self.assertTrue(any(p['kind'] == 'cleanup_timeout' for p in problems))

    def test_expected_nonzero_probe_is_ok(self):
        gone = component('inspect_gone', rc=1, stderr='No such object: x', expect=(1,),
                         tolerated=True)
        self.assertTrue(workflow_timing.component_ok(gone))
        self.assertEqual(gone['status'], 'ok')

    def test_three_observations_one_failure_listed_and_not_repeated(self):
        obs_list = [passing_observation('volume', rep=i) for i in range(3)]
        obs_list[1] = passing_observation('volume', rep=1)
        obs_list[1]['status'] = 'not_run_wall_budget'
        obs_list[1]['components'][2] = component('cp_in', rc=1)
        obs_list[1]['error'] = 'boom'
        workflow_timing.finalize_observation(obs_list[1], 'volume')
        summary = workflow_timing.summarize({'volume': {'observations': obs_list}})
        self.assertEqual(summary['volume']['passed'], 2)
        self.assertFalse(summary['volume']['repeated_observations'])
        problems = workflow_timing.list_problems({'volume': {'observations': obs_list}})
        self.assertTrue(any(p['rep'] == 1 for p in problems))


class PayloadAndParsingTests(unittest.TestCase):
    def test_payload_is_deterministic_and_pinned(self):
        first = workflow_timing.fixed_payload('harddind-wf payload one-mib v1', 1024 * 1024)
        second = workflow_timing.fixed_payload('harddind-wf payload one-mib v1', 1024 * 1024)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1024 * 1024)
        self.assertEqual(workflow_timing.sha256_bytes(first), PAYLOAD_1M_SHA)
        with self.assertRaises(ValueError):
            workflow_timing.fixed_payload('seed', 31)

    def test_digest_line_requires_exact_path_and_hex(self):
        good = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  /payload\n'
        self.assertEqual(
            workflow_timing.digest_line(good, '/payload'),
            'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
        self.assertIsNone(workflow_timing.digest_line(good, '/payload2'))
        self.assertIsNone(workflow_timing.digest_line('abc  /payload\n', '/payload'))

    def test_alpine_release_pinned(self):
        self.assertTrue(workflow_timing.alpine_release_ok('3.24.2\n'))
        self.assertFalse(workflow_timing.alpine_release_ok('3.24.3'))

    def test_nslookup_requires_peer_and_nonloopback_address(self):
        real = ('Server:\t\t127.0.0.11\nAddress:\t127.0.0.11:53\n\n'
                'Name:\t\tharddind-wf-x-srv0\nAddress: 172.18.0.3\n')
        self.assertEqual(workflow_timing.parse_nslookup(real, 'harddind-wf-x-srv0'),
                         ['172.18.0.3'])
        self.assertIsNone(workflow_timing.parse_nslookup(';; connection timed out\n',
                                                         'harddind-wf-x-srv0'))
        self.assertIsNone(workflow_timing.parse_nslookup(real, 'other-name'))

    def test_capped_output_keeps_full_byte_count(self):
        text = 'x' * (workflow_timing.OUTPUT_CAP + 100)
        capped, truncated, total = workflow_timing.capped_output(text)
        self.assertTrue(truncated)
        self.assertEqual(total, len(text))
        self.assertLess(len(capped), total)


class RootfsAndOptionsTests(unittest.TestCase):
    def test_rootfs_hash_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            rootfs = pathlib.Path(directory) / 'rootfs.tar.gz'
            rootfs.write_bytes(b'pinned fixture')
            good = hashlib.sha256(b'pinned fixture').hexdigest()
            info = workflow_timing.verify_rootfs(rootfs, good)
            self.assertEqual(info['bytes'], 14)
            with self.assertRaises(ValueError):
                workflow_timing.verify_rootfs(rootfs, '0' * 64)
            with self.assertRaises(ValueError):
                workflow_timing.verify_rootfs(rootfs / 'missing', good)

    def test_shard_parsing(self):
        self.assertEqual(workflow_timing.parse_shards('lifecycle, net'), ('lifecycle', 'net'))
        for bad in ('', 'lifecycle,lifecycle', 'compose', 'lifecycle,,'):
            with self.assertRaises(ValueError):
                workflow_timing.parse_shards(bad)

    def test_effective_timeout_never_exceeds_remaining_budget(self):
        self.assertEqual(workflow_timing.effective_timeout(deadline=105, now=100,
                                                           per_command=40), 5.0)
        self.assertEqual(workflow_timing.effective_timeout(deadline=105, now=200,
                                                           per_command=40), 0.0)


class ExactCleanupTests(unittest.TestCase):
    def test_cleanup_plan_is_exact_and_never_prunes(self):
        objects = [
            {'kind': 'image', 'name': 'harddind-wf-x-img0'},
            {'kind': 'container', 'name': 'harddind-wf-x-lc0'},
            {'kind': 'volume', 'name': 'harddind-wf-x-vol0'},
            {'kind': 'network', 'name': 'harddind-wf-x-net0'},
            {'kind': 'container', 'name': 'harddind-wf-x-lc0'},
        ]
        plan = workflow_timing.exact_cleanup_commands(objects)
        kinds = [kind for kind, _ in plan]
        self.assertEqual(kinds, ['container', 'volume', 'network', 'image'])
        argv_text = ' '.join(arg for _, argv in plan for arg in argv)
        self.assertNotIn('prune', argv_text)
        for kind, argv in plan:
            self.assertIn('harddind-wf-x', argv[-1])
            # Every removal names exactly one object created by this run.
            if kind == 'image':
                self.assertEqual(argv, ['rmi', argv[-1]])
            else:
                self.assertGreaterEqual(len(argv), 3)
        self.assertEqual(plan[0][1], ['rm', '-f', 'harddind-wf-x-lc0'])
        self.assertEqual(plan[3][1], ['rmi', 'harddind-wf-x-img0'])


def report_fixture(kernel_sha='a' * 64, workflow='lifecycle', statuses=('pass',) * 3):
    observations = []
    for rep, status in enumerate(statuses):
        if status == 'pass':
            obs = passing_observation(workflow, rep)
        elif status == 'not_run_wall_budget':
            obs = workflow_timing.not_run_observation(workflow, rep, 'not scheduled')
        else:
            obs = passing_observation(workflow, rep)
            obs['status'] = 'pending'
            if status == 'timeout':
                obs['components'][0] = component(
                    workflow_timing.REQUIRED_STAGES[workflow][0], timed_out=True)
            else:
                obs['components'][0] = component(
                    workflow_timing.REQUIRED_STAGES[workflow][0], rc=1, stderr='boom')
            obs['error'] = status
            workflow_timing.finalize_observation(obs, workflow)
        observations.append(obs)
    return {
        'kind': 'docker_workflow_timing', 'experiment': 'exp-' + kernel_sha[:6],
        'shards': [workflow], 'reps_requested': len(statuses),
        'full_acceptance': False, 'near_native_acceptance': False,
        'native_docker_comparison': 'not_tested', 'performance_threshold_enforced': False,
        'inputs': {
            'kernel': {'sha256': kernel_sha},
            'disk': {'sha256': 'd' * 64, 'mode': 'uml_cow'},
            'rootfs': {'sha256': 'r' * 64, 'bytes': 1},
            'payload_one_mib': {'sha256': PAYLOAD_1M_SHA, 'bytes': 1048576},
            'payload_64kib': {'sha256': 'b' * 64, 'bytes': 65536}},
        'workflows': {workflow: {'observations': observations}},
        'inputs_unchanged': True,
        'instance_cleanup': {'clean_shutdown': True, 'forced_shutdown': False},
        'leftovers': {'objects': {}}}


class OverallGateTests(unittest.TestCase):
    def test_clean_run_passes_structural_gate_but_never_native(self):
        report = report_fixture()
        self.assertTrue(workflow_timing.overall_passed(report))
        for tampered, key, value in (
                ('near native', 'near_native_acceptance', True),
                ('full', 'full_acceptance', True),
                ('native', 'native_docker_comparison', 'equal'),
                ('threshold', 'performance_threshold_enforced', True)):
            broken = json.loads(json.dumps(report))
            broken[key] = value
            self.assertFalse(workflow_timing.overall_passed(broken), tampered)

    def test_failed_timeout_dirty_instance_and_leftovers_fail(self):
        for statuses, key, bad in (
                (('pass', 'fail', 'pass'), 'obs', None),
                (('pass', 'timeout', 'pass'), 'obs', None),
                (('pass', 'not_run_wall_budget', 'pass'), 'obs', None),
                (('pass',) * 3, 'clean', False),
                (('pass',) * 3, 'leftover', True)):
            report = report_fixture(statuses=statuses)
            if key == 'clean':
                report['instance_cleanup']['clean_shutdown'] = bad
                report['instance_cleanup']['forced_shutdown'] = True
            elif key == 'leftover':
                report['leftovers']['objects'] = {'containers': ['deadbeef']}
            self.assertFalse(workflow_timing.overall_passed(report), (statuses, key))

    def test_missing_reps_cannot_pass(self):
        report = report_fixture(statuses=('pass', 'pass'))
        self.assertFalse(workflow_timing.overall_passed(report))


class AnalyzerTests(unittest.TestCase):
    def test_kernel_join_keeps_failures_and_pins_other_inputs(self):
        first = report_fixture(kernel_sha='a' * 64)
        second = report_fixture(kernel_sha='b' * 64,
                                statuses=('pass', 'timeout', 'pass'))
        result = analyzer.compare([first, second])
        self.assertEqual(len(result['kernels']), 2)
        self.assertTrue(result['fixed_inputs_match_except_kernel'])
        by_sha = {item['kernel_sha256']: item for item in result['kernels']}
        good = by_sha['a' * 64]['workflows']['lifecycle']
        bad = by_sha['b' * 64]['workflows']['lifecycle']
        self.assertTrue(good['repeated_observations'])
        self.assertFalse(bad['repeated_observations'])
        self.assertEqual(bad['observations']['timeout'], 1)
        self.assertEqual(len(bad['problems']), 1)
        self.assertTrue(good['stage_seconds']['create']['median_seconds'] >= 0)
        self.assertEqual(result['native_docker_comparison'], 'not_tested')
        self.assertFalse(result['near_native_acceptance'])

    def test_changed_rootfs_is_flagged(self):
        first = report_fixture(kernel_sha='a' * 64)
        second = report_fixture(kernel_sha='b' * 64)
        second['inputs']['rootfs']['sha256'] = 'f' * 64
        result = analyzer.compare([first, second])
        self.assertFalse(result['fixed_inputs_match_except_kernel'])
        self.assertIn('exp-' + 'b' * 6, result['fixed_input_mismatch_experiments'])

    def test_wrong_report_kind_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'x.json'
            path.write_text(json.dumps({'kind': 'other'}))
            with self.assertRaises(ValueError):
                analyzer.load_reports([path])


if __name__ == '__main__':
    unittest.main()
