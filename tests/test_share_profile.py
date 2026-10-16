import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from suites.functional.shared_files import EXCLUSIVE_CREATE, LOOKUP_RACER, METADATA_BATCH, metadata_profile


class MetadataProfile(unittest.TestCase):
    def sample(self):
        return '\n'.join(f'phase {phase} 0.001\nqueue q\nwork 1 2 kick 3 4\n'
                         f'1 {i * 256} {i * 256} {i * 1024} {i * 256} {i * 512} {i * 256} {i * 4096}'
                         for i, phase in enumerate(('before', 'create', 'stat', 'rename', 'unlink')))

    def test_phase_deltas(self):
        for row in metadata_profile(self.sample()).values():
            self.assertEqual(row['operations'], 256)
            self.assertEqual(row['opcodes']['q:1']['submitted'], 256)
            self.assertEqual(row['opcodes']['q:1']['total_ns'], 1024)

    def test_no_profile_is_explicitly_empty(self):
        text = '\n'.join('phase ' + p + ' 0' for p in ('before', 'create', 'stat', 'rename', 'unlink'))
        self.assertTrue(all(not row['opcodes'] for row in metadata_profile(text).values()))

    def test_background_and_waiter_counters(self):
        text = '\n'.join(f'phase {phase} 0.001\nqueue q\n'
                         f'1 {i * 256} {i * 256} {i * 1024} {i * 256} {i * 512} '
                         f'{i * 256} {i * 4096} {i * 8} {i * 248} {i * 744}'
                         for i, phase in enumerate(('before', 'create', 'stat', 'rename', 'unlink')))
        for row in metadata_profile(text).values():
            counters = row['opcodes']['q:1']
            self.assertEqual(counters['background'], 8)
            self.assertEqual(counters['wake_count'], 248)
            self.assertEqual(counters['wake_ns'], 744)

    def test_notification_work_and_regression(self):
        text = '\n'.join(f'phase {phase} 0.001\nqueue notifications\n'
                         f'work {i} {i * 2} kick 0 0 notify {i * 256} {i * 1024}'
                         for i, phase in enumerate(('before', 'create', 'stat', 'rename', 'unlink')))
        for row in metadata_profile(text).values():
            counters = row['transport']['notifications']
            self.assertEqual(counters['notify_count'], 256)
            self.assertEqual(counters['notify_ns'], 1024)
        with self.assertRaises(ValueError):
            metadata_profile(text.replace('notify 512', 'notify 1'))

    def test_host_clock_samples_and_malformed_trace(self):
        sample = self.sample().replace('phase unlink',
            'trace 1 42 100 110 120 130 140\nphase unlink')
        row = metadata_profile(sample)['rename']['traces']
        self.assertEqual(row, [[1, 42, 100, 110, 120, 130, 140]])
        for bad in ('trace 1 42 100 90 120 130 140',
                    'trace 1 42 0 110 120 130 140', 'trace 1 42',
                    'trace 15 42 100 110 120 130 140'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                metadata_profile(sample.replace('trace 1 42 100 110 120 130 140', bad))

    def test_reject_incomplete_and_regressing(self):
        for text in (self.sample().replace('phase unlink', 'phase stat'),
                     self.sample().split('phase unlink')[0],
                     self.sample().replace('phase unlink 0.001\nqueue q\nwork 1 2 kick 3 4',
                                           'phase unlink 0.001\nqueue q'),
                     self.sample().replace('1 512 512', '1 1 512'),
                     self.sample().replace('1 512 512 2048 512 1024 512 8192', ''),
                     self.sample().replace('1 256 256 1024 256 512 256 4096',
                                           '1 256 256 1024 256 512 256 4096\n1 256 256 1024 256 512 256 4096'),
                     self.sample().replace('1 256 256 1024 256 512 256 4096', '1 256')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                metadata_profile(text)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_native_workload_and_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, binary = root / 'batch.c', root / 'batch'
            source.write_text(METADATA_BATCH)
            flags = ['-static'] if sys.platform == 'linux' else []
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            directory = root / 'files'
            directory.mkdir()
            run = subprocess.run([str(binary), str(directory)], capture_output=True, text=True,
                                 check=True, timeout=10)
            self.assertEqual(len(metadata_profile(run.stdout)), 4)
            self.assertEqual(list(directory.iterdir()), [])


class LookupRace(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_first_access_and_missing_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, binary, target = root / 'race.c', root / 'race', root / 'target'
            source.write_text(LOOKUP_RACER)
            flags = ['-static'] if sys.platform == 'linux' else []
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            for present in (True, False):
                target.unlink(missing_ok=True)
                with subprocess.Popen([str(binary), str(target), '10'], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    self.assertEqual(process.stdout.read(1), b'R')
                    for _ in range(32):
                        target.write_bytes(b'old')
                        target.unlink()
                    if present:
                        target.write_bytes(b'x' * 8192)
                    output, _ = process.communicate(b'S', timeout=5)
                    row = json.loads(output)
                    self.assertEqual(process.returncode, 0 if present else 10)
                    self.assertEqual(row['visible'], present)
                    self.assertEqual(row['first_access'], present)


class ExclusiveCreate(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_names_modes_and_nonexclusive_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, binary, directory = root / 'create.c', root / 'create', root / 'files'
            source.write_text(EXCLUSIVE_CREATE)
            directory.mkdir()
            flags = ['-static'] if sys.platform == 'linux' else []
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            result = subprocess.run([str(binary), str(directory)], capture_output=True, text=True,
                                    check=True, timeout=10)
            self.assertEqual(result.stdout.strip(), 'exclusive create: ok')
            self.assertEqual(list(directory.iterdir()), [])


class InlineCompletion(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_async_pages_and_large_replies_stay_in_worker(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        patch = (root / 'kernel/candidates/share-complete.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        function = re.search(r'static bool virtio_fs_can_complete_inline\(.*?\n}', added, re.S).group()
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
enum { FR_ASYNC, FR_BACKGROUND };
struct arg { unsigned size; };
struct args { bool may_block, out_pages; unsigned out_numargs; struct arg out_args[3]; };
struct fuse_req { struct args *args; struct { struct { unsigned len; } h; } out; unsigned long flags; };
static bool test_bit(unsigned bit, unsigned long *flags) { return (*flags & (1UL << bit)) != 0; }
static unsigned fuse_len_args(unsigned count, const struct arg *args) {
    unsigned size = 0;
    for (unsigned i = 0; i < count; i++) size += args[i].size;
    return size;
}
''' + function + r'''
int main(void) {
    struct args args = {.out_numargs = 2, .out_args = {{128}, {16}}};
    struct fuse_req req = {.args = &args, .out.h.len = 160};
    assert(virtio_fs_can_complete_inline(&req));
    for (unsigned flag = 0; flag < 2; flag++) {
        req.flags = 1UL << flag;
        assert(!virtio_fs_can_complete_inline(&req));
    }
    req.flags = 0;
    args.may_block = true;
    assert(!virtio_fs_can_complete_inline(&req));
    args.may_block = false; args.out_pages = true;
    assert(!virtio_fs_can_complete_inline(&req));
    args.out_pages = false; req.out.h.len = 513;
    assert(!virtio_fs_can_complete_inline(&req));
    req.out.h.len = 16; args.out_args[0].size = 4096;
    assert(!virtio_fs_can_complete_inline(&req));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            source, binary = pathlib.Path(tmp) / 'inline.c', pathlib.Path(tmp) / 'inline'
            source.write_text(code)
            flags = ['-static'] if sys.platform == 'linux' else []
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, timeout=5)


if __name__ == '__main__':
    unittest.main()
