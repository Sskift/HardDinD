// SPDX-License-Identifier: GPL-2.0
/*
 * faultaround_semantics_v2 - semantic/accounting checks for the
 * exploratory UML anonymous faultaround window (uml_faultaround=0..5).
 *
 * Output protocol: stdout is one JSON object per line: exactly one
 * {"case":...,"result":"pass|fail|skip",...} per case plus a final
 * summary.  Exit 0 only when no case failed (skips allowed).
 *
 * --selfcheck deliberately emits one skip and one fail and exits 3, so
 * an external harness can prove skip/fail emission without any kernel
 * feature being involved.
 *
 * Bounded design: every trapping scenario runs in its OWN child with a
 * parent waitpid timeout plus SIGKILL cleanup; a hang can never be
 * reported as a pass.  This binary validates semantics only when run
 * INSIDE the candidate guest; a host run is harness validation, not
 * kernel evidence.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/userfaultfd.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
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
#include <time.h>
#include <unistd.h>

#define CHILD_TRAP	42	/* SIGSEGV/SIGBUS delivered as expected */
#define CHILD_SETUP	2	/* child could not set the scenario up */

static long page_size;
static int case_pass, case_fail, case_skip;

static void json_escape(FILE *f, const char *s)
{
	for (; *s; s++) {
		unsigned char c = (unsigned char)*s;

		switch (c) {
		case '"': fputs("\\\"", f); continue;
		case '\\': fputs("\\\\", f); continue;
		case '\n': fputs("\\n", f); continue;
		case '\r': fputs("\\r", f); continue;
		case '\t': fputs("\\t", f); continue;
		default:
			if (c < 0x20)
				fprintf(f, "\\u%04x", c);
			else
				fputc((int)c, f);
		}
	}
}

/* tail is a pre-formed JSON fragment (without leading object open). */
static void report(const char *name, const char *result, const char *tail, ...)
{
	va_list ap;

	fputs("{\"case\":\"", stdout);
	json_escape(stdout, name);
	fputs("\",\"result\":\"", stdout);
	json_escape(stdout, result);
	fputc('"', stdout);
	if (tail && *tail) {
		fputc(',', stdout);
		va_start(ap, tail);
		vprintf(tail, ap);
		va_end(ap);
	}
	puts("}");
	fflush(stdout);

	if (!strcmp(result, "pass"))
		case_pass++;
	else if (!strcmp(result, "skip"))
		case_skip++;
	else
		case_fail++;
}

static unsigned long minflt_now(void)
{
	struct rusage r;

	if (getrusage(RUSAGE_SELF, &r))
		return 0;
	return (unsigned long)r.ru_minflt;
}

static long minor_delta(unsigned long before)
{
	return (long)minflt_now() - (long)before;
}

/* Fork a bounded child.  Returns waitpid status; -1 fork error,
 * -2 timed out (child killed and reaped). */
static int bounded_fork(void (*child)(void), int timeout_ms)
{
	pid_t pid = fork();

	if (pid < 0)
		return -1;
	if (pid == 0) {
		child();
		_exit(CHILD_SETUP);
	}
	for (;;) {
		int status = 0;
		pid_t r;
		struct timespec ts = { .tv_sec = 0, .tv_nsec = 10000000 };

		r = waitpid(pid, &status, WNOHANG);
		if (r == pid)
			return status;
		if (r < 0)
			return -1;
		if (timeout_ms <= 0) {
			kill(pid, SIGKILL);
			waitpid(pid, &status, 0);
			return -2;
		}
		nanosleep(&ts, NULL);
		timeout_ms -= 10;
	}
}

static int reap_timeout(pid_t pid, int timeout_ms)
{
	for (;;) {
		int status = 0;
		pid_t r;

		r = waitpid(pid, &status, WNOHANG);
		if (r == pid)
			return status;
		if (r < 0)
			return -1;
		if (timeout_ms <= 0) {
			kill(pid, SIGKILL);
			waitpid(pid, &status, 0);
			return -2;
		}
		usleep(10000);
		timeout_ms -= 10;
	}
}

