import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


class DirectoryInvalidation(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_directory_fields_pages_and_bad_messages(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        patch = (root / 'kernel/candidates/share-dir-cache.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        function = re.search(r'static int virtio_fs_invalidate_directory\(.*?\n}', added, re.S).group()
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
typedef uint64_t u64;
#define FUSE_STATX_MODDIR (0x40U | 0x80U | 0x200U | 0x400U | 0x4U)
struct fuse_conn { unsigned attr_version; };
struct fuse_inode { int lock; unsigned attr_version; };
struct inode { unsigned i_mode; void *i_mapping; };
static struct inode inode;
static struct fuse_inode fi;
static unsigned mask, pages, versions, put_count;
static struct inode *fuse_ilookup(struct fuse_conn *fc, u64 id, void *unused) {
    (void)fc; (void)unused; return id == 7 ? &inode : NULL;
}
static struct fuse_inode *get_fuse_inode(struct inode *p) { assert(p == &inode); return &fi; }
static void spin_lock(int *p) { assert(!(*p)++); }
static void spin_unlock(int *p) { assert((*p)-- == 1); }
static unsigned atomic64_inc_return(unsigned *p) { return ++*p; }
static void iput(struct inode *p) { assert(p == &inode); put_count++; }
static void fuse_invalidate_attr_mask(struct inode *p, unsigned value) { assert(p == &inode); mask |= value; }
static void inode_maybe_inc_iversion(struct inode *p, bool force) { assert(p == &inode && !force); versions++; }
static void invalidate_inode_pages2_range(void *mapping, unsigned long first, unsigned long last) {
    assert(mapping == inode.i_mapping && first == 0 && last == (unsigned long)-1); pages++;
}
''' + function + r'''
int main(void) {
    struct fuse_conn fc = {0};
    u64 id = 7;
    assert(virtio_fs_invalidate_directory(&fc, &id, 0) == -EINVAL);
    id = 8;
    assert(virtio_fs_invalidate_directory(&fc, &id, sizeof(id)) == -ENOENT);
    id = 7; inode.i_mode = S_IFREG | 0600;
    assert(virtio_fs_invalidate_directory(&fc, &id, sizeof(id)) == -EINVAL);
    assert(put_count == 1 && !mask && !pages && !fc.attr_version);
    inode.i_mode = S_IFDIR | 0755;
    assert(!virtio_fs_invalidate_directory(&fc, &id, sizeof(id)));
    assert(mask == FUSE_STATX_MODDIR && pages == 1 && versions == 1 && put_count == 2);
    assert(!(mask & (0x2U | 0x8U | 0x10U)));
    assert(fi.attr_version == 1 && fc.attr_version == 1 && !fi.lock);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            source, binary = pathlib.Path(tmp) / 'directory.c', pathlib.Path(tmp) / 'directory'
            source.write_text(code)
            flags = ['-static'] if sys.platform == 'linux' else []
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                            str(source), '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, timeout=5)


if __name__ == '__main__':
    unittest.main()
