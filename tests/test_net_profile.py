import importlib.util
import contextlib
import pathlib
import unittest
from unittest import mock


path = pathlib.Path(__file__).parent / 'publish/net_profile.py'
spec = importlib.util.spec_from_file_location('net_profile', path)
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class ProfileTests(unittest.TestCase):
    def test_socket_rows_only_include_owned_sockets(self):
        rows = profile.socket_rows('State Recv-Q Send-Q\n'
            'ESTAB 0 0 127.0.0.1:1 127.0.0.1:2 users:(("backend",pid=42,fd=3))\n'
            '  skmem:(r0,rb131072,t0,tb2626560)\n'
            'ESTAB 0 0 127.0.0.1:3 127.0.0.1:4 users:(("other",pid=142,fd=4))\n'
            '  skmem:(r0,rb1,t0,tb2)\n', 42)
        self.assertEqual(len(rows), 1)
        self.assertIn('tb2626560', rows[0])
        self.assertNotIn('pid=142', rows[0])

    def test_wrap_marks_only_first_call_as_warmup(self):
        calls = []
        @contextlib.contextmanager
        def sample(ctx, metric, side, pair, trace):
            calls.append((pair, trace))
            yield {}
        with mock.patch.object(profile, 'sample', sample):
            run = profile.wrap(None, 'net.publish_rr', 'guest', lambda: '{"value": 1}', True)
            for _ in range(3):
                self.assertEqual(run(), '{"value": 1}')
        self.assertEqual(calls, [(None, True), (0, True), (1, True)])

    def test_measured_pairs_and_native_warmups_never_attach(self):
        with mock.patch.dict(profile.os.environ, {'HD_NET_STRACE': '1'}), \
                mock.patch.object(profile.subprocess, 'Popen') as spawn:
            for side, pair in (('guest', 0), ('guest', 1), ('native', None)):
                with profile.trace(None, 'net.bridge_stream', side, pair, 1) as detail:
                    self.assertIsNone(detail)
            spawn.assert_not_called()

    def test_stat_command_parentheses_and_spaces(self):
        fields = ['S', '3', '42'] + ['0'] * 8 + ['150', '25'] + ['0'] * 6 + ['900']
        item = profile.parse_stat('41 (linux (guest) ) ' + ' '.join(fields))
        self.assertEqual(item, {'pid': 41, 'comm': 'linux (guest) ', 'pgrp': 42,
                                'user': 150, 'system': 25, 'start': 900,
                                'children_user': 0, 'children_system': 0})

    def test_group_cpu_tracks_reaped_and_new_children(self):
        before = {(1, 10): {'pid': 1, 'comm': 'linux', 'user': 10, 'system': 0},
                  (2, 20): {'pid': 2, 'comm': 'linux', 'user': 5, 'system': 0}}
        after = {(1, 10): {'pid': 1, 'comm': 'linux', 'user': 12, 'system': 0, 'children_user': 9},
                 (3, 30): {'pid': 3, 'comm': 'linux', 'user': 3, 'system': 0}}
        delta = profile.cpu_delta(before, after, 100)
        self.assertEqual(delta['user_seconds'], 0.02)
        self.assertEqual(delta['group_user_seconds'], 0.09)

    def test_pid_reuse_and_exited_tasks_are_not_cpu_samples(self):
        before = {(1, 10): {'user': 500, 'system': 100}, (2, 20): {'user': 90, 'system': 30}}
        after = {(1, 10): {'pid': 1, 'comm': 'linux', 'user': 520, 'system': 105},
                 (2, 40): {'pid': 2, 'comm': 'linux', 'user': 1, 'system': 1}}
        result = profile.cpu_delta(before, after, 100)
        self.assertEqual(result['user_seconds'], 0.2)
        self.assertEqual(result['system_seconds'], 0.05)
        self.assertEqual(result['new_tasks'], 1)
        self.assertEqual(result['exited_tasks'], 1)
