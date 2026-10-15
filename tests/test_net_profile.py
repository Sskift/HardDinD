import importlib.util
import pathlib
import unittest


path = pathlib.Path(__file__).parent / 'publish/net_profile.py'
spec = importlib.util.spec_from_file_location('net_profile', path)
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class ProfileTests(unittest.TestCase):
    def test_stat_command_parentheses_and_spaces(self):
        fields = ['S', '3', '42'] + ['0'] * 8 + ['150', '25'] + ['0'] * 6 + ['900']
        item = profile.parse_stat('41 (linux (guest) ) ' + ' '.join(fields))
        self.assertEqual(item, {'pid': 41, 'comm': 'linux (guest) ', 'pgrp': 42,
                                'user': 150, 'system': 25, 'start': 900})

    def test_pid_reuse_and_exited_tasks_are_not_cpu_samples(self):
        before = {(1, 10): {'user': 500, 'system': 100}, (2, 20): {'user': 90, 'system': 30}}
        after = {(1, 10): {'pid': 1, 'comm': 'linux', 'user': 520, 'system': 105},
                 (2, 40): {'pid': 2, 'comm': 'linux', 'user': 1, 'system': 1}}
        result = profile.cpu_delta(before, after, 100)
        self.assertEqual(result['user_seconds'], 0.2)
        self.assertEqual(result['system_seconds'], 0.05)
        self.assertEqual(result['new_tasks'], 1)
        self.assertEqual(result['exited_tasks'], 1)