static bool exited_with(const int status, const int code)
{
	return WIFEXITED(status) && WEXITSTATUS(status) == code;
}

static void trap_handler(int signo)
{
	(void)signo;
	_exit(CHILD_TRAP);
}

static void install_trap_handler(void)
{
	struct sigaction sa = { .sa_handler = trap_handler };

	sigemptyset(&sa.sa_mask);
	sigaction(SIGSEGV, &sa, NULL);
	sigaction(SIGBUS, &sa, NULL);
}

/* ---------------- dense per-page first touch ------------------------- */

static void case_dense(void)
{
	const unsigned pages = 128;
	size_t bytes = pages * page_size;
	unsigned char *m;
	unsigned long before;
	long delta;
	unsigned i, j;
	bool ok = true;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED) {
		report("dense", "fail", "\"errno\":%d", errno);
		return;
	}
	before = minflt_now();
	for (i = 0; i < pages; i++)
		m[i * page_size] = (unsigned char)(i + 1);
	delta = minor_delta(before);
	if (delta < 1 || delta > (long)pages + 1)
		ok = false;
	for (i = 0; i < pages; i++) {
		if (m[i * page_size] != (unsigned char)(i + 1))
			ok = false;
		for (j = 1; j < (unsigned)page_size; j++)
			if (m[i * page_size + j] != 0) {
				ok = false;
				break;
			}
	}
	munmap(m, bytes);
	report("dense", ok ? "pass" : "fail",
	       "\"pages\":%u,\"minflt\":%ld", pages, delta);
}

/* ---------------- one touched page per window ------------------------ */

static void case_sparse_zero(void)
{
	const unsigned windows = 16, window_pages = 32;
	const unsigned pages = windows * window_pages;
	size_t bytes = pages * page_size;
	unsigned char *m;
	unsigned long before;
	long delta;
	unsigned w, i;
	bool ok = true;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED) {
		report("sparse_zero", "fail", "\"errno\":%d", errno);
		return;
	}
	before = minflt_now();
	for (w = 0; w < windows; w++)
		m[(w * window_pages) * page_size] = (unsigned char)(w + 100);
	delta = minor_delta(before);
	if (delta < (long)windows || delta > (long)pages + 1)
		ok = false;
	for (w = 0; w < windows; w++) {
		unsigned idx = w * window_pages;

		if (m[idx * page_size] != (unsigned char)(w + 100))
			ok = false;
		for (i = 0; i < window_pages; i++) {
			unsigned p = w * window_pages + i;

			if (p != idx && m[p * page_size] != 0)
				ok = false;
		}
	}
	munmap(m, bytes);
	report("sparse_zero", ok ? "pass" : "fail",
	       "\"touched\":%u,\"pages\":%u,\"minflt\":%ld",
	       windows, pages, delta);
}

/* -------- residency policy difference, REPORTED not assumed ---------- */

static void case_sparse_residency(void)
{
	const unsigned window_pages = 32;
	size_t bytes = window_pages * page_size;
	unsigned char *m, *res;
	unsigned resident = 0, i;
	bool ok;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED) {
		report("sparse_residency", "fail", "\"errno\":%d", errno);
		return;
	}
	res = calloc(window_pages, 1);
	if (!res) {
		munmap(m, bytes);
		report("sparse_residency", "fail", "\"reason\":\"calloc\"");
		return;
	}
	m[0] = 0x42;
	if (mincore(m, bytes, res) != 0) {
		free(res);
		munmap(m, bytes);
		report("sparse_residency", "skip",
		       "\"reason\":\"mincore\",\"errno\":%d", errno);
		return;
	}
	for (i = 0; i < window_pages; i++)
		if (res[i] & 1)
			resident++;
	/* Touched page must be resident; nothing outside the window can be.
	 * Off -> 1, on order>=5 -> 32, smaller orders -> their size. The
	 * owner matrix compares off vs on with this number. */
	ok = (res[0] & 1) && resident >= 1 && resident <= window_pages;
	free(res);
	munmap(m, bytes);
	report("sparse_residency", ok ? "pass" : "fail",
	       "\"resident_pages\":%u,\"window_pages\":%u,"
	       "\"speculative_residency\":%s",
	       resident, window_pages,
	       resident > 1 ? "true" : "false");
}

