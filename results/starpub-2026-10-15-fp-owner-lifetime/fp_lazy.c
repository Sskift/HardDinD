// SPDX-License-Identifier: GPL-2.0
/*
 * Lazy XSTATE handoff for the UML SECCOMP syscall path (experiment 0018).
 *
 * In SECCOMP mode every guest syscall traps through a host SIGSYS. The host
 * saves the guest XSTATE into the parked signal frame on the runner's stub
 * stack and reloads it on rt_sigreturn. Without laziness the UML kernel also
 * memcpys the whole image out of the frame into current->...regs.fp on entry
 * and back into the same frame on exit: roughly two full-XSTATE copies per
 * guest syscall, even though the overwhelming majority of syscalls never
 * touch FP state.
 *
 * This file keeps the parked frame authoritative and moves only ownership
 * bookkeeping. Unlike the earlier salvage-list prototype there is no heap
 * allocation on any path: the task descriptor is embedded in
 * struct uml_pt_regs and the per-runner handoff record in struct mm_id.
 *
 * States (descriptor in struct uml_pt_regs):
 *
 *   UML_FP_EAGER (0): regs->fp is authoritative (historical behaviour; the
 *                     state at boot and after materialise/discard/fork/exit).
 *   UML_FP_LIVE:     the authoritative bytes sit in the parked frame
 *                     fp_frame on runner fp_home; fp[] is stale.
 *
 *   entry  uml_fp_enter(): host just wrote frame F on runner R -> arm LIVE
 *   exit   uml_fp_leave(): same task still owns the same frame -> skip the
 *                          write-back so rt_sigreturn reloads it verbatim
 *                          (fast path); otherwise collect the task's own
 *                          bytes (cross-CPU migration), push the bytes of
 *                          the frame's previous owner into that owner's own
 *                          regs->fp buffer, then write the frame.
 *   reader uml_fp_materialize(): signal delivery, FP/XSTATE regsets
 *                          (ptrace and coredumps), fork -> frame into fp[]
 *   writer uml_fp_discard(): sigreturn/exec overwrite fp[] wholesale ->
 *                          drop ownership without copying
 *   exit   uml_fp_exit(): dying task detaches using ONLY its task lock, so
 *                          it never dereferences its mm after exit_mm(); the
 *                          runner-side record is cleared by the next frame
 *                          handoff or by uml_fp_handoff_reset() at mm death.
 *
 * Locking: one handoff lock per runner and one lock per task. The handoff
 * lock is always taken first; uml_fp_exit() takes only the task lock. The
 * words are plain integers in headers shared with userspace-compiled units;
 * all operations on them live in this kernel-side file.
 *
 * Default off (lazyxstate=0): behaviour is the old eager copy. i386 always
 * uses the eager path because it additionally converts the legacy i387
 * image on every transition.
 */

#include <linux/atomic.h>
#include <linux/build_bug.h>
#include <linux/irqflags.h>
#include <linux/kernel.h>
#include <linux/panic.h>
#include <linux/processor.h>
#include <linux/string.h>
#include <registers.h>
#include <skas/mm_id.h>
#include <init.h>

unsigned int uml_lazy_xstate;

static_assert(sizeof(struct um_fp_handoff) == 24);

#ifdef __x86_64__

/* ---- tiny IRQ-safe test-and-set locks over the embedded lock words ---- */

static __always_inline void handoff_lock(struct um_fp_handoff *handoff,
					 unsigned long *flags)
{
	local_irq_save(*flags);
	if (unlikely(cmpxchg(&handoff->lock, 0, 1) != 0)) {
		do {
			cpu_relax();
		} while (READ_ONCE(handoff->lock) ||
			 cmpxchg(&handoff->lock, 0, 1) != 0);
	}
}

static __always_inline void handoff_unlock(struct um_fp_handoff *handoff,
					   unsigned long flags)
{
	smp_store_release(&handoff->lock, 0);
	local_irq_restore(flags);
}

