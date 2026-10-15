// SPDX-License-Identifier: GPL-2.0
/*
 * Copyright (C) 2015 Thomas Meyer (thomas@m3y3r.de)
 * Copyright (C) 2002 - 2007 Jeff Dike (jdike@{addtoit,linux.intel}.com)
 */

#include <linux/mm.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>

#include <shared/irq_kern.h>
#include <asm/pgalloc.h>
#include <asm/sections.h>
#include <asm/mmu_context.h>
#include <as-layout.h>
#include <os.h>
#include <skas.h>
#include <stub-data.h>

/* Ensure the stub_data struct covers the allocated area */
static_assert(sizeof(struct stub_data) == STUB_DATA_PAGES * UM_KERN_PAGE_SIZE);

/* The GPU payload must stay in the first stub data page, which is mapped
 * into every GPU worker; it must not grow the shared stub region. */
static_assert(STUB_GPU_PAYLOAD_SIZE == 128);
static_assert(offsetof(struct stub_data, gpu_payload) % 64 == 0);
static_assert(offsetof(struct stub_data, gpu_payload) + STUB_GPU_PAYLOAD_SIZE
	      <= UM_KERN_PAGE_SIZE);

/* 0024: the relay table occupies the first two pages of the four-page
 * STUB_GPU_RELAY slot; the relay thread stack grows down through the other
 * two.  Entries are fixed-size for both the kernel and the stub-side relay. */
static_assert(sizeof(struct gpu_relay_entry) == 40);
static_assert(offsetof(struct gpu_relay_entry, fd) == 0);
static_assert(offsetof(struct gpu_relay_page, entry) == 24);
static_assert(sizeof(struct gpu_relay_page) <= 2 * UM_KERN_PAGE_SIZE);
/* Workspace in page 2, relay stack grows down through the rest of pages
 * 2..3: keep at least 4 KiB of stack after the workspace. */
static_assert(2 * UM_KERN_PAGE_SIZE >= sizeof(struct gpu_relay_work) + 4096);
static_assert(STUB_GPU_SHADOW_PAGES * UM_KERN_PAGE_SIZE >= 9264);

static_assert(NR_CPUS <= 64);

static spinlock_t mm_list_lock;
static struct list_head mm_list;

/* Only idle mapping brokers are cached; user runners are always destroyed.
 * Four entries bound retained host processes, sockets and guest pages.
 * Cache entries stay in mm_list so unexpected death is still tracked.
 */
#define BROKER_CACHE_SIZE 4
static struct mm_context broker_cache[BROKER_CACHE_SIZE];
static unsigned long broker_cache_hits, broker_cache_stores;

static int take_cached_broker(struct mm_context *ctx)
{
	if (!parallel_userspace || !seccomp_broker_cache)
		return 0;
	guard(spinlock_irqsave)(&mm_list_lock);
	for (int i = 0; i < BROKER_CACHE_SIZE; i++) {
		struct mm_context *cached = &broker_cache[i];
		if (!cached->id.stack || cached->id.pid < 2)
			continue;
		ctx->id = cached->id;
		memset(&cached->id, 0, sizeof(cached->id));
		list_replace(&cached->list, &ctx->list);
		broker_cache_hits++;
		return 1;
	}
	return 0;
}

static int store_cached_broker(struct mm_context *ctx)
{
	struct stub_data *data = (void *)ctx->id.stack;
	int err;

	if (!parallel_userspace || !seccomp_broker_cache || ctx->id.pid < 2)
		return 0;
	/* All runners have been killed and reaped. Drop EVERY guest mapping and
	 * runner stack before publishing a reusable broker. On any error, kill it.
	 * The broker itself never executes application code or saves guest regs.
	 */
	err = unmap(&ctx->id, 0, STUB_START);
	if (!err)
		err = unmap(&ctx->id, STUB_THREAD_DATA(0),
			    STUB_END - STUB_THREAD_DATA(0));
	if (!err)
		err = syscall_stub_flush(&ctx->id);
	if (err || ctx->id.pid < 2)
		return 0;
	memset(data->syscall_data, 0, sizeof(data->syscall_data));
	memset(ctx->id.syscall_fd_map, 0, sizeof(ctx->id.syscall_fd_map));
	data->clone_pid = 0;
	data->child_socket = 0;
	guard(spinlock_irqsave)(&mm_list_lock);
	if (!seccomp_broker_cache)
		return 0;
	for (int i = 0; i < BROKER_CACHE_SIZE; i++) {
		struct mm_context *cached = &broker_cache[i];
		if (cached->id.stack)
			continue;
		cached->id = ctx->id;
		memset(&ctx->id, 0, sizeof(ctx->id));
		list_add(&cached->list, &mm_list);
		broker_cache_stores++;
		return 1;
	}
	return 0;
}

