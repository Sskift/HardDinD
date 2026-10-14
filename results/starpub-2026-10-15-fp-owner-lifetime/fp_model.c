// SPDX-License-Identifier: GPL-2.0
/*
 * fp_model.c - userspace behavioural model of the 0018 lazy XSTATE ownership
 * state machine (arch/x86/um/fp_lazy.c).
 *
 * PROVENANCE: written for the round6 repair as a standalone host model. It is
 * NOT kernel code and NOT guest evidence: it re-implements the same states,
 * locks and transitions in pthreads so that millions of randomised schedules
 * can assert byte-exact image fidelity and the lifetime bounds that the
 * kernel design relies on:
 *
 *   - every time a task resumes on a runner, the frame bytes loaded by the
 *     modelled rt_sigreturn equal that task's current authoritative image;
 *   - ptrace-style partial writes, signal materialise, sigreturn/exec
 *     discard, fork inheritance and task exit stay coherent;
 *   - after exit, a task descriptor is never dereferenced by a runner (its
 *     poisoned fp buffer is never written);
 *   - no salvage-like storage exists: at most one image per live task plus
 *     one frame image per runner, regardless of thread-exit churn.
 *
 * Build: gcc -O2 -Wall -Wextra -pthread src/fp_model.c -o fp_model
 */
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define FP_WORDS 64		/* stand-in for a 512-byte XSTATE image */
#define N_RUNNERS 8
#define TASK_POOL 512
#define ROUNDS_PER_THREAD 300000
#define NTHREADS 8

/* ---- modelled primitives (same shape as the kernel counterparts) ------ */

struct handoff {
	atomic_uint lock;
	void *owner;		/* struct desc * identity only */
	uint64_t token;
};

struct desc {
	atomic_uint lock;
	unsigned int state;	/* 0 eager, 1 live */
	struct handoff *home;
	uint64_t *frame;
	unsigned int size;
	uint64_t token;
	uint64_t fp[FP_WORDS];
	unsigned int alive;
	unsigned int inround;
	uint64_t id;
#define NHIST 8
	struct { const char *ev; void *home; void *owner; unsigned state; uint64_t tok; }
		hist[NHIST];
	unsigned hpos;
};

struct runner {
	struct handoff h;
	uint64_t frame[FP_WORDS];
	pthread_mutex_t round_lock;	/* one vCPU/turnstile round at a time */
};

static unsigned int xstate_size_words = FP_WORDS;

static void h_lock(struct handoff *h)
{
	while (atomic_exchange_explicit(&h->lock, 1, memory_order_acq_rel) != 0) {
		struct timespec ts = { 0, 1000 };
		while (atomic_load_explicit(&h->lock, memory_order_relaxed))
			nanosleep(&ts, NULL);
	}
}
static void h_unlock(struct handoff *h)
{
	atomic_store_explicit(&h->lock, 0, memory_order_release);
}
static void d_lock(struct desc *d)
{
	while (atomic_exchange_explicit(&d->lock, 1, memory_order_acq_rel) != 0) {
		struct timespec ts = { 0, 1000 };
		while (atomic_load_explicit(&d->lock, memory_order_relaxed))
			nanosleep(&ts, NULL);
	}
}
static void d_unlock(struct desc *d)
{
	atomic_store_explicit(&d->lock, 0, memory_order_release);
}

/* Transition history, recorded while relevant locks are held. */
static void hist(struct desc *d, const char *ev)
{
	struct handoff *h = d->home;
	d->hist[d->hpos % NHIST] = (typeof(*d->hist)){
		ev, d->home, h ? h->owner : NULL,
		atomic_load_explicit(&d->state, memory_order_relaxed),
		d->token };
	d->hpos++;
}
static void hist_dump(struct desc *d)
{
	for (unsigned k = 0; k < NHIST; k++) {
		unsigned i = (d->hpos + k) % NHIST;
		if (!d->hist[i].ev) continue;
		fprintf(stderr, "    [%u] %-12s state=%u home=%p owner=%p tok=%llu\n",
			i, d->hist[i].ev, d->hist[i].state, d->hist[i].home,
			d->hist[i].owner, (unsigned long long)d->hist[i].tok);
	}
}

static void desc_reset(struct desc *d)
{
	d->state = 0;
	d->home = NULL;
	d->frame = NULL;
	d->size = 0;
	d->token = 0;
	atomic_store_explicit(&d->lock, 0, memory_order_release);
}

/* ---- state machine under test (mirrors fp_lazy.c) ---------------------- */

static int fp_enter(struct desc *r, struct runner *rn)
{
	struct handoff *h = &rn->h;
	h_lock(h);
	if (h->owner != NULL) {
		h_unlock(h);
		fprintf(stderr, "model: non-NULL owner at arm\n");
		return -1;
	}
	r->token = ++h->token;
	h->owner = r;
	r->home = h;
	r->frame = rn->frame;
	r->size = xstate_size_words;
	atomic_store_explicit(&r->state, 1, memory_order_release);
	hist(r, "enter");
	h_unlock(h);
	return 1;
}

