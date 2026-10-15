// SPDX-License-Identifier: GPL-2.0
/*
 * Copyright (C) 2000 - 2007 Jeff Dike (jdike@{addtoit,linux.intel}.com)
 */

#include <linux/mm.h>
#include <linux/module.h>
#include <linux/sched/signal.h>

#include <asm/tlbflush.h>
#include <asm/mmu_context.h>
#include <as-layout.h>
#include <mem_user.h>
#include <os.h>
#include <skas.h>
#include <kern_util.h>
#include <faultstat.h>

/*
 * Instrumented map/unmap records.  They are only wired into the walk
 * when uml_faultstat_enabled is set; otherwise __um_tlb_sync() uses the
 * original map()/unmap() operations directly, i.e. zero wrapper calls.
 * The operation's result is returned byte for byte: observation cannot
 * change a driver/syscall result.
 */
static int fs_map(struct mm_id *mm_idp, unsigned long virt, unsigned long len,
		  int prot, int phys_fd, unsigned long long offset)
{
	int ret = map(mm_idp, virt, len, prot, phys_fd, offset);

	uml_fs_inc(&uml_fs_map);
	return ret;
}

static int fs_unmap(struct mm_id *mm_idp, unsigned long virt,
		    unsigned long len)
{
	int ret = unmap(mm_idp, virt, len);

	uml_fs_inc(&uml_fs_unmap);
	return ret;
}

struct vm_ops {
	struct mm_id *mm_idp;

	int (*mmap)(struct mm_id *mm_idp,
		    unsigned long virt, unsigned long len, int prot,
		    int phys_fd, unsigned long long offset);
	int (*unmap)(struct mm_id *mm_idp,
		     unsigned long virt, unsigned long len);
};

static int kern_map(struct mm_id *mm_idp,
		    unsigned long virt, unsigned long len, int prot,
		    int phys_fd, unsigned long long offset)
{
	return os_map_memory((void *)virt, phys_fd, offset, len,
			     prot & UM_PROT_READ, prot & UM_PROT_WRITE,
			     prot & UM_PROT_EXEC);
}

static int kern_unmap(struct mm_id *mm_idp,
		      unsigned long virt, unsigned long len)
{
	return os_unmap_memory((void *)virt, len);
}

void report_enomem(void)
{
	printk(KERN_ERR "UML ran out of memory on the host side! "
			"This can happen due to a memory limitation or "
			"vm.max_map_count has been reached.\n");
}

static inline int update_pte_range(pmd_t *pmd, unsigned long addr,
				   unsigned long end,
				   struct vm_ops *ops)
{
	pte_t *pte;
	int ret = 0;

	/* The parent is a stable snapshot; a concurrent clear must not turn
	 * our table pointer into NULL before its invalidation waits for us. */
	pte = pte_offset_kernel(pmd, addr);
	do {
		pte_t entry = READ_ONCE(*pte);
		if (!pte_needsync(entry))
			continue;

		if (pte_present(entry)) {
			__u64 offset;
			unsigned long phys = pte_val(entry) & PAGE_MASK;
			int fd = phys_mapping(phys, &offset);
			int r, w, x, prot;

			r = pte_read(entry);
			w = pte_write(entry);
			x = pte_exec(entry);
			if (!pte_young(entry)) {
				r = 0;
				w = 0;
			} else if (!pte_dirty(entry))
				w = 0;

			prot = (r ? UM_PROT_READ : 0) |
			       (w ? UM_PROT_WRITE : 0) |
			       (x ? UM_PROT_EXEC : 0);

			ret = ops->mmap(ops->mm_idp, addr, PAGE_SIZE,
					prot, fd, offset);
		} else
			ret = ops->unmap(ops->mm_idp, addr, PAGE_SIZE);

		cmpxchg(&pte_val(*pte), pte_val(entry),
			pte_val(pte_mkuptodate(entry)));
	} while (pte++, addr += PAGE_SIZE, ((addr < end) && !ret));
	return ret;
}

static inline int update_pmd_range(pud_t *pud, unsigned long addr,
				   unsigned long end,
				   struct vm_ops *ops)
{
	pmd_t *pmd;
	unsigned long next;
	int ret = 0;

	pmd = pmd_offset(pud, addr);
	do {
		next = pmd_addr_end(addr, end);
		pmd_t entry = READ_ONCE(*pmd);
		if (!pmd_present(entry)) {
			if (pmd_needsync(entry)) {
				ret = ops->unmap(ops->mm_idp, addr,
						 next - addr);
				cmpxchg(&pmd_val(*pmd), pmd_val(entry),
				pmd_val(entry) & ~_PAGE_NEEDSYNC);
			}
		}
		else ret = update_pte_range(&entry, addr, next, ops);
	} while (pmd++, addr = next, ((addr < end) && !ret));
	return ret;
}

static inline int update_pud_range(p4d_t *p4d, unsigned long addr,
				   unsigned long end,
				   struct vm_ops *ops)
{
	pud_t *pud;
	unsigned long next;
	int ret = 0;

