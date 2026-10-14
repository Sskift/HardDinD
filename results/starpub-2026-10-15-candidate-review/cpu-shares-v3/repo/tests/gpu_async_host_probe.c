/* Host-side stress harness for the 0017 GPU worker completion protocol.
 *
 * Reproduces the user-space half of the kernel<->worker handoff with the
 * same primitives (the real stub-futex phase word and an AF_UNIX SOCK_DGRAM
 * pair for SCM-less completion bytes), then ping-pongs as fast as possible:
 *
 *   kernel: gpu_async=1; exchange word to IN_CHILD; FUTEX_WAKE;
 *           block until one completion datagram; read result; repeat
 *   worker: wait until IN_CHILD; store result; exchange word to IN_KERN
 *           (park); claim gpu_async; send completion byte; repeat
 *
 * The byte must be sent only AFTER parking in IN_KERN: otherwise the next
 * submission's IN_CHILD exchange can be clobbered by the worker's park
 * exchange and both sides deadlock.  This test fails fast (watchdog) on a
 * lost wakeup and validates result ordering on every roundtrip.
 *
 * It is a protocol model, not a kernel build: no UML internals, no GPU.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include <linux/futex.h>

#include "stub-futex.h"

#define ITERATIONS 50000

static unsigned int futex_word;
static unsigned char gpu_async;
static long shared_result;
static int socks[2];

static long futex_op(unsigned int *addr, int op, unsigned int val,
		     const struct timespec *to)
{
	return syscall(SYS_futex, addr, op, val, to, NULL, 0);
}

/* Mirrors wait_stub_done_seccomp's wait loop: sleep while the word is in
 * @phase; return as soon as it changes. */
static void wait_while_phase(unsigned int phase)
{
	while (stub_futex_phase(&futex_word) == phase) {
		if (!stub_futex_prepare_wait(&futex_word, phase))
			break;
		(void)futex_op(&futex_word, FUTEX_WAIT,
			       phase | STUB_FUTEX_WAITING, NULL);
	}
}

static void *worker(void *arg)
{
	(void)arg;
	for (long i = 1; i <= ITERATIONS; i++) {
		char done = 0;

		/* Wait for a submitted batch parked in IN_CHILD. */
		wait_while_phase(FUTEX_IN_KERN);

		/* Execute the "ioctl": publish result. */
		atomic_store_explicit((_Atomic long *)&shared_result, i,
				      memory_order_release);

		/* Park before notifying: the ordering 0017 depends on. */
		if (stub_futex_handoff(&futex_word, FUTEX_IN_KERN))
			(void)futex_op(&futex_word, FUTEX_WAKE, 1, NULL);

		if (atomic_exchange_explicit((_Atomic unsigned char *)&gpu_async,
					     0, memory_order_acq_rel)) {
			ssize_t n;
			do {
				n = send(socks[1], &done, 1, 0);
			} while (n < 0 && errno == EINTR);
			if (n != 1) {
				fprintf(stderr, "worker send failed: %s\n",
					strerror(errno));
				exit(1);
			}
		}
	}
	return NULL;
}

int main(void)
{
	pthread_t t;
	long expect = 0;

	if (socketpair(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC,
		       0, socks)) {
		perror("socketpair");
		return 1;
	}
	/* Kernel end blocks in recv (as the guest IRQ wait blocks in
	 * schedule()); the 2 s receive timeout is the lost-wakeup watchdog. */
	if (fcntl(socks[0], F_SETFL, 0) < 0) {
		perror("fcntl");
		return 1;
	}
	{
		struct timeval watchdog = { .tv_sec = 2 };
		if (setsockopt(socks[0], SOL_SOCKET, SO_RCVTIMEO,
			       &watchdog, sizeof(watchdog)) < 0) {
			perror("setsockopt");
			return 1;
		}
	}
	futex_word = FUTEX_IN_KERN;
	if (pthread_create(&t, NULL, worker, NULL))
		return 1;

	for (long i = 1; i <= ITERATIONS; i++) {
		char byte;
		ssize_t n;

		/* Submission (stub_submit_async ordering). */
		atomic_store_explicit((_Atomic unsigned char *)&gpu_async, 1,
				      memory_order_release);
		if (stub_futex_handoff(&futex_word, FUTEX_IN_CHILD))
			(void)futex_op(&futex_word, FUTEX_WAKE, 1, NULL);

		/* Wait for the completion datagram (guest IRQ equivalent).
		 * SO_RCVTIMEO turns a lost wakeup into a reported failure. */
		do {
			n = recv(socks[0], &byte, 1, 0);
		} while (n < 0 && errno == EINTR);
		if (n != 1) {
			fprintf(stderr, "lost completion at %ld: %s\n", i,
				strerror(errno));
			return 1;
		}
		long got = atomic_load_explicit((_Atomic long *)&shared_result,
						memory_order_acquire);
		if (got != i) {
			fprintf(stderr, "ordering at %ld: got %ld\n", i, got);
			return 1;
		}
		expect = i;
	}
	pthread_join(t, NULL);
	if (expect != ITERATIONS)
		return 1;
	printf("gpu_async_host_probe: %d ordered roundtrips, no lost wakeup\n",
	       ITERATIONS);
	return 0;
}
