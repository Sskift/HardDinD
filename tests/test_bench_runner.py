import contextlib
import json
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from pipeline import bench


class ParseTests(unittest.TestCase):
    def test_last_json_and_noise(self):
        self.assertEqual(bench.parse_sample('make log\n{"value": 1}\nnoise\n{"value": 2.5, "verified": 3}\n'), 2.5)

    def test_bad_last_json_cannot_fall_back_to_good_sample(self):
        for bad in ['NaN', 'Infinity', '-1', '0', 'true', '"2"', 'null']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bench.parse_sample('{"value": 2}\n{"value": ' + bad + '}')
        for bad in ['', 'hello', '{}', '[]']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bench.parse_sample(bad)

    def test_affinity_ranges(self):
        self.assertEqual(bench.cpuset('0,2-4,3'), {0, 2, 3, 4})
        for value in ['', '1-', '3-2', '-1', 'a', '1,,2']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bench.cpuset(value)

    def test_publish_port(self):
        self.assertEqual(bench.published_port('0.0.0.0:42317\n[::]:42317'), 42317)
        self.assertEqual(bench.published_port('127.0.0.1:1234'), 1234)
        for value in ['[::]:1234', '127.0.0.1:0', 'garbage', '0.0.0.0:99999']:
            with self.assertRaises(ValueError):
                bench.published_port(value)

    def test_build_cache_requires_copy_and_run(self):
        cached = '#4 [1/3] FROM hd-bench:test\n#4 CACHED\n#5 [2/3] COPY marker /marker\n#5 CACHED\n#6 [3/3] RUN test "$(cat /marker)" = hd-bench\n#6 CACHED\n'
        bench.check_build_cache(cached)
        for output in [cached.replace('#6 CACHED', '#6 DONE 0.1s'), cached.replace('#5 CACHED', '#5 DONE 0.1s'),
                       '#4 CACHED\n', '']:
            with self.assertRaisesRegex(RuntimeError, 'COPY and RUN'):
                bench.check_build_cache(output)

    def test_entire_matrix_matches_targets(self):
        self.assertEqual(len(bench.METRICS), 30)
        self.assertFalse(any(name.startswith('gpu.') for name in bench.METRICS))
        for name in bench.METRICS:
            unit, higher = bench.metric_info(name)
            self.assertTrue(unit)
            self.assertEqual(higher, not name.startswith(('app.', 'docker.')))


class FakeRunner:
    def __init__(self, ctx):
        self.ctx = ctx; self.calls = []; self.closed = False
        ctx.runner = self

    def prepare(self):
        if self.ctx.prepare_error:
            raise RuntimeError('cannot import')
        return {'root_key': 'abc'}

    def begin_metric(self, name):
        return bench.metric_budget(name, self.ctx.quick)

    def durability(self):
        return {'error': 'guest fsync not durable: test evidence'}

    @contextlib.contextmanager
    def samples(self, name):
        def sample(side):
            self.calls.append((name, side))
            if name == self.ctx.fail_metric and side == 'guest':
                raise RuntimeError('bad checksum')
            return '{"value": 12, "verified": 1}'
        yield (None if name.startswith('docker.') else lambda: sample('native')), lambda: sample('guest')

    def close(self):
        self.closed = True


