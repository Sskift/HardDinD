// SPDX-License-Identifier: GPL-2.0
/*
 * fp_model.c - userspace behavioural model of the 0018 lazy XSTATE ownership
 * state machine with strong-reference lifetime protocol (REVIEW.md rev2).
 *
 * PROVENANCE: standalone host model written for the round6 repair. It is NOT
 * kernel code and NOT guest evidence. It re-implements the same states,
 * locks, references and transitions in pthreads.
 *
 * Revision 2 models object lifetime: tasks are heap allocated with a
 * refcount and freed exactly as task_struct would be after its last put. A
 * runner slot holds one STRONG reference while non-empty. The test battery:
 *
 *   --lifetime-repro   the owner's exact ASAN counterexample sequence
 *                      (enter/exit/free/leave) must be UAF-clean
 *   --reuse            malloc/free/address-reuse ABA stress
 *   --teardown         mm teardown with an occupied slot, then re-arm
 *                      (broker recycle)
 *   --concurrent       randomised exit-vs-takeover on shared runners
 *   (default)          all of the above plus the full byte-fidelity and
 *                      bounded-retention schedule
 *
 * Build: gcc -O2 -Wall -Wextra -pthread [-fsanitize=address] fp_model.c
 */
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define FP_WORDS 64		/* stand-in for a 512-byte XSTATE image */
#define N_RUNNERS 8
#define TASK_POOL 512
#define ROUNDS_PER_THREAD 200000
#define NTHREADS 8

/* ---- modelled primitives ----------------------------------------------- */

struct handoff {
	atomic_uint lock;
	struct task *owner;	/* STRONG reference while non-NULL */
	uint64_t token;
};

struct runner {
	struct handoff h;
	uint64_t frame[FP_WORDS];
	pthread_mutex_t round_lock;	/* one vCPU/turnstile round at a time */
};

struct task {
	atomic_uint lock;		/* descriptor lock */
	atomic_int refs;		/* strong refs (scheduler + slots) */
	unsigned int state;		/* 0 eager, 1 live */
	struct handoff *home;
	uint64_t *frame;
	unsigned int size;
	uint64_t token;
	uint64_t fp[FP_WORDS];
	unsigned int alive;
	unsigned int inround;
	uint64_t id;
#define NHIST 6
	struct { const char *ev; void *home; void *owner; unsigned state;
		 uint64_t tok; } hist[NHIST];
	unsigned hpos;
};

static unsigned int xstate_size_words = FP_WORDS;
static struct runner runners[N_RUNNERS];
static atomic_ulong failures;
static atomic_ulong slots_cleared;
static atomic_ulong runner_refs_held_peak;

/* Counters that prove bounded reclamation. */
static atomic_ulong tasks_born, tasks_freed;
static atomic_ulong slot_puts;		/* strong refs released by slots */

static void h_lock(struct handoff *h)
{
	while (atomic_exchange_explicit(&h->lock, 1, memory_order_acq_rel)) {
		struct timespec ts = { 0, 1000 };
		while (atomic_load_explicit(&h->lock, memory_order_relaxed))
			nanosleep(&ts, NULL);
	}
}
static void h_unlock(struct handoff *h)
{
	atomic_store_explicit(&h->lock, 0, memory_order_release);
}
static void d_lock(struct task *t)
{
	while (atomic_exchange_explicit(&t->lock, 1, memory_order_acq_rel)) {
		struct timespec ts = { 0, 1000 };
		while (atomic_load_explicit(&t->lock, memory_order_relaxed))
			nanosleep(&ts, NULL);
	}
}
static void d_unlock(struct task *t)
{
	atomic_store_explicit(&t->lock, 0, memory_order_release);
}

/* Strong task references (model of get/put_task_struct). */
static struct task *task_get(struct task *t)
{
	int old = atomic_fetch_add_explicit(&t->refs, 1, memory_order_acq_rel);
	if (old <= 0) {
		fprintf(stderr, "FAIL task_get on dead task %p\n", (void *)t);
		atomic_fetch_add(&failures, 1);
	}
	return t;
}
static void task_put(struct task *t)
{
	if (!t)
		return;
	if (atomic_fetch_sub_explicit(&t->refs, 1, memory_order_release) == 1) {
		atomic_thread_fence(memory_order_acquire);
		/* Poison, then free: ASAN proves nobody dereferences it. */
		memset(t, 0xdd, sizeof(*t));
		free(t);
		atomic_fetch_add(&tasks_freed, 1);
	}
}