/* ---------------- zero-page reads then sparse writes ----------------- */

static void case_read_then_write(void)
{
	const unsigned pages = 128;
	size_t bytes = pages * page_size;
	unsigned char *m;
	unsigned i;
	long sum = 0;
	bool ok = true;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED) {
		report("read_then_write", "fail", "\"errno\":%d", errno);
		return;
	}
	for (i = 0; i < pages; i++)
		sum += m[i * page_size];
	if (sum != 0)
		ok = false;
	for (i = 0; i < pages; i += 3)
		m[i * page_size] = (unsigned char)(i + 5);
	for (i = 0; i < pages; i++) {
		unsigned char want = (i % 3 == 0) ?
			(unsigned char)(i + 5) : 0;

		if (m[i * page_size] != want)
			ok = false;
	}
	munmap(m, bytes);
	report("read_then_write", ok ? "pass" : "fail", "");
}

/* ---------------- fork COW parent isolation -------------------------- */

static unsigned char *cow_m;
static unsigned cow_pages;

static void cow_child(void)
{
	unsigned i;

	for (i = 0; i < cow_pages; i++)
		cow_m[i * page_size] = (unsigned char)(0xa0 + i);
	for (i = 0; i < cow_pages; i++)
		if (cow_m[i * page_size] != (unsigned char)(0xa0 + i))
			_exit(1);
	_exit(0);
}

static void case_fork_cow(void)
{
	const unsigned pages = 128;
	size_t bytes = pages * page_size;
	unsigned char *m;
	unsigned i;
	int status;
	bool ok = true;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED) {
		report("fork_cow", "fail", "\"errno\":%d", errno);
		return;
	}
	for (i = 0; i < pages; i++)
		m[i * page_size] = (unsigned char)(i + 1);
	cow_m = m;
	cow_pages = pages;
	status = bounded_fork(cow_child, 15000);
	if (!exited_with(status, 0))
		ok = false;
	for (i = 0; i < pages; i++)
		if (m[i * page_size] != (unsigned char)(i + 1))
			ok = false;
	munmap(m, bytes);
	report("fork_cow", ok ? "pass" : "fail", "\"status\":%d", status);
}

/* -------- separate children: PROT_READ and PROT_NONE ----------------- */

static void ro_child(void)
{
	const unsigned pages = 8;
	size_t bytes = pages * page_size;
	unsigned char *m;

	install_trap_handler();
	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED)
		_exit(CHILD_SETUP);
	memset(m, 0x33, bytes);
	if (mprotect(m + page_size, page_size, PROT_READ) != 0)
		_exit(CHILD_SETUP);
	m[page_size] = 1;	/* must trap; never returns */
	_exit(1);
}

static void none_child(void)
{
	const unsigned pages = 8;
	size_t bytes = pages * page_size;
	unsigned char *m;
	volatile unsigned char sink;

	install_trap_handler();
	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED)
		_exit(CHILD_SETUP);
	memset(m, 0x44, bytes);
	if (mprotect(m + 3 * page_size, page_size, PROT_NONE) != 0)
		_exit(CHILD_SETUP);
	sink = m[3 * page_size];	/* READ must trap; never returns */
	(void)sink;
	m[3 * page_size] = 1;	/* WRITE must trap */
	_exit(1);
}

static void case_mprotect(void)
{
	int ro = bounded_fork(ro_child, 10000);
	int none = bounded_fork(none_child, 10000);

	report("mprotect_ro",
	       exited_with(ro, CHILD_TRAP) ? "pass" : "fail",
	       "\"status\":%d", ro);
	report("mprotect_protnone",
	       exited_with(none, CHILD_TRAP) ? "pass" : "fail",
	       "\"status\":%d", none);
}

/* ------------- munmap mid-window; neighbours survive ----------------- */

