// SPDX-License-Identifier: GPL-2.0
/*
 * faultaround_semantics - semantic preservation checks for the
 * exploratory UML anonymous faultaround window (uml_faultaround=<order>).
 *
 * The optimization changes WHEN ordinary anonymous pages are resolved,
 * not their contents or protections; it deliberately moves residency,
 * RSS/memcg charging and minor-fault accounting earlier (THP-like).
 * This program exercises:
 *   - zero fill and minor-fault bounds (dense + sparse one-page/window)
 *   - read-before-write zero page -> private COW conversion
 *   - fork()/COW isolation
 *   - mprotect(PROT_READ/PROT_NONE) and munmap, including mid-window
 *   - multi-thread interleaved first touch
 *   - userfaultfd exclusion (best effort; skipped if unavailable)
 *   - a tight memcg limit in a forked child (best effort; skipped if the
 *     harness cannot create cgroups)
 *
 * Run with uml_faultaround=0 and =1..5; every non-skipped check must
 * pass.  Exit 0 on pass, 1 on failure.  Pure POSIX/Linux, no GPU.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/userfaultfd.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static int failures;
static int skips;

#define NOTE(...) do { printf(__VA_ARGS__); printf("\n"); fflush(stdout); } while (0)
#define CHECK(cond, ...) do {						\
	if (!(cond)) {							\
		printf("{\"check_fail\": \"%s\"", #cond);		\
		printf(__VA_ARGS__);					\
		printf("}\n"); fflush(stdout);				\
		failures++;						\
	} } while (0)

static long page_size(void)
{
	long p = sysconf(_SC_PAGESIZE);

	return p > 0 ? p : 4096;
}

static long minor_delta(const struct rusage *before)
{
	struct rusage after;

	if (getrusage(RUSAGE_SELF, &after))
		return -1;
	return after.ru_minflt - before->ru_minflt;
}

static void expect_segv(int signo)
{
	(void)signo;
	_exit(42);
}

/* Dense per-page first touch: contents, zero fill, fault accounting. */
static void test_dense(long ps, unsigned pages)
{
	unsigned char *m;
	struct rusage before;
	size_t bytes = (size_t)pages * ps;
	long delta;
	unsigned i, j;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(m != MAP_FAILED, "{\"case\":\"dense_mmap\"}");
	if (m == MAP_FAILED)
		return;

	getrusage(RUSAGE_SELF, &before);
	for (i = 0; i < pages; i++)
		m[i * ps] = (unsigned char)(i + 1);
	delta = minor_delta(&before);
	/* At least one real fault, never more pages than the mapping. */
	CHECK(delta >= 1 && delta <= (long)pages + 1,
	      ",{\"case\":\"dense_minflt\",\"minflt\":%ld,\"pages\":%u}",
	      delta, pages);
	NOTE("{\"info\":\"dense\",\"pages\":%u,\"minflt\":%ld}", pages, delta);
	for (i = 0; i < pages; i++) {
		CHECK(m[i * ps] == (unsigned char)(i + 1),
		      ",{\"case\":\"dense_value\",\"page\":%u}", i);
		for (j = 1; j < (unsigned)ps; j++) {
			if (m[i * ps + j] != 0) {
				CHECK(0, ",{\"case\":\"dense_zero\",\"page\":%u,"
				      "\"off\":%u}", i, j);
				break;
			}
		}
	}
	munmap(m, bytes);
}

/*
 * Sparse: touch one page per 128 KiB window (order 4 default) across
 * several windows.  Touched pages keep correct distinct values, every
 * other page still reads zero, and faults stay bounded by the mapping.
 */
static void test_sparse(long ps)
{
	static const unsigned windows = 16;
	static const unsigned window_pages = 32; /* order 5 worst case */
	unsigned pages = windows * window_pages;
	unsigned char *m;
	size_t bytes = (size_t)pages * ps;
	struct rusage before;
	long delta;
	unsigned w, i;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(m != MAP_FAILED, "{\"case\":\"sparse_mmap\"}");
	if (m == MAP_FAILED)
		return;

	getrusage(RUSAGE_SELF, &before);
	for (w = 0; w < windows; w++)
		m[(w * window_pages) * ps] = (unsigned char)(w + 100);
	delta = minor_delta(&before);
	CHECK(delta >= (long)windows && delta <= (long)pages + 1,
	      ",{\"case\":\"sparse_minflt\",\"minflt\":%ld,\"touched\":%u,"
	      "\"pages\":%u}", delta, windows, pages);
	NOTE("{\"info\":\"sparse\",\"touched\":%u,\"minflt\":%ld}",
	     windows, delta);
	for (w = 0; w < windows; w++) {
		unsigned idx = w * window_pages;

		CHECK(m[idx * ps] == (unsigned char)(w + 100),
		      ",{\"case\":\"sparse_value\",\"window\":%u}", w);
		for (i = 0; i < window_pages; i++) {
			unsigned p = w * window_pages + i;

			if (p == idx)
				continue;
			if (m[p * ps] != 0) {
				CHECK(0, ",{\"case\":\"sparse_zero_ro\","
				      "\"page\":%u}", p);
				break;
			}
		}
	}
	munmap(m, bytes);
}