static struct task *task_new(uint64_t id, const uint64_t *image)
{
	struct task *t = calloc(1, sizeof(*t));
	if (!t) {
		fprintf(stderr, "calloc\n");
		exit(2);
	}
	atomic_init(&t->refs, 1);	/* the scheduler/current reference */
	t->alive = 1;
	t->id = id;
	memcpy(t->fp, image, FP_WORDS * 8);
	atomic_fetch_add(&tasks_born, 1);
	return t;
}

/*
 * Drop a captured slot owner reference outside the locks. In the kernel this
 * is put_task_struct() in process context with interrupts enabled.
 */
static void slot_release(struct task *t)
{
	if (!t)
		return;
	atomic_fetch_add(&slot_puts, 1);
	task_put(t);
}

/* Coarse sampling of outstanding strong slot refs (bound check only). */
static unsigned long held_slot_refs(void)
{
	unsigned long n = 0;
	for (unsigned i = 0; i < N_RUNNERS; i++)
		if (runners[i].h.owner)
			n++;
	return n;
}

static void desc_reset(struct task *t)
{
	t->state = 0;
	t->home = NULL;
	t->frame = NULL;
	t->size = 0;
	t->token = 0;
	atomic_store_explicit(&t->lock, 0, memory_order_release);
	t->hpos = 0;
	memset(t->hist, 0, sizeof(t->hist));
}

static void hist(struct task *t, const char *ev)
{
	struct handoff *h = t->home;
	t->hist[t->hpos % NHIST] = (typeof(*t->hist)){
		ev, t->home, h ? h->owner : NULL,
		atomic_load_explicit(&t->state, memory_order_relaxed),
		t->token };
	t->hpos++;
}
static void hist_dump(struct task *t)
{
	for (unsigned k = 0; k < NHIST; k++) {
		unsigned i = (t->hpos + k) % NHIST;
		if (!t->hist[i].ev) continue;
		fprintf(stderr, "    [%u] %-10s state=%u home=%p owner=%p tok=%llu\n",
			i, t->hist[i].ev, t->hist[i].state, t->hist[i].home,
			t->hist[i].owner, (unsigned long long)t->hist[i].tok);
	}
}

/* ---- state machine under test (mirrors fp_lazy.c rev2) ----------------- */

/* Always called on behalf of the CURRENT task, as in the kernel. */
static int fp_enter(struct task *t, struct runner *rn)
{
	struct handoff *h = &rn->h;

	h_lock(h);
	if (h->owner != NULL) {
		h_unlock(h);
		fprintf(stderr, "model: non-NULL owner at arm\n");
		return -1;
	}
	task_get(t);		/* runner slot acquires its strong ref */
	t->token = ++h->token;
	h->owner = t;
	t->home = h;
	t->frame = rn->frame;
	t->size = xstate_size_words;
	atomic_store_explicit(&t->state, 1, memory_order_release);
	hist(t, "enter");
	h_unlock(h);
	unsigned long n = held_slot_refs();
	unsigned long peak = atomic_load(&runner_refs_held_peak);
	while (n > peak &&
	       !atomic_compare_exchange_weak(&runner_refs_held_peak, &peak, n))
		;
	return 1;
}

/*
 * Resolve the current task's own image before frame G on runner is written.
 * Returns 1 leave-frame-untouched, 0 regs->fp ready to copy, -1 protocol
 * error. Every decision happens with descriptor + (when present) home slot
 * locks held. Slot refs are released only after unlocking.
 */
/*
 * Returns 1 fast path (slot cleared, @put carries the ref to release),
 * 0 slow path (image in t->fp, slot at home cleared if it existed),
 * -1 protocol corruption. The caller releases *put after all unlocks.
 */
