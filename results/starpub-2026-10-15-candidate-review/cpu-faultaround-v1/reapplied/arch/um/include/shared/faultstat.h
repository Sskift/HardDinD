/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Minimal UML fault/rendezvous counters (optional, default off).
 *
 * This header is shared between proper kernel code and os-Linux user-space
 * style code, so it deliberately contains no kernel includes: GCC atomics
 * work in both compilation environments.
 *
 * When disabled every helper is a single predicted-not-taken branch and
 * no counter storage is touched.  All counters are per-machine, bounded
 * unsigned 64-bit values; attribute multi-CPU increments only as
 * approximate.
 */
#ifndef __UM_SHARED_FAULTSTAT_H
#define __UM_SHARED_FAULTSTAT_H

extern volatile int uml_faultstat_enabled;

/* Userspace SIGSEGV rendezvous entered (seccomp userspace() loop). */
extern volatile unsigned long long uml_fs_segv;
/* Successful user write faults resolved by handle_page_fault(). */
extern volatile unsigned long long uml_fs_write_fault;
/* Faultaround windows evaluated and neighbour pages requested. */
extern volatile unsigned long long uml_fs_window;
extern volatile unsigned long long uml_fs_window_page;
/* Windows aborted by VMA/alignment/OOM gating. */
extern volatile unsigned long long uml_fs_window_skip;
/* TLB sync walks with dirty ranges; mmap/unmap records generated. */
extern volatile unsigned long long uml_fs_sync;
extern volatile unsigned long long uml_fs_map;
extern volatile unsigned long long uml_fs_unmap;
/* Broker stub batches flushed and cumulative ns spent flushing them. */
extern volatile unsigned long long uml_fs_flush;
extern volatile unsigned long long uml_fs_flush_ns;
/* Runner resume/trap waits and cumulative ns spent inside them. */
extern volatile unsigned long long uml_fs_resume;
extern volatile unsigned long long uml_fs_resume_ns;

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

#endif