static __always_inline void desc_lock(struct uml_pt_regs *regs)
{
	if (unlikely(cmpxchg(&regs->fp_lock, 0, 1) != 0)) {
		do {
			cpu_relax();
		} while (READ_ONCE(regs->fp_lock) ||
			 cmpxchg(&regs->fp_lock, 0, 1) != 0);
	}
}

static __always_inline void desc_unlock(struct uml_pt_regs *regs)
{
	smp_store_release(&regs->fp_lock, 0);
}

/* ---- entry / exit ------------------------------------------------------- */

int uml_fp_enter(struct uml_pt_regs *regs, struct mm_id *runner,
		 void *frame, unsigned int size)
{
	struct um_fp_handoff *handoff = &runner->fp_handoff;
	unsigned long flags;

	if (!uml_lazy_xstate)
		goto eager;

	handoff_lock(handoff, &flags);
	if (unlikely(handoff->owner != NULL)) {
		/*
		 * Every frame write (uml_fp_leave) clears ownership, so an
		 * armed owner here means a missing set/get round: refuse
		 * rather than silently overwrite someone's image.
		 */
		handoff_unlock(handoff, flags);
		return -EINVAL;
	}

	regs->fp_token = ++handoff->token;
	handoff->owner = regs;
	regs->fp_home = runner;
	regs->fp_frame = frame;
	regs->fp_size = size;
	smp_store_release(&regs->fp_state, UML_FP_LIVE);
	handoff_unlock(handoff, flags);
	return 1;

eager:
	uml_fp_reset(regs);
	memcpy(regs->fp, frame, size);
	return 0;
}

/*
 * Resolve this task's own image before frame G on runner is written.
 * Returns 1 when the frame must be left untouched (fast path), 0 when
 * regs->fp is ready to copy, -EINVAL on unrecoverable protocol corruption.
 *
 * Every decision is made with the descriptor lock plus, when a home exists,
 * the home handoff lock. Taking the lockless fp_home hint at face value
 * would race a concurrent remote relinquish (it clears fp_home before
 * flipping state to EAGER) and could observe a torn pair.
 */
static int fp_resolve_self(struct uml_pt_regs *regs, struct mm_id *runner,
			   void *frame)
{
	struct um_fp_handoff *handoff;
	struct mm_id *home;
	unsigned long flags;

	home = READ_ONCE(regs->fp_home);
	if (home) {
		handoff = &home->fp_handoff;
		handoff_lock(handoff, &flags);
		desc_lock(regs);
		if (regs->fp_home) {
			if (smp_load_acquire(&regs->fp_state) != UML_FP_LIVE) {
				/*
				 * A remote runner already pushed the image
				 * into regs->fp; the release of this lock
				 * pairs with that copy.
				 */
				desc_unlock(regs);
				handoff_unlock(handoff, flags);
				return 0;
			}
			if (WARN_ONCE(handoff->owner != regs ||
				      regs->fp_token != handoff->token,
				      "lazy xstate: live state without frame owner\n")) {
				desc_unlock(regs);
				handoff_unlock(handoff, flags);
				return -EINVAL;
			}
			if (home == runner && regs->fp_frame == frame) {
				/* Fast path: host reloads its own saved frame. */
				handoff->owner = NULL;
				regs->fp_home = NULL;
				smp_store_release(&regs->fp_state, UML_FP_EAGER);
				desc_unlock(regs);
				handoff_unlock(handoff, flags);
				return 1;
			}
			/* Cross-CPU migration or a moved nested frame slot. */
			memcpy(regs->fp, regs->fp_frame, regs->fp_size);
			handoff->owner = NULL;
			regs->fp_home = NULL;
			smp_store_release(&regs->fp_state, UML_FP_EAGER);
		}
		desc_unlock(regs);
		handoff_unlock(handoff, flags);
		return 0;
	}

	/*
	 * No home. Remote relinquishes only clear fp_home, never re-arm it,
	 * so state cannot flip to LIVE here.
	 */
	desc_lock(regs);
	WRITE_ONCE(regs->fp_home, NULL);
	if (WARN_ON_ONCE(smp_load_acquire(&regs->fp_state) == UML_FP_LIVE,
			 "lazy xstate: live state without a home runner\n")) {
		desc_unlock(regs);
		return -EINVAL;
	}
	desc_unlock(regs);
	return 0;
}