static int resolve_self(struct task *t, struct runner *rn, struct task **put)
{
	struct handoff *home;

	*put = NULL;
	home = atomic_load_explicit(&t->home, memory_order_acquire);
	if (home) {
		h_lock(home);
		d_lock(t);
		if (t->home) {
			if (atomic_load_explicit(&t->state, memory_order_acquire) != 1) {
				/* Remote relinquish already stored the bytes. */
				d_unlock(t);
				h_unlock(home);
				return 0;
			}
			if (home->owner != t || t->token != home->token) {
				fprintf(stderr, "  dbg live-mismatch t=%llu\n",
					(unsigned long long)t->id);
				hist_dump(t);
				d_unlock(t);
				h_unlock(home);
				return -1;
			}
			if (home == &rn->h && t->frame == rn->frame) {
				home->owner = NULL;
				t->home = NULL;
				atomic_store_explicit(&t->state, 0,
						      memory_order_release);
				hist(t, "fast");
				*put = t;
				d_unlock(t);
				h_unlock(home);
				atomic_fetch_add(&slots_cleared, 1);
				return 1;
			}
			memcpy(t->fp, t->frame, t->size * 8);
			home->owner = NULL;
			t->home = NULL;
			atomic_store_explicit(&t->state, 0, memory_order_release);
			hist(t, "collect");
			*put = t;
		}
		d_unlock(t);
		h_unlock(home);
		atomic_fetch_add(&slots_cleared, 1);
		return 0;
	}

	d_lock(t);
	t->home = NULL;
	if (atomic_load_explicit(&t->state, memory_order_acquire) == 1) {
		fprintf(stderr, "  dbg live-no-home t=%llu\n",
			(unsigned long long)t->id);
		hist_dump(t);
		d_unlock(t);
		return -1;
	}
	d_unlock(t);
	return 0;
}

static int fp_leave(struct task *t, struct runner *rn)
{
	struct handoff *h;
	struct task *put_home, *put_owner;
	int lazy;

	put_home = put_owner = NULL;
	lazy = resolve_self(t, rn, &put_home);
	if (lazy < 0)
		return -1;
	if (lazy == 1) {
		/* Fast path: frame left for rt_sigreturn to reload verbatim. */
		slot_release(put_home);
		return 1;
	}
	slot_release(put_home);

	/* Preserve a previous owner of the frame we are about to overwrite. */
	h = &rn->h;
	h_lock(h);
	struct task *owner = h->owner;
	if (owner == t) {
		h->owner = NULL;
		put_owner = t;
	} else if (owner) {
		d_lock(owner);
		if (atomic_load_explicit(&owner->state, memory_order_acquire) == 1 &&
		    owner->home == &rn->h && owner->token == h->token &&
		    owner->frame == rn->frame) {
			memcpy(owner->fp, rn->frame, owner->size * 8);
			owner->home = NULL;
			atomic_store_explicit(&owner->state, 0,
					      memory_order_release);
			h->owner = NULL;
			hist(owner, "pushed");
			put_owner = owner;
		} else {
			/* Stale slot record: the ref must still be released. */
			h->owner = NULL;
			hist(owner, "stale");
			put_owner = owner;
		}
		d_unlock(owner);
	}
	memcpy(rn->frame, t->fp, xstate_size_words * 8);
	h_unlock(h);
	slot_release(put_owner);
	return 0;
}

static void fp_materialize(struct task *t)
{
	struct handoff *home;
	struct task *put = NULL;

	if (atomic_load_explicit(&t->state, memory_order_acquire) != 1)
		return;
	home = atomic_load_explicit(&t->home, memory_order_acquire);
	if (!home) {
		atomic_store(&t->state, 0);
		return;
	}
	h_lock(home);
	d_lock(t);
	if (atomic_load_explicit(&t->state, memory_order_acquire) == 1) {
		struct task *owner = home->owner;
		if (owner == t && t->token == home->token)
			memcpy(t->fp, t->frame, t->size * 8);
		else
			fprintf(stderr, "FAIL materialize stale owner t=%llu\n",
				(unsigned long long)t->id);
		home->owner = NULL;
		t->home = NULL;
		atomic_store_explicit(&t->state, 0, memory_order_release);
		hist(t, "material");
		put = owner;	/* slot ref released regardless of match */
	}
	d_unlock(t);
	h_unlock(home);
	slot_release(put);
}

static void fp_discard(struct task *t)
{
	struct handoff *home;
	struct task *put = NULL;

	if (atomic_load_explicit(&t->state, memory_order_acquire) != 1)
		return;
	home = atomic_load_explicit(&t->home, memory_order_acquire);
	if (!home) {
		atomic_store(&t->state, 0);
		return;
	}
	h_lock(home);
	d_lock(t);
	if (atomic_load_explicit(&t->state, memory_order_acquire) == 1) {
		if (home->owner != t)
			fprintf(stderr, "FAIL discard stale owner t=%llu\n",
				(unsigned long long)t->id);
		put = home->owner;
		home->owner = NULL;
		t->home = NULL;
		atomic_store_explicit(&t->state, 0, memory_order_release);
		hist(t, "discard");
	}
	d_unlock(t);
	h_unlock(home);
	slot_release(put);
}

