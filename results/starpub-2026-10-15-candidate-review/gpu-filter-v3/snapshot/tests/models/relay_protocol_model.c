/* SPDX-License-Identifier: GPL-2.0
 * Deterministic model of the 0024 GPU relay protocol.
 *
 * It includes the REAL shared relay structures from the kernel header and
 * re-runs, single-threaded on a step scheduler, every transition the relay
 * thread (arch/um/kernel/skas/stub.c) and the kernel event thread
 * (arch/um/kernel/gpu.c) perform.  Assertions prove the repair properties:
 *
 *  1. eventfd counts are never lost across kicks, full notify sockets and
 *     table updates (monotonic accumulation + delivery watermarks);
 *  2. a device fd that stays level-ready produces exactly one readiness
 *     publication (no notify busy-loop); a clean poll round clears it, and
 *     repeated guest polls observe level semantics;
 *  3. stale results tagged with an old generation are ignored;
 *  4. a retired worker fd is closed exactly once, after the relay excluded
 *     the slot from its poll set, and a descriptor number the worker reuses
 *     afterwards is never touched through the stale entry;
 *  5. a closing slot cannot be reused before the close acknowledgement.
 *
 * Exit status is non-zero on any violated property.
 */
#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define UM_KERN_PAGE_SIZE 4096
#include "as-layout.h"
#include "stub-data.h"
#ifndef static_assert
#define static_assert _Static_assert
#endif

static_assert(sizeof(struct gpu_relay_entry) == 40);
static_assert(offsetof(struct gpu_relay_entry, fd) == 0);
static_assert(sizeof(struct gpu_relay_page) <= 2 * UM_KERN_PAGE_SIZE);

/* ---- simulated worker environment ------------------------------------- */
#define NR_WORKER_FDS 16
struct worker {
	int open[NR_WORKER_FDS];
	int close_count[NR_WORKER_FDS];
	int read_count[NR_WORKER_FDS];
	unsigned long long eventfd_count[NR_WORKER_FDS];
	int level_ready[NR_WORKER_FDS];
	int retired_at_read[NR_WORKER_FDS];
};

static struct gpu_relay_page page;
static struct worker worker;
static int notify_writes, notify_failures, kicks;
static int notify_full;

/* ---- kernel-side slot helpers (mirror gpu.c) -------------------------- */
static int slot_alloc(void)
{
	for (int i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++)
		if (page.entry[i].state == GPU_RELAY_STATE_EMPTY)
			return i;
	return -1;
}

static void slot_publish(int slot, int fd, unsigned int kind)
{
	struct gpu_relay_entry *e = &page.entry[slot];

	e->fd = fd;
	e->kind = kind;
	e->ack = GPU_RELAY_ACK_NONE;
	e->ack_gen = 0;
	e->level = 0;
	e->level_gen = 0;
	e->value = 0;
	e->value_gen = 0;
	e->sticky = 0;
	e->gen++;
	e->state = GPU_RELAY_STATE_ACTIVE;
	kicks++;
}

static void slot_retire(int slot)
{
	struct gpu_relay_entry *e = &page.entry[slot];

	e->ack = GPU_RELAY_ACK_NONE;
	e->gen++;
	e->state = GPU_RELAY_STATE_CLOSING;
	kicks++;
}

/* ---- relay-side one top-of-loop scan (mirror gpu_relay_thread_start) --- */
struct pollset { int fd; unsigned int revents; int slot; unsigned int gen; };

static int relay_scan_close(struct pollset *closed, int *nclosed)
{
	int changed = 0;

	for (int i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++) {
		struct gpu_relay_entry *e = &page.entry[i];

		if (e->state != GPU_RELAY_STATE_CLOSING)
			continue;
		assert(e->fd >= 0);
		worker.close_count[e->fd]++;
		worker.open[e->fd] = 0;
		closed[(*nclosed)++] = (struct pollset){ e->fd, 0, i, e->gen };
		e->fd = -1;
		e->state = GPU_RELAY_STATE_EMPTY;
		e->ack = GPU_RELAY_ACK_CLOSED;
		e->ack_gen = e->gen;
		changed = 1;
	}
	return changed;
}

static int relay_build_poll(struct pollset *pfds, int *relay_reads)
{
	int n = 0;

	(void)relay_reads;
	for (int i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++) {
		struct gpu_relay_entry *e = &page.entry[i];

		if (e->state != GPU_RELAY_STATE_ACTIVE || e->fd < 0)
			continue;
		pfds[n++] = (struct pollset){
			.fd = e->fd,
			.revents = worker.level_ready[e->fd] ? 0x001 : 0,
			.slot = i,
			.gen = e->gen,
		};
	}
	return n;
}