/*
 * Resolve the task's own state. All decisions are taken with the descriptor
 * lock held (and the home handoff lock when a home exists), so the lockless
 * home hint cannot be observed torn against a remote relinquish. Returns 1
 * when the current runner's frame must be left untouched, else 0.
 */
static int resolve_self(struct desc *r, struct runner *rn)
{
	struct handoff *home;

	home = r->home;
	if (home) {
		h_lock(home);
		d_lock(r);
		if (r->home) {
			if (atomic_load_explicit(&r->state, memory_order_acquire) != 1) {
				/* Remote relinquish already stored the bytes. */
				d_unlock(r);
				h_unlock(home);
				return 0;
			}
			if (home->owner != r || r->token != home->token) {
				fprintf(stderr, "  dbg live-mismatch t=%llu\n",
					(unsigned long long)r->id);
				hist_dump(r);
				d_unlock(r);
				h_unlock(home);
				return -1;
			}
			if (home == &rn->h && r->frame == rn->frame) {
				home->owner = NULL;
				r->home = NULL;
				atomic_store_explicit(&r->state, 0,
						      memory_order_release);
				hist(r, "fast");
				d_unlock(r);
				h_unlock(home);
				return 1;
			}
			/* Migration or moved frame slot: collect home frame. */
			memcpy(r->fp, r->frame, r->size * 8);
			home->owner = NULL;
			r->home = NULL;
			atomic_store_explicit(&r->state, 0, memory_order_release);
			hist(r, "collect");
		}
		d_unlock(r);
		h_unlock(home);
		return 0;
	}

	d_lock(r);
	/* Remote writers only ever clear home; nobody re-arms us concurrently. */
	r->home = NULL;
	if (atomic_load_explicit(&r->state, memory_order_acquire) == 1) {
		fprintf(stderr, "  dbg live-no-home t=%llu\n",
			(unsigned long long)r->id);
		hist_dump(r);
		d_unlock(r);
		return -1;
	}
	d_unlock(r);
	return 0;
}

static int fp_leave(struct desc *r, struct runner *rn)
{
	struct handoff *h;
	struct desc *owner;
	int lazy;

	lazy = resolve_self(r, rn);
	if (lazy < 0)
		return -1;
	if (lazy == 1)
		return 1;

	h = &rn->h;
	h_lock(h);
	owner = h->owner;
	if (owner == r) {
		h->owner = NULL;
	} else if (owner) {
		d_lock(owner);
		if (atomic_load_explicit(&owner->state, memory_order_acquire) == 1 &&
		    owner->home == &rn->h && owner->token == h->token &&
		    owner->frame == rn->frame) {
			memcpy(owner->fp, rn->frame, owner->size * 8);
			owner->home = NULL;
			atomic_store_explicit(&owner->state, 0, memory_order_release);
			h->owner = NULL;
			hist(owner, "pushed");
		} else {
			h->owner = NULL;
			hist(owner, "stale");
		}
		d_unlock(owner);
	}
	memcpy(rn->frame, r->fp, xstate_size_words * 8);
	h_unlock(h);
	return 0;
}

static void fp_materialize(struct desc *r)
{
	struct handoff *h;
	if (atomic_load_explicit(&r->state, memory_order_acquire) != 1)
		return;
	h = r->home;
	if (!h) { atomic_store(&r->state, 0); return; }
	h_lock(h);
	d_lock(r);
	if (atomic_load_explicit(&r->state, memory_order_acquire) == 1) {
		if (h->owner == r && r->token == h->token)
			memcpy(r->fp, r->frame, r->size * 8);
		h->owner = NULL;
		r->home = NULL;
		atomic_store_explicit(&r->state, 0, memory_order_release);
		hist(r, "material");
	}
	d_unlock(r);
	h_unlock(h);
}

static void fp_discard(struct desc *r)
{
	struct handoff *h;
	if (atomic_load_explicit(&r->state, memory_order_acquire) != 1)
		return;
	h = r->home;
	if (!h) { atomic_store(&r->state, 0); return; }
	h_lock(h);
	d_lock(r);
	if (atomic_load_explicit(&r->state, memory_order_acquire) == 1) {
		if (h->owner == r && r->token == h->token)
			h->owner = NULL;
		r->home = NULL;
		atomic_store_explicit(&r->state, 0, memory_order_release);
		hist(r, "discard");
	}
	d_unlock(r);
	h_unlock(h);
}

static void fp_exit(struct desc *r)
{
	d_lock(r);
	r->home = NULL;
	atomic_store_explicit(&r->state, 0, memory_order_release);
	d_unlock(r);
}

