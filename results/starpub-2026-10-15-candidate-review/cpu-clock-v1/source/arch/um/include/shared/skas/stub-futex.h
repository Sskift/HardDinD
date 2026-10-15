/* SPDX-License-Identifier: GPL-2.0 */
#ifndef __UM_STUB_FUTEX_H
#define __UM_STUB_FUTEX_H

#define FUTEX_IN_CHILD 0U
#define FUTEX_IN_KERN 1U
#define STUB_FUTEX_WAITING 2U

/* One owner and at most one waiter. The phase also publishes shared data. */
static inline unsigned int stub_futex_phase(unsigned int *word)
{
	return __atomic_load_n(word, __ATOMIC_ACQUIRE) & ~STUB_FUTEX_WAITING;
}

/* Return true only when the peer armed its sleep before this handoff. */
static inline int stub_futex_handoff(unsigned int *word, unsigned int phase)
{
	return __atomic_exchange_n(word, phase, __ATOMIC_ACQ_REL) & STUB_FUTEX_WAITING;
}

/*
 * Register before sleeping. A concurrent handoff either observes WAITING,
 * or wins this compare-exchange, in which case the caller must not sleep.
 * FUTEX_WAIT still compares the complete word to close the final race.
 */
static inline int stub_futex_prepare_wait(unsigned int *word, unsigned int phase)
{
	unsigned int expected = phase;

	if (__atomic_compare_exchange_n(word, &expected,
				phase | STUB_FUTEX_WAITING, 0,
				__ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
		return 1;
	return expected == (phase | STUB_FUTEX_WAITING);
}

#endif