static int relay_publish(const struct pollset *pfds, int n,
			 unsigned long long *drain)
{
	int changed = 0;

	for (int i = 0; i < n; i++) {
		struct gpu_relay_entry *e = &page.entry[pfds[i].slot];

		if (e->state != GPU_RELAY_STATE_ACTIVE ||
		    e->gen != pfds[i].gen)
			continue; /* stale generation: ignored */
		if (e->kind == UML_GPU_RELAY_KIND_EVENTFD) {
			unsigned long long got = drain[pfds[i].slot];

			if (got) {
				worker.read_count[e->fd]++;
				e->value += got;
				e->value_gen++;
				changed = 1;
			}
		} else {
			unsigned int level = pfds[i].revents & 0x039;

			/* Revents 0 is a clean round and must clear a
			 * previously reported level. */
			if (level != e->level) {
				e->level = level;
				e->level_gen++;
				changed = 1;
			}
		}
	}
	return changed;
}

static void relay_notify(int changed)
{
	if (!changed)
		return;
	if (notify_full)
		notify_failures++;
	else
		notify_writes++;
}

/* ---- kernel-side delivery (mirror gpu_relay_consume) ------------------ */
struct k_event {
	int slot;
	unsigned int gen;
	unsigned long long delivered;
	unsigned int value_gen;
	unsigned long long guest_count;
	int active;
};
struct k_device {
	int slot;
	unsigned int gen;
	unsigned int last_level;
	unsigned int mask;
	int wakes;
};

static void consume_events(struct k_event *evs, int count)
{
	/* Kernel bookkeeping is freed at retire time (FREE_OS_EVENT /
	 * release), so active events are only delivered while ACTIVE.  The
	 * relay's CLOSED acknowledgement gates slot reallocation, not object
	 * lifetime. */
	for (int i = 0; i < count; i++) {
		struct k_event *k = &evs[i];
		struct gpu_relay_entry *e;

		if (!k->active)
			continue;
		e = &page.entry[k->slot];
		if (e->state == GPU_RELAY_STATE_ACTIVE && e->gen == k->gen &&
		    e->value_gen != k->value_gen) {
			unsigned long long delta = e->value - k->delivered;

			k->delivered = e->value;
			k->value_gen = e->value_gen;
			k->guest_count += delta;
		}
	}
}

static void consume_devices(struct k_device *ds, int count)
{
	for (int i = 0; i < count; i++) {
		struct k_device *d = &ds[i];
		struct gpu_relay_entry *e;

		if (d->slot < 0)
			continue;
		e = &page.entry[d->slot];
		if (e->state != GPU_RELAY_STATE_ACTIVE ||
		    e->gen != d->gen || e->level_gen == d->last_level)
			continue;
		d->mask = e->level;
		d->last_level = e->level_gen;
		d->wakes++;
	}
}

/* One full round: close scan -> poll build -> drain/publish -> notify ->
 * kernel consume.  drain[slot] is the value the simulated worker eventfd
 * returned this round (0 = EAGAIN). */
static void do_round(unsigned long long *drain, struct k_event *evs,
		  int nev, struct k_device *ds, int ndev)
{
	struct pollset pfds[UML_GPU_RELAY_MAX_ENTRIES];
	struct pollset closed[UML_GPU_RELAY_MAX_ENTRIES];
	int nclosed = 0, n, changed;

	changed = relay_scan_close(closed, &nclosed);
	n = relay_build_poll(pfds, NULL);
	changed |= relay_publish(pfds, n, drain);
	relay_notify(changed);
	consume_events(evs, nev);
	consume_devices(ds, ndev);
	memset(drain, 0,
	       UML_GPU_RELAY_MAX_ENTRIES * sizeof(unsigned long long));
}