int uml_fp_leave(struct uml_pt_regs *regs, struct mm_id *runner,
		 void *frame, unsigned int size)
{
	struct um_fp_handoff *handoff;
	struct uml_pt_regs *owner;
	unsigned long flags;
	int lazy;

	if (!uml_lazy_xstate)
		return 0;

	lazy = fp_resolve_self(regs, runner, frame);
	if (lazy < 0)
		return lazy;
	if (lazy == 1)
		return 1;

	/*
	 * The frame may still hold the image of a previous owner: preserve
	 * it in that owner's own fp[] buffer before we overwrite it. Owner
	 * pointers are identity only; the per-task lock serialises a
	 * concurrent uml_fp_exit()/materialise.
	 */
	handoff = &runner->fp_handoff;
	handoff_lock(handoff, &flags);
	owner = handoff->owner;
	if (owner == regs) {
		/* Own live frame on this runner but a different frame slot
		 * (nested signal); the image was collected above. */
		handoff->owner = NULL;
	} else if (owner) {
		desc_lock(owner);
		if (smp_load_acquire(&owner->fp_state) == UML_FP_LIVE &&
		    owner->fp_home == runner &&
		    owner->fp_token == handoff->token &&
		    owner->fp_frame == frame) {
			memcpy(owner->fp, frame, owner->fp_size);
			owner->fp_home = NULL;
			smp_store_release(&owner->fp_state, UML_FP_EAGER);
			handoff->owner = NULL;
		} else {
			/* Token/identity mismatch (stale record): drop it. */
			handoff->owner = NULL;
		}
		desc_unlock(owner);
	}

	memcpy(frame, regs->fp, size);
	handoff_unlock(handoff, flags);
	return 0;
}

/* ---- genuine FP consumers ---------------------------------------------- */

void uml_fp_materialize(struct uml_pt_regs *regs)
{
	struct um_fp_handoff *handoff;
	struct mm_id *home;
	unsigned long flags;

	if (smp_load_acquire(&regs->fp_state) != UML_FP_LIVE)
		return;
	home = READ_ONCE(regs->fp_home);
	if (unlikely(!home)) {
		smp_store_release(&regs->fp_state, UML_FP_EAGER);
		return;
	}

	handoff = &home->fp_handoff;
	handoff_lock(handoff, &flags);
	desc_lock(regs);
	if (smp_load_acquire(&regs->fp_state) == UML_FP_LIVE) {
		if (WARN_ONCE(handoff->owner != regs ||
			      regs->fp_token != handoff->token,
			      "lazy xstate: live state without frame owner\n")) {
			/* Impossible by construction; keep fp[] authoritative. */
		} else {
			memcpy(regs->fp, regs->fp_frame, regs->fp_size);
			handoff->owner = NULL;
		}
		regs->fp_home = NULL;
		smp_store_release(&regs->fp_state, UML_FP_EAGER);
	}
	desc_unlock(regs);
	handoff_unlock(handoff, flags);
}

void uml_fp_discard(struct uml_pt_regs *regs)
{
	struct um_fp_handoff *handoff;
	struct mm_id *home;
	unsigned long flags;

	if (smp_load_acquire(&regs->fp_state) != UML_FP_LIVE)
		return;
	home = READ_ONCE(regs->fp_home);
	if (unlikely(!home)) {
		smp_store_release(&regs->fp_state, UML_FP_EAGER);
		return;
	}

	handoff = &home->fp_handoff;
	handoff_lock(handoff, &flags);
	desc_lock(regs);
	if (smp_load_acquire(&regs->fp_state) == UML_FP_LIVE) {
		if (handoff->owner == regs &&
		    regs->fp_token == handoff->token)
			handoff->owner = NULL;
		/* The caller is about to overwrite fp[] wholesale. */
		regs->fp_home = NULL;
		smp_store_release(&regs->fp_state, UML_FP_EAGER);
	}
	desc_unlock(regs);
	handoff_unlock(handoff, flags);
}