/* Read faults map the zero page; later writes must private-COW it. */
static void test_read_then_write(long ps, unsigned pages)
{
	unsigned char *m;
	size_t bytes = (size_t)pages * ps;
	unsigned i;
	long sum = 0;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(m != MAP_FAILED, "{\"case\":\"rw_mmap\"}");
	if (m == MAP_FAILED)
		return;
	for (i = 0; i < pages; i++)
		sum += m[i * ps];
	CHECK(sum == 0, "{\"case\":\"zero_page_reads\"}");

	/* Sparse writes through the former zero-page mappings. */
	for (i = 0; i < pages; i += 3)
		m[i * ps] = (unsigned char)(i + 5);
	for (i = 0; i < pages; i++) {
		unsigned char want = (i % 3 == 0) ?
			(unsigned char)(i + 5) : 0;

		CHECK(m[i * ps] == want,
		      ",{\"case\":\"rw_values\",\"page\":%u,\"got\":%u}",
		      i, m[i * ps]);
	}
	munmap(m, bytes);
}

/* Fork COW: child rewrites the window; parent stays unchanged. */
static void test_fork_cow(long ps, unsigned pages)
{
	unsigned char *m;
	size_t bytes = (size_t)pages * ps;
	pid_t pid;
	int status;
	unsigned i;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(m != MAP_FAILED, "{\"case\":\"fork_mmap\"}");
	if (m == MAP_FAILED)
		return;
	for (i = 0; i < pages; i++)
		m[i * ps] = (unsigned char)(i + 1);

	pid = fork();
	CHECK(pid >= 0, "{\"case\":\"fork\"}");
	if (pid == 0) {
		for (i = 0; i < pages; i++)
			m[i * ps] = (unsigned char)(0xa0 + i);
		for (i = 0; i < pages; i++)
			if (m[i * ps] != (unsigned char)(0xa0 + i))
				_exit(1);
		_exit(0);
	}
	waitpid(pid, &status, 0);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0,
	      ",{\"case\":\"child\",\"status\":%d}", status);
	for (i = 0; i < pages; i++)
		CHECK(m[i * ps] == (unsigned char)(i + 1),
		      ",{\"case\":\"parent_unchanged\",\"page\":%u,\"got\":%u}",
		      i, m[i * ps]);
	munmap(m, bytes);
}

/* mprotect RO/NONE mid-window and munmap mid-window. */
static void test_protect_unmap(long ps, unsigned pages)
{
	unsigned char *m;
	size_t bytes = (size_t)pages * ps;
	pid_t pid;
	int status, pipefd[2];
	unsigned i;
	char token;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(m != MAP_FAILED, "{\"case\":\"prot_mmap\"}");
	if (m == MAP_FAILED)
		return;
	for (i = 0; i < pages; i++)
		m[i * ps] = (unsigned char)(i + 7);

	CHECK(mprotect(m + ps, ps, PROT_READ) == 0,
	      "{\"case\":\"mprotect_ro\"}");
	CHECK(mprotect(m + 3 * ps, ps, PROT_NONE) == 0,
	      "{\"case\":\"mprotect_none\"}");
	CHECK(pipe(pipefd) == 0, "{\"case\":\"pipe\"}");

	pid = fork();
	if (pid == 0) {
		struct sigaction sa = {0};

		close(pipefd[0]);
		sa.sa_handler = expect_segv;
		sigemptyset(&sa.sa_mask);
		sigaction(SIGSEGV, &sa, NULL);
		m[ps] = 1;		/* RO -> must trap */
		m[3 * ps] = 1;		/* NONE -> must trap */
		if (write(pipefd[1], "x", 1) < 0)
			_exit(1);
		_exit(0);
	}
	close(pipefd[1]);
	waitpid(pid, &status, 0);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 42,
	      ",{\"case\":\"protect_traps\",\"status\":%d}", status);
	CHECK(read(pipefd[0], &token, 1) <= 0,
	      "{\"case\":\"protect_no_continue\"}");
	close(pipefd[0]);

	/* Restore and unmap a page in the middle of a window. */
	CHECK(mprotect(m + 3 * ps, ps, PROT_READ | PROT_WRITE) == 0,
	      "{\"case\":\"mprotect_restore\"}");
	m[3 * ps] = 0x55;
	CHECK(munmap(m + 2 * ps, ps) == 0, "{\"case\":\"munmap_mid\"}");
	pid = fork();
	if (pid == 0) {
		struct sigaction sa = {0};

		sa.sa_handler = expect_segv;
		sigemptyset(&sa.sa_mask);
		sigaction(SIGSEGV, &sa, NULL);
		m[2 * ps] = 2;
		_exit(0);
	}
	waitpid(pid, &status, 0);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 42,
	      ",{\"case\":\"munmap_traps\",\"status\":%d}", status);
	CHECK(m[ps + 1] == 0, "{\"case\":\"ro_page_zero_rest\"}");
	CHECK(m[3 * ps] == 0x55, "{\"case\":\"none_restored\"}");
	munmap(m, bytes);
}

