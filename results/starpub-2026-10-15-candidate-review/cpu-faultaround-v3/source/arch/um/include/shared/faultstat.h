/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Minimal UML fault/rendezvous counters (optional, default off).
 *
 * This header is shared between proper kernel code and os-Linux
 * user-space style code, so it deliberately contains no kernel includes:
 * GCC atomics work in both compilation environments.
 *
 * Overhead contract (verified by tests/faultstat_unit.c and the static
 * contract check in tests/run_faultstat_checks.py):
 *
 *  - When uml_faultstat_enabled is 0, uml_fs_inc/uml_fs_add are a single
 *    predicted-not-taken branch and NO atomic instruction is emitted;
 *    uml_fs_time_begin/end do not read any clock at all (the gate runs
 *    before the clock call).
 *  - When enabled, all timestamps come from ONE source,
 *    uml_fs_clock_ns() (host CLOCK_MONOTONIC ns via os_nsecs()), in
 *    both kernel and host compilation contexts.  Local jiffies clocks
 *    are deliberately not used.
 *  - Counter helpers return void and have no errno.  The map/unmap
 *    wrappers in tlb.c return the underlying operation's result byte for
 *    byte, so instrumentation can never change a driver/syscall result.
 *
 * Counters are per-machine and approximate under multi-CPU updates.
 */
#ifndef __UM_SHARED_FAULTSTAT_H
#define __UM_SHARED_FAULTSTAT_H

/*
 * No includes on purpose: this header is compiled both by the kernel
 * (-nostdinc) and by os-Linux user objects.  Only built-in integer
 * types are used, which need no header in either environment.
 */

extern volatile int uml_faultstat_enabled;

/* Userspace reports handled (per userspace() round, any signal). */
extern volatile unsigned long long uml_fs_trap;
/* SIGSEGV reports specifically. */
extern volatile unsigned long long uml_fs_segv;
/* Successful user write faults resolved by handle_page_fault(). */
extern volatile unsigned long long uml_fs_write_fault;
/* Faultaround windows entered/skipped and neighbour faults issued. */
extern volatile unsigned long long uml_fs_window;
extern volatile unsigned long long uml_fs_window_page;
extern volatile unsigned long long uml_fs_window_skip;
/* TLB sync walks with dirty ranges; mmap/unmap records generated. */
extern volatile unsigned long long uml_fs_sync;
extern volatile unsigned long long uml_fs_map;
extern volatile unsigned long long uml_fs_unmap;
/* Broker stub batches completed and ns spent inside the flush call. */
extern volatile unsigned long long uml_fs_flush;
extern volatile unsigned long long uml_fs_flush_ns;
/*
 * userspace() rounds and ns spent in them.  This interval STARTS when
 * the vCPU hands the runner off (resume) and ENDS when the runner traps
 * back.  It therefore INCLUDES guest user execution time up to the
 * trap; it is NOT pure fault-handling overhead.
 */
extern volatile unsigned long long uml_fs_run;
extern volatile unsigned long long uml_fs_run_ns;

/* One host CLOCK_MONOTONIC source for both compilation contexts. */
unsigned long long uml_fs_clock_ns(void);

static inline void uml_fs_inc(volatile unsigned long long *p)
{
	if (__builtin_expect(uml_faultstat_enabled, 0))
		__atomic_add_fetch(p, 1ULL, __ATOMIC_RELAXED);
}

static inline void uml_fs_add(volatile unsigned long long *p,
			      unsigned long long n)
{
	if (__builtin_expect(uml_faultstat_enabled, 0))
		__atomic_add_fetch(p, n, __ATOMIC_RELAXED);
}

/* Consistent (torn-free) snapshot for /proc readers. */
static inline unsigned long long uml_fs_read(
		const volatile unsigned long long *p)
{
	return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}

/*
 * Timing brackets.  The enabled check happens BEFORE any clock access;
 * a disabled probe returns 0 and uml_fs_time_end() is a no-op, so zero
 * clock syscalls and zero atomics occur on the default-off path.
 */
static inline unsigned long long uml_fs_time_begin(void)
{
	if (__builtin_expect(!uml_faultstat_enabled, 1))
		return 0;
	return uml_fs_clock_ns();
}

static inline void uml_fs_time_end(volatile unsigned long long *sum,
				   unsigned long long t0)
{
	unsigned long long now;

	if (__builtin_expect(!uml_faultstat_enabled, 1))
		return;
	now = uml_fs_clock_ns();
	/* Ignore non-monotonic samples instead of recording garbage. */
	if (now >= t0)
		__atomic_add_fetch(sum, now - t0, __ATOMIC_RELAXED);
}

#endif
