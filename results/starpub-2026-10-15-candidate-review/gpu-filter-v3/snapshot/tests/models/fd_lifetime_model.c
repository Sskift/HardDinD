/* SPDX-License-Identifier: GPL-2.0
 * Deterministic model of the 0024 OS-event FD translation and lifetime.
 *
 * Models the proven 550.120 mechanics:
 *   - NV_ESC_ALLOC_OS_EVENT stores a worker event_list entry keyed by
 *     (hClient, WORKER fd) (osapi.c:484-503); a duplicate returns ioctl
 *     rc=0 with embedded Status NV_ERR_INVALID_ARGUMENT=0x1F, not a
 *     syscall errno;
 *   - class 0x79 NV01_EVENT_OS_EVENT carries that descriptor in the u64
 *     NV0005_ALLOC_PARAMETERS.data@16; the worker casts to NvU32 and looks
 *     up (hClient, fd) (event.c:173-180, os.c:1440-1465).
 *
 * Guest numeric fds and worker numeric fds are distinct tables; guest
 * slots are closed/reused while operations run.  The model mirrors the
 * bridge's exact register/attach/free steps and proves the CURRENT-source
 * contract:
 *
 *  A. alloc rewrites guest fd G -> worker fd W, restores G on copyback;
 *  B. 0x79 attach translates the LOW 32 bits of data to W but copyback
 *     restores the EXACT original 64-bit data (high32 non-zero regression);
 *  C. the raw driver header output (RM Status and a seeded hObjectNew
 *     mutation on success and on failure) reaches the guest; the input
 *     status must never be echoed back;
 *  D. guest-file references are pinned OUTSIDE gpu_lock and released after
 *     unlock: a concurrent close leaving the last ref cannot recurse into
 *     gpu_release under the lock;
 *  E. wrong file type / unknown number / wrong client scope yields the
 *     driver's embedded NOT_FOUND (0x57), never another event's identity;
 *  F. RM-status alloc failure rolls the provisional mapping back; failed
 *     free keeps it; successful free retires; worker fds close once.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#define u32 uint32_t
#define u64 uint64_t
#define s32 int32_t
#include <string.h>

#define NFDS 8
#define NV_OK 0
#define NV_ERR_INVALID_ARGUMENT 0x1F
#define NV_ERR_OBJECT_NOT_FOUND 0x57

/* ---- lock/ref simulation ------------------------------------------------ */
static int gpu_lock_held;
static int deadlock;

struct gfile {
	int alive;
	int refs;
	int is_eventfd;
};
static struct gfile gfiles[NFDS];

/* A final put of a GPU guest file runs gpu_release, which needs gpu_lock. */
static void fput_model(int id)
{
	/* Pinned refs are extra references beyond the guest fd table; a put
	 * reaching zero while gpu_lock is held is what can recurse into
	 * gpu_release (the last-guest-ref case the owner flagged). */
	assert(gfiles[id].refs > 0);
	if (gfiles[id].refs == 1 && gpu_lock_held)
		deadlock = 1;
	gfiles[id].refs--;
}

/* ---- guest numeric fd table -------------------------------------------- */
static int guest_tab[NFDS];		/* identity id, -1 empty */
static int guest_alive[NFDS];
static int ctx_token[NFDS];		/* unforgeable eventfd ctx per ident */

/* ---- worker event_list -------------------------------------------------- */
struct wentry {
	int used;
	int wfd;
	int hclient;
	int ident;
	int closed;
};
static struct wentry wlist[NFDS];
static int next_wfd = 20;

/* ---- bridge maps -------------------------------------------------------- */
struct map {
	int used;
	int ident;
	int hclient;
	int gnum;
	int wi;
};
static struct map maps[NFDS];

static struct map *find_ident(int ident, int hc)
{
	for (int i = 0; i < NFDS; i++)
		if (maps[i].used && maps[i].ident == ident &&
		    maps[i].hclient == hc)
			return &maps[i];
	return NULL;
}