class RunnerTests(unittest.TestCase):
    def ctx(self, names, quick=True, fail=None, prepare=False):
        return types.SimpleNamespace(bench_cpus='8-11', quick=quick, selected=lambda n: n in names,
                                     fail_metric=fail, prepare_error=prepare, log=lambda x: None)

    @mock.patch.object(bench, 'Workloads', FakeRunner)
    def test_warmups_pairs_and_selection(self):
        ctx = self.ctx(['cpu.single'])
        data = bench.run(ctx)
        self.assertEqual(set(data['metrics']), {'cpu.single'})
        self.assertEqual(ctx.runner.calls, [('cpu.single', side) for _ in range(3) for side in ['native', 'guest']])
        self.assertEqual(data['metrics']['cpu.single']['native'], [12, 12])
        self.assertEqual(data['metrics']['cpu.single']['guest'], [12, 12])
        self.assertEqual(len(data['host_load']), 2)
        self.assertEqual(data['pairs'], 2)
        self.assertTrue(ctx.runner.closed)
        samples = data['metrics']['cpu.single']['detail']['samples']
        self.assertEqual(len(samples), 6)
        self.assertEqual([s['phase'] for s in samples], ['warmup'] * 2 + ['sample'] * 4)
        for sample in samples:
            self.assertEqual(sample['value'], 12)
            self.assertEqual(len(sample['host_load']), 3)
            self.assertEqual(len(sample['host_load_end']), 3)
            self.assertEqual(sample['workload'], {'value': 12, 'verified': 1})

    @mock.patch.object(bench, 'Workloads', FakeRunner)
    def test_normal_pairs(self):
        ctx = self.ctx(['cpu.multi'], quick=False)
        data = bench.run(ctx)
        self.assertEqual(data['pairs'], 5)
        self.assertEqual(len(data['metrics']['cpu.multi']['native']), 5)
        self.assertEqual(len(ctx.runner.calls), 12)

    @mock.patch.object(bench, 'Workloads', FakeRunner)
    def test_failed_metric_does_not_stop_next(self):
        ctx = self.ctx(['cpu.single', 'cpu.multi'], fail='cpu.single')
        data = bench.run(ctx)
        self.assertIn('bad checksum', data['metrics']['cpu.single']['error'])
        self.assertIsNone(data['metrics']['cpu.multi']['error'])
        self.assertEqual(len(data['metrics']['cpu.multi']['guest']), 2)

    @mock.patch.object(bench, 'Workloads', FakeRunner)
    def test_setup_failure_marks_all_selected(self):
        ctx = self.ctx(['cpu.single', 'app.compile'], prepare=True)
        data = bench.run(ctx)
        for metric in data['metrics'].values():
            self.assertIn('cannot import', metric['error'])
            self.assertEqual(metric['guest'], [])
        self.assertTrue(ctx.runner.closed)

    @mock.patch.object(bench, 'Workloads', FakeRunner)
    def test_control_plane_has_no_native(self):
        ctx = self.ctx(['docker.exec'])
        data = bench.run(ctx)
        metric = data['metrics']['docker.exec']
        self.assertEqual(metric['native'], [])
        self.assertEqual(len(metric['guest']), 2)
        self.assertFalse(metric['higher_is_better'])
        self.assertEqual(ctx.runner.calls, [('docker.exec', 'guest')] * 3)

    @mock.patch.object(bench, 'Workloads')
    def test_empty_selection_does_not_prepare(self, runner):
        self.assertEqual(bench.run(self.ctx([]))['metrics'], {})
        runner.assert_not_called()

    def test_cleanup_error_keeps_results_and_marks_failure(self):
        class BadCleanup(FakeRunner):
            def close(self):
                raise RuntimeError('cleanup failure')
        with mock.patch.object(bench, 'Workloads', BadCleanup):
            result = bench.run(self.ctx(['cpu.single']))
        self.assertEqual(result['metrics']['cpu.single']['guest'], [12, 12])
        self.assertIn('cleanup failure', result['metrics']['cpu.single']['error'])

    def test_archive_contents(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'files.tar'
            bench.small_archive(path)
            with tarfile.open(path) as archive:
                self.assertEqual(len(archive.getmembers()), 20000)
                self.assertEqual(archive.extractfile('12345').read(), b'harddind-file-12345\n')

    def test_package_locks_have_hashes_and_match_requested_versions(self):
        lock = bench.build.LOCK
        packages = {v['package']: v for k, v in lock.items() if k.startswith('bench_apk_')}
        for value in packages.values():
            self.assertRegex(value['sha256'], r'^[a-f0-9]{64}$')
            self.assertIn(value['package'] + '-' + value['version'], lock['bench_package_manifest'])
        for request in lock['bench_packages']:
            name, version = request.split('=')
            self.assertEqual(packages[name]['version'], version)

    def test_partial_pair_is_retained_but_failed(self):
        ctx = self.ctx(['cpu.single'])
        metric = {'native': [], 'guest': []}
        class PartialRunner:
            @contextlib.contextmanager
            def samples(self, _name):
                native = mock.Mock(return_value='{"value": 3}')
                guest = mock.Mock(side_effect=['{"value": 2}', '{"value": 2}', RuntimeError('lost guest')])
                yield native, guest
        with self.assertRaisesRegex(RuntimeError, 'lost guest'):
            bench.measure_pair(ctx, PartialRunner(), 'cpu.single', metric, [], 2)
        self.assertEqual(metric['native'], [3, 3])
        self.assertEqual(metric['guest'], [2])
        self.assertIn('lost guest', metric['detail']['samples'][-1]['error'])

    def test_failed_guest_command_is_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(run_id='test', run_dir=pathlib.Path(tmp), bench_cpus='8-11', quick=True,
                                        log=lambda x: None)
            runner = bench.Workloads(ctx); runner.image = 'hd-bench:test'
            runner.docker = mock.Mock(side_effect=RuntimeError('timed out'))
            runner.cleanup = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                runner.guest('/bin/true')
            args = runner.cleanup.call_args.args
            self.assertEqual(args[:2], ('rm', '-f'))
            self.assertIn('-sample-', args[2])

    def test_container_cleanup_failure_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(run_id='test', run_dir=pathlib.Path(tmp), bench_cpus='8-11', quick=True,
                                        log=lambda x: None)
            runner = bench.Workloads(ctx); runner.image = 'hd-bench:test'
            runner.docker = mock.Mock(return_value='containerid')
            runner.cleanup = mock.Mock(side_effect=RuntimeError('cleanup failed'))
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                with runner.container():
                    pass

    def test_deadline_does_not_block_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(run_id='test', run_dir=pathlib.Path(tmp), bench_cpus='8-11', quick=True,
                                        docker=mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', '')))
            runner = bench.Workloads(ctx); runner.deadline = 0
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                runner.docker('run')
            runner.cleanup('rm', '-f', 'sample')
            ctx.docker.assert_called_once_with('rm', '-f', 'sample', timeout=20)

    def test_metric_budget_resets_after_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(run_id='test', run_dir=pathlib.Path(tmp), bench_cpus='8-11', quick=True)
            runner = bench.Workloads(ctx); runner.deadline = 0
            runner.begin_metric('cpu.single')
            self.assertGreater(runner.timeout(1000), 119)
            runner.deadline = 0
            runner.begin_metric('app.compile')
            self.assertGreater(runner.timeout(1000), 239)

    def test_cleanup_missing_allowed_only_for_uncreated_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(run_id='test', run_dir=pathlib.Path(tmp), bench_cpus='8-11', quick=True,
                                        docker=mock.Mock(return_value=subprocess.CompletedProcess([], 1, '', 'No such image: tag')))
            runner = bench.Workloads(ctx)
            runner.cleanup('image', 'rm', 'tag', missing_ok=True)
            with self.assertRaisesRegex(RuntimeError, 'No such image'):
                runner.cleanup('image', 'rm', 'tag')
            ctx.docker.return_value.stderr = 'permission denied'
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                runner.cleanup('image', 'rm', 'tag', missing_ok=True)

    def test_failed_build_keeps_original_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(run_id='test', run_dir=pathlib.Path(tmp), bench_cpus='8-11', quick=True,
                                        log=lambda _: None)
            runner = bench.Workloads(ctx); runner.image = 'hd-bench:test'
            runner.docker = mock.Mock(side_effect=RuntimeError('build timed out'))
            runner.cleanup = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, 'build timed out'):
                with runner.control_samples('docker.build_cached'):
                    self.fail('failed build yielded a sample')
            self.assertTrue(runner.cleanup.call_args.kwargs['missing_ok'])

    @mock.patch.object(bench, 'Workloads', FakeRunner)
    def test_nondurable_disk_still_produces_samples_and_fails(self):
        metric = bench.run(self.ctx(['disk.seq_write']))['metrics']['disk.seq_write']
        self.assertEqual(metric['native'], [12, 12])
        self.assertEqual(metric['guest'], [12, 12])
        self.assertIn('guest fsync not durable', metric['error'])

    def test_ubd_flush_dispatch_analysis(self):
        source = ('if (op == REQ_OP_FLUSH)\n segs = 0;\nio_req->desc_cnt = segs;\n'
                  'void *io_thread(void *arg) {\n'
                  'for (i = 0; !req->error && i < req->desc_cnt; i++)\n do_io(req, desc);\n}')
        self.assertTrue(bench.ubd_flush_analysis(source)['flush_skipped'])
        fixed = source.replace('for (i', 'if (req_op(req->req) == REQ_OP_FLUSH) do_io(req, NULL);\nfor (i')
        self.assertFalse(bench.ubd_flush_analysis(fixed)['flush_skipped'])

    def test_host_sync_counter_requires_success_and_cow_path(self):
        trace = ('123 10:00:00 fsync(5</x/root.cow>) = 0\n'
                 '123 10:00:01 fdatasync(5</x/root.cow> <unfinished ...>\n'
                 '123 10:00:02 <... fdatasync resumed>) = 0\n'
                 '123 10:00:03 fsync(5</x/root.cow>) = -1 EIO\n'
                 '123 10:00:04 fsync(6</other>) = 0\n'
                 '123 10:00:05 sync_file_range(5</x/root.cow>, 0, 0, 0) = 0\n')
        self.assertEqual(bench.successful_syncs(trace, '/x/root.cow'), 2)

    def test_durability_distinguishes_source_proof_from_denied_trace(self):
        evidence = {'same_filesystem': True, 'cow_descriptors': [{'synchronous': False}],
                    'ubd_source': {'matches_running_kernel': True, 'flush_skipped': True},
                    'native_syncs': 3, 'guest_syncs': 0, 'attached_pids': []}
        error = bench.durability_error(evidence)
        self.assertIn('guest fsync not durable', error)
        self.assertIn('ptrace attach denied', error)
        self.assertNotIn('0 host COW syncs observed', error)
        evidence['ubd_source']['matches_running_kernel'] = False
        self.assertIn('durability unverified', bench.durability_error(evidence))
        evidence['guest_syncs'] = 3
        self.assertIsNone(bench.durability_error(evidence))

    def test_ubd_identity_requires_pinned_bytes_untouched_patches_and_kernel_key(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); relative = 'arch/um/drivers/ubd_kern.c'
            source = root / ('linux-' + bench.build.LOCK['linux']['version']) / relative
            source.parent.mkdir(parents=True); source.write_text('pinned source')
            archive = root / 'linux.tar'
            with tarfile.open(archive, 'w') as tar:
                tar.add(source, arcname=str(source.relative_to(root)))
            patch = root / 'fix.patch'; patch.write_text('another file')
            ctx = types.SimpleNamespace(artifacts={'kernel': root / 'expected/linux'})
            with mock.patch.object(bench.config, 'WORK', root), \
                 mock.patch.object(bench.build, 'fetch', return_value=archive), \
                 mock.patch.object(bench.build, 'series', return_value=[patch]), \
                 mock.patch.object(bench.build, 'candidate_paths', return_value=[]), \
                 mock.patch.object(bench.build, 'key_of', return_value='expected'):
                self.assertTrue(bench.ubd_source_evidence(ctx)['matches_running_kernel'])
                patch.write_text(relative)
                self.assertFalse(bench.ubd_source_evidence(ctx)['matches_running_kernel'])
                patch.write_text('another file'); source.write_text('untracked change')
                self.assertFalse(bench.ubd_source_evidence(ctx)['matches_running_kernel'])
                source.write_text('pinned source'); ctx.artifacts['kernel'] = root / 'different/linux'
                self.assertFalse(bench.ubd_source_evidence(ctx)['matches_running_kernel'])

    def test_network_topologies_use_required_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = types.SimpleNamespace(run_id='test', run_dir=pathlib.Path(tmp), bench_cpus='8-11', quick=True)
            for metric in ('net.publish_stream', 'net.egress_stream', 'net.bridge_rr'):
                with self.subTest(metric=metric):
                    runner = bench.Workloads(ctx); runner.image = 'hd-bench:test'
                    runner.native_server = mock.Mock(return_value=contextlib.nullcontext(2345))
                    runner.container = mock.Mock(return_value=contextlib.nullcontext('server'))
                    runner.guest_server_ready = mock.Mock(); runner.cleanup = mock.Mock()
                    runner.native = mock.Mock(); runner.guest = mock.Mock()
                    def docker(*args):
                        if args[0] == 'port':
                            return '127.0.0.1:4321'
                        if args[0] == 'inspect':
                            return '172.18.0.2'
                        return ''
                    runner.docker = mock.Mock(side_effect=docker)
                    with runner.network_samples(metric) as (native, guest):
                        native(); guest()
                    self.assertEqual(runner.native.call_args_list[0].args[2:4], ('127.0.0.1', '2345'))
                    if metric.startswith('net.publish'):
                        self.assertEqual(runner.native.call_args.args[2:4], ('127.0.0.1', '4321'))
                        self.assertEqual(runner.container.call_args.args[0], ['-p', '127.0.0.1::9000'])
                    elif metric.startswith('net.egress'):
                        self.assertEqual(runner.guest.call_args.args[2:4], ('10.0.2.2', '2345'))
                        self.assertEqual(runner.guest.call_args.kwargs, {})
                    else:
                        self.assertEqual(runner.guest.call_args.args[2:5], ('172.18.0.2', '9000', 'rr'))
                        self.assertEqual(runner.guest.call_args.kwargs['options'][0], '--network')
                        self.assertEqual(runner.cleanup.call_args.args[:2], ('network', 'rm'))


if __name__ == '__main__':
    unittest.main()