void cleanup_broker_cache(void)
{
	/* Disable insertion before draining; shutdown must leave no host child. */
	scoped_guard(spinlock_irqsave, &mm_list_lock)
		seccomp_broker_cache = 0;
	for (int i = 0; i < BROKER_CACHE_SIZE; i++) {
		struct mm_id id;
		scoped_guard(spinlock_irqsave, &mm_list_lock) {
			id = broker_cache[i].id;
			if (id.stack) {
				memset(&broker_cache[i].id, 0, sizeof(id));
				list_del(&broker_cache[i].list);
			}
		}
		if (!id.stack)
			continue;
		if (id.pid > 1)
			os_kill_ptraced_process(id.pid, 1);
		if (id.sock)
			os_close_file(id.sock);
		free_pages(id.stack, ilog2(STUB_DATA_PAGES));
	}
	/* UML console exit callbacks have already run; use the host log. */
	os_info("HARDDIND_BROKER_CACHE hits=%lu stores=%lu\n",
		broker_cache_hits, broker_cache_stores);
}

struct mutex *__get_turnstile(struct mm_id *mm_id)
{
	struct mm_context *ctx = container_of(mm_id, struct mm_context, id);

	return &ctx->turnstile;
}

void enter_turnstile(struct mm_id *mm_id)
{
	mutex_lock(__get_turnstile(mm_id));
}

void exit_turnstile(struct mm_id *mm_id)
{
	mutex_unlock(__get_turnstile(mm_id));
}

int init_new_context(struct task_struct *task, struct mm_struct *mm)
{
	struct mm_id *new_id = &mm->context.id;
	unsigned long stack = 0;
	int ret = -ENOMEM;

	memset(mm->context.cpu_ids, 0, sizeof(mm->context.cpu_ids));
	mutex_init(&mm->context.turnstile);
	mutex_init(&mm->context.gpu_lock);
	memset(&mm->context.gpu_id, 0, sizeof(mm->context.gpu_id));
	mm->context.gpu_owner = NULL;
	init_completion(&mm->context.gpu_done);
	mm->context.gpu_async_irq = 0;
	mm->context.gpu_relay_pages = 0;
	mm->context.gpu_shadow_pages = 0;
	mm->context.gpu_kick_fd = -1;
	mm->context.gpu_notify_fd = -1;
	mm->context.gpu_relay_irq = 0;
	init_waitqueue_head(&mm->context.gpu_relay_wq);
	mm->context.gpu_relay_pending = 0;
	mm->context.gpu_eventd = NULL;
	INIT_LIST_HEAD(&mm->context.gpu_files);
	INIT_LIST_HEAD(&mm->context.gpu_events);
	spin_lock_init(&mm->context.sync_tlb_lock);
	mm->context.sync_tlb_range_from = 0;
	mm->context.sync_tlb_range_to = 0;
	if (take_cached_broker(&mm->context))
		return 0;

	stack = __get_free_pages(GFP_KERNEL | __GFP_ZERO, ilog2(STUB_DATA_PAGES));
	if (stack == 0)
		goto out;

	new_id->stack = stack;
	new_id->syscall_data_len = 0;
	new_id->syscall_fd_num = 0;
	new_id->cached_phys_fd = -1;

	scoped_guard(spinlock_irqsave, &mm_list_lock) {
		/* Insert into list, used for lookups when the child dies */
		list_add(&mm->context.list, &mm_list);
	}

	ret = start_userspace(new_id);
	if (ret < 0)
		goto out_free;

	/* Ensure the new MM is clean and nothing unwanted is mapped */
	unmap(new_id, 0, STUB_START);

	return 0;

 out_free:
	scoped_guard(spinlock_irqsave, &mm_list_lock)
		list_del(&mm->context.list);
	free_pages(new_id->stack, ilog2(STUB_DATA_PAGES));
 out:
	return ret;
}

