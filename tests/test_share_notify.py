"""Peer timestamps distinguish stale observations from delayed replies."""
import unittest
from suites.functional.shared_files import change_window, window_summary, check_windows


class ChangeWindow(unittest.TestCase):
    @staticmethod
    def read(start, value, duration=1_000_000):
        return {'start_ns': start, 'end_ns': start + duration, 'value': [value]}

    def score(self, reads, offset=0):
        return change_window(reads, [7], 1, 0, 5_000_000, offset)

    def test_fresh_first_read_ignores_late_start_and_reply(self):
        row = self.score([self.read(57_000_000, 7)])
        self.assertTrue(row['first_access'])
        self.assertEqual(row['staleness_ms'], 0)

    def test_last_stale_read_start_not_end_or_fresh_reply(self):
        row = self.score([self.read(6_000_000, 6, 10_000_000), self.read(20_000_000, 6),
                          self.read(100_000_000, 7)])
        self.assertEqual(row['staleness_ms'], 15)
        self.assertEqual(row['stale_reads'], 2)

    def test_stale_read_overlapping_change_is_zero(self):
        self.assertEqual(self.score([self.read(1_000_000, 6, 5_000_000),
                                     self.read(10_000_000, 7)])['staleness_ms'], 0)

    def test_offset_is_subtracted(self):
        self.assertEqual(self.score([self.read(6_100_000, 6), self.read(8_100_000, 7)],
                                     100_000)['staleness_ms'], 1)

    def test_missing_stale_late_and_malformed_fail(self):
        for reads in ([], [self.read(6_000_000, 6)],
                      [self.read(55_000_001, 6), self.read(57_000_000, 7)],
                      [self.read(1_006_000_000, 7)], [self.read(6_000_000, 7, -1)],
                      [self.read(7_000_000, 6), self.read(6_000_000, 7)]):
            with self.subTest(reads=reads), self.assertRaises(AssertionError):
                self.score(reads)

    def test_clock_uses_minimum_network_rtt_and_checks_bound(self):
        from suites.functional.shared_files import clock_offset
        result = clock_offset([(0, 20_000_000, 15_000_000, 15_000_000),
                               (0, 200_000, 110_000, 110_000)])
        self.assertEqual(result['offset_ns'], 10_000)
        for samples in ([], [(0, 100_000, 600_001, 600_001)],
                        [(0, 100_000, -500_001, -500_001)], [(2, 1, 0, 0)]):
            with self.subTest(samples=samples), self.assertRaises(AssertionError):
                clock_offset(samples)

    def test_window_bounds_and_percentile(self):
        class Case:
            def write(self, text):
                self.output = text
        case = Case()
        def rows(values):
            return [{'origin': 'outer_change_end', 'fresh': True, 'staleness_ms': v,
                     'first_access': False} for v in values]
        result = check_windows(case, 'native', rows([1] * 19 + [50]))
        self.assertEqual(result['p95_staleness_ms'], 1)
        self.assertEqual(result['first_access_misses'], 20)
        for values in ([], [1] * 18 + [11, 12], [1] * 19 + [50.001], [-1], [float('nan')]):
            with self.subTest(values=values), self.assertRaises(AssertionError):
                check_windows(case, 'docker', rows(values))
        with self.assertRaises(AssertionError):
            check_windows(case, 'docker', [{'staleness_ms': 1, 'first_access': True}])
        self.assertIn('coherence_window', case.output)