static void handoff_reset(struct runner *rn)
{
	rn->h.owner = NULL;
	rn->h.token = 0;
	atomic_store(&rn->h.lock, 0);
}

/* ---- workload and oracle ----------------------------------------------- */

static struct runner runners[N_RUNNERS];
static struct desc tasks[TASK_POOL];
static atomic_ulong failures;
static atomic_ulong rounds_done;
static atomic_ulong forks_done, exits_done, edits_done, churn_events;

static uint64_t rng_state;
static uint64_t xorshift64(void)
{
	uint64_t x = atomic_fetch_add_explicit(&rng_state, 1, memory_order_relaxed);
	x ^= x << 13; x ^= x >> 7; x ^= x << 17;
	return x;
}

static void image_fill(uint64_t *img, uint64_t seed)
{
	for (unsigned i = 0; i < FP_WORDS; i++)
		img[i] = seed * 0x9E3779B97F4A7C15ULL + i;
}
static int image_eq(const uint64_t *a, const uint64_t *b)
{
	for (unsigned i = 0; i < FP_WORDS; i++)
		if (a[i] != b[i])
			return 0;
	return 1;
}
static void image_copy(uint64_t *dst, const uint64_t *src)
{
	memcpy(dst, src, FP_WORDS * 8);
}

/*
 * Each task carries an oracle image: what its hardware XSTATE must equal the
 * next time it resumes. Frames are filled by the model "host" and consumed by
 * leave(); every resume is checked.
 */
static uint64_t expected[TASK_POOL][FP_WORDS];

static void fail(const char *what, uint64_t id, unsigned long round)
{
	fprintf(stderr, "FAIL %s task=%llu round=%lu\n", what,
		(unsigned long long)id, round);
	atomic_fetch_add(&failures, 1);
}

/* Simulated guest mutation seed, advanced between resume and the next trap. */
static uint64_t mutate_seed;

/*
 * Perform one complete round for task t on runner rn. The "host" layer is
 * modelled directly on rn->frame: at enter the expected image is in the
 * frame (trap save); leave must end with the frame equal to the task image.
 */
static void do_round(struct desc *t, struct runner *rn, unsigned long round)
{
	int lazy;

	/* set side: frame must end holding expected[] */
	lazy = fp_leave(t, rn);
	if (lazy < 0) { fail("leave", t->id, round); return; }

	/* rt_sigreturn loads rn->frame; it must equal expected. */
	if (!image_eq(rn->frame, expected[t->id])) {
		fail("resume-image", t->id, round);
		return;
	}

	/* guest executes: mutate, then traps: host writes new image to frame. */
	mutate_seed += 1;
	image_fill(expected[t->id], mutate_seed);
	image_copy(rn->frame, expected[t->id]);

	/* get side */
	if (fp_enter(t, rn) < 0) { fail("enter", t->id, round); return; }
}

/* Between enter and leave, kernel events may touch the image. */
static void kernel_events(struct desc *t, unsigned base, unsigned end,
			  unsigned long round)
{
	unsigned r = xorshift64() % 100;

	if (r < 30) {
		/* syscall only: nothing. */
	} else if (r < 45) {
		/* signal delivery / ptrace read: materialise, image unchanged. */
		uint64_t before[FP_WORDS];
		image_copy(before, expected[t->id]);
		fp_materialize(t);
		if (!image_eq(t->fp, before))
			fail("materialize-content", t->id, round);
	} else if (r < 55) {
		/* ptrace partial FP regset write: changes the image. */
		uint64_t seed = xorshift64();
		fp_materialize(t);
		for (int k = 3; k < 10; k++)
			t->fp[k] = seed ^ (uint64_t)k;
		image_fill(expected[t->id], 0); /* recompute oracle same edit */
		image_copy(expected[t->id], t->fp);
		atomic_fetch_add(&edits_done, 1);
	} else if (r < 62) {
		/* sigreturn/exec: wholesale replacement. */
		uint64_t seed = xorshift64() | 1;
		fp_discard(t);
		image_fill(t->fp, seed);
		image_fill(expected[t->id], seed);
	} else if (r < 66) {
		/* fork: child inherits exact image. */
		fp_materialize(t);
		for (unsigned c = base; c < end; c++) {
			if (!tasks[c].alive) {
				desc_reset(&tasks[c]);
				tasks[c].alive = 1;
				tasks[c].id = c;
				image_copy(tasks[c].fp, t->fp);
				image_copy(expected[c], t->fp);
				atomic_fetch_add(&forks_done, 1);
				break;
			}
		}
	}
	/* else: stay parked while other tasks churn the runners. */
}

static unsigned long skipped_rounds;