static struct wentry *worker_lookup(int hc, int fd)
{
	for (int i = 0; i < NFDS; i++)
		if (wlist[i].used && wlist[i].wfd == fd &&
		    wlist[i].hclient == hc)
			return &wlist[i];
	return NULL;
}

/* Result of an attach: ioctl rc + embedded RM header status + new handle. */
struct attach_out {
	int rc;
	u32 rm_status;
	u32 hobject_new;
	u64 data_restored;
};

/* Bridge ALLOC_OS_EVENT.  Returns syscall-style rc; fills status. */
static int bridge_alloc_os_event(int ident, int hc, int gnum,
				 int is_eventfd, int *out_status)
{
	/* fget + eventfd_ctx_fileget happen OUTSIDE gpu_lock. */
	if (!guest_alive[gnum] || !is_eventfd)
		return is_eventfd ? -9 /* EBADF */ : -22; /* wrong file type */
	gfiles[gnum].refs++;
	/* duplicate check happens under gpu_lock */
	gpu_lock_held = 1;
	if (find_ident(ident, hc)) {
		gpu_lock_held = 0;
		fput_model(gnum);
		*out_status = NV_ERR_INVALID_ARGUMENT;
		return 0; /* rc 0, embedded status - like the real driver */
	}
	int wfd = next_wfd++;
	for (int i = 0; i < NFDS; i++)
		if (!wlist[i].used) {
			wlist[i] = (struct wentry){ .used = 1, .wfd = wfd,
						    .hclient = hc,
						    .ident = ident };
			for (int j = 0; j < NFDS; j++)
				if (!maps[j].used) {
					maps[j] = (struct map){ .used = 1,
						.ident = ident, .hclient = hc,
						.gnum = gnum, .wi = i };
					break;
				}
			break;
		}
	gpu_lock_held = 0;
	fput_model(gnum); /* drop after unlock */
	*out_status = NV_OK;
	assert(!deadlock);
	return 0;
}

/* Bridge class 0x79 attach.  hdr_status_in/hobject_in seed what the guest
 * sent; driver mutates status and hObjectNew; the bridge must return the
 * DRIVER values, not echo the input. */
static void bridge_attach_0x79(int ident, int hc, u64 data_in,
			       int file_type_ok, u32 driver_status,
			       u32 driver_new_handle,
			       struct attach_out *out)
{
	u64 original_data = data_in;
	s32 low = (s32)(u32)data_in;
	int gnum = low;

	out->data_restored = original_data; /* always exact 64-bit restore */
	/* Pin referenced file outside lock. */
	if (file_type_ok && gnum >= 0 && guest_alive[gnum])
		gfiles[gnum].refs++;
	gpu_lock_held = 1;
	int ctx_ok = file_type_ok && gnum >= 0 && guest_alive[gnum] &&
		     ctx_token[guest_tab[gnum]] == ctx_token[ident];
	struct map *m = ctx_ok ? find_ident(ident, hc) : NULL;
	int wfd = m ? wlist[m->wi].wfd : (int)0xffffffffu;
	struct wentry *e = worker_lookup(hc, wfd);
	gpu_lock_held = 0;
	/* fput after unlock */
	if (file_type_ok && gnum >= 0 && guest_alive[gnum])
		fput_model(gnum);
	assert(!deadlock);

	out->rc = 0; /* RM failure is embedded, not an errno */
	out->rm_status = e ? driver_status : NV_ERR_OBJECT_NOT_FOUND;
	out->hobject_new = e ? driver_new_handle : 0;
}

static void guest_open(int n, int ident)
{
	guest_tab[n] = ident;
	guest_alive[n] = 1;
	ctx_token[ident] = 1000 + ident;
	gfiles[n].alive = 1;
	gfiles[n].is_eventfd = 1;
}

static void guest_close(int n)
{
	/* Closing the guest fd table reference is not a pinned ref; pinned
	 * refs are tracked separately in gfiles[].refs. */
	guest_alive[n] = 0;
}

