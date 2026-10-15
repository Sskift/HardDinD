// SPDX-License-Identifier: GPL-2.0
/*
 * Copyright (C) 2025 Ant Group
 * Author: Tiwei Bie <tiwei.btw@antgroup.com>
 */

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <string.h>
#include <kern_util.h>
#include <um_malloc.h>
#include <init.h>
#include <os.h>
#include <smp.h>
#include <ipi-defer.h>
#include "internal.h"

struct cpu_thread_data {
	int cpu;
	sigset_t sigset;
};

static __thread int __curr_cpu;

int uml_curr_cpu(void)
{
	return __curr_cpu;
}

static pthread_t cpu_threads[CONFIG_NR_CPUS];

static int defer_ipi;
static __thread int ipi_host_unblocked;
static __thread struct uml_ipi_defer pending_ipis;
static struct {
	unsigned long received, deferred, handled;
} ipi_stats[CONFIG_NR_CPUS];

static int __init setup_defer_ipi(char *str, int *add)
{
	if (strcmp(str, "0") && strcmp(str, "1")) {
		os_info("defer_ipi must be 0 or 1\n");
		return -1;
	}
	defer_ipi = str[0] == '1';
	*add = 0;
	return 0;
}

__uml_setup("defer_ipi=", setup_defer_ipi,
"defer_ipi=<0|1>\n"
"    Defer IPI work in software while guest interrupts are disabled.\n");

static void report_deferred_ipis(void)
{
	unsigned long received = 0, deferred = 0, handled = 0;
	if (!defer_ipi)
		return;
	for (int i = 0; i < CONFIG_NR_CPUS; i++) {
		received += __atomic_load_n(&ipi_stats[i].received, __ATOMIC_RELAXED);
		deferred += __atomic_load_n(&ipi_stats[i].deferred, __ATOMIC_RELAXED);
		handled += __atomic_load_n(&ipi_stats[i].handled, __ATOMIC_RELAXED);
	}
	os_info("HARDDIND_DEFER_IPI enabled=1 received=%lu deferred=%lu handled=%lu\n",
		received, deferred, handled);
}
__uml_exitcall(report_deferred_ipis);

static void deliver_pending_ipis(void)
{
	if (!signals_enabled || !uml_ipi_defer_pending(&pending_ipis) ||
	    !uml_ipi_defer_begin(&pending_ipis))
		return;
	/* Nested signal handlers only enqueue. CALL vectors drain their Linux
	 * per-CPU work queues, and RESCHEDULE/STOP are level-triggered work;
	 * repeated notifications for a vector may therefore be coalesced. */
	do {
		unsigned int bits;
		signals_enabled = 0;
		asm volatile("" ::: "memory");
		um_trace_signals_off();
		bits = uml_ipi_defer_take(&pending_ipis);
		for (unsigned int vector = 0; bits; vector++, bits >>= 1) {
			if (!(bits & 1))
				continue;
			__atomic_add_fetch(&ipi_stats[__curr_cpu].handled, 1,
					   __ATOMIC_RELAXED);
			uml_ipi_handler(vector);
		}
		um_trace_signals_on();
		signals_enabled = 1;
		asm volatile("" ::: "memory");
		/* An IPI arriving before this CAS keeps ownership here. One
		 * arriving afterwards acquires ownership in its signal handler. */
	} while (!uml_ipi_defer_end(&pending_ipis));
}

static void *cpu_thread(void *arg)
{
	struct cpu_thread_data *data = arg;

	__curr_cpu = data->cpu;

	uml_start_secondary(data);

	return NULL;
}

int os_start_cpu_thread(int cpu)
{
	struct cpu_thread_data *data;
	sigset_t sigset, oset;
	int err;

	data = uml_kmalloc(sizeof(*data), UM_GFP_ATOMIC);
	if (!data)
		return -ENOMEM;

	sigfillset(&sigset);
	if (sigprocmask(SIG_SETMASK, &sigset, &oset) < 0) {
		err = errno;
		goto err;
	}

	data->cpu = cpu;
	data->sigset = oset;

	err = pthread_create(&cpu_threads[cpu], NULL, cpu_thread, data);
	if (sigprocmask(SIG_SETMASK, &oset, NULL) < 0)
		panic("Failed to restore the signal mask, errno = %d", errno);
	if (err != 0)
		goto err;

	return 0;

err:
	kfree(data);
	return -err;
}

void os_start_secondary(void *arg, jmp_buf *switch_buf)
{
	struct cpu_thread_data *data = arg;

	sigaddset(&data->sigset, IPI_SIGNAL);
	sigaddset(&data->sigset, SIGIO);

	if (sigprocmask(SIG_SETMASK, &data->sigset, NULL) < 0)
		panic("Failed to restore the signal mask, errno = %d", errno);

	kfree(data);
	longjmp(*switch_buf, 1);

	/* unreachable */
	printk(UM_KERN_ERR "impossible long jump!");
	fatal_sigsegv();
}

int os_send_ipi(int cpu, int vector)
{
	union sigval value = { .sival_int = vector };

	return pthread_sigqueue(cpu_threads[cpu], IPI_SIGNAL, value);
}

static void __local_ipi_set(int enable)
{
	sigset_t sigset;

	sigemptyset(&sigset);
	sigaddset(&sigset, IPI_SIGNAL);

	if (sigprocmask(enable ? SIG_UNBLOCK : SIG_BLOCK, &sigset, NULL) < 0)
		panic("%s: sigprocmask failed, errno = %d", __func__, errno);
}

void os_local_ipi_enable(void)
{
	if (defer_ipi) {
		/* Secondary CPU threads start with host signals blocked. Open
		 * the host IPI signal once; later IRQ masking is software only. */
		if (!ipi_host_unblocked) {
			ipi_host_unblocked = 1;
			__local_ipi_set(1);
		}
		deliver_pending_ipis();
		return;
	}
	__local_ipi_set(1);
}

int os_local_ipi_pending(void)
{
	return defer_ipi && uml_ipi_defer_pending(&pending_ipis);
}

void os_local_ipi_disable(void)
{
	if (defer_ipi)
		return;
	__local_ipi_set(0);
}

static void ipi_sig_handler(int sig, siginfo_t *si, void *uc)
{
	int save_errno = errno;

	if (defer_ipi) {
		int vector = si->si_value.sival_int;
		if (vector < 0 || vector >= 31)
			panic("Invalid deferred IPI vector %d", vector);
		__atomic_add_fetch(&ipi_stats[__curr_cpu].received, 1,
				   __ATOMIC_RELAXED);
		if (!signals_enabled)
			__atomic_add_fetch(&ipi_stats[__curr_cpu].deferred, 1,
					   __ATOMIC_RELAXED);
		uml_ipi_defer_add(&pending_ipis, vector);
		deliver_pending_ipis();
		errno = save_errno;
		return;
	}

	signals_enabled = 0;
	um_trace_signals_off();

	uml_ipi_handler(si->si_value.sival_int);

	um_trace_signals_on();
	signals_enabled = 1;

	errno = save_errno;
}

void __init os_init_smp(void)
{
	struct sigaction action = {
		.sa_sigaction = ipi_sig_handler,
		.sa_flags = SA_SIGINFO | SA_ONSTACK | SA_RESTART,
	};

	sigfillset(&action.sa_mask);

	if (sigaction(IPI_SIGNAL, &action, NULL) < 0)
		panic("%s: sigaction failed, errno = %d", __func__, errno);

	cpu_threads[0] = pthread_self();
}
