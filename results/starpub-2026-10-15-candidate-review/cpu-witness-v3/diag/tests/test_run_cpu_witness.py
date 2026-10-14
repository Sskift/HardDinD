#!/usr/bin/env python3
"""Host-side tests for run-cpu-witness v3 harness mechanics (no guest/VM).

Covers the preflight review:
  - unique poll labels never collide on the run() xb evidence files;
  - real witness + real host_correlate sampler via run_cell, stopped by the
    explicit planned SIGTERM path: sampler metadata/cleanup recorded,
    witness strict count honored, correlation produced;
  - injected failing witness: sampler is force-reaped and its pid/pgid/group
    audit and forced cleanup are still recorded in cells[label] (no lost
    resource evidence);
  - build/smoke failure is recorded by the shared bounded runner
    (label/rows/cleanup present) rather than an undefined variable.

These tests create only short-lived local processes under EX coordination;
no Docker, no UML, no cgroup changes.
"""
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest

import importlib.util

HERE = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    'run_cpu_witness', HERE / 'run-cpu-witness.py')
rw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rw)

REPO = pathlib.Path('/root/harddind-rebuild/cpu-observer-owner-v1/repo')
WITNESS_BIN = pathlib.Path('/tmp/cpu-capture-witness-test')


def setUpModule():
    """The witness is a real static binary in these host tests; build it if
    absent. Callers hold the global EX lock for any compile."""
    if WITNESS_BIN.exists():
        return
    src = HERE / 'cpu_capture_witness.c'
    subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                    '-pthread', str(src), '-o', str(WITNESS_BIN)],
                   check=True, timeout=60)


def tearDownModule():
    # Keep the binary for repeated runs within the round; owner removes /tmp.
    pass


def make_args(work, count=10, sampler_duration_s=15.0, cell_timeout_s=30.0):
    return types.SimpleNamespace(
        work=str(work), name='unit', kernel='/dev/null', disk='/dev/null',
        repo=str(REPO), count=count, interval_ms=100, busy_duration_ms=45000,
        sampler_duration_s=sampler_duration_s, cell_timeout_s=cell_timeout_s,
        affinity='0,1,2,3', cpus=4)


def make_harness(tmp, **kw):
    work = pathlib.Path(tmp)
    out = work / 'results' / 'unit'
    instance = work / 'instances' / 'unit'
    out.mkdir(parents=True, exist_ok=True)
    instance.mkdir(parents=True, exist_ok=True)
    h = rw.Harness(make_args(work, **kw))
    h.report = {'commands': [], 'cells': {}}
    leader = subprocess.Popen(['sleep', '60'], start_new_session=True)
    (instance / 'state.json').write_text(
        json.dumps({'guest_pid': leader.pid, 'phase': 'ready',
                    'supervisor_pid': os.getpid(), 'supervisor_start': 0}))
    h._leader = leader
    return h


def cleanup_leader(h):
    if h._leader.poll() is None:
        os.killpg(h._leader.pid, signal.SIGTERM)
        try:
            h._leader.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(h._leader.pid, signal.SIGKILL)
            h._leader.wait(timeout=5)


class TestUniqueLabels(unittest.TestCase):
    def test_repeated_labels_distinct_and_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = make_harness(tmp)
            try:
                labels = [h.unique_label('busy-poll') for _ in range(5)]
                self.assertEqual(len(set(labels)), 5)
                self.assertEqual(labels[0], 'busy-poll')
                self.assertIn('busy-poll-4', labels)
                # run() opens files with xb: five real commands must succeed.
                for lab in labels:
                    h.run(lab, ['true'], timeout=10)
                self.assertEqual(
                    [c['label'] for c in h.report['commands'] if 'poll' in c['label']],
                    labels)
            finally:
                cleanup_leader(h)