static void unmap_trap_child(void)
{
	const unsigned pages = 8;
	size_t bytes = pages * page_size;
	unsigned char *m;

	install_trap_handler();
	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED)
		_exit(CHILD_SETUP);
	memset(m, 0x55, bytes);
	if (munmap(m + 2 * page_size, page_size) != 0)
		_exit(CHILD_SETUP);
	m[2 * page_size] = 2;	/* must trap */
	_exit(1);
}

static void case_munmap_mid(void)
{
	const unsigned pages = 8;
	size_t bytes = pages * page_size;
	unsigned char *m;
	unsigned i;
	int status;
	bool ok = true;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED) {
		report("munmap_mid", "fail", "\"errno\":%d", errno);
		return;
	}
	memset(m, 0x55, bytes);
	if (munmap(m + 2 * page_size, page_size) != 0) {
		report("munmap_mid", "fail", "\"reason\":\"munmap\"");
		munmap(m, bytes);
		return;
	}
	status = bounded_fork(unmap_trap_child, 10000);
	if (!exited_with(status, CHILD_TRAP))
		ok = false;
	/* Neighbours of the hole stay valid and untouched in this mm. */
	for (i = 0; i < pages; i++)
		if (i != 2 && m[i * page_size] != 0x55)
			ok = false;
	munmap(m, bytes);
	report("munmap_mid", ok ? "pass" : "fail", "\"status\":%d", status);
}

/* ---------------- pthread interleaved first touch -------------------- */

struct thread_arg {
	unsigned char *m;
	unsigned pages, tid, nthreads;
};

static void *thread_worker(void *opaque)
{
	struct thread_arg *a = opaque;
	unsigned i;

	for (i = a->tid; i < a->pages; i += a->nthreads)
		a->m[i * page_size] = (unsigned char)(i + 1);
	return NULL;
}

static void case_threads(void)
{
	enum { N = 4 };
	const unsigned pages = 256;
	size_t bytes = pages * page_size;
	pthread_t th[N];
	struct thread_arg args[N];
	unsigned created = 0, i;
	bool ok = true;
	unsigned char *m;

	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED) {
		report("threads", "fail", "\"errno\":%d", errno);
		return;
	}
	for (i = 0; i < N; i++) {
		int rc;

		args[i].m = m;
		args[i].pages = pages;
		args[i].tid = i;
		args[i].nthreads = N;
		rc = pthread_create(&th[i], NULL, thread_worker, &args[i]);
		if (rc != 0)
			break;	/* join only what was created */
		created++;
	}
	for (i = 0; i < created; i++)
		if (pthread_join(th[i], NULL) != 0)
			ok = false;
	if (created != N)
		ok = false;
	else
		for (i = 0; i < pages; i++)
			if (m[i * page_size] != (unsigned char)(i + 1))
				ok = false;
	munmap(m, bytes);
	report("threads", ok ? "pass" : "fail",
	       "\"created\":%u,\"requested\":%d", created, N);
}

/* ---------------- userfaultfd exclusion ------------------------------ */

struct uffd_pos {
	int uffd;
	unsigned char *m;
	int notifyfd;
};

static int uffd_open(void)
{
	return (int)syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
}

static bool uffd_api_ok(int fd)
{
	struct uffdio_api api;

	memset(&api, 0, sizeof(api));
	api.api = UFFD_API;
	return ioctl(fd, UFFDIO_API, &api) == 0;
}

