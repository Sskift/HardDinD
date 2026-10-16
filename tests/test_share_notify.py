"""Visibility is bounded from the start of an outer change, including successful reads."""
import unittest
from suites.functional.shared_files import change_window, window_summary, check_windows


class ChangeWindow(unittest.TestCase):
    def clock(self):
        self.now += 1_000_000
        return self.now

    def setUp(self):
        self.now = 0

    def test_immediate(self):
        row = change_window(lambda: 7, 7, clock=self.clock)
        self.assertTrue(row['first_access'])
        self.assertEqual(row['stale_reads'], 0)
        self.assertEqual(row['visible_by_ms'], 2)

    def test_delayed_keeps_first_access_failure(self):
        replies = iter([6, 6, 7])
        row = change_window(lambda: next(replies), 7, clock=self.clock)
        self.assertFalse(row['first_access'])
        self.assertEqual(row['stale_reads'], 2)
        self.assertEqual(row['last_stale_ms'], 3)
        self.assertEqual(row['visible_by_ms'], 6)

    def test_window_includes_outer_operation(self):
        started = self.clock()
        self.now += 5_000_000
        row = change_window(lambda: 7, 7, clock=self.clock, started_ns=started)
        self.assertTrue(row['first_access'])
        self.assertEqual(row['visible_by_ms'], 7)

    def test_missing_error_and_stale_fail(self):
        for reply in (None, -1, 6):
            with self.subTest(reply=reply), self.assertRaises(AssertionError):
                change_window(lambda: reply, 7, clock=self.clock, timeout_ns=4_000_000)

    def test_transport_failure_propagates(self):
        def failed():
            raise OSError('peer died')
        with self.assertRaises(OSError):
            change_window(failed, 7, clock=self.clock)

    def test_late_success_fails(self):
        with self.assertRaises(AssertionError):
            change_window(lambda: 7, 7, clock=self.clock, timeout_ns=1_000_000)

    def test_window_bounds_and_percentile(self):
        class Case:
            def write(self, text):
                self.output = text
        case = Case()
        def rows(values):
            return [{'origin': 'outer_change_start', 'visible_by_ms': v, 'first_access': False} for v in values]
        result = check_windows(case, 'native', rows([1] * 19 + [50]))
        self.assertEqual(result['p95_visible_by_ms'], 1)
        self.assertEqual(result['first_access_misses'], 20)
        for values in ([], [1] * 18 + [11, 12], [1] * 19 + [50.001], [-1], [float('nan')]):
            with self.subTest(values=values), self.assertRaises(AssertionError):
                check_windows(case, 'docker', rows(values))
        with self.assertRaises(AssertionError):
            check_windows(case, 'docker', [{'visible_by_ms': 1, 'first_access': True}])
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


class MmapWaiter(unittest.TestCase):
    def test_native_waiter_observes_each_outer_write(self):
        import os
        import pathlib
        import select
        import shutil
        import subprocess
        import sys
        import tempfile
        from suites.functional.shared_files import MMAP_WAITER
        if not shutil.which('cc'):
            self.skipTest('C compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, binary, data = root / 'waiter.c', root / 'waiter', root / 'data'
            source.write_text(MMAP_WAITER)
            data.write_bytes(b'A' * 4096)
            flags = ['-static'] if sys.platform.startswith('linux') else []
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(source), '-o', str(binary)], check=True)
            process = subprocess.Popen([str(binary), str(data)], stdout=subprocess.PIPE)
            try:
                self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                self.assertEqual(os.read(process.stdout.fileno(), 1), b'A')
                with data.open('r+b', buffering=0) as file:
                    for value in range(66, 98):
                        os.pwrite(file.fileno(), bytes([value]), 0)
                        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                        self.assertEqual(os.read(process.stdout.fileno(), 1), bytes([value]))
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()


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