/* ---- lifetime ----------------------------------------------------------- */

void uml_fp_reset(struct uml_pt_regs *regs)
{
	regs->fp_home = NULL;
	regs->fp_frame = NULL;
	regs->fp_size = 0;
	regs->fp_token = 0;
	WRITE_ONCE(regs->fp_lock, 0);
	smp_store_release(&regs->fp_state, UML_FP_EAGER);
}

void uml_fp_exit(struct uml_pt_regs *regs)
{
	/*
	 * No mm dereference: exit_mm() may already have destroyed the home
	 * runner. A concurrent handoff either finishes first (its copy lands
	 * in this still allocated task struct) or observes EAGER and merely
	 * clears the runner-side record.
	 */
	desc_lock(regs);
	WRITE_ONCE(regs->fp_home, NULL);
	smp_store_release(&regs->fp_state, UML_FP_EAGER);
	desc_unlock(regs);
}

void uml_fp_handoff_reset(struct mm_id *runner)
{
	/* No guest task can reference a runner being destroyed or recycled. */
	WRITE_ONCE(runner->fp_handoff.owner, NULL);
	WRITE_ONCE(runner->fp_handoff.token, 0);
	WRITE_ONCE(runner->fp_handoff.lock, 0);
}

static int __init uml_lazy_xstate_setup(char *line, int *add)
{
	*add = 0;
	if (!strcmp(line, "0")) {
		uml_lazy_xstate = 0;
		return 0;
	}
	if (!strcmp(line, "1")) {
		uml_lazy_xstate = 1;
		return 0;
	}
	panic("Invalid lazyxstate value, expected 0 or 1\n");
}
__uml_setup("lazyxstate=", uml_lazy_xstate_setup,
"lazyxstate=<0|1>\n"
"    Keep guest XSTATE in the parked SECCOMP signal frame until a real\n"
"    consumer (guest signal, ptrace/coredump, fork, exec) needs it.\n"
"    Experimental, default 0, x86_64 only. Validate with the FP lifecycle\n"
"    regression probes and the parallel-runner/GPU kernels.\n"
);

#else /* !__x86_64__ */

/*
 * i386 converts the legacy i387 image on every transition; keep the eager
 * copies and provide no-op bookkeeping.
 */
int uml_fp_enter(struct uml_pt_regs *regs, struct mm_id *runner,
		 void *frame, unsigned int size)
{
	uml_fp_reset(regs);
	memcpy(regs->fp, frame, size);
	return 0;
}

int uml_fp_leave(struct uml_pt_regs *regs, struct mm_id *runner,
		 void *frame, unsigned int size)
{
	return 0;
}

void uml_fp_materialize(struct uml_pt_regs *regs)
{
}

void uml_fp_discard(struct uml_pt_regs *regs)
{
}

void uml_fp_reset(struct uml_pt_regs *regs)
{
	regs->fp_home = NULL;
	regs->fp_frame = NULL;
	regs->fp_size = 0;
	regs->fp_token = 0;
	WRITE_ONCE(regs->fp_lock, 0);
	smp_store_release(&regs->fp_state, UML_FP_EAGER);
}

void uml_fp_exit(struct uml_pt_regs *regs)
{
	uml_fp_reset(regs);
}

void uml_fp_handoff_reset(struct mm_id *runner)
{
	runner->fp_handoff.owner = NULL;
	runner->fp_handoff.token = 0;
	runner->fp_handoff.lock = 0;
}

#endif
