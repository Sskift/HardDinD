import importlib.util
import contextlib
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock


path = pathlib.Path(__file__).parent / 'publish/net_transfer.py'
spec = importlib.util.spec_from_file_location('net_transfer', path)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class TransferTests(unittest.TestCase):
    def test_restore_bypasses_expired_metric_budget(self):
        work = types.SimpleNamespace(docker=mock.Mock(side_effect=RuntimeError('budget exhausted')),
                                     cleanup=mock.Mock())
        transfer.guest_command(work, 'inspector', 'echo 1 > /setting', cleanup=True)
        work.docker.assert_not_called()
        work.cleanup.assert_called_once_with('exec', 'inspector', 'nsenter', '-t', '1', '-m', '-n', '--',
                                             'chroot', '/proc/1/root', '/bin/sh', '-ec', 'echo 1 > /setting')

    def test_diagnostic_settings_restore_after_failed_transfer(self):
        for metric, failure, original in (
            ('net.egress_stream', 'rps_cpu1', {
                '/sys/class/net/vec0/queues/rx-0/rps_cpus': '0',
                '/sys/class/net/vec0/queues/rx-0/rps_flow_cnt': '128',
                '/proc/sys/net/core/rps_sock_flow_entries': '4096'}),
            ('net.bridge_stream', 'bridge_nf_off', {'/proc/sys/net/bridge/bridge-nf-call-iptables': '1'}),
        ):
            with self.subTest(metric=metric):
                settings = dict(original)
                ctx = types.SimpleNamespace(forward=mock.Mock())
                work = types.SimpleNamespace(ctx=ctx, container=lambda *_: contextlib.nullcontext('inspector'))
                def command(_work, _inspector, script, **_kwargs):
                    args = script.split()
                    if args[0] == 'cat':
                        return settings[args[1]]
                    self.assertEqual(args[0], 'echo')
                    settings[args[3]] = args[1]
                def measure(*_args, **kwargs):
                    if kwargs.get('variant') == failure:
                        raise RuntimeError('transfer failed')
                with mock.patch.object(transfer, 'guest_command', command), \
                        mock.patch.object(transfer, 'measure', measure), \
                        mock.patch.object(transfer, 'topology'):
                    with self.assertRaisesRegex(RuntimeError, 'transfer failed'):
                        transfer.run(work, metric, '10.0.2.2', 1234)
                self.assertEqual(settings, original)
                self.assertEqual(ctx.forward.call_args_list, [mock.call('profile on'), mock.call('profile off')])

    def test_counters_validate_identity_and_monotonicity(self):
        before = transfer.counters('cpu tid RX_CALLS SIGIO\n0 123 10 5\n1 456 0 1\n')
        after = transfer.counters('cpu tid RX_CALLS SIGIO\n0 123 15 7\n1 456 2 3\n')
        self.assertEqual(transfer.counter_delta(before, after)['total'], {'RX_CALLS': 7, 'SIGIO': 4})
        for bad in ('cpu tid RX_CALLS SIGIO\n0 999 15 7\n1 456 2 3\n',
                    'cpu tid RX_CALLS SIGIO\n0 123 9 7\n1 456 2 3\n',
                    'cpu tid RX_CALLS SIGIO\n0 123 15 7\n'):
            with self.assertRaises(ValueError):
                transfer.counter_delta(before, transfer.counters(bad))
        for bad in ('cpu tid X\n0 123\n', 'cpu tid X\n0 123 -1\n', 'cpu tid X\n',
                    'cpu tid X\n0 123 1\n0 123 2\n'):
            with self.assertRaises(ValueError):
                transfer.counters(bad)

    def test_tasks_do_not_double_count_process_aggregate(self):
        def stat(pid, user):
            fields = ['S', '1', '10'] + ['0'] * 8 + [str(user), '3'] + ['0'] * 6 + ['90']
            return f'{pid} (linux) ' + ' '.join(fields)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            proc = root / '10'; proc.mkdir()
            (proc / 'stat').write_text(stat(10, 300))
            (proc / 'status').write_text('State:\tS (sleeping)\n')
            for tid, user in ((10, 100), (11, 200)):
                task = proc / 'task' / str(tid); task.mkdir(parents=True)
                (task / 'stat').write_text(stat(tid, user))
                (task / 'status').write_text('State:\tS (sleeping)\nvoluntary_ctxt_switches:\t7\n')
                (task / 'wchan').write_text('futex_wait')
            rows = transfer.tasks(10, root)
            self.assertEqual(sum(row['user'] for row in rows.values()), 300)
            self.assertEqual({row['tid'] for row in rows.values()}, {10, 11})
            (proc / 'task/10/children').write_text('20')
            (proc / 'task/11/children').write_text('')
            child = root / '20/task/20'; child.mkdir(parents=True)
            (child / 'stat').write_text(stat(20, 50))
            (child / 'children').write_text('')
            fast = transfer.tasks(10, root, pids=[10], follow_children=True, cpu_only=True)
            self.assertEqual(sum(row['user'] for row in fast.values()), 350)
            self.assertEqual(len(fast), 3)
            self.assertIsNone(fast[(20, 90)]['voluntary'])
            self.assertEqual(fast.errors, [])
            (proc / 'task/11/status').unlink()
            partial = transfer.tasks(10, root)
            self.assertEqual(len(partial.errors), 1)
            self.assertFalse(transfer.thread_delta(partial, partial, 100, {10: 0})['complete'])

    def test_thread_kind_and_reused_tid(self):
        def row(tid, comm, user):
            return dict(tgid=10, tid=tid, comm=comm, user=user, system=0, voluntary=0,
                        involuntary=0, state='S', wchan='wait')
        before = {(10, 1): row(10, 'linux', 100), (11, 1): row(11, 'uml-sigio', 100),
                  (12, 1): row(12, 'uml-userspace', 100)}
        after = {(10, 1): row(10, 'linux', 110), (11, 1): row(11, 'uml-sigio', 102),
                 (12, 2): row(12, 'uml-userspace', 1)}
        delta = transfer.thread_delta(before, after, 100, {10: 0})
        self.assertEqual(delta['groups']['vcpu']['user'], 0.1)
        self.assertEqual(delta['groups']['sigio_helper']['user'], 0.02)
        self.assertFalse(delta['complete'])
        self.assertEqual(len(delta['new']), 1)
        self.assertEqual(len(delta['exited']), 1)

    def test_result_requires_verified_count_and_finite_duration(self):
        self.assertEqual(transfer.result('{"verified":8192,"seconds":1,"value":8192}', 8192)['verified'], 8192)
        for bad in ('{"verified":1,"seconds":1,"value":1}',
                    '{"verified":8192,"seconds":NaN,"value":1}',
                    '{"verified":8192,"seconds":0,"value":1}',
                    '{"verified":8192,"seconds":true,"value":1}'):
            with self.assertRaises(ValueError):
                transfer.result(bad, 8192)

    def test_new_runner_cpu_and_exited_runner_lower_bound(self):
        def row(tid, start, user):
            return dict(tgid=tid, tid=tid, start=start, comm='uml-userspace', user=user,
                        system=0, voluntary=0, involuntary=0, state='S', wchan='wait')
        before = {(10, 1): row(10, 1, 100)}
        sample = {'threads': [row(10, 1, 120), row(20, 102, 7)]}
        after = {(20, 102): row(20, 102, 10)}
        delta = transfer.thread_delta(before, after, 100, {}, [sample], start_tick=100)
        self.assertAlmostEqual(delta['groups']['userspace_runner']['user'], 0.3)
        self.assertFalse(delta['complete'])
        self.assertEqual(len(delta['exited']), 1)
        self.assertEqual(delta['unaccounted'], [])

    def test_marker_pipe_timeout_eof_and_back_to_back_lines(self):
        read, write = os.pipe()
        with os.fdopen(read, 'rb', buffering=0) as output:
            proc = types.SimpleNamespace(stdout=output)
            with self.assertRaises(TimeoutError):
                transfer.read_line(proc, timeout=0)
            os.write(write, b'READY\nDONE\n')
            self.assertEqual(transfer.read_line(proc), 'READY')
            self.assertEqual(transfer.read_line(proc), 'DONE')
            os.close(write)
            with self.assertRaises(RuntimeError):
                transfer.read_line(proc)