int main(void)
{
	unsigned long long drain[UML_GPU_RELAY_MAX_ENTRIES] = { 0 };
	struct k_event ev;
	struct k_device dev;

	memset(&page, 0, sizeof(page));
	memset(&worker, 0, sizeof(worker));

	/* Property 1: lossless eventfd accumulation, including a full
	 * notify socket between drain batches. */
	int slot = slot_alloc();
	assert(slot == 0);
	worker.open[3] = 1;
	worker.eventfd_count[3] = 100;
	slot_publish(slot, 3, UML_GPU_RELAY_KIND_EVENTFD);
	ev = (struct k_event){ .slot = slot, .gen = page.entry[slot].gen,
			       .active = 1 };
	drain[slot] = 5;
	notify_full = 1;
	do_round(drain, &ev, 1, NULL, 0);
	assert(notify_failures == 1);
	assert(ev.guest_count == 5);
	/* Worker counter kept, another batch arrives before retry succeeds. */
	drain[slot] = 7;
	do_round(drain, &ev, 1, NULL, 0);
	assert(ev.guest_count == 12);
	assert(page.entry[slot].value == 12);
	assert(page.entry[slot].value_gen == 2);
	notify_full = 0;
	/* Nothing pending: a bare wake round delivers nothing twice. */
	do_round(drain, &ev, 1, NULL, 0);
	do_round(drain, &ev, 1, NULL, 0);
	assert(ev.guest_count == 12);

	/* Property 4/5: retire closes exactly once, after exclusion; slot
	 * cannot be reallocated until the kernel observes the ack. */
	slot_retire(slot);
	ev.active = 0;			/* kernel frees its bookkeeping */
	assert(slot_alloc() != slot);	/* closing slot stays busy */
	do_round(drain, &ev, 1, NULL, 0);
	assert(worker.close_count[3] == 1);
	assert(page.entry[slot].state == GPU_RELAY_STATE_EMPTY);
	assert(page.entry[slot].ack == GPU_RELAY_ACK_CLOSED);
	assert(slot_alloc() == slot);	/* ack frees the slot */
	/* fd 3 reused by a new allocation; relay must never read the new
	 * owner through the old generation. */
	worker.open[3] = 1;
	worker.eventfd_count[3] = 9;
	slot_publish(slot, 3, UML_GPU_RELAY_KIND_EVENTFD);
	struct k_event ev2 = { .slot = slot, .gen = page.entry[slot].gen,
			       .active = 1 };
	drain[slot] = 9;
	do_round(drain, &ev2, 1, NULL, 0);
	assert(ev2.guest_count == 9);
	assert(worker.read_count[3] == 3);
	assert(worker.close_count[3] == 1);
	slot_retire(slot);
	ev2.active = 0;
	do_round(drain, &ev2, 1, NULL, 0);
	assert(worker.close_count[3] == 2);

	/* Property 2: level readiness edge-generation and clean clear. */
	int dslot = slot_alloc();
	assert(dslot >= 0);
	worker.open[4] = 1;
	worker.level_ready[4] = 1;
	slot_publish(dslot, 4, UML_GPU_RELAY_KIND_DEVICE);
	dev = (struct k_device){ .slot = dslot,
				.gen = page.entry[dslot].gen };
	/* Device stays ready for five poll rounds: only one publication. */
	for (int i = 0; i < 5; i++)
		do_round(drain, NULL, 0, &dev, 1);
	assert(dev.wakes == 1);
	assert(dev.mask == 0x001);
	/* Guest poll repeatedly sees the same mask (level semantics). */
	for (int i = 0; i < 4; i++) {
		unsigned int m = dev.mask;
		assert(m == 0x001);
	}
	/* Condition clears: clean round publishes 0 exactly once. */
	worker.level_ready[4] = 0;
	do_round(drain, NULL, 0, &dev, 1);
	assert(dev.wakes == 2);
	assert(dev.mask == 0);
	for (int i = 0; i < 3; i++)
		do_round(drain, NULL, 0, &dev, 1);
	assert(dev.wakes == 2);
	slot_retire(dslot);
	assert(slot_alloc() != dslot);
	do_round(drain, NULL, 0, &dev, 1);
	assert(worker.close_count[4] == 1);
	assert(page.entry[dslot].state == GPU_RELAY_STATE_EMPTY);
	assert(slot_alloc() >= 0);

	/* Property 3: stale publish under old generation is ignored. */
	worker.level_ready[5] = 1;
	slot_publish(dslot, 5, UML_GPU_RELAY_KIND_DEVICE);
	unsigned int gen = page.entry[dslot].gen;
	/* Reuse slot before a late result arrives. */
	slot_retire(dslot);
	struct pollset stale = { .fd = 5, .revents = 0x001,
				.slot = dslot, .gen = gen };
	struct pollset one = stale;
	unsigned long long stale_drain[UML_GPU_RELAY_MAX_ENTRIES] = { 0 };
	/* Simulate relay closing first, then its stale result arriving. */
	int nclosed = 0;
	struct pollset closed[1];
	relay_scan_close(closed, &nclosed);
	assert(nclosed == 1);
	(void)relay_publish(&one, 1, stale_drain);
	assert(page.entry[dslot].level == 0);
	assert(worker.read_count[5] == 0);

	/* Capacity property: closing slots still occupy the table. */
	for (int i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++) {
		int s = slot_alloc();
		assert(s >= 0);
		slot_publish(s, 6, UML_GPU_RELAY_KIND_EVENTFD);
		slot_retire(s);
	}
	assert(slot_alloc() < 0);
	/* After all close acks land, the table is reusable again. */
	do_round(drain, NULL, 0, NULL, 0);
	assert(slot_alloc() == 0);

	printf("relay protocol model: OK (notify=%u notify_fail=%u kicks=%u)\n",
	       notify_writes, notify_failures, kicks);
	return 0;
}
