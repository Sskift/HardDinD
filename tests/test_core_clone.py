"""Exercise the real runner setup with ordered queues and injected failures."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from pipeline import config


class CloneBatchTests(unittest.TestCase):
    def test_mapping_precedes_clone_and_failures_stop_it(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-clone-batch.patch').read_text()
        part = patch.split('+++ b/arch/um/os-Linux/skas/mem.c\n', 1)[1]
        body = '\n'.join(line[1:] for line in part.splitlines()
                         if line.startswith((' ', '+')))
        source = '''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#define AF_UNIX 1
#define SOCK_DGRAM 2
#define SOCK_CLOEXEC 4
#define SOCK_NONBLOCK 8
#define STUB_THREAD_PAGES 2
#define UM_KERN_PAGE_SIZE 4096
#define UM_PROT_READ 1
#define UM_PROT_WRITE 2
#define STUB_SYSCALL_CLONE 2
struct stub_data { unsigned int spin_limit; int clone_pid, err; };
struct mm_id { void *stack; int sock, pid; };
struct stub_syscall { int syscall; struct { unsigned long addr; int fd; } mem; };
unsigned int uml_seccomp_spin_limit = 256;
int uml_clone_batch;
static struct stub_syscall queue[3];
static int length, mapped, clones, batches, waited, closed, fail_map, fail_clone, capacity;
static unsigned long uml_to_phys(void *p) { return (unsigned long)p; }
static int phys_mapping(unsigned long p, unsigned long long *offset) { *offset = p; return 7; }
static int socketpair(int a, int b, int c, int *s) {
    (void)a; (void)b; (void)c; s[0] = 10; s[1] = 11; return 0;
}
static int close(int fd) { assert(fd == 10 || fd == 11); closed++; return 0; }
static int map(struct mm_id *m, unsigned long a, unsigned long n, int prot, int fd,
               unsigned long long offset) {
    (void)m; (void)a; (void)n; (void)prot; (void)fd; (void)offset;
    queue[length++].syscall = 1; return 0;
}
static int execute(struct mm_id *m) {
    if (length < 0) return length;
    if (length) batches++;
    for (int i = 0; i < length; i++) {
        if (queue[i].syscall == 1) {
            if (fail_map) return length = -ENOMEM;
            mapped = 1;
        } else {
            assert(mapped);
            if (fail_clone) return length = -EAGAIN;
            ((struct stub_data *)m->stack)->clone_pid = 42;
            clones++;
        }
    }
    return length = 0;
}
static int syscall_stub_flush(struct mm_id *m) { int r = execute(m); length = 0; return r; }
static int get_stub_fd(struct mm_id *m, int fd) {
    if (capacity && length > 0) execute(m);
    return fd;
}
static struct stub_syscall *syscall_stub_alloc(struct mm_id *m) {
    (void)m;
    return length < 0 ? queue : &queue[length++];
}
static void wait_stub_done_seccomp(struct mm_id *m, int a, int b) {
    assert(m->pid == 42 && a == 1 && b == 1 && mapped && clones == 1); waited++;
}
''' + body + '''
int main(void) {
    for (int enabled = 0; enabled < 2; enabled++) {
        for (int full = 0; full < 2; full++) {
            for (int failure = 0; failure < 3; failure++) {
                struct stub_data parent = {0}, child = {0};
                struct mm_id broker = {.stack = &parent}, runner = {.stack = &child};
                uml_clone_batch = enabled; capacity = full;
                fail_map = failure == 1; fail_clone = failure == 2;
                length = mapped = clones = batches = waited = closed = 0;
                int result = start_userspace_cpu(&broker, &runner, 0x1000);
                assert(child.spin_limit == 256);
                if (!failure) {
                    assert(result == 0 && clones == 1 && waited == 1 && closed == 1);
                    assert(batches == (enabled && !full ? 1 : 2));
                } else {
                    assert(result == (fail_map ? -ENOMEM : -EAGAIN));
                    assert(clones == 0 && waited == 0 && closed == 2 && runner.sock == 0);
                }
            }
        }
    }
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as d:
            path = pathlib.Path(d)
            (path / 'check.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror',
                            str(path / 'check.c'), '-o', str(path / 'check')], check=True)
            subprocess.run([str(path / 'check')], check=True, timeout=10)
