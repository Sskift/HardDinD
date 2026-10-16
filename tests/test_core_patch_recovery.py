"""Execute the candidate's recovery logic at every old instruction boundary."""
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

from pipeline import config


class PatchRecoveryTests(unittest.TestCase):
    def test_stack_prefix_fault_address_requires_exact_metadata(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        patch = (config.REPO / 'kernel/candidates/core-syscall-patch.patch').read_text()
        part = patch.split('+++ b/arch/um/include/shared/core-patch.h\n')[1].split('--- a/')[0]
        header = '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        source = '#include <assert.h>\n#include <string.h>\n#include "core-patch.h"\n' + r'''
int main(void) {
    unsigned char slot[256];
    unsigned long end = 0x401007;
    for (unsigned int form = 0; form < 2; form++) {
        unsigned char old[] = {form ? 0x48 : 0x4c, 0x8b, form ? 0x44 : 0x4c,
                               0x24, 8, 0x0f, 0x05};
        memset(slot, 0, sizeof(slot));
        slot[44] = 1; slot[88] = 7;
        memcpy(slot + 48, &end, 8); memcpy(slot + 89, old, 7); memcpy(slot + 136, old, 5);
        for (unsigned long offset = 0; offset < 256; offset++)
            assert(uml_patch_prefix_ip(slot, offset) == (offset == 136 ? end - 7 : 0));
        unsigned int fields[] = {44, 88, 89, 90, 91, 92, 93, 94, 95, 136, 137, 138, 139, 140};
        for (unsigned int i = 0; i < sizeof(fields)/sizeof(fields[0]); i++) {
            slot[fields[i]] ^= 1;
            assert(!uml_patch_prefix_ip(slot, 136));
            slot[fields[i]] ^= 1;
        }
        memset(slot + 48, 0, 8);
        assert(!uml_patch_prefix_ip(slot, 136));
    }
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'core-patch.h').write_text(header)
            (path / 'test.c').write_text(source)
            subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64',
                         'Linux low-address mappings')
    def test_fixed_and_go_inflight_registers(self):
        patch = (config.REPO / 'kernel/candidates/core-syscall-patch.patch').read_text()
        def added(name):
            part = patch.split('+++ b/' + name + '\n')[1].split('--- a/')[0]
            return '\n'.join(line[1:] for line in part.splitlines() if line.startswith('+'))
        header = added('arch/um/include/shared/core-patch.h')
        source = added('arch/um/kernel/core-patch.c')
        function = 'int uml_patch_fault(' + source.split('int uml_patch_fault(', 1)[1].split('\nvoid uml_patch_count', 1)[0]
        source = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
typedef uint32_t u32;
typedef uint64_t u64;
#define __user
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define HOST_R11 11
struct uml_pt_regs { unsigned long ip, ax, bx, cx, dx, si, di, flags, gp[20]; int fast_syscall; };
#define UPT_IP(r) ((r)->ip)
#define UPT_AX(r) ((r)->ax)
#define UPT_BX(r) ((r)->bx)
#define UPT_CX(r) ((r)->cx)
#define UPT_DX(r) ((r)->dx)
#define UPT_SI(r) ((r)->si)
#define UPT_DI(r) ((r)->di)
#define UPT_EFLAGS(r) ((r)->flags)
#define PT_SYSCALL_NR(gp) ((gp)[19])
static int copy_from_user_nofault(void *dst, const void *src, unsigned long len) {
    memcpy(dst, src, len); return 0;
}
int uml_syscall_patch = 1, parallel_userspace = 1;
''' + header + '\n' + function + r'''
int main(void) {
    unsigned char fixed[] = {0xb8,39,0,0,0,0x0f,0x05};
    unsigned char longer[] = {0x48,0xc7,0xc0,39,0,0,0,0x0f,0x05};
    unsigned char go[] = {0x49,0x89,0xf2,0x48,0x89,0xfa,0x48,0x89,0xce,0x48,0x89,0xdf,0x0f,0x05};
    unsigned char *forms[] = {fixed, longer, go};
    unsigned int lengths[] = {sizeof(fixed), sizeof(longer), sizeof(go)};
    unsigned char *table = mmap((void *)UML_PATCH_BASE, UML_PATCH_END-UML_PATCH_BASE, 3,
                                MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED_NOREPLACE, -1, 0);
    unsigned char *text = mmap(NULL, 4096, 3, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    assert(table == (void *)UML_PATCH_BASE && text != MAP_FAILED);
    unsigned char *code = table + UML_PATCH_SLOT, *site = text + 32;
    for (unsigned int form = 0; form < ARRAY_SIZE(forms); form++) {
        unsigned int len = lengths[form];
        uint64_t end = (uintptr_t)site + len;
        uint32_t nr = 39;
        memset(text, 0x90, 4096); memset(code, 0, UML_PATCH_SLOT);
        memcpy(code + 40, &nr, 4); memcpy(code + 48, &end, 8);
        code[88] = len; memcpy(code + 89, forms[form], len);
        uml_patch_jump(site, len, (uintptr_t)code);
        for (unsigned int offset = 0; offset < len; offset++) {
            struct uml_pt_regs r = {.ip=(uintptr_t)site+offset, .ax=39, .bx=10, .cx=20,
                                   .dx=99, .di=30, .si=40, .flags=0x203};
            if (form == 2) {
                if (offset >= 3) r.gp[10] = 40;
                if (offset >= 6) r.dx = 30;
                if (offset >= 9) r.si = 20;
                if (offset >= 12) r.di = 10;
            }
            int expected = offset == len - 2 || (form == 2 && offset && !(offset % 3));
            assert(uml_patch_fault(&r) == expected);
            if (!expected) { assert(r.ip == (uintptr_t)site+offset && !r.fast_syscall); continue; }
            assert(r.ip == end && r.cx == end && r.gp[HOST_R11] == r.flags);
            assert(r.ax == nr && PT_SYSCALL_NR(r.gp) == nr && r.fast_syscall);
            if (form == 2) assert(r.gp[10] == 40 && r.dx == 30 && r.si == 20 && r.di == 10);
        }
        site[len-1] ^= 1;
        struct uml_pt_regs r = {.ip=(uintptr_t)site+len-2};
        assert(!uml_patch_fault(&r));
    }
}
'''
        with tempfile.TemporaryDirectory(prefix='.core-test-', dir=config.REPO) as directory:
            path = pathlib.Path(directory)
            (path / 'test.c').write_text(source)
            subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static', path / 'test.c',
                            '-o', path / 'test'], check=True)
            subprocess.run([path / 'test'], check=True, timeout=10)