/* Negative fixtures, all bounded; each failure is recorded explicitly. */
static void case_uffd_negatives(void)
{
	int fd, evidence = 0;
	struct uffdio_api bad;
	char ch;
	ssize_t r;

	fd = uffd_open();
	if (fd < 0 && (errno == EPERM || errno == ENOSYS)) {
		report("uffd_negatives", "skip",
		       "\"reason\":\"unavailable\",\"errno\":%d", errno);
		return;
	}
	if (fd < 0 || !uffd_api_ok(fd)) {
		report("uffd_negatives", "fail", "\"reason\":\"api\"");
		return;
	}
	close(fd);

	/* 1) bogus API version must fail with EINVAL, not succeed. */
	fd = uffd_open();
	if (fd < 0) {
		report("uffd_negatives", "fail", "\"reason\":\"fd2\"");
		return;
	}
	memset(&bad, 0, sizeof(bad));
	bad.api = 0xdeadbeefUL;
	if (ioctl(fd, UFFDIO_API, &bad) == 0 || errno != EINVAL)
		evidence = 1;
	close(fd);

	/* 2) read() on an idle NONBLOCK uffd must give EAGAIN, not block. */
	fd = uffd_open();
	if (fd < 0 || !uffd_api_ok(fd)) {
		report("uffd_negatives", "fail", "\"reason\":\"fd3\"");
		return;
	}
	r = read(fd, &ch, 1);
	if (!(r < 0 && errno == EAGAIN))
		evidence = 2;
	close(fd);

	/* 3) a registered range nobody touches must time out poll() in a
	 *    bounded child (exit 2); proves the resolver timeout path. */
	fd = uffd_open();
	if (fd >= 0 && uffd_api_ok(fd)) {
		unsigned char *nm;
		struct uffdio_register reg;

		nm = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
			  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (nm != MAP_FAILED) {
			memset(&reg, 0, sizeof(reg));
			reg.range.start = (unsigned long)nm;
			reg.range.len = page_size;
			reg.mode = UFFDIO_REGISTER_MODE_MISSING;
			if (ioctl(fd, UFFDIO_REGISTER, &reg) == 0) {
				pid_t p = fork();

				if (p == 0) {
					struct pollfd pfd = {
						.fd = fd, .events = POLLIN
					};
					int pr = poll(&pfd, 1, 300);

					_exit(pr == 0 ? 2 : 21);
				}
				if (p > 0) {
					int st = reap_timeout(p, 5000);

					if (!exited_with(st, 2))
						evidence = 3;
				} else {
					evidence = 4;
				}
			}
			munmap(nm, page_size);
		}
	}
	close(fd);

	report("uffd_negatives", evidence ? "fail" : "pass",
	       "\"evidence\":%d", evidence);
}

static int resolver_fn(void *opaque)
{
	struct uffd_pos *p = opaque;
	struct pollfd pfd = { .fd = p->uffd, .events = POLLIN };
	struct uffd_msg msg;
	struct uffdio_zeropage zp;
	char done = 'r';
	int pr;
	ssize_t n;

	pr = poll(&pfd, 1, 5000);
	if (pr != 1)
		_exit(20);
	memset(&msg, 0, sizeof(msg));
	n = read(p->uffd, &msg, sizeof(msg));
	if (n != (ssize_t)sizeof(msg))
		_exit(21);
	if (msg.event != UFFD_EVENT_PAGEFAULT)
		_exit(22);
	if ((unsigned char *)(uintptr_t)msg.arg.pagefault.address != p->m)
		_exit(23);
	if (!(msg.arg.pagefault.flags & UFFD_PAGEFAULT_FLAG_WRITE))
		_exit(24);
	memset(&zp, 0, sizeof(zp));
	zp.range.start = (unsigned long)p->m;
	zp.range.len = page_size;
	if (ioctl(p->uffd, UFFDIO_ZEROPAGE, &zp) < 0)
		_exit(25);
	if (write(p->notifyfd, &done, 1) != 1)
		_exit(26);
	return 0;
}

static int faulter_fn(void *opaque)
{
	struct uffd_pos *p = opaque;
	char done = 'f';

	p->m[0] = 0x11;		/* blocks until resolver zero-fills */
	if (write(p->notifyfd, &done, 1) != 1)
		_exit(30);
	return 0;
}

static pid_t clone_vm(int (*fn)(void *), void *arg, void **stack_out)
{
	const size_t stk_size = 1 << 16;
	char *stk = mmap(NULL, stk_size, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS | MAP_STACK, -1, 0);

	if (stk == MAP_FAILED)
		return -1;
	*stack_out = stk;
	return clone(fn, stk + stk_size, CLONE_VM | SIGCHLD, arg);
}

