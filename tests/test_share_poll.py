"""Compile the candidate's bounded poll loop against a deterministic fake ring."""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
class PollLoopTests(unittest.TestCase):
    def test_progress_fallback_and_budget(self):
        patch = (REPO / 'kernel/candidates/share-poll.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        function = re.search(r'static void vu_poll_fs\(.*?\n}', added, re.S).group()
        before = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
typedef uint64_t u64;
typedef int atomic_t;
typedef long long atomic64_t;
#define VIRTIO_ID_FS 26
#define IRQ_HANDLED 1
#define min(a, b) ((a) < (b) ? (a) : (b))
struct virtio_uml_vq_info { atomic_t calls; };
struct device { struct { int device; } id; unsigned features; };
struct virtqueue { void *priv; struct device *vdev; unsigned index; };
static unsigned int poll_iters, probes, clocks, pauses;
static int completion_after, interrupt_after;
static atomic64_t poll_attempts, poll_hits, poll_interrupts, poll_loops, poll_ns;
static int atomic_read(atomic_t *p) { return *p; }
static int virtio_has_feature(struct device *d, unsigned bit) { return !!(d->features & (1U << bit)); }
static void atomic64_inc(atomic64_t *p) { ++*p; }
static void atomic64_add(u64 n, atomic64_t *p) { *p += n; }
static u64 os_nsecs(void) { return ++clocks; }
static void cpu_relax(void) { pauses++; }
static int vring_interrupt(int irq, struct virtqueue *vq) {
    struct virtio_uml_vq_info *info = vq->priv;
    (void)irq;
    if ((int)probes == interrupt_after) info->calls++;
    return (int)probes++ == completion_after ? IRQ_HANDLED : 0;
}
static void reset(void) {
    probes = clocks = pauses = 0;
    completion_after = interrupt_after = -1;
    poll_attempts = poll_hits = poll_interrupts = poll_loops = poll_ns = 0;
}
'''
        after = r'''
int main(void) {
    struct virtio_uml_vq_info info = { .calls = 9 };
    struct device dev = { .id.device = VIRTIO_ID_FS };
    struct virtqueue vq = { .priv = &info, .vdev = &dev, .index = 1 };
    reset(); poll_iters = 0; vu_poll_fs(&vq, 9);
    assert(!clocks && !probes && !poll_attempts);
    reset(); poll_iters = 4; dev.id.device = 1; vu_poll_fs(&vq, 9);
    assert(!clocks && !probes && !poll_attempts); dev.id.device = VIRTIO_ID_FS;
    reset(); vq.index = 0; vu_poll_fs(&vq, 9);
    assert(!clocks && !probes && !poll_attempts);
    reset(); vq.index = 1; dev.features = 1; vu_poll_fs(&vq, 9);
    assert(!clocks && !probes && !poll_attempts);
    vq.index = 2;
    reset(); vu_poll_fs(&vq, 9);
    assert(probes == 4 && pauses == 4 && poll_loops == 4 && poll_attempts == 1);
    assert(!poll_hits && !poll_interrupts && clocks == 2);
    reset(); completion_after = 0; vu_poll_fs(&vq, 9);
    assert(probes == 1 && !pauses && poll_hits == 1 && poll_loops == 1);
    reset(); vu_poll_fs(&vq, 8);
    assert(!probes && poll_interrupts == 1 && poll_loops == 1);
    reset(); interrupt_after = 0; vu_poll_fs(&vq, 9);
    assert(probes == 1 && poll_interrupts == 1 && poll_loops == 2);
    reset(); poll_iters = UINT32_MAX; vu_poll_fs(&vq, info.calls);
    assert(probes == 65536 && poll_loops == 65536 && poll_attempts == 1);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / 'poll.c'
            binary = pathlib.Path(directory) / 'poll'
            source.write_text(before + function + after)
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-std=c11',
                            *(['-static'] if sys.platform == 'linux' else []),
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=5)