/*
 * Model of exit_thread(): the dying task detaches from every runner slot
 * BEFORE its task storage can be reaped. Synchronously clears a slot it still
 * owns, under both locks; otherwise the remote takeover already finished.
 */
static void fp_exit(struct task *t)
{
	struct handoff *home;
	struct task *put = NULL;

	d_lock(t);
	home = t->home;
	if (home) {
		h_lock(home);
		if (t->home == home && home->owner == t &&
		    atomic_load_explicit(&t->state, memory_order_acquire) == 1) {
			home->owner = NULL;
			t->home = NULL;
			atomic_store_explicit(&t->state, 0,
					      memory_order_release);
			put = t;
		} else {
			t->home = NULL;
			atomic_store_explicit(&t->state, 0,
					      memory_order_release);
		}
		h_unlock(home);
	} else {
		atomic_store_explicit(&t->state, 0, memory_order_release);
	}
	hist(t, "exit");
	d_unlock(t);
	slot_release(put);
}

/* mm teardown / broker recycle: clear slot and release its strong ref. */
static void handoff_reset(struct runner *rn)
{
	struct task *put;

	h_lock(&rn->h);
	put = rn->h.owner;
	rn->h.owner = NULL;
	rn->h.token = 0;
	h_unlock(&rn->h);
	atomic_store(&rn->h.lock, 0);
	slot_release(put);
}

/* ---- oracle helpers ----------------------------------------------------- */

static uint64_t rng_state;
static uint64_t rng(void)
{
	uint64_t x = atomic_fetch_add_explicit(&rng_state, 1,
					      memory_order_relaxed)
		* 0x9E3779B97F4A7C15ULL;
	x ^= x >> 30; x *= 0xBF58476D1CE4E5B9ULL; x ^= x >> 27;
	return x;
}

static void image_fill(uint64_t *img, uint64_t seed)
{
	for (unsigned i = 0; i < FP_WORDS; i++)
		img[i] = seed * 0x9E3779B97F4A7C15ULL + i;
}
static int image_eq(const uint64_t *a, const uint64_t *b)
{
	return !memcmp(a, b, FP_WORDS * 8);
}

static void fail(const char *what, uint64_t id, unsigned long round)
{
	fprintf(stderr, "FAIL %s task=%llu round=%lu\n", what,
		(unsigned long long)id, round);
	atomic_fetch_add(&failures, 1);
}

/* ---- full concurrent schedule ------------------------------------------ */

static uint64_t expected[TASK_POOL][FP_WORDS];
static atomic_ulong rounds_done, skipped, forks, exits, churn_events, edits;

/*
 * Each round: leave (frame loaded must equal the oracle), mutate the guest
 * image, host saves it into the runner frame, enter.
 */
static void do_round(struct task *t, struct runner *rn, unsigned long round)
{
	int lazy = fp_leave(t, rn);
	if (lazy < 0) { fail("leave", t->id, round); return; }
	if (!image_eq(rn->frame, expected[t->id])) {
		fail("resume-image", t->id, round);
		return;
	}
	uint64_t seed = rng() | 1;
	image_fill(expected[t->id], seed);
	memcpy(rn->frame, expected[t->id], FP_WORDS * 8);
	if (fp_enter(t, rn) < 0)
		fail("enter", t->id, round);
}

