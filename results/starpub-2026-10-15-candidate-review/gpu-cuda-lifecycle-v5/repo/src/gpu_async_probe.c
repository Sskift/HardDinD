/* HardDinD 0017 schedulable GPU wait probe.
 *
 * Functional evidence for the asynchronous GPU worker wait:
 *   - every NV_ESC_RM_ALLOC/FREE roundtrip through the bridge still succeeds
 *     (transport correctness is unchanged);
 *   - while the calling task is blocked inside the GPU ioctl path, a sibling
 *     task on the same guest keeps taking timer ticks.  On a uniprocessor
 *     guest (ncpus=1) the 0016 local_irq_save handoff stalls the ticker for
 *     the whole host ioctl; with gpu_async_wait=1 the ticker must keep
 *     running because the waiter sleeps in the guest scheduler.
 *
 * With gpu_async_debug=2 the worker injects a bounded ~0.1 s host-side stall
 * before each ioctl, which makes the contrast deterministic without needing
 * a long-blocking CUDA command (those are still outside the ioctl allowlist).
 * This is not CUDA acceptance and not a performance benchmark.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdatomic.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#if !defined(__x86_64__)
#error The pinned NVIDIA NVOS ABI layout below is validated for x86_64 only.
#endif

#define RM_REQUEST_ALLOC 0x2b
#define RM_REQUEST_FREE  0x29
#define RM_IOCTL(nr, size) _IOC(_IOC_READ | _IOC_WRITE, 'F', (nr), (size))

#define NV01_ROOT 0x00000000U
#define NV_OK     0x00000000U

struct nvos00 {
	uint32_t hRoot;
	uint32_t hObjectParent;
	uint32_t hObjectOld;
	uint32_t status;
};

struct nvos64 {
	uint32_t hRoot;
	uint32_t hObjectParent;
	uint32_t hObjectNew;
	uint32_t hClass;
	void *pAllocParms;
	void *pRightsRequested;
	uint32_t paramsSize;
	uint32_t flags;
	uint32_t status;
};

_Static_assert(sizeof(struct nvos00) == 16, "NVOS00 is 16 bytes");
_Static_assert(sizeof(struct nvos64) == 48, "NVOS64 is 48 bytes");

/* A gap above this while an ioctl is in flight means the vCPU stalled.
 * 50 ms cleanly separates scheduler-grade ticks (~2 ms) from the ~100 ms
 * injected worker stall; it is deliberately not a latency guarantee. */
#define MAX_GAP_US_WARN 50000
#define TICK_PERIOD_NS 2000000

static int failed;

static void check(const char *name, int ok)
{
	printf("GPU_ASYNC_CHECK %s %s\n", name, ok ? "pass" : "fail");
	if (!ok)
		failed = 1;
}

static long now_us(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

struct ticker {
	pthread_t thread;
	atomic_int stop;
	atomic_int ready;
	long max_gap_us;
	long ticks;
};

static void *tick_thread(void *arg)
{
	struct ticker *t = arg;
	struct timespec next;
	long previous = now_us();

	clock_gettime(CLOCK_MONOTONIC, &next);
	atomic_store(&t->ready, 1);
	while (!atomic_load(&t->stop)) {
		long current;

		next.tv_nsec += TICK_PERIOD_NS;
		if (next.tv_nsec >= 1000000000) {
			next.tv_nsec -= 1000000000;
			next.tv_sec++;
		}
		while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL) == EINTR) {}
		current = now_us();
		if (current - previous > t->max_gap_us)
			t->max_gap_us = current - previous;
		previous = current;
		t->ticks++;
	}
	return NULL;
}

static int rm_alloc_root(int fd, uint32_t *handle)
{
	struct nvos64 args;

	memset(&args, 0, sizeof(args));
	args.hClass = NV01_ROOT;
	if (ioctl(fd, RM_IOCTL(RM_REQUEST_ALLOC, sizeof(args)), &args) < 0)
		return -errno;
	if (args.status != NV_OK || args.hObjectNew == 0)
		return -1;
	*handle = args.hObjectNew;
	return 0;
}

static int rm_free(int fd, uint32_t handle)
{
	struct nvos00 args;

	memset(&args, 0, sizeof(args));
	args.hRoot = handle;
	args.hObjectParent = handle;
	args.hObjectOld = handle;
	if (ioctl(fd, RM_IOCTL(RM_REQUEST_FREE, sizeof(args)), &args) < 0)
		return -errno;
	return args.status == NV_OK ? 0 : -1;
}

int main(int argc, char **argv)
{
	int iterations, fd, i, ops_ok = 1;
	struct ticker ticker = { .max_gap_us = 0 };
	long start;

	if (argc != 3) {
		fprintf(stderr, "usage: gpu-async-probe CTL_DEVICE ITERATIONS\n");
		return 2;
	}
	iterations = atoi(argv[2]);
	if (iterations < 1 || iterations > 1000)
		return 2;
	setvbuf(stdout, NULL, _IOLBF, 0);

	fd = open(argv[1], O_RDWR | O_CLOEXEC);
	check("open_control", fd >= 0);
	if (fd < 0)
		return 1;

	if (pthread_create(&ticker.thread, NULL, tick_thread, &ticker))
		return 2;
	while (!atomic_load(&ticker.ready))
		sched_yield();
	printf("GPU_ASYNC_PHASE begin\n");
	start = now_us();
	for (i = 0; i < iterations; i++) {
		uint32_t handle = 0;

		if (rm_alloc_root(fd, &handle) != 0 || rm_free(fd, handle) != 0) {
			printf("GPU_ASYNC_INFO roundtrip_failed i=%d errno=%d\n",
			       i, errno);
			ops_ok = 0;
			break;
		}
	}
	atomic_store(&ticker.stop, 1);
	pthread_join(ticker.thread, NULL);

	printf("GPU_ASYNC_INFO iterations=%d ioctl_roundtrips=%d ticks=%ld "
	       "max_tick_gap_us=%ld ioctl_phase_ms=%ld\n",
	       i, i * 2, ticker.ticks, ticker.max_gap_us,
	       (now_us() - start) / 1000);
	check("rm_alloc_free_roundtrips", ops_ok);
	/* Interpret with ncpus=1; the gap is the scheduling evidence. */
	check("scheduler_tick_steady", ticker.ticks > iterations
	      && ticker.max_gap_us < MAX_GAP_US_WARN);

	close(fd);
	printf("GPU_ASYNC_END status=%d internal_cuda_verified=0\n", failed);
	return failed;
}