struct thread_arg {
	unsigned char *m;
	unsigned pages;
	unsigned tid;
	unsigned nthreads;
};

static void *thread_worker(void *opaque)
{
	struct thread_arg *a = opaque;
	unsigned i;

	for (i = a->tid; i < a->pages; i += a->nthreads)
		a->m[i * page_size()] = (unsigned char)(i + 1);
	return NULL;
}

static void test_concurrent(long ps, unsigned pages)
{
	enum { NTHREADS = 4 };
	pthread_t th[NTHREADS];
	struct thread_arg args[NTHREADS];
	unsigned char *m;
	size_t bytes = (size_t)pages * ps;
	unsigned i, t;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(m != MAP_FAILED, "{\"case\":\"thread_mmap\"}");
	if (m == MAP_FAILED)
		return;
	for (t = 0; t < NTHREADS; t++) {
		args[t].m = m;
		args[t].pages = pages;
		args[t].tid = t;
		args[t].nthreads = NTHREADS;
		CHECK(pthread_create(&th[t], NULL, thread_worker,
				     &args[t]) == 0,
		      ",{\"case\":\"pthread_create\",\"t\":%u}", t);
	}
	for (t = 0; t < NTHREADS; t++)
		CHECK(pthread_join(th[t], NULL) == 0,
		      ",{\"case\":\"pthread_join\",\"t\":%u}", t);
	for (i = 0; i < pages; i++)
		CHECK(m[i * ps] == (unsigned char)(i + 1),
		      ",{\"case\":\"thread_value\",\"page\":%u,\"got\":%u}",
		      i, m[i * ps]);
	munmap(m, bytes);
}

/*
 * userfaultfd exclusion: with an armed missing uffd on the VMA, a write
 * must be handled by uffd (block until resolved) and faultaround's VMA
 * gate excludes the region.  Best effort: SKIP if userfaultfd is denied.
 */
