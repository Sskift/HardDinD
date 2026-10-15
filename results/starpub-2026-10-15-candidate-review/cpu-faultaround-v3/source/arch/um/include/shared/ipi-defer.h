/* SPDX-License-Identifier: GPL-2.0 */
#ifndef __UM_IPI_DEFER_H
#define __UM_IPI_DEFER_H

/* Pending vectors and the drain owner share one word. Releasing the owner
 * succeeds only with no pending vectors, closing the final-enable race. */
#define UML_IPI_DRAIN_ACTIVE (1U << 31)
struct uml_ipi_defer { unsigned int state; };

static inline void uml_ipi_defer_add(struct uml_ipi_defer *d, unsigned int vector)
{
	__atomic_fetch_or(&d->state, 1U << vector, __ATOMIC_RELEASE);
}

static inline int uml_ipi_defer_pending(struct uml_ipi_defer *d)
{
	return __atomic_load_n(&d->state, __ATOMIC_ACQUIRE) & ~UML_IPI_DRAIN_ACTIVE;
}

static inline int uml_ipi_defer_begin(struct uml_ipi_defer *d)
{
	return !(__atomic_fetch_or(&d->state, UML_IPI_DRAIN_ACTIVE,
				  __ATOMIC_ACQ_REL) & UML_IPI_DRAIN_ACTIVE);
}

static inline unsigned int uml_ipi_defer_take(struct uml_ipi_defer *d)
{
	return __atomic_fetch_and(&d->state, UML_IPI_DRAIN_ACTIVE,
				 __ATOMIC_ACQ_REL) & ~UML_IPI_DRAIN_ACTIVE;
}

static inline int uml_ipi_defer_end(struct uml_ipi_defer *d)
{
	unsigned int expected = UML_IPI_DRAIN_ACTIVE;
	return __atomic_compare_exchange_n(&d->state, &expected, 0, 0,
					  __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
}
#endif