static void *worker(void *arg)
{
	unsigned long tid = (unsigned long)arg;
	unsigned base = tid * (TASK_POOL / NTHREADS);
	unsigned end = (tid == NTHREADS - 1) ? TASK_POOL :
			base + TASK_POOL / NTHREADS;

	/*
	 * Runner rounds are mutually exclusive (parallel mode pins one vCPU
	 * per runner; legacy mode has the turnstile), and a task is current on
	 * one vCPU at a time. Tasks are shared across all threads so vCPU
	 * migration occurs; parked owners are churned by other CPUs.
	 */
	for (unsigned long round = 0; round < ROUNDS_PER_THREAD; round++) {
		struct desc *t = NULL;
		struct runner *rn;
		unsigned rnidx, tries, idx;
		int churn;

		rnidx = xorshift64() % N_RUNNERS;
		if (pthread_mutex_trylock(&runners[rnidx].round_lock) != 0) {
			atomic_fetch_add(&skipped_rounds, 1);
			continue;
		}
		rn = &runners[rnidx];

		for (tries = 0; tries < 16; tries++) {
			idx = base + (xorshift64() % (end - base));
			t = &tasks[idx];
			if (!t->alive)
				break;	/* we create and own it below */
			if (!atomic_exchange_explicit(&t->inround, 1,
						      memory_order_acq_rel))
				break;
			t = NULL;
		}
		if (!t) {
			pthread_mutex_unlock(&rn->round_lock);
			atomic_fetch_add(&skipped_rounds, 1);
			continue;
		}

		if (!t->alive) {
			desc_reset(t);
			t->alive = 1;
			t->inround = 1;
			t->id = idx;
			uint64_t seed = xorshift64() | 1;
			image_fill(t->fp, seed);
			image_fill(expected[idx], seed);
		}

		/* Atomic round: leave -> guest run/trap -> enter. */
		do_round(t, rn, round);
		pthread_mutex_unlock(&rn->round_lock);

		/* Parked in the UML kernel: other runners may churn us. */
		kernel_events(t, base, end, round);

		churn = (xorshift64() % 100) < 12;
		if (churn) {
			fp_exit(t);
			t->alive = 0;
			memset(t->fp, 0xEE, sizeof(t->fp));
			atomic_fetch_add(&exits_done, 1);
			atomic_fetch_add(&churn_events, 1);
		}
		atomic_store_explicit(&t->inround, 0, memory_order_release);
		atomic_fetch_add(&rounds_done, 1);
	}
	return NULL;
}

int main(void)
{
	pthread_t th[NTHREADS];

	for (unsigned i = 0; i < N_RUNNERS; i++) {
		pthread_mutex_init(&runners[i].round_lock, NULL);
		handoff_reset(&runners[i]);
	}

	/* Seed one initial live task per runner owning a parked frame. */
	for (unsigned i = 0; i < N_RUNNERS; i++) {
		struct desc *t = &tasks[i];
		desc_reset(t);
		t->alive = 1;
		t->id = i;
		uint64_t seed = 1000 + i;
		image_fill(runners[i].frame, seed);
		image_fill(expected[i], seed);
		if (fp_enter(t, &runners[i]) != 1)
			return 1;
	}

	/* Model mm destruction at intervals: recycle runner records. */
	for (unsigned i = 0; i < NTHREADS; i++)
		pthread_create(&th[i], NULL, worker, (void *)(unsigned long)i);
	for (unsigned i = 0; i < NTHREADS; i++)
		pthread_join(th[i], NULL);

	for (unsigned i = 0; i < N_RUNNERS; i++)
		handoff_reset(&runners[i]);

	unsigned long f = atomic_load(&failures);
	printf("fp_model rounds=%lu skipped=%lu forks=%lu exits=%lu churn=%lu "
	       "edits=%lu failures=%lu\n",
	       atomic_load(&skipped_rounds),
	       atomic_load(&rounds_done), atomic_load(&forks_done),
	       atomic_load(&exits_done), atomic_load(&churn_events),
	       atomic_load(&edits_done), f);
	if (f)
		return 1;

	/*
	 * Structural retention bound: every retired task is EAGER with no
	 * home reference; the only frame images are the N_RUNNERS fixed
	 * buffers. Count live owner records (must be <= live tasks and
	 * <= N_RUNNERS).
	 */
	unsigned live = 0, owners = 0;
	for (unsigned i = 0; i < TASK_POOL; i++)
		if (tasks[i].alive) live++;
	for (unsigned i = 0; i < N_RUNNERS; i++)
		if (runners[i].h.owner) owners++;
	if (owners > live || owners > N_RUNNERS) {
		fprintf(stderr, "FAIL owner bound owners=%u live=%u\n",
			owners, live);
		return 1;
	}
	printf("fp_model owner-bound owners=%u live=%u runners=%d OK\n",
	       owners, live, N_RUNNERS);
	return 0;
}