static void test_uffd(long ps)
{
	long uffd;
	struct uffdio_api api;
	struct uffdio_register reg;
	struct uffdio_zeropage zp;
	unsigned char *m;
	size_t bytes = 128 * ps;
	pid_t pid;
	int status, pf[2];
	struct {
		unsigned char ev;
		unsigned char pad1[7];
		unsigned long long flags;
		unsigned long long address;
		union { unsigned long long n[3]; } arg;
	} msg;
	struct pollfd pfd;
	char done;

	uffd = syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
	if (uffd < 0 && (errno == EPERM || errno == ENOSYS)) {
		NOTE("{\"skip\":\"userfaultfd_unavailable\",\"errno\":%d}",
		     errno);
		skips++;
		return;
	}
	CHECK(uffd >= 0, ",{\"case\":\"uffd_open\",\"errno\":%d}", errno);
	if (uffd < 0)
		return;
	memset(&api, 0, sizeof(api));
	api.api = UFFD_API;
	if (ioctl(uffd, UFFDIO_API, &api) < 0) {
		NOTE("{\"skip\":\"userfaultfd_api\",\"errno\":%d}", errno);
		close(uffd);
		skips++;
		return;
	}
	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(m != MAP_FAILED, "{\"case\":\"uffd_mmap\"}");
	if (m == MAP_FAILED)
		return;
	memset(&reg, 0, sizeof(reg));
	reg.range.start = (unsigned long)m;
	reg.range.len = bytes;
	reg.mode = UFFDIO_REGISTER_MODE_MISSING;
	CHECK(ioctl(uffd, UFFDIO_REGISTER, &reg) == 0,
	      ",{\"case\":\"uffd_register\",\"errno\":%d}", errno);

	CHECK(pipe(pf) == 0, "{\"case\":\"uffd_pipe\"}");
	pid = fork();
	if (pid == 0) {
		close(pf[0]);
		memset(&pfd, 0, sizeof(pfd));
		pfd.fd = uffd;
		pfd.events = POLLIN;
		if (poll(&pfd, 1, 5000) != 1 ||
		    read(uffd, &msg, sizeof(msg)) != (ssize_t)sizeof(msg))
			_exit(4);
		memset(&zp, 0, sizeof(zp));
		zp.range.start = msg.address & ~(unsigned long)(ps - 1);
		zp.range.len = ps;
		if (ioctl(uffd, UFFDIO_ZEROPAGE, &zp) < 0)
			_exit(5);
		if (write(pf[1], "o", 1) != 1)
			_exit(6);
		_exit(0);
	}
	close(pf[1]);
	m[0] = 0x11;	/* blocks until the parent resolves it */
	CHECK(read(pf[0], &done, 1) == 1 && done == 'o',
	      "{\"case\":\"uffd_resolved\"}");
	CHECK(m[0] == 0, "{\"case\":\"uffd_zero_content\"}");
	waitpid(pid, &status, 0);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0,
	      ",{\"case\":\"uffd_handler\",\"status\":%d}", status);
	close(pf[0]);
	close(uffd);
	munmap(m, bytes);
}

/*
 * Tight memcg limit in a forked child (cgroup v2).  Only checks that the
 * limit is enforced; exact kill timing is the documented THP-like
 * difference the owner matrix compares.  SKIP without cgroup access.
 */
static void test_memcg_limit(long ps)
{
	const char *path = "/sys/fs/cgroup/fa-semantics-faultaround";
	pid_t pid;
	int status;

	if (mkdir(path, 0755) < 0) {
		NOTE("{\"skip\":\"memcg_no_mkdir\",\"errno\":%d}", errno);
		skips++;
		return;
	}
	pid = fork();
	if (pid == 0) {
		int fd;
		volatile unsigned char *m;
		size_t bytes = 64UL * 1024 * 1024, i;
		char me[32];
		int n;

		fd = open("/sys/fs/cgroup/fa-semantics-faultaround/memory.max",
			  O_WRONLY);
		if (fd < 0 || write(fd, "8M", 2) != 2)
			_exit(2);
		close(fd);
		fd = open("/sys/fs/cgroup/fa-semantics-faultaround/cgroup.procs",
			  O_WRONLY);
		if (fd < 0)
			_exit(2);
		n = snprintf(me, sizeof(me), "%d", getpid());
		if (write(fd, me, n) != n)
			_exit(2);
		close(fd);
		m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (m == MAP_FAILED)
			_exit(2);
		for (i = 0; i < bytes; i += ps)
			m[i] = (unsigned char)i;
		_exit(0);
	}
	waitpid(pid, &status, 0);
	if (WIFEXITED(status) && WEXITSTATUS(status) == 2) {
		NOTE("{\"skip\":\"memcg_not_enforceable\"}");
		skips++;
	} else {
		CHECK(WIFSIGNALED(status) ||
		      (WIFEXITED(status) && WEXITSTATUS(status) != 0),
		      ",{\"case\":\"memcg_enforced\",\"status\":%d}", status);
		NOTE("{\"info\":\"memcg_child\",\"signaled\":%d,\"exit\":%d}",
		     WIFSIGNALED(status),
		     WIFEXITED(status) ? WEXITSTATUS(status) : -1);
	}
	rmdir(path);
}

int main(void)
{
	long ps = page_size();
	unsigned pages = 128;	/* 512 KiB: spans several windows */

	printf("{\"test\":\"faultaround_semantics\",\"page_size\":%ld,"
	       "\"pages\":%u}\n", ps, pages);
	fflush(stdout);
	test_dense(ps, pages);
	test_sparse(ps);
	test_read_then_write(ps, pages);
	test_fork_cow(ps, pages);
	test_protect_unmap(ps, pages);
	test_concurrent(ps, pages);
	test_uffd(ps);
	test_memcg_limit(ps);
	if (failures) {
		printf("{\"result\":\"FAIL\",\"failures\":%d,\"skips\":%d}\n",
		       failures, skips);
		return 1;
	}
	printf("{\"result\":\"PASS\",\"skips\":%d}\n", skips);
	return 0;
}
