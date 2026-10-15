// SPDX-License-Identifier: GPL-2.0
/*
 * Copyright (C) 2002 - 2007 Jeff Dike (jdike@{addtoit,linux.intel}.com)
 */

#include <linux/init.h>
#include <linux/sched/mm.h>
#include <linux/sched/task_stack.h>
#include <linux/sched/task.h>
#include <linux/smp-internal.h>

#include <asm/tlbflush.h>

#include <as-layout.h>
#include <kern.h>
#include <os.h>
#include <skas.h>
#include <kern_util.h>

extern void start_kernel(void);

static int __init start_kernel_proc(void *unused)
{
	block_signals_trace();

	start_kernel();
	return 0;
}

char cpu_irqstacks[NR_CPUS][THREAD_SIZE] __aligned(THREAD_SIZE);

int __init start_uml(void)
{
	stack_protections((unsigned long) &cpu_irqstacks[0]);
	set_sigstack(cpu_irqstacks[0], THREAD_SIZE);

	init_new_thread_signals();

	init_task.thread.request.thread.proc = start_kernel_proc;
	init_task.thread.request.thread.arg = NULL;
	return start_idle_thread(task_stack_page(&init_task),
				 &init_task.thread.switch_buf);
}

unsigned long current_stub_stack(void)
{
	if (current->mm == NULL)
		return 0;

	return current_mm_id()->stack;
}

struct mm_id *current_mm_id(void)
{
	if (current->mm == NULL)
		return NULL;

	if (parallel_userspace)
		return &current->mm->context.cpu_ids[raw_smp_processor_id()];
	return &current->mm->context.id;
}

struct mm_id *current_mm_enter(void)
{
	struct mm_struct *mm = current->mm;
	struct mm_id *id;
	unsigned long stack = 0;
	int err;

	if (!parallel_userspace) {
		id = &mm->context.id;
		enter_turnstile(id);
		return id;
	}
	/* Allocate before pinning; allocation can schedule onto another CPU. */
	for (;;) {
		preempt_disable();
		id = current_mm_id();
		if (id->stack || stack)
			break;
		preempt_enable();
		stack = __get_free_pages(GFP_KERNEL | __GFP_ZERO,
					ilog2(STUB_THREAD_PAGES));
		if (!stack)
			panic("Unable to allocate parallel userspace runner");
	}
	if (!id->stack) {
		guard(spinlock_irqsave)(&mm->context.sync_tlb_lock);
		id->stack = stack;
		stack = 0;
		err = start_userspace_cpu(&mm->context.id, id,
			STUB_THREAD_DATA(raw_smp_processor_id()));
		if (err)
			panic("Unable to start parallel userspace runner: %d", err);
	}
	if (stack)
		free_pages(stack, ilog2(STUB_THREAD_PAGES));
	return id;
}

void current_mm_exit(struct mm_id *id)
{
	if (parallel_userspace)
		preempt_enable();
	else
		exit_turnstile(id);
}

void alarm_current_mm(void)
{
	struct mm_id *id = current_mm_id();
	if (id && id->pid > 1)
		os_alarm_process(id->pid);
}

void current_mm_sync(void)
{
	if (current->mm == NULL)
		return;

	if (um_tlb_sync(current->mm))
		panic("Unable to synchronize userspace mappings");
}

static DEFINE_SPINLOCK(initial_jmpbuf_spinlock);

void initial_jmpbuf_lock(void)
{
	spin_lock_irq(&initial_jmpbuf_spinlock);
}

void initial_jmpbuf_unlock(void)
{
	spin_unlock_irq(&initial_jmpbuf_spinlock);
}
