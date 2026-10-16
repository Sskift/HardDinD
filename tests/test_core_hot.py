"""Hotness hints must never patch before the threshold, including races."""
import json
import pathlib
import platform
import shutil
import subprocess
import tempfile
import sys
import unittest

from pipeline import config


class CoreHotTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64', 'Linux x86-64 syscall ABI')
    def test_native_lifetime_checker(self):
        with tempfile.TemporaryDirectory(prefix='.core-hot-', dir=config.REPO) as directory:
            binary = pathlib.Path(directory) / 'hot'
            subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                            config.REPO / 'src/core_hot.c', '-o', binary], check=True)
            result = subprocess.run([binary], capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(json.loads(result.stdout), {'schema': 'hd.core-hot/1', 'enabled': False,
                             'threshold': 0, 'cold_calls': 63, 'lazy': False, 'fork': True,
                             'exec_clean': True, 'verified': True})

    def test_threshold_collisions_and_concurrency(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-syscall-hot.patch').read_text()
        part = patch.split('+++ b/arch/um/include/shared/core-hot.h\n')[1].split('\n--- ')[0]
        header = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        source = '#include <assert.h>\n#include <pthread.h>\n' + header + '''
static unsigned long slot;
static void *work(void *arg) {
    (void)arg;
    for (int i = 0; i < 4096; i++) uml_patch_hot(&slot, 0x7fffabcde007UL, 128);
    return 0;
}
int main(void) {
    for (unsigned int threshold = 1; threshold <= 65535; threshold *= 3) {
        slot = 0;
        for (unsigned int i = 1; i <= threshold; i++)
            assert(uml_patch_hot(&slot, 0x7fffabcde007UL, threshold) == (i == threshold));
        assert(uml_patch_hot(&slot, 0x7fffabcde007UL, threshold));
        if (threshold > 1) assert(!uml_patch_hot(&slot, 0x400007, threshold));
    }
    slot = 0;
    for (int i = 0; i < 1000; i++) {
        assert(!uml_patch_hot(&slot, 0x400007, 64));
        assert(!uml_patch_hot(&slot, 0x500007, 64));
    }
    pthread_t threads[4]; slot = 0;
    for (int i = 0; i < 4; i++) assert(!pthread_create(threads + i, 0, work, 0));
    for (int i = 0; i < 4; i++) assert(!pthread_join(threads[i], 0));
    assert((slot >> 16) == 0x7fffabcde007UL && (slot & 65535) == 128);
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-hot-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(source)
            subprocess.run([cc, '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)