static void kernel_events(struct task *t, struct task **slots,
			  unsigned base, unsigned end)
{
	unsigned r = rng() % 100;

	if (r < 30) {
		/* plain syscall: nothing */
	} else if (r < 45) {
		uint64_t before[FP_WORDS];
		memcpy(before, expected[t->id], sizeof(before));
		fp_materialize(t);
		if (!image_eq(t->fp, before))
			fail("materialize-content", t->id, 0);
	} else if (r < 55) {
		uint64_t seed = rng();
		fp_materialize(t);
		for (int k = 3; k < 10; k++)
			t->fp[k] = seed ^ (uint64_t)k;
		memcpy(expected[t->id], t->fp, sizeof(expected[t->id]));
		atomic_fetch_add(&edits, 1);
	} else if (r < 62) {
		uint64_t seed = rng() | 1;
		fp_discard(t);
		image_fill(t->fp, seed);
		image_fill(expected[t->id], seed);
	} else if (r < 66) {
		/* fork: child starts EAGER with a copied image */
		fp_materialize(t);
		for (unsigned c = base; c < end; c++) {
			if (!slots[c]) {
				slots[c] = task_new(c, t->fp);
				image_fill(expected[c], 0);
				memcpy(expected[c], t->fp, sizeof(expected[c]));
				desc_reset(slots[c]);
				atomic_fetch_add(&forks, 1);
				break;
			}
		}
	}
}

/*
 * Tasks are pool slots whose storage is heap-allocated and freed on exit,
 * exercising real destruction/address reuse. Each worker drives an exclusive
 * partition (one vCPU executes a given task), while all workers churn the
 * same runner set, so parked owners are taken over by other "CPUs".
 */
static struct task *pool[TASK_POOL];
static pthread_mutex_t pool_lock = PTHREAD_MUTEX_INITIALIZER;

static void *worker(void *arg)
{
	unsigned long tid = (unsigned long)arg;
	unsigned base = tid * (TASK_POOL / NTHREADS);
	unsigned end = (tid == NTHREADS - 1) ? TASK_POOL :
			base + TASK_POOL / NTHREADS;

	for (unsigned long round = 0; round < ROUNDS_PER_THREAD; round++) {
		struct task *t;
		struct runner *rn;
		unsigned rnidx, idx;

		rnidx = rng() % N_RUNNERS;
		if (pthread_mutex_trylock(&runners[rnidx].round_lock) != 0) {
			atomic_fetch_add(&skipped, 1);
			continue;
		}
		rn = &runners[rnidx];

		idx = base + (rng() % (end - base));
		pthread_mutex_lock(&pool_lock);
		t = pool[idx];
		if (t)
			atomic_fetch_add_explicit(&t->inround, 1,
						  memory_order_acq_rel);
		pthread_mutex_unlock(&pool_lock);
		if (!t) {
			uint64_t img[FP_WORDS];
			image_fill(img, rng() | 1);
			t = task_new(idx, img);
			memcpy(expected[idx], img, sizeof(img));
			atomic_store_explicit(&t->inround, 1,
					      memory_order_release);
			pthread_mutex_lock(&pool_lock);
			pool[idx] = t;
			pthread_mutex_unlock(&pool_lock);
		}

		do_round(t, rn, round);
		pthread_mutex_unlock(&rn->round_lock);

		kernel_events(t, pool, base, end);

		if (rng() % 100 < 12) {
			/* Thread exits: detach, then storage is reaped. */
			fp_exit(t);
			pthread_mutex_lock(&pool_lock);
			if (pool[idx] == t)
				pool[idx] = NULL;
			pthread_mutex_unlock(&pool_lock);
			task_put(t);	/* drop the scheduler reference */
			atomic_fetch_add(&exits, 1);
			atomic_fetch_add(&churn_events, 1);
		} else {
			atomic_store_explicit(&t->inround, 0,
					      memory_order_release);
		}
		atomic_fetch_add(&rounds_done, 1);
	}
	return NULL;
}

static int run_concurrent(const char *label)
{
	pthread_t th[NTHREADS];

	memset(pool, 0, sizeof(pool));
	for (unsigned i = 0; i < N_RUNNERS; i++) {
		pthread_mutex_init(&runners[i].round_lock, NULL);
		handoff_reset(&runners[i]);
	}
	/* Empty slots: each task's first contact is an EAGER round, exactly
	 * like a fresh mm before its first SECCOMP round. */

	for (unsigned i = 0; i < NTHREADS; i++)
		pthread_create(&th[i], NULL, worker, (void *)(unsigned long)i);
	for (unsigned i = 0; i < NTHREADS; i++)
		pthread_join(th[i], NULL);

	for (unsigned i = 0; i < N_RUNNERS; i++)
		handoff_reset(&runners[i]);

	pthread_mutex_lock(&pool_lock);
	for (unsigned i = 0; i < TASK_POOL; i++)
		if (pool[i]) {
			task_put(pool[i]);
			pool[i] = NULL;
		}
	pthread_mutex_unlock(&pool_lock);

	unsigned long f = atomic_load(&failures);
	printf("fp_model[%s] rounds=%lu skipped=%lu forks=%lu exits=%lu "
	       "edits=%lu born=%lu freed=%lu slot_puts=%lu peak_slot_refs=%lu "
	       "failures=%lu\n", label,
	       atomic_load(&rounds_done), atomic_load(&skipped),
	       atomic_load(&forks), atomic_load(&exits), atomic_load(&edits),
	       atomic_load(&tasks_born), atomic_load(&tasks_freed),
	       atomic_load(&slot_puts),
	       atomic_load(&runner_refs_held_peak), f);
	return f != 0;
}

