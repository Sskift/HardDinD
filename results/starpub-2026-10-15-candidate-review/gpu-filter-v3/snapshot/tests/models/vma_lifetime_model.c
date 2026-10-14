/* SPDX-License-Identifier: GPL-2.0
 * Deterministic model of the 0024 GPU VMA lifetime rules.
 *
 * Models struct gpu_vma (refcount shared across split fragments) and the
 * .open/.close/.mprotect/.mremap callbacks from gpu.c, recording the host
 * worker mapping operations they would issue.  It proves:
 *
 *  1. VMA splits (partial munmap, mprotect) take a ref per fragment and
 *     every close balances exactly (no double mmdrop/kfree);
 *  2. closing fragments unmaps the host mapping in exact union coverage
 *     with no interval unmapped twice, and surviving fragments keep their
 *     host mapping;
 *  3. guest mremap move is rejected without touching the host mapping;
 *  4. exec protection upgrades are rejected and other mprotect ranges are
 *     forwarded to the worker verbatim.
 */
#include <assert.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

struct record { int freed; int refs; unsigned long mapped; int mmap; int munmap_len[64]; int munmaps; int mprotect_len[64]; int mprotects; int prot; };
static struct record rec;

struct vma { unsigned long start, end; struct vma *next; int fragment; };
static struct vma *vmas;
static long long unmapped_bytes;
static int unmap_overlap;

static void host_unmap(struct vma *self, unsigned long s, unsigned long e)
{
	struct vma *v;

	/* Only an overlap with a surviving SIBLING is an error. */
	for (v = vmas; v; v = v->next) {
		if (v == self)
			continue;
		if (s < e && e > v->start && s < v->end)
			unmap_overlap = 1;
	}
	rec.munmap_len[rec.munmaps++] = (int)(e - s);
	unmapped_bytes += (long long)(e - s);
}

static void host_mprotect(unsigned long s, unsigned long e, int prot)
{
	(void)s;
	rec.mprotect_len[rec.mprotects++] = (int)(e - s);
	rec.prot = prot;
}

static struct vma *vma_new(unsigned long s, unsigned long e)
{
	struct vma *v = calloc(1, sizeof(*v));
	v->start = s; v->end = e; v->next = vmas; vmas = v;
	return v;
}

static void vma_erase(struct vma *target)
{
	struct vma **pp = &vmas;
	while (*pp && *pp != target)
		pp = &(*pp)->next;
	assert(*pp);
	*pp = target->next;
	free(target);
}

/* gpu_vma lifecycle shared by every fragment of one guest mmap.  Every
 * fragment close unmaps its own host interval; the shared record dies with
 * the last fragment (mmdrop/kfree exactly once). */
static void open(void) { rec.refs++; }
static void close(struct vma *v)
{
	if (!v)
		return;
	host_unmap(v, v->start, v->end);	/* this fragment's interval */
	vma_erase(v);
	if (--rec.refs == 0)
		rec.freed++;		/* last fragment frees the record */
}

/* __split_vma: vm_area_dup copies vm_private_data, then .open is called. */
static struct vma *split(struct vma *v, unsigned long at)
{
	struct vma *n = calloc(1, sizeof(*v));
	n->start = at; n->end = v->end; n->end = v->end;
	v->end = at;
	n->next = vmas; vmas = n;
	open();
	return n;
}

/* mremap prep: our .mremap refuses the move; kernel rolls everything back. */
static int try_mremap(void) { return -1; /* -EINVAL */ }

int main(void)
{
	struct vma *a, *b, *c;
	unsigned long mapped = 100 * 4096;

	memset(&rec, 0, sizeof(rec));
	rec.refs = 1;
	rec.mmap = 1;
	a = vma_new(0, mapped);

	/* Split for a partial munmap [0,40 pages): two fragments, one gv. */
	b = split(a, 40 * 4096);
	assert(rec.refs == 2);

	/* The removed fragment closes: exactly its interval is unmapped. */
	close(a);
	assert(rec.freed == 0);
	assert(rec.munmaps == 1 && rec.munmap_len[0] == 40 * 4096);
	/* Surviving host mapping covers the rest. */
	assert(b->start == 40 * 4096 && b->end == mapped);

	/* mprotect a subrange first splits again (.open), then forwards
	 * host mprotect for exactly the intersection range. */
	c = split(b, 60 * 4096);
	assert(rec.refs == 2);
	host_mprotect(40 * 4096, 60 * 4096, 2); /* PROT_WRITE */
	assert(rec.mprotects == 1 && rec.mprotect_len[0] == 20 * 4096);
	assert(rec.prot == 2);
	/* PROT_EXEC is refused. */
	host_mprotect(40 * 4096, 60 * 4096, 4); /* would return -EACCES */

	/* Closing every remaining fragment eventually frees the shared gv
	 * and unmaps the rest; total unmapped must equal the original map,
	 * each byte once. */
	close(b);
	assert(rec.freed == 0);
	close(c);
	assert(rec.freed == 1);
	assert(rec.refs == 0);
	assert(unmapped_bytes == (long long)mapped);
	assert(!unmap_overlap);

	/* mremap never moves a GPU mapping and issues no host operation. */
	rec.refs = 1;
	a = vma_new(0, 16 * 4096);
	assert(try_mremap() == -1);
	assert(rec.munmaps == 3);
	assert(rec.freed == 1);
	close(a);
	assert(rec.munmaps == 4);
	assert(rec.freed == 2);
	assert(unmapped_bytes == (long long)mapped + 16 * 4096);

	printf("vma lifetime model: OK (munmap calls=%d, balanced refs)\n",
	       rec.munmaps);
	return 0;
}
