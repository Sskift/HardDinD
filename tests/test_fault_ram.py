"""RAM population is opt-in and cannot report unsupported/failed advice as success."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class FaultRamTests(unittest.TestCase):
    def test_population_errors_and_opt_in(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/fault-ram-populate.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        helper = added[added.index('int __init os_populate_ram'):]
        helper = helper[:helper.index('\n}') + 2]
        source = '''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#define __init
#define MADV_POPULATE_WRITE 23
static int uml_ram_populate, uml_memfd_ram, calls, logs, result;
static void *last_address;
static unsigned long last_length;
static long long os_nsecs(void) { static long long t; return t += 100; }
static void os_info(const char *format, ...) { (void)format; logs++; }
static int madvise(void *address, size_t length, int advice) {
    assert(advice == MADV_POPULATE_WRITE);
    calls++; last_address = address; last_length = length; return result;
}
''' + helper + '''
int main(void) {
    void *address = (void *)(uintptr_t)4096;
    assert(!os_populate_ram(address, 8192) && !calls && !logs);
    uml_ram_populate = 1;
    assert(os_populate_ram(address, 8192) == -EINVAL && !calls && !logs);
    uml_memfd_ram = 1; result = -1; errno = ENOSYS;
    assert(os_populate_ram(address, 8192) == -ENOSYS && calls == 1 && !logs);
    errno = ENOMEM;
    assert(os_populate_ram(address, 8192) == -ENOMEM && calls == 2 && !logs);
    result = 0;
    assert(!os_populate_ram(address, 8192) && calls == 3 && logs == 1);
    assert(last_address == address && last_length == 8192);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'ram.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(root / 'ram.c'), '-o', str(root / 'ram')], check=True)
            subprocess.run([str(root / 'ram')], check=True, timeout=10)