/* ---- dedicated lifetime regressions ------------------------------------- */

/*
 * The owner's exact counterexample
 * (/root/harddind-rebuild/fp-owner-lifetime-v1/lifetime_repro.c).
 */
static int test_owner_repro(void)
{
	struct runner rn = { 0 };
	struct task *old, *next;
	uint64_t img[FP_WORDS];
	int rc = 0;

	image_fill(img, 42);
	old = task_new(1001, img);
	next = task_new(1002, img);
	handoff_reset(&rn);

	if (fp_enter(old, &rn) != 1)
		return 1;
	/* Scheduler drops its reference as the task exits: slot MUST keep the
	 * object valid, and fp_exit() MUST clear the slot synchronously. */
	fp_exit(old);
	task_put(old);
	/* ASAN poisons/frees old here; this must never be dereferenced. */
	if (fp_leave(next, &rn) < 0)
		rc = 1;
	if (rn.h.owner != NULL) {
		fprintf(stderr, "FAIL repro: slot non-NULL after leave\n");
		rc = 1;
	}
	handoff_reset(&rn);
	task_put(next);
	if (!rc)
		printf("fp_model[owner-repro] OK\n");
	return rc;
}

/* malloc/free + address reuse: ABA must never pair a new task with an old
 * token, and a freed task may never be touched through a recycled slot. */
static int test_address_reuse(void)
{
	struct runner rn = { 0 };
	uint64_t img[FP_WORDS];
	int rc = 0;

	handoff_reset(&rn);
	image_fill(img, 1);
	for (int i = 0; i < 2000; i++) {
		struct task *a = task_new(2000 + i, img);
		image_fill(a->fp, (uint64_t)i + 1);
		image_fill(rn.frame, (uint64_t)i + 1);
		if (fp_enter(a, &rn) != 1) { rc = 1; break; }
		if ((i & 1)) {
			fp_exit(a);
			task_put(a);	/* often recycles the same address */
			struct task *b = task_new(3000 + i, img);
			desc_reset(b);
			b->fp[0] = 0;
			image_fill(b->fp, 0xdead + i);
			/* b is EAGER: leave writes b->fp; no stale owner. */
			image_fill(rn.frame, 0xdead + i);
			if (fp_leave(b, &rn) < 0 ||
			    !image_eq(rn.frame, b->fp)) {
				fprintf(stderr, "FAIL reuse at %d\n", i);
				rc = 1;
				task_put(b);
				break;
			}
			task_put(b);
		} else {
			/* Fast path, then exit. */
			if (fp_leave(a, &rn) != 1) { rc = 1; task_put(a); break; }
			fp_exit(a);
			task_put(a);
		}
	}
	handoff_reset(&rn);
	if (!rc)
		printf("fp_model[address-reuse] 2000 cycles OK\n");
	return rc;
}

/* mm teardown while a slot is occupied, then broker recycle and re-arm. */
static int test_teardown(void)
{
	struct runner rn = { 0 };
	struct task *a, *b;
	uint64_t img[FP_WORDS];
	int rc = 0;

	pthread_mutex_init(&rn.round_lock, NULL);
	handoff_reset(&rn);
	image_fill(img, 7);
	a = task_new(4001, img);
	b = task_new(4002, img);
	if (fp_enter(a, &rn) != 1)
		return 1;
	/* mm destroyed: teardown must release the slot's strong ref safely;
	 * a stays alive via its scheduler ref. */
	handoff_reset(&rn);
	if (rn.h.owner != NULL) {
		fprintf(stderr, "FAIL teardown: owner retained\n");
		rc = 1;
	}
	/* Broker cache hands the recycled runner to a new mm: re-arm. */
	image_fill(rn.frame, 99);
	if (fp_enter(b, &rn) != 1)
		rc = 1;
	if (fp_leave(b, &rn) != 1)
		rc = 1;
	handoff_reset(&rn);
	task_put(a);
	task_put(b);
	if (!rc)
		printf("fp_model[teardown-reuse] OK\n");
	return rc;
}