void destroy_context(struct mm_struct *mm)
{
	struct mm_context *mmu = &mm->context;

	/*
	 * If init_new_context wasn't called, this will be
	 * zero, resulting in a kill(0), which will result in the
	 * whole UML suddenly dying.  Also, cover negative and
	 * 1 cases, since they shouldn't happen either.
	 *
	 * Negative cases happen if the child died unexpectedly.
	 */
	if (mmu->id.pid >= 0 && mmu->id.pid < 2) {
		printk(KERN_ERR "corrupt mm_context - pid = %d\n",
		       mmu->id.pid);
		return;
	}

	scoped_guard(spinlock_irqsave, &mm_list_lock)
		list_del(&mm->context.list);

	for (int cpu = 0; cpu < NR_CPUS; cpu++) {
		struct mm_id *id = &mmu->cpu_ids[cpu];
		if (!id->stack)
			continue;
		if (id->pid > 1)
			os_kill_ptraced_process(id->pid, 1);
		if (id->sock)
			os_close_file(id->sock);
		free_pages(id->stack, ilog2(STUB_THREAD_PAGES));
		memset(id, 0, sizeof(*id));
	}

	if (mmu->gpu_id.stack) {
		/* 0017: stop completion interrupts and fail any waiter before
		 * the worker is killed and its socket is closed. */
		if (mmu->gpu_async_irq) {
			complete_all(&mmu->gpu_done);
			um_free_irq(mmu->gpu_async_irq, mm);
			mmu->gpu_async_irq = 0;
		}
		/* 0024: join the event thread and release relay sockets
		 * before killing the worker thread group (which contains the
		 * relay thread).  No mmgrab backs the event thread: this join
		 * is the single reason it cannot outlive the mm. */
		gpu_bridge_teardown(mm);
		if (mmu->gpu_id.pid > 1)
			os_kill_ptraced_process(mmu->gpu_id.pid, 1);
		if (mmu->gpu_id.sock)
			os_close_file(mmu->gpu_id.sock);
		free_pages(mmu->gpu_id.stack, ilog2(STUB_THREAD_PAGES));
		if (mmu->gpu_relay_pages)
			free_pages(mmu->gpu_relay_pages,
				   ilog2(STUB_THREAD_PAGES));
		if (mmu->gpu_shadow_pages)
			free_pages(mmu->gpu_shadow_pages,
				   ilog2(STUB_GPU_SHADOW_PAGES));
		put_pid(mmu->gpu_owner);
		/* GPU-touched address spaces are never cached. */
	} else if (store_cached_broker(mmu)) {
		return;
	}

	if (mmu->id.pid > 0) {
		os_kill_ptraced_process(mmu->id.pid, 1);
		mmu->id.pid = -1;
	}

	if (using_seccomp && mmu->id.sock)
		os_close_file(mmu->id.sock);

	free_pages(mmu->id.stack, ilog2(STUB_DATA_PAGES));
}

static irqreturn_t mm_sigchld_irq(int irq, void* dev)
{
	struct mm_context *mm_context;
	pid_t pid;
	int status;

	guard(spinlock)(&mm_list_lock);

	while ((pid = os_reap_child(&status)) > 0) {
		/*
		* A child died, check if we have an MM with the PID. This is
		* only relevant in SECCOMP mode (as ptrace will fail anyway).
		*
		* See wait_stub_done_seccomp for more details.
		*/
		list_for_each_entry(mm_context, &mm_list, list) {
			struct mm_id *id = &mm_context->id;
			if (id->pid != pid) {
				for (int cpu = 0; cpu < NR_CPUS; cpu++) {
					if (mm_context->cpu_ids[cpu].pid == pid) {
						id = &mm_context->cpu_ids[cpu];
						break;
					}
				}
			}
			if (mm_context->gpu_id.pid == pid)
				id = &mm_context->gpu_id;
			if (id->pid == pid) {
				struct stub_data *stub_data;
				printk("Unexpectedly lost MM child! pid=%d host_status=%#x broker=%d err=%ld. Affected tasks will segfault.\n",
				       pid, status, id == &mm_context->id,
				       ((struct stub_data *)id->stack)->err);

				/* Marks the MM as dead */
				id->pid = -1;

				/* 0017: an asynchronous GPU waiter sleeps in
				 * guest context, not on this futex; wake it
				 * with an explicit failure. */
				if (id == &mm_context->gpu_id)
					complete_all(&mm_context->gpu_done);

				stub_data = (void *)id->stack;
				stub_futex_handoff(&stub_data->futex, FUTEX_IN_KERN);
#if IS_ENABLED(CONFIG_SMP)
				os_futex_wake(&stub_data->futex);
#endif

				/*
				 * NOTE: Currently executing syscalls by
				 * affected tasks may finish normally.
				 */
				break;
			}
		}
	}

	return IRQ_HANDLED;
}

static int __init init_child_tracking(void)
{
	int err;

	spin_lock_init(&mm_list_lock);
	INIT_LIST_HEAD(&mm_list);

	err = request_irq(SIGCHLD_IRQ, mm_sigchld_irq, 0, "SIGCHLD", NULL);
	if (err < 0)
		panic("Failed to register SIGCHLD IRQ: %d", err);

	return 0;
}
early_initcall(init_child_tracking)