class KernelNotificationWire(unittest.TestCase):
    def test_reject_malformed_notifications(self):
        import pathlib
        import re
        import shutil
        import subprocess
        import sys
        import tempfile
        if not shutil.which('cc'):
            self.skipTest('C compiler unavailable')
        root = pathlib.Path(__file__).resolve().parents[1]
        patch = (root / 'kernel/patches/0035-fuse-virtiofs-notifications.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        function = re.search(r'static int virtio_fs_notification\(.*?\n}', added, re.S).group()
        before = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#define NAME_MAX 255
#define SHARE_EVENT 0x4844
#define SHARE_FAILED 0x4845
#define FUSE_NOTIFY_INVAL_INODE 2
#define FUSE_NOTIFY_INVAL_ENTRY 3
struct fuse_conn { int killsb; };
struct fuse_out_header { uint32_t len; int32_t error; uint64_t unique; };
struct fuse_notify_inval_inode_out { uint64_t ino; int64_t off, len; };
struct fuse_notify_inval_entry_out { uint64_t parent; uint32_t namelen, flags; };
struct qstr { const char *name; unsigned int len; };
static unsigned calls;
static void down_read(int *p) { ++*p; }
static void up_read(int *p) { --*p; }
static int fuse_reverse_inval_inode(struct fuse_conn *fc, uint64_t ino, int64_t off, int64_t len) {
    (void)fc; assert(ino == 7 && off == 0 && len == 0); calls++; return 0;
}
static int fuse_reverse_inval_entry(struct fuse_conn *fc, uint64_t parent, uint64_t child,
                                  struct qstr *name, uint32_t flags) {
    (void)fc; assert(parent == 7 && child == 0 && flags == 0);
    assert(name->len == 4 && !memcmp(name->name, "file", 4)); calls++; return -ENOENT;
}
static int virtio_fs_share_event(struct fuse_conn *fc, void *data, unsigned int size) {
    (void)fc; (void)data; (void)size; return -EINVAL;
}
'''
        after = r'''
int main(void) {
    union { uint64_t align; char bytes[512]; } buf = {0};
    struct fuse_conn fc = {0};
    struct fuse_out_header *h = (void *)buf.bytes;
    struct fuse_notify_inval_inode_out *inode = (void *)(h + 1);
    struct fuse_notify_inval_entry_out *entry = (void *)(h + 1);
    h->len = sizeof(*h) + sizeof(*inode); h->error = 2; inode->ino = 7;
    assert(virtio_fs_notification(&fc, h, h->len) == 0 && calls == 1);
    h->unique = 1; assert(virtio_fs_notification(&fc, h, h->len) == -EINVAL);
    h->unique = 0; assert(virtio_fs_notification(&fc, h, 15) == -EINVAL);
    assert(virtio_fs_notification(&fc, h, h->len - 1) == -EINVAL);
    h->error = 3; entry->parent = 7; entry->namelen = 4; entry->flags = 0;
    memcpy(entry + 1, "file", 5); h->len = sizeof(*h) + sizeof(*entry) + 5;
    assert(virtio_fs_notification(&fc, h, h->len) == 0 && calls == 2);
    entry->namelen = UINT32_MAX;
    assert(virtio_fs_notification(&fc, h, h->len) == -EINVAL);
    entry->namelen = 4; memcpy(entry + 1, "a/bc", 5);
    assert(virtio_fs_notification(&fc, h, h->len) == -EINVAL);
    memcpy(entry + 1, "a\0bc", 5);
    assert(virtio_fs_notification(&fc, h, h->len) == -EINVAL);
    memcpy(entry + 1, "abcde", 5);
    assert(virtio_fs_notification(&fc, h, h->len) == -EINVAL);
    h->error = SHARE_FAILED;
    assert(virtio_fs_notification(&fc, h, h->len) == -EIO);
    assert(calls == 2 && fc.killsb == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source, binary = pathlib.Path(directory) / 'wire.c', pathlib.Path(directory) / 'wire'
            source.write_text(before + function + after)
            flags = ['-static'] if sys.platform.startswith('linux') else []
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


class WindowHelper(unittest.TestCase):
    @unittest.skipUnless(__import__('sys').platform == 'linux', 'Linux inotify ABI')
    def test_peer_loops_and_timestamps_stale_and_fresh_reads(self):
        import json
        import os
        import pathlib
        import subprocess
        import tempfile
        import time
        from suites.functional.shared_files import TREE_PROBE
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, binary = root / 'probe.c', root / 'probe'
            source.write_text(TREE_PROBE)
            subprocess.run(['gcc', '-static', '-O2', '-Wall', '-Wextra', '-Werror', source, '-o', binary], check=True)
            data = root / 'data'
            data.write_bytes(b'A' * 8192)
            proc = subprocess.Popen([str(binary), str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertTrue(json.loads(proc.stdout.readline())['ready'])
                proc.stdin.write('clock\n');proc.stdin.flush()
                clock = json.loads(proc.stdout.readline())
                self.assertLessEqual(clock['start_ns'], clock['end_ns'])
                deadline = time.time_ns() + 1_000_000_000
                proc.stdin.write(f'poll {deadline} 5 0 0 66 0 0 0 0 0|read data\n');proc.stdin.flush()
                stale = json.loads(proc.stdout.readline())
                self.assertEqual(stale['value'][2], 65)
                self.assertFalse(stale['last'])
                started = time.time_ns()
                data.write_bytes(b'B' * 8192)
                ended = time.time_ns()
                rows = [stale]
                while not rows[-1]['last']:
                    rows.append(json.loads(proc.stdout.readline()))
                row = change_window(rows, [0, 0, 66, 0, 0, 0, 0, 0], 5, started, ended)
                self.assertTrue(row['fresh'])
                self.assertGreater(row['stale_reads'], 0)
                proc.stdin.write('quit\n');proc.stdin.flush()
                self.assertEqual(proc.wait(timeout=5), 0)
            finally:
                if proc.poll() is None:
                    proc.kill();proc.wait()
                proc.stdin.close();proc.stdout.close()


class InotifyRecords(unittest.TestCase):
    def test_split_records_preserve_names_and_move_cookies(self):
        import struct
        from suites.functional.shared_files import inotify_records
        first = struct.pack('=iIII', 3, 0x40, 912, 8) + b'old\0\0\0\0\0'
        second = struct.pack('=iIII', 3, 0x80, 912, 8) + b'new\0\0\0\0\0'
        for cut in range(len(first + second) + 1):
            rows, pending = inotify_records((first + second)[:cut])
            tail, pending = inotify_records(pending + (first + second)[cut:])
            self.assertEqual(pending, b'')
            self.assertEqual(rows + tail, [
                {'wd': 3, 'mask': 0x40, 'cookie': 912, 'name': 'old'},
                {'wd': 3, 'mask': 0x80, 'cookie': 912, 'name': 'new'}])

    def test_malformed_names_fail(self):
        import struct
        from suites.functional.shared_files import inotify_records
        for data in (struct.pack('=iIII', 1, 2, 0, 257),
                     struct.pack('=iIII', 1, 2, 0, 4) + b'name'):
            with self.assertRaises(ValueError):
                inotify_records(data)
