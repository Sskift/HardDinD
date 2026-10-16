import pathlib
import shutil
import subprocess
import tempfile
import unittest


class MetadataWait(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_actual_waiter_bounds_signals_lifetime_and_opcode_gate(self):
        patch = (pathlib.Path(__file__).resolve().parents[1] /
                 'kernel/candidates/share-wait.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        start = added.index('static void virtio_fs_poll_metadata(')
        end = added.index('\n}', added.index('static bool virtio_fs_wants_poll(')) + 2
        source = r'''
#include <assert.h>
#include <stdbool.h>
#define min(a,b) ((a)<(b)?(a):(b))
#define FR_SENT 0
#define FR_FINISHED 1
#define FR_BACKGROUND 2
#define FR_ASYNC 3
#define FUSE_LOOKUP 1
#define FUSE_GETATTR 3
#define test_bit(bit,p) ((*(p) >> (bit)) & 1UL)
#define current 0
#define signal_pending(p) signalled
#define local_irq_save(f) ((f)=1)
#define local_irq_restore(f) assert((f)==1)
#define spin_lock(p) do { assert(!*(p)); *(p)=1; } while (0)
#define spin_unlock(p) do { assert(*(p)); *(p)=0; } while (0)
#define cpu_relax() ((void)0)
struct virtio_fs_vq { bool connected; void *vq; int lock, in_flight; };
struct fuse_req { unsigned long flags; void *share_poll_data; struct { struct { unsigned int opcode; } h; } in; };
static unsigned int poll_metadata, calls, finish_at;
static bool inline_completion, signalled;
static struct fuse_req *active;
static void inc_in_flight_req(struct virtio_fs_vq *q) { assert(q->lock); q->in_flight++; }
static void dec_in_flight_req(struct virtio_fs_vq *q) { assert(q->lock && q->in_flight); q->in_flight--; }
static void vring_interrupt(int irq, void *vq) {
    (void)irq; struct virtio_fs_vq *q=vq;
    assert(q->in_flight==1);
    if (++calls==finish_at) active->flags |= 1UL << FR_FINISHED;
}
''' + added[start:end] + r'''
int main(void) {
    struct virtio_fs_vq q={.connected=true}; q.vq=&q;
    struct fuse_req req={.flags=1UL << FR_SENT, .share_poll_data=&q}; active=&req;
    poll_metadata=100; finish_at=3;
    virtio_fs_poll_metadata(&req); assert(calls==3 && !q.in_flight && !q.lock);
    req.flags=1UL << FR_SENT; calls=0; signalled=true;
    virtio_fs_poll_metadata(&req); assert(!calls && !q.in_flight);
    signalled=false; poll_metadata=99999; finish_at=99999;
    virtio_fs_poll_metadata(&req); assert(calls==4096 && !q.in_flight);
    calls=0; q.connected=false;
    virtio_fs_poll_metadata(&req); assert(!calls && !q.in_flight);
    q.connected=true; req.flags=(1UL << FR_SENT) | (1UL << FR_FINISHED);
    virtio_fs_poll_metadata(&req); assert(!calls && !q.in_flight);
    req.flags=0;
    virtio_fs_poll_metadata(&req); assert(!calls && !q.in_flight);
    inline_completion=true;
    for (unsigned int opcode=0; opcode<100; opcode++) {
        req.in.h.opcode=opcode;
        assert(virtio_fs_wants_poll(&req)==(opcode==1 || opcode==3));
    }
    req.in.h.opcode=1;
    req.flags=1UL << FR_BACKGROUND; assert(!virtio_fs_wants_poll(&req));
    req.flags=1UL << FR_ASYNC; assert(!virtio_fs_wants_poll(&req));
    req.flags=0; inline_completion=false; assert(!virtio_fs_wants_poll(&req));
    inline_completion=true; poll_metadata=0; assert(!virtio_fs_wants_poll(&req));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            file, binary = root / 'wait.c', root / 'wait'
            file.write_text(source)
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', str(file), '-o', str(binary)],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