static void case_uffd_positive(void)
{
	size_t bytes = 128 * (size_t)page_size;
	unsigned char *m = MAP_FAILED;
	int fd = -1, pipefd[2] = { -1, -1 };
	pid_t resolver = -1, faulter = -1;
	void *rstk = NULL, *fstk = NULL;
	struct uffd_pos pos;
	struct uffdio_register reg;
	struct pollfd pfd;
	int pr = -1, fstat = -1, rstat = -1;
	unsigned char marker = 0;
	bool ok = false;

	fd = uffd_open();
	if (fd < 0 && (errno == EPERM || errno == ENOSYS)) {
		report("userfaultfd", "skip",
		       "\"reason\":\"unavailable\",\"errno\":%d", errno);
		return;
	}
	if (fd < 0 || !uffd_api_ok(fd)) {
		report("userfaultfd", "fail", "\"reason\":\"api\"");
		return;
	}
	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED || pipe(pipefd) != 0) {
		report("userfaultfd", "fail", "\"reason\":\"setup\"");
		goto out;
	}
	memset(&reg, 0, sizeof(reg));
	reg.range.start = (unsigned long)m;
	reg.range.len = bytes;
	reg.mode = UFFDIO_REGISTER_MODE_MISSING;
	if (ioctl(fd, UFFDIO_REGISTER, &reg) != 0) {
		report("userfaultfd", "fail", "\"reason\":\"register\"");
		goto out;
	}
	pos.uffd = fd;
	pos.m = m;
	pos.notifyfd = pipefd[1];
	resolver = clone_vm(resolver_fn, &pos, &rstk);
	if (resolver < 0) {
		report("userfaultfd", "skip",
		       "\"reason\":\"clone_vm\",\"errno\":%d", errno);
		goto out;
	}
	usleep(20000);		/* let the resolver arm poll() */
	faulter = clone_vm(faulter_fn, &pos, &fstk);
	if (faulter < 0) {
		kill(resolver, SIGKILL);
		reap_timeout(resolver, 1000);
		report("userfaultfd", "fail", "\"reason\":\"clone_fault\"");
		goto out;
	}

	pfd.fd = pipefd[0];
	pfd.events = POLLIN;
	pr = poll(&pfd, 1, 10000);
	if (pr != 1 || read(pipefd[0], &marker, 1) != 1 || marker != 'f') {
		/* Bounded: never leave children blocking, never call a hang
		 * a pass. */
		kill(faulter, SIGKILL);
		kill(resolver, SIGKILL);
		reap_timeout(faulter, 1000);
		reap_timeout(resolver, 1000);
		report("userfaultfd", "fail",
		       "\"reason\":\"timeout_or_marker\",\"poll\":%d,"
		       "\"marker\":%u", pr, marker);
		goto out;
	}
	fstat = reap_timeout(faulter, 5000);
	rstat = reap_timeout(resolver, 5000);
	/* ZEROPAGE released the write fault; the store itself then
	 * completed, so m[0] must equal the written value, not zero. */
	ok = exited_with(fstat, 0) && exited_with(rstat, 0) &&
	     m[0] == 0x11;
	report("userfaultfd", ok ? "pass" : "fail",
	       "\"faulter_status\":%d,\"resolver_status\":%d,"
	       "\"content\":%u,\"expected\":17", fstat, rstat, m[0]);
out:
	if (fstk)
		munmap(fstk, 1 << 16);
	if (rstk)
		munmap(rstk, 1 << 16);
	if (m != MAP_FAILED)
		munmap(m, bytes);
	if (pipefd[0] >= 0)
		close(pipefd[0]);
	if (pipefd[1] >= 0)
		close(pipefd[1]);
	if (fd >= 0)
		close(fd);
}

/* --------- memcg: SIGKILL plus memory.events oom_kill proof ----------- */

#define MEMCG_PATH "/sys/fs/cgroup/fa-semantics-v2"

