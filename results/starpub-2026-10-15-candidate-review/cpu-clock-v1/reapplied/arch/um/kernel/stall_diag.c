// SPDX-License-Identifier: GPL-2.0
/*
 * HARDDIND round12 stall diagnostic - DEFAULT OFF.
 *
 * Enable with the UML boot parameter harddind_stall_diag=<milliseconds>.
 * When disabled, callers perform one integer comparison only and this file
 * emits nothing.
 *
 * Records (one single printk() per record, so seq/clock/payload cannot be
 * interleaved or reassembled on SMP):
 *   - rendezvous: a seccomp runner futex handoff longer than the threshold;
 *   - cfs_throttle / cfs_unthrottle for a cfs_rq.
 *
 * Rate limiting uses the standard kernel ratelimit_state / ___ratelimit(),
 * which is IRQ/nested safe (raw_spinlock with an atomic trylock fast path)
 * and therefore safe from the runqueue-lock and nested-timer-signal contexts
 * these hooks run in; one independent state per kind. Console ordering of
 * records across CPUs is NOT guaranteed to match seq order.
 *
 * Suppression semantics are intentionally limited: a ratelimit bounds
 * console volume only. It does NOT keep throttle/unthrottle pairs together,
 * and a printed record (or seq gap) cannot prove an event did NOT occur,
 * including trailing events after the last printed record. Every event
 * increments ONE global seq whether printed or not; analysis must treat a
 * missing counterpart as inconclusive.
 *
 * Each record carries three explicit clock domains: host_ns (host
 * CLOCK_MONOTONIC via os_nsecs), guest_sched_ns (the scheduler clock under
 * test) and jiffies. The console prefix time is never used for alignment.
 */
#include <linux/atomic.h>
#include <linux/jiffies.h>
#include <linux/math64.h>
#include <linux/printk.h>
#include <linux/ratelimit.h>
#include <linux/sched/clock.h>
#include <os.h>

/* Exactly one definition; the value is written by the __uml_setup parser in
 * os-Linux/start_up.c before start_kernel() reaches time_init(). */
unsigned int harddind_stall_diag_ms;

static atomic64_t harddind_diag_seq = ATOMIC64_INIT(0);

/* 5 s window / 200 records, per kind. Standard kernel implementation => no
 * custom shared-counter data race. */
static DEFINE_RATELIMIT_STATE(harddind_rs_cfs, 5 * HZ, 200);
static DEFINE_RATELIMIT_STATE(harddind_rs_rv, 5 * HZ, 200);

void harddind_stall_rendezvous(unsigned long runner_pid, long long elapsed_ns,
			       unsigned int loops, int last_futex_err)
{
	u64 seq;

	if (!harddind_stall_diag_ms ||
	    elapsed_ns < (long long)harddind_stall_diag_ms * NSEC_PER_MSEC)
		return;
	seq = (u64)atomic64_inc_return(&harddind_diag_seq);
	if (!__ratelimit(&harddind_rs_rv))
		return;
	/* Single printk = one atomic console record.
	 * last_futex_err semantics: negative errno of the LAST failed futex
	 * syscall observed anywhere inside this rendezvous (including earlier
	 * iterations); 0 means no futex call returned an error during the
	 * whole wait. A successful final wake after an earlier EINTR/EAGAIN
	 * still reports the last negative value, so it must not be read as
	 * "the final wake failed". */
	printk(KERN_WARNING
	       "HARDDIND_STALL seq=%llu host_ns=%lld guest_sched_ns=%llu "
	       "jiffies=%lu kind=rendezvous runner_pid=%lu elapsed_ms=%lld "
	       "loops=%u last_futex_err=%d\n",
	       (unsigned long long)seq, (long long)os_nsecs(),
	       (unsigned long long)sched_clock(), jiffies,
	       runner_pid, (long long)div_s64(elapsed_ns, NSEC_PER_MSEC),
	       loops, last_futex_err);
}

void harddind_stall_cfs(int throttled, const void *cfs_rq_p, int cpu,
			long long remaining_ns, unsigned long long period_ns,
			int nr_periods, unsigned long long throttled_self_ns)
{
	u64 seq;

	if (!harddind_stall_diag_ms)
		return;
	seq = (u64)atomic64_inc_return(&harddind_diag_seq);
	if (!__ratelimit(&harddind_rs_cfs))
		return;
	if (throttled)
		printk(KERN_WARNING
		       "HARDDIND_STALL seq=%llu host_ns=%lld guest_sched_ns=%llu "
		       "jiffies=%lu kind=cfs_throttle cfs_rq=%p cpu=%d "
		       "runtime_remaining_ns=%lld period_us=%llu nr_periods=%d\n",
		       (unsigned long long)seq, (long long)os_nsecs(),
		       (unsigned long long)sched_clock(), jiffies,
		       cfs_rq_p, cpu, remaining_ns,
		       (unsigned long long)div_u64(period_ns, NSEC_PER_USEC),
		       nr_periods);
	else
		printk(KERN_WARNING
		       "HARDDIND_STALL seq=%llu host_ns=%lld guest_sched_ns=%llu "
		       "jiffies=%lu kind=cfs_unthrottle cfs_rq=%p cpu=%d "
		       "throttled_self_ms=%llu period_us=%llu\n",
		       (unsigned long long)seq, (long long)os_nsecs(),
		       (unsigned long long)sched_clock(), jiffies,
		       cfs_rq_p, cpu,
		       (unsigned long long)div_u64(throttled_self_ns,
						     NSEC_PER_MSEC),
		       (unsigned long long)div_u64(period_ns, NSEC_PER_USEC));
}
