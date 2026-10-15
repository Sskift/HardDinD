/* SPDX-License-Identifier: GPL-2.0 */
/* 
 * Copyright (C) 2002 - 2007 Jeff Dike (jdike@{addtoit,linux.intel}.com)
 */

#ifndef __ARCH_UM_MMU_H
#define __ARCH_UM_MMU_H

#include "linux/types.h"
#include <linux/completion.h>
#include <linux/mutex.h>
#include <linux/spinlock.h>
#include <mm_id.h>

typedef struct mm_context {
	struct mm_id id;
	/* Parallel seccomp runners share the broker's host address space. */
	struct mm_id cpu_ids[NR_CPUS];
	struct mutex turnstile;
	struct mm_id gpu_id;
	struct mutex gpu_lock;
	struct pid *gpu_owner;
	/* 0017 asynchronous GPU completion: one outstanding batch per mm. */
	struct completion gpu_done;
	int gpu_async_irq;

	struct list_head list;

	/* Address range in need of a TLB sync */
	spinlock_t sync_tlb_lock;
	unsigned long sync_tlb_range_from;
	unsigned long sync_tlb_range_to;
} mm_context_t;

#define INIT_MM_CONTEXT(mm)						\
	.context = {							\
		.turnstile = __MUTEX_INITIALIZER(mm.context.turnstile),	\
		.sync_tlb_lock = __SPIN_LOCK_INITIALIZER(mm.context.sync_tlb_lock), \
	}

#endif
