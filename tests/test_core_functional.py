"""Core helper summaries must establish every requested operation."""
import json
import unittest
from unittest import mock

from pipeline import functional as f


class CoreFunctionalTests(unittest.TestCase):
    def sample(self, mode, threads=1):
        return {'mode': mode, 'threads': threads, 'operations': 256 * threads,
                'verified': True, 'ns_per_op': 123.0}

    def validate(self, samples, mode='filters'):
        return f.core_summary(json.dumps({'schema': 'hd.core-syscalls/1', 'samples': samples}),
                              'core_syscalls', mode)

    def test_policy_requires_both_allow_and_deny(self):
        samples = [self.sample('seccomp_allow'), self.sample('seccomp_errno')]
        self.validate(samples)
        for bad in ([], samples[:1], samples[::-1], samples + samples):
            with self.subTest(bad=bad), self.assertRaises(AssertionError):
                self.validate(bad)
        for key, value in [('verified', False), ('verified', 1), ('operations', 255),
                           ('ns_per_op', 0), ('ns_per_op', float('nan')),
                           ('ns_per_op', float('inf'))]:
            bad = [dict(samples[0]), dict(samples[1], **{key: value})]
            with self.subTest(key=key, value=value), self.assertRaises(AssertionError):
                self.validate(bad)

    def test_ptrace_signals_and_every_thread_count(self):
        self.validate([self.sample('ptraced_getpid')], 'ptrace')
        self.validate([self.sample('local_sigsys')], 'signals')
        rows = [self.sample('getpid', n) for n in range(1, 5)]
        self.validate(rows, 'threads')
        with self.assertRaises(AssertionError):
            self.validate(rows[:-1], 'threads')

    def test_fault_and_fp_results(self):
        row = {'schema': 'hd.core/1', 'mode': 'semantics', 'verified': True}
        f.core_summary('progress\n' + json.dumps(row), 'core_faults', 'semantics')
        row['verified'] = False
        with self.assertRaises(AssertionError):
            f.core_summary(json.dumps(row), 'core_faults', 'semantics')
        row = {'schema': 'hd.core-fpstate/1', 'sse_calls': 8192, 'avx_calls': 8192,
               'signal_upper_edit': True, 'ptrace_upper_edit': True, 'verified': True}
        f.core_summary(json.dumps(row), 'core_fpstate', 'state', 4096)
        for key in row:
            incomplete = {k: v for k, v in row.items() if k != key}
            with self.subTest(key=key), self.assertRaises(AssertionError):
                f.core_summary(json.dumps(incomplete), 'core_fpstate', 'state', 4096)

    def test_rewrite_requires_enabled_path_and_every_semantic_result(self):
        row = {'schema': 'hd.core-patch/1', 'enabled': True, 'sites': 32, 'calls': 131072,
               'signals': 5, 'trampoline_lifetime': True, **{key: True for key in ('fork', 'exec_clean', 'file_clean',
                   'ptrace', 'restart', 'long_form', 'ambiguous_fallback', 'mremap', 'discard_remap_write', 'verified')}}
        f.core_summary(json.dumps(row), 'core_patch', 'rewrite', patch_enabled=True)
        for key in row:
            bad = {k: v for k, v in row.items() if k != key}
            with self.subTest(key=key), self.assertRaises((AssertionError, KeyError)):
                f.core_summary(json.dumps(bad), 'core_patch', 'rewrite', patch_enabled=True)
        with self.assertRaises(AssertionError):
            f.core_summary(json.dumps(row), 'core_patch', 'rewrite', patch_enabled=False)

    def test_registered_helpers_use_privileged_container_and_validate(self):
        checks = [row for row in f.CHECKS if row[0] in ('process.core_faults', 'process.core_fpstate')
                  or row[0].startswith('process.core_syscalls_')]
        self.assertEqual(len(checks), 6)
        for name, _, _, _, helpers, callback in checks:
            self.assertTrue(helpers, name)
            case = mock.Mock()
            case.ctx.instance_options = {}
            case.output.return_value = '{}'
            with self.subTest(name=name), self.assertRaises((AssertionError, KeyError)):
                callback(case)
            self.assertEqual(case.output.call_args.kwargs,
                             {'image': f.HELPERS, 'opts': ('--privileged',)})

    def test_helper_cache_hashes_actual_source(self):
        for name in ('core_faults', 'core_syscalls', 'core_fpstate'):
            helper = f.FIXTURES / 'helpers' / (name + '.c')
            self.assertTrue(helper.is_symlink())
            self.assertEqual(helper.resolve(), f.config.REPO / 'src' / (name + '.c'))
            self.assertIn(name, (f.FIXTURES / 'Dockerfile').read_text().split())
