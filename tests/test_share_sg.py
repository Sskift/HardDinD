import pathlib
import shutil
import subprocess
import tempfile
import unittest


class ScatterRuns(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_actual_initializer_preserves_ranges_and_boundaries(self):
        patch = (pathlib.Path(__file__).resolve().parents[1] /
                 'kernel/candidates/share-sg.patch').read_text()
        lines = patch.splitlines()
        # Reconstruct the function from both context and added patch lines.
        source = '\n'.join(line[1:] for line in lines if line.startswith((' ', '+'))
                           and not line.startswith('+++'))
        start = source.index('static unsigned int sg_init_fuse_folios(')
        end = source.index('\n}', start) + 2
        function = source[start:end]
        harness = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <stdbool.h>
#define min(a,b) ((a)<(b)?(a):(b))
struct folio { uint64_t addr; };
struct fuse_folio_desc { unsigned int length, offset; };
struct scatterlist { uint64_t addr; unsigned int length, offset; };
static bool coalesce_sg;
#define folio_page(f,n) (f)
#define page_to_phys(p) ((p)->addr)
#define sg_phys(s) ((s)->addr + (s)->offset)
static void sg_init_table(struct scatterlist *s, unsigned int n) { memset(s,0,n*sizeof(*s)); }
static void sg_set_folio(struct scatterlist *s, struct folio *f, unsigned int n, unsigned int off) {
    s->addr=f->addr; s->length=n; s->offset=off;
}
''' + function + r'''
int main(void) {
    struct folio data[]={{0x1000},{0x2000},{0x3000},{0x9000}};
    struct folio *folios[]={&data[0],&data[1],&data[2],&data[3]};
    struct fuse_folio_desc desc[]={{4083,13},{4096,0},{4096,0},{4096,0}};
    struct scatterlist sg[4];
    assert(sg_init_fuse_folios(sg,folios,desc,4,0)==0);
    for (unsigned int size=1; size<=16371; size+=17) {
        coalesce_sg=false;
        unsigned int original=sg_init_fuse_folios(sg,folios,desc,4,size);
        unsigned int bytes=0;
        for (unsigned int i=0;i<original;i++) bytes+=sg[i].length;
        assert(bytes==size);
        coalesce_sg=true;
        unsigned int merged=sg_init_fuse_folios(sg,folios,desc,4,size);
        assert(merged==(size<=12275?1:2));
        assert(sg_phys(&sg[0])==0x100d);
        assert(sg[0].length==(size<12275?size:12275));
        if (merged==2) assert(sg_phys(&sg[1])==0x9000 && sg[1].length==size-12275);
        assert(merged<=original);
    }
    desc[1].offset=8; desc[1].length=4088;
    assert(sg_init_fuse_folios(sg,folios,desc,4,12267)==2);
    assert(sg[0].length==4083 && sg_phys(&sg[1])==0x2008 && sg[1].length==8184);
    data[1].addr=0x1000; desc[1].offset=0; desc[1].length=4096;
    assert(sg_init_fuse_folios(sg,folios,desc,4,8179)==2);
    assert(sg_phys(&sg[0])==0x100d && sg_phys(&sg[1])==0x1000);
    coalesce_sg=false;
    assert(sg_init_fuse_folios(sg,folios,desc,4,16371)==4);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source, binary = root / 'sg.c', root / 'sg'
            source.write_text(harness)
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