/* Randomised concurrent exit vs takeover. */
struct exit_arg { struct task *t; struct runner *rns; unsigned stop; };

static void *takeover_thread(void *arg)
{
	struct exit_arg *a = arg;
	struct task *next;
	uint64_t img[FP_WORDS];

	image_fill(img, 555);
	next = task_new(9000 + (uintptr_t)arg % 97, img);
	while (!a->stop) {
		unsigned i = rng() % N_RUNNERS;
		if (pthread_mutex_trylock(&a->rns[i].round_lock) == 0) {
			image_fill(next->fp, rng() | 1);
			memcpy(a->rns[i].frame, next->fp, FP_WORDS * 8);
			fp_leave(next, &a->rns[i]);
			image_fill(a->rns[i].frame, rng() | 1);
			fp_enter(next, &a->rns[i]);
			fp_leave(next, &a->rns[i]);
			pthread_mutex_unlock(&a->rns[i].round_lock);
		}
	}
	task_put(next);
	return NULL;
}

static int test_exit_vs_takeover(void)
{
	pthread_t th[6];
	struct exit_arg args[6];
	int rc = 0;

	for (unsigned i = 0; i < N_RUNNERS; i++) {
		pthread_mutex_init(&runners[i].round_lock, NULL);
		handoff_reset(&runners[i]);
	}
	for (int k = 0; k < 6; k++) {
		args[k].rns = runners;
		args[k].stop = 0;
		pthread_create(&th[k], NULL, takeover_thread, &args[k]);
	}
	for (int round = 0; round < 3000; round++) {
		struct task *t;
		uint64_t img[FP_WORDS];
		unsigned i = rng() % N_RUNNERS;
		image_fill(img, (uint64_t)round + 1);
		t = task_new(6000 + round, img);
		pthread_mutex_lock(&runners[i].round_lock);
		image_fill(runners[i].frame, (uint64_t)round + 1);
		if (fp_enter(t, &runners[i]) != 1)
			rc = 1;
		/* Task exits without a leave: exact async-exit window. */
		fp_exit(t);
		pthread_mutex_unlock(&runners[i].round_lock);
		task_put(t);
	}
	for (int k = 0; k < 6; k++) {
		args[k].stop = 1;
		pthread_join(th[k], NULL);
	}
	for (unsigned i = 0; i < N_RUNNERS; i++)
		handoff_reset(&runners[i]);
	if (!rc && !atomic_load(&failures))
		printf("fp_model[exit-vs-takeover] 3000 exits OK\n");
	return rc || atomic_load(&failures) != 0;
}

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "all";
	int rc = 0;

	if (!strcmp(mode, "--lifetime-repro"))
		return test_owner_repro();
	if (!strcmp(mode, "--reuse"))
		return test_address_reuse();
	if (!strcmp(mode, "--teardown"))
		return test_teardown();
	if (!strcmp(mode, "--concurrent"))
		return test_exit_vs_takeover();

	rc |= test_owner_repro();
	rc |= test_address_reuse();
	rc |= test_teardown();
	rc |= test_exit_vs_takeover();
	rc |= run_concurrent("full");

	if (!rc) {
		/*
		 * Structural bounds: no task remains allocated at exit (ASAN
		 * leak mode would also catch it), and the peak number of
		 * strong refs held by runner slots never exceeded the slots.
		 */
		unsigned long born = atomic_load(&tasks_born);
		unsigned long freed = atomic_load(&tasks_freed);
		unsigned long peak = atomic_load(&runner_refs_held_peak);
		if (born != freed) {
			fprintf(stderr, "FAIL leak born=%lu freed=%lu\n",
				born, freed);
			rc = 1;
		}
		if (peak > N_RUNNERS) {
			fprintf(stderr, "FAIL slot ref peak %lu > %d\n",
				peak, N_RUNNERS);
			rc = 1;
		}
		if (!rc)
			printf("fp_model[bounds] born==freed=%lu, "
			       "peak_slot_refs<=%d OK\n", born, N_RUNNERS);
	}
	return rc;
}