	pud = pud_offset(p4d, addr);
	do {
		next = pud_addr_end(addr, end);
		pud_t entry = READ_ONCE(*pud);
		if (!pud_present(entry)) {
			if (pud_needsync(entry)) {
				ret = ops->unmap(ops->mm_idp, addr,
						 next - addr);
				cmpxchg(&pud_val(*pud), pud_val(entry),
				pud_val(entry) & ~_PAGE_NEEDSYNC);
			}
		}
		else ret = update_pmd_range(&entry, addr, next, ops);
	} while (pud++, addr = next, ((addr < end) && !ret));
	return ret;
}

static inline int update_p4d_range(pgd_t *pgd, unsigned long addr,
				   unsigned long end,
				   struct vm_ops *ops)
{
	p4d_t *p4d;
	unsigned long next;
	int ret = 0;

	p4d = p4d_offset(pgd, addr);
	do {
		next = p4d_addr_end(addr, end);
		p4d_t entry = READ_ONCE(*p4d);
		if (!p4d_present(entry)) {
			if (p4d_needsync(entry)) {
				ret = ops->unmap(ops->mm_idp, addr,
						 next - addr);
				cmpxchg(&p4d_val(*p4d), p4d_val(entry),
				p4d_val(entry) & ~_PAGE_NEEDSYNC);
			}
		} else
			ret = update_pud_range(&entry, addr, next, ops);
	} while (p4d++, addr = next, ((addr < end) && !ret));
	return ret;
}

static int __um_tlb_sync(struct mm_struct *mm)
{
	pgd_t *pgd;
	struct vm_ops ops;
	unsigned long addr, next;
	int ret = 0;

	guard(spinlock_irqsave)(&mm->context.sync_tlb_lock);

	if (mm->context.sync_tlb_range_to == 0)
		return parallel_userspace && mm != &init_mm ?
			syscall_stub_flush(&mm->context.id) : 0;

	ops.mm_idp = &mm->context.id;
	if (mm == &init_mm) {
		ops.mmap = kern_map;
		ops.unmap = kern_unmap;
	} else if (__builtin_expect(uml_faultstat_enabled, 0)) {
		ops.mmap = fs_map;
		ops.unmap = fs_unmap;
	} else {
		ops.mmap = map;
		ops.unmap = unmap;
	}

	uml_fs_inc(&uml_fs_sync);
	addr = mm->context.sync_tlb_range_from;
	pgd = pgd_offset(mm, addr);
	do {
		next = pgd_addr_end(addr, mm->context.sync_tlb_range_to);
		pgd_t entry = READ_ONCE(*pgd);
		if (!pgd_present(entry)) {
			if (pgd_needsync(entry)) {
				ret = ops.unmap(ops.mm_idp, addr,
						next - addr);
				cmpxchg(&pgd_val(*pgd), pgd_val(entry),
				pgd_val(entry) & ~_PAGE_NEEDSYNC);
			}
		} else
			ret = update_p4d_range(&entry, addr, next, &ops);
	} while (pgd++, addr = next,
		 ((addr < mm->context.sync_tlb_range_to) && !ret));

	if (!ret && parallel_userspace && mm != &init_mm) {
		unsigned long long fs_t0 = uml_fs_time_begin();

		ret = syscall_stub_flush(&mm->context.id);
		/* begin() returns 0 without touching the clock when off;
		 * end() is then a pure enabled-check, no clock, no atomic. */
		uml_fs_time_end(&uml_fs_flush_ns, fs_t0);
		uml_fs_inc(&uml_fs_flush);
	}

	if (ret == -ENOMEM)
		report_enomem();

	mm->context.sync_tlb_range_from = 0;
	mm->context.sync_tlb_range_to = 0;

	return ret;
}

int um_tlb_sync(struct mm_struct *mm)
{
	/* No mapping work means no broker round trip or IRQ-mask transition.
	 * A concurrent invalidation performs its own synchronous flush before
	 * returning; this runner must not wait for an invalidation not yet begun.
	 * Keep the queued-command check for a freshly created address space.
	 */
	if (parallel_userspace && mm != &init_mm &&
	    !READ_ONCE(mm->context.sync_tlb_range_to) &&
	    !READ_ONCE(mm->context.id.syscall_data_len))
		return 0;
	/* A flush can be called under the PTE lock. Parallel mode serializes
	 * the broker and snapshots PTEs without recursively taking that lock.
	 * Invalidation waits for this lock before page tables can be freed.
	 */
	if (parallel_userspace && mm != &init_mm)
		return __um_tlb_sync(mm);
	scoped_guard(spinlock_irqsave, &mm->page_table_lock)
		return __um_tlb_sync(mm);
	return 0;
}

void um_tlb_sync_if_parallel(struct mm_struct *mm)
{
	if (parallel_userspace && mm != &init_mm && um_tlb_sync(mm))
		panic("Unable to synchronize shared host mappings");
}

void flush_tlb_all(void)
{
	/*
	 * Don't bother flushing if this address space is about to be
	 * destroyed.
	 */
	if (atomic_read(&current->mm->mm_users) == 0)
		return;

	flush_tlb_mm(current->mm);
}

void flush_tlb_mm(struct mm_struct *mm)
{
	struct vm_area_struct *vma;
	VMA_ITERATOR(vmi, mm, 0);

	for_each_vma(vmi, vma)
		um_tlb_mark_sync(mm, vma->vm_start, vma->vm_end);
	um_tlb_sync_if_parallel(mm);
}