static void memcg_child(void)
{
	const size_t bytes = 64UL * 1024 * 1024;
	volatile unsigned char *m;
	int fd;
	char me[32];
	size_t i;

	fd = open(MEMCG_PATH "/memory.max", O_WRONLY);
	if (fd < 0 || write(fd, "8M", 2) != 2)
		_exit(CHILD_SETUP);
	close(fd);
	fd = open(MEMCG_PATH "/cgroup.procs", O_WRONLY);
	if (fd < 0)
		_exit(CHILD_SETUP);
	snprintf(me, sizeof(me), "%d", (int)getpid());
	if (write(fd, me, strlen(me)) != (ssize_t)strlen(me))
		_exit(CHILD_SETUP);
	close(fd);
	m = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED)
		_exit(7);
	for (i = 0; i < bytes; i += page_size)
		m[i] = (unsigned char)(i * 131 + 7);
	_exit(0);
}

static long read_oom_kill(void)
{
	FILE *f = fopen(MEMCG_PATH "/memory.events", "r");
	long oom = -1;
	char key[64];
	long val;

	if (!f)
		return -1;
	while (fscanf(f, "%63s %ld\n", key, &val) == 2)
		if (!strcmp(key, "oom_kill"))
			oom = val;
	fclose(f);
	return oom;
}

static void case_memcg_oom(void)
{
	int status;
	long before, after;
	char peak[64] = "unavailable";
	FILE *f;
	const char *result;

	if (mkdir(MEMCG_PATH, 0755) != 0) {
		report("memcg_oom", "skip",
		       "\"reason\":\"mkdir\",\"errno\":%d", errno);
		return;
	}
	before = read_oom_kill();
	status = bounded_fork(memcg_child, 30000);
	after = read_oom_kill();
	f = fopen(MEMCG_PATH "/memory.peak", "r");
	if (f) {
		if (fgets(peak, sizeof(peak), f)) {
			char *nl = strchr(peak, '\n');

			if (nl)
				*nl = 0;
		}
		fclose(f);
	}

	if (WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL &&
	    before >= 0 && after > before)
		result = "pass";
	else if (WIFEXITED(status) && WEXITSTATUS(status) == CHILD_SETUP)
		result = "skip";
	else
		result = "fail";	/* incl. plain ENOMEM exit: not OOM proof */
	report("memcg_oom", result,
	       "\"signaled\":%s,\"term_signal\":%d,\"exit_code\":%d,"
	       "\"oom_before\":%ld,\"oom_after\":%ld,\"memory_peak\":\"%s\"",
	       WIFSIGNALED(status) ? "true" : "false",
	       WIFSIGNALED(status) ? WTERMSIG(status) : -1,
	       WIFEXITED(status) ? WEXITSTATUS(status) : -1,
	       before, after, peak);

	/* No false success from stale state: cgroup must be removable. */
	if (rmdir(MEMCG_PATH) == 0 || errno == ENOENT)
		report("memcg_cleanup", "pass", "");
	else
		report("memcg_cleanup", "fail", "\"errno\":%d", errno);
}

/* ---------------- emission self-check -------------------------------- */

static void selfcheck(void)
{
	report("selfcheck_skip", "skip", "\"deliberate\":true");
	report("selfcheck_fail", "fail", "\"deliberate\":true");
	printf("{\"case\":\"summary\",\"pass\":%d,\"fail\":%d,\"skip\":%d,"
	       "\"result\":\"fail\"}\n", case_pass, case_fail, case_skip);
	exit(3);
}

int main(int argc, char **argv)
{
	page_size = sysconf(_SC_PAGESIZE);
	if (page_size <= 0)
		page_size = 4096;
	if (argc == 2 && !strcmp(argv[1], "--selfcheck"))
		selfcheck();

	case_dense();
	case_sparse_zero();
	case_sparse_residency();
	case_read_then_write();
	case_fork_cow();
	case_mprotect();
	case_munmap_mid();
	case_threads();
	case_uffd_negatives();
	case_uffd_positive();
	case_memcg_oom();

	printf("{\"case\":\"summary\",\"pass\":%d,\"fail\":%d,\"skip\":%d,"
	       "\"result\":\"%s\"}\n",
	       case_pass, case_fail, case_skip,
	       case_fail ? "fail" : "pass");
	return case_fail ? 1 : 0;
}