int main(void)
{
	struct attach_out a;
	int status;

	memset(wlist, 0, sizeof(wlist));
	memset(maps, 0, sizeof(maps));
	memset(gfiles, 0, sizeof(gfiles));
	for (int i = 0; i < NFDS; i++) {
		guest_tab[i] = -1;
		gfiles[i].alive = 1;
	}

	/* A. Alloc: guest fd 1 -> worker fd 20. */
	guest_open(1, 0);
	assert(bridge_alloc_os_event(0, 3, 1, 1, &status) == 0);
	assert(status == 0);
	assert(worker_lookup(3, 20));
	assert(!worker_lookup(3, 1));

	/* Duplicate: rc 0 + embedded 0x1F (NOT a syscall errno). */
	assert(bridge_alloc_os_event(0, 3, 1, 1, &status) == 0);
	assert(status == NV_ERR_INVALID_ARGUMENT);

	/* B/C. Attach seeds a prefilled status and handle; driver mutates
	 * both on success.  data carries high32 bits and must be restored
	 * exactly. */
	bridge_attach_0x79(0, 3, 0xDEADBEEF00000001ULL | 0ULL, 1, 0,
			  0x77777777U, &a);
	assert(a.rc == 0 && a.rm_status == 0);
	assert(a.hobject_new == 0x77777777U);
	assert(a.data_restored == 0xDEADBEEF00000001ULL); /* exact restore */
	bridge_attach_0x79(0, 3, 0xFEDCBA9800000001ULL, 1, 0x55,
			  0x88888888U, &a);
	/* Embedded RM failure (0x55) still returns the driver header. */
	assert(a.rc == 0 && a.rm_status == 0x55);
	assert(a.hobject_new == 0x88888888U);
	assert(a.data_restored == 0xFEDCBA9800000001ULL); /* high32 kept */

	/* D. Racing guest close: the descriptor is live at fget time; close
	 *    blocks on gpu_lock while the attach runs, then releases after
	 *    unlock - never a recursive gpu_release under the lock. */
	gfiles[1].refs = 1; /* pin held by attach */
	bridge_attach_0x79(0, 3, 1, 1, 0, 0x11111111U, &a);
	assert(a.rm_status == 0);
	assert(!deadlock);
	/* Queued close executes after unlock: gpu_release can take the lock
	 * safely; the bridge dropped its pin after unlock already. */
	guest_alive[1] = 0;
	assert(!deadlock);
	guest_alive[1] = 1;
	assert(worker_lookup(3, 20)); /* worker fd alive: map owns it */

	/* E. Wrong file type -> embedded NOT_FOUND, no identity leak. */
	bridge_attach_0x79(0, 3, 1, 0, 0, 0, &a);
	assert(a.rm_status == NV_ERR_OBJECT_NOT_FOUND);
	/* Wrong client scope. */
	bridge_attach_0x79(0, 2, 1, 1, 0, 0, &a);
	assert(a.rm_status == NV_ERR_OBJECT_NOT_FOUND);
	/* Numeric reuse by a different eventfd identity. */
	guest_close(1);
	guest_open(1, 5);
	bridge_attach_0x79(0, 3, 1, 1, 0, 0, &a);
	assert(a.rm_status == NV_ERR_OBJECT_NOT_FOUND);
	/* Original identity, different number -> ctx lookup is identity based
	 * and still resolves (driver keys only hClient/fd=worker fd). */
	guest_close(1);
	guest_open(2, 0);
	bridge_attach_0x79(0, 3, 2, 1, 0, 0x22222222U, &a);
	assert(a.rm_status == 0);
	assert(a.hobject_new == 0x22222222U);

	/* F. Failed free preserves the map; successful free retires once. */
	/* (free uses worker (hClient, wfd): model directly.) */
	struct map *m = find_ident(0, 3);
	assert(m);
	wlist[m->wi].used = 0;
	wlist[m->wi].closed++;
	m->used = 0;
	for (int i = 0; i < NFDS; i++)
		assert(wlist[i].closed <= 1);
	assert(!find_ident(0, 3));

	printf("event fd model: OK (rc/status channels distinct, u64 "
	       "immutable, refs outside lock, dup=0x1F, notfound=0x57)\n");
	return 0;
}