class TestRunCellReal(unittest.TestCase):
    def test_planned_path_records_resource_and_correlation(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = make_harness(tmp, count=10)
            try:
                cmd = [str(WITNESS_BIN), 'witness', '--count', '10',
                       '--interval-ms', '100']
                h.run_cell('quiet-unit', cmd)
                cell = h.report['cells']['quiet-unit']
                self.assertEqual(cell['status'], 'complete')
                sam = cell['sampler']
                self.assertIsInstance(sam['pid'], int)
                self.assertEqual(sam['termination'], 'planned_sigterm')
                self.assertFalse(sam['forced_kill'])
                self.assertTrue(sam['cleanup']['clean_shutdown'])
                self.assertIsNotNone(sam['cpu_s'])
                self.assertGreater(sam['samples'], 0)
                # correlation artifact exists with strict witness accepted
                corr = json.loads(
                    (h.out / 'quiet-unit/corr/correlation.json').read_text())
                self.assertTrue(corr['schema']['witness_complete'])
                self.assertTrue(corr['run_integrity']['ok'])
                # coverage is whatever 10 ms sampling yields; count reported
                self.assertEqual(sum(corr['coverage_counts'].values()),
                                 10 * 5)
            finally:
                cleanup_leader(h)

    def test_failed_witness_still_records_sampler_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = make_harness(tmp, count=3)
            try:
                # A nonexistent witness binary: run() must fail the cell but
                # the sampler it started must be reaped and recorded.
                with self.assertRaises(RuntimeError):
                    h.run_cell('failed-cell',
                               ['/nonexistent-cpu-capture-witness-xyz'])
                cell = h.report['cells']['failed-cell']
                self.assertEqual(cell['status'], 'failed')
                self.assertIn('failed-cell',
                              [c['label'] for c in h.report['commands']])
                sam = cell['sampler']
                self.assertIsInstance(sam['pid'], int)
                self.assertIsNotNone(sam['pgid'])
                self.assertEqual(sam['termination'], 'forced_cleanup')
                self.assertTrue(sam['forced_kill'])
                self.assertTrue(sam['cleanup']['clean_shutdown'])
                self.assertIsNotNone(sam['end_ns'])
                # No process may remain.
                self.assertFalse(
                    pathlib.Path('/proc/%d' % sam['pid']).exists())
            finally:
                cleanup_leader(h)

    def test_sampler_early_exit_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = make_harness(tmp, count=3)
            try:
                # Leader stays alive; make the sampler binary unlaunchable so
                # it exits before its first tick, exercising the early-exit
                # branch and its failure record.
                old_corr = rw.CORRELATOR
                rw.CORRELATOR = pathlib.Path('/nonexistent-correlator-xyz')
                try:
                    with self.assertRaises(RuntimeError):
                        h.run_cell('dead-sampler',
                                   [str(WITNESS_BIN), 'witness', '--count', '3',
                                    '--interval-ms', '100'])
                finally:
                    rw.CORRELATOR = old_corr
                cell = h.report['cells']['dead-sampler']
                self.assertEqual(cell['status'], 'failed')
                self.assertIn('sampler exited early', cell['failure'])
                sam = cell['sampler']
                self.assertIsNotNone(sam['cleanup'])
                self.assertFalse(
                    pathlib.Path('/proc/%d' % sam['pid']).exists())
            finally:
                cleanup_leader(h)


class TestRecordedBuild(unittest.TestCase):
    def test_failed_build_keeps_rc_and_cleanup_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = make_harness(tmp)
            try:
                # Route a failing build through the same bounded runner: a
                # command row with rc/cleanup must exist, no undefined var.
                with self.assertRaises(RuntimeError):
                    h.run('build-witness', ['sh', '-c', 'exit 7'], timeout=10)
                rows = [c for c in h.report['commands']
                        if c['label'] == 'build-witness']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['exit_code'], 7)
                self.assertIn('cleanup', rows[0])
                self.assertTrue(rows[0]['cleanup']['clean_shutdown'])
            finally:
                cleanup_leader(h)


if __name__ == '__main__':
    unittest.main(verbosity=2)
