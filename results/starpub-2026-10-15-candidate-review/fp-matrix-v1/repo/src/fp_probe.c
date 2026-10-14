// SPDX-License-Identifier: GPL-2.0
/*
 * fp_probe.c - round6 experiment 0018 guest FP/XSTATE + GP regression probe.
 *
 * Static, pthread. Every case emits one JSON line:
 *   {"test":"fp_probe","case":"...","ok":bool,"status":"pass|fail|skip",
 *    "detail":"..."}
 *
 * Exit codes: 0 all passed; 1 real failure; 2 one or more cases could not
 * run (INCOMPLETE - never a pass). Written for the owner to run inside the
 * guest under both seccomp_threads=0 and =1 and with/without lazyxstate=1;
 * a native run here is tooling sanity, never kernel validation.
 *
 * Coverage:
 *   gp-syscall         callee-saved GP regs (rbx,rbp,r12-r15) over syscalls
 *   gp-signal-edit     ucontext mcontext GP edits (incl. R12-R15)
 *   fp-preserve        x87/SSE over mixed + blocking syscalls
 *   fp-signal-preserve 100 self-directed handlers doing SSE work
 *   fp-signal-edit     ucontext fpstate mxcsr/xmm0 edits observed after
 *                      rt_sigreturn
 *   fp-nested          SA_NODEFER nested signal frames
 *   fp-threads         distinct images, affinity rotation/migration
 *   frame-churn        blocked parked owners + syscall storm
 *   thread-exit-churn  hundreds of short threads + RSS instrumentation
 *   runner-lifetime    short-lived fork/exit mm/runner lifecycle churn
 *   fork-fp            raw clone inheritance/independence
 *   exec-reset         mxcsr 0x1f80, fcw 0x37f, xmm zeroed after execve
 *   ptrace-gp-fp       NT_PRSTATUS GP writes (full and partial) and partial
 *                      NT_PRFPREG / NT_X86_XSTATE SETREGSET at syscall stops,
 *                      verified by the tracee after resume
 */
#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/ptrace.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#ifndef NT_X86_XSTATE
#define NT_X86_XSTATE 0x202
#endif
#ifndef PTRACE_GETREGSET
#define PTRACE_GETREGSET 0x4204
#define PTRACE_SETREGSET 0x4205
#endif

static int incomplete;
static int failures;
static unsigned ncpu;

static void emit(const char *name, int ok, const char *status, const char *fmt, ...)
{
	char detail[512];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(detail, sizeof(detail), fmt, ap);
	va_end(ap);
	for (char *p = detail; *p; p++)
		if (*p == '"' || *p == '\\' || (unsigned char)*p < 0x20)
			*p = ' ';
	printf("{\"test\":\"fp_probe\",\"case\":\"%s\",\"ok\":%s,\"status\":\"%s\","
	       "\"detail\":\"%s\"}\n", name, ok ? "true" : "false",
	       status, detail);
	fflush(stdout);
	if (!ok) {
		if (!strcmp(status, "skip"))
			incomplete = 1;
		else
			failures = 1;
	}
}
#define PASS(c, ...) emit((c), 1, "pass", __VA_ARGS__)
#define FAIL(c, ...) emit((c), 0, "fail", __VA_ARGS__)
#define SKIP(c, ...) emit((c), 0, "skip", __VA_ARGS__)

static long tgkill_self(int sig)
{
	return syscall(SYS_tgkill, getpid(), (long)syscall(SYS_gettid), sig);
}
static void tiny_nanosleep(void)
{
	struct timespec ts = { 0, 1000000 };
	asm volatile("syscall"::"a"(SYS_nanosleep),"D"(&ts),"S"(0)
		     : "rcx","r11","memory");
}

/* ---- x87/SSE state helpers --------------------------------------------- */

static unsigned get_mxcsr(void)
{
	unsigned v;

	asm("stmxcsr %0":"=m"(v));
	return v;
}

static unsigned short get_fcw(void)
{
	unsigned short v;

	asm("fnstcw %0":"=m"(v));
	return v;
}

#define LOAD_XMM(i) asm volatile("movdqu %0,%%xmm" #i ::"m"(xmm[(i)*16]))
#define STORE_XMM(i) asm volatile("movdqu %%xmm" #i ",%0":"=m"(xmm[(i)*16]))

static inline void load_all_xmm(const unsigned char xmm[256])
{
	LOAD_XMM(0); LOAD_XMM(1); LOAD_XMM(2); LOAD_XMM(3);
	LOAD_XMM(4); LOAD_XMM(5); LOAD_XMM(6); LOAD_XMM(7);
	LOAD_XMM(8); LOAD_XMM(9); LOAD_XMM(10); LOAD_XMM(11);
	LOAD_XMM(12); LOAD_XMM(13); LOAD_XMM(14); LOAD_XMM(15);
}
static inline void store_all_xmm(unsigned char xmm[256])
{
	STORE_XMM(0); STORE_XMM(1); STORE_XMM(2); STORE_XMM(3);
	STORE_XMM(4); STORE_XMM(5); STORE_XMM(6); STORE_XMM(7);
	STORE_XMM(8); STORE_XMM(9); STORE_XMM(10); STORE_XMM(11);
	STORE_XMM(12); STORE_XMM(13); STORE_XMM(14); STORE_XMM(15);
}
#undef LOAD_XMM
#undef STORE_XMM

/*
 * Fill a distinctive, hardware-valid x87+SSE image and FXSAVE it as the
 * reference for "bytes the frame must still hold after the syscall round".
 * Threads run concurrently, so the reference is thread-local.
 */
static _Thread_local unsigned char ref_fx[512] __attribute__((aligned(16)));

static void fill_state(unsigned seed)
{
	double d[8];
	unsigned char xmm[256] __attribute__((aligned(16)));
	unsigned short fcw;
	unsigned mxcsr;

	for (int i = 0; i < 8; i++)
		d[i] = (double)(seed + 1) * 1.0000001 + i * 0.25;
	for (unsigned i = 0; i < sizeof(xmm); i++)
		xmm[i] = (unsigned char)(seed * 131 + i * 7);
	/* Keep every exception mask bit set; vary only rounding control and
	 * the sticky status flags, which cannot raise. */
	fcw = (unsigned short)(0x037f | ((seed & 3) << 10));
	mxcsr = 0x1f80 | ((seed & 3) << 13) | ((seed >> 2) & 0x3f);
	asm volatile("fldcw %0"::"m"(fcw));
	asm volatile("ldmxcsr %0"::"m"(mxcsr));
	asm volatile(
		"fldl %0\n fldl %1\n fldl %2\n fldl %3\n"
		"fldl %4\n fldl %5\n fldl %6\n fldl %7\n"
		:: "m"(d[0]), "m"(d[1]), "m"(d[2]), "m"(d[3]),
		   "m"(d[4]), "m"(d[5]), "m"(d[6]), "m"(d[7]));
	load_all_xmm(xmm);
	asm volatile("fxsave64 %0"::"m"(ref_fx):"memory");
}

static int state_unchanged(const char *where)
{
	unsigned char now[512] __attribute__((aligned(16)));

	asm volatile("fxsave64 %0"::"m"(now):"memory");
	if (memcmp(now + 0, ref_fx + 0, 2) ||
	    memcmp(now + 24, ref_fx + 24, 4) ||
	    memcmp(now + 32, ref_fx + 32, 80) ||
	    memcmp(now + 160, ref_fx + 160, 256)) {
		FAIL("fp-preserve", "%s fxstate changed", where);
		return 0;
	}
	return 1;
}

/* ---- gp-syscall --------------------------------------------------------- */

static int case_gp_syscall(void)
{
	unsigned long out[6] = { 0 };
	const unsigned long v[6] = {
		0x1111111111111111UL, 0x2222222222222222UL,
		0x3333333333333333UL, 0x4444444444444444UL,
		0x5555555555555555UL, 0x6666666666666666UL,
	};
	int ok = 1;

	/*
	 * Both memory bases are staged into owned scratch registers BEFORE
	 * any tested GPR is touched: gcc may allocate those pointer operands
	 * in any of rbx/rbp/r12-r15.
	 */
	asm volatile(
		"mov %[v],%%r8\n mov %[o],%%r9\n"
		"push %%rbx\n push %%rbp\n push %%r12\n push %%r13\n"
		"push %%r14\n push %%r15\n"
		"mov 0(%%r8),%%rbx\n mov 8(%%r8),%%rbp\n"
		"mov 16(%%r8),%%r12\n mov 24(%%r8),%%r13\n"
		"mov 32(%%r8),%%r14\n mov 40(%%r8),%%r15\n"
		"mov $39,%%eax\n syscall\n"
		"mov $39,%%eax\n syscall\n"
		"mov $0,%%eax\n xor %%edi,%%edi\n xor %%esi,%%esi\n"
		"xor %%edx,%%edx\n syscall\n"
		"mov $39,%%eax\n syscall\n"
		"mov %%rbx,0(%%r9)\n mov %%rbp,8(%%r9)\n"
		"mov %%r12,16(%%r9)\n mov %%r13,24(%%r9)\n"
		"mov %%r14,32(%%r9)\n mov %%r15,40(%%r9)\n"
		"pop %%r15\n pop %%r14\n pop %%r13\n pop %%r12\n"
		"pop %%rbp\n pop %%rbx\n"
		: : [v]"r"(v), [o]"r"(out)
		: "rax","rcx","r11","rdi","rsi","rdx","r8","r9","memory");
	for (int i = 0; i < 6; i++)
		if (out[i] != v[i])
			ok = 0;
	if (ok)
		PASS("gp-syscall", "rbx,rbp,r12-r15 across syscalls");
	else
		FAIL("gp-syscall", "%lx %lx %lx %lx %lx %lx",
		     out[0], out[1], out[2], out[3], out[4], out[5]);
	return ok;
}

/* ---- fp-preserve -------------------------------------------------------- */

static void *writer_thread(void *arg)
{
	int *pfd = arg;
	char b = 1;

	usleep(30000);
	(void)!write(pfd[1], &b, 1);
	return NULL;
}

static int case_fp_syscalls(void)
{
	pthread_t th;
	int pfd[2];
	char c = 0;
	int ok = 1;

	if (pipe(pfd)) {
		FAIL("fp-preserve", "pipe: %s", strerror(errno));
		return 0;
	}
	if (pthread_create(&th, NULL, writer_thread, pfd) != 0) {
		FAIL("fp-preserve", "pthread_create: %s", strerror(errno));
		close(pfd[0]); close(pfd[1]);
		return 0;
	}

	fill_state(0xabcdef);
	asm volatile(
		"mov $39,%%eax\n syscall\n"
		"mov $0,%%eax\n xor %%edi,%%edi\n xor %%esi,%%esi\n"
		"xor %%edx,%%edx\n syscall\n"
		"mov $39,%%eax\n syscall\n"
		::: "rax","rcx","r11","rdi","rsi","rdx","memory");
	ok &= state_unchanged("mix");

	fill_state(0x123456);
	(void)!read(pfd[0], &c, 1);	/* blocks until the writer fires */
	ok &= state_unchanged("blocking-read");

	pthread_join(th, NULL);
	close(pfd[0]);
	close(pfd[1]);
	if (ok)
		PASS("fp-preserve", "x87+sse across mixed/blocking syscalls");
	return ok;
}

/* ---- fp-signal-preserve ------------------------------------------------- */

static volatile unsigned handler_count;

static void sse_handler(int sig)
{
	unsigned char b[16] __attribute__((aligned(16)));
	unsigned mx = get_mxcsr();

	(void)sig;
	asm volatile("movdqu %%xmm7,%0":"=m"(b));
	asm volatile("ldmxcsr %0"::"m"(mx));
	handler_count++;
}

static int case_fp_signal_preserve(void)
{
	struct sigaction sa = { .sa_handler = sse_handler };

	sigaction(SIGUSR1, &sa, NULL);
	handler_count = 0;
	for (int round = 0; round < 100; round++) {
		fill_state(0x1000 + round);
		tgkill_self(SIGUSR1);
		tiny_nanosleep();
		if (!state_unchanged("signal-round"))
			return 0;
	}
	if (handler_count != 100) {
		FAIL("fp-signal-preserve", "only %u handlers", handler_count);
		return 0;
	}
	PASS("fp-signal-preserve", "100 self-directed handlers");
	return 1;
}

/* ---- fp-signal-edit ----------------------------------------------------- */

static void edit_fp_handler(int sig, siginfo_t *si, void *ctxv)
{
	ucontext_t *uc = ctxv;
	unsigned char *fp;

	(void)sig;
	(void)si;
	fp = (unsigned char *)uc->uc_mcontext.fpregs;
	*(unsigned *)(fp + 24) = 0x1f80 | 0x40;
	memset(fp + 160, 0x7a, 16);
}

static int case_fp_signal_edit(void)
{
	struct sigaction sa = { .sa_sigaction = edit_fp_handler,
			       .sa_flags = SA_SIGINFO };
	unsigned char xmm0[16];
	unsigned mx;
	int ok = 1;

	sigaction(SIGUSR1, &sa, NULL);
	fill_state(0x333);
	tgkill_self(SIGUSR1);
	tiny_nanosleep();
	mx = get_mxcsr();
	asm volatile("movdqu %%xmm0,%0":"=m"(xmm0));
	if (mx != (0x1f80 | 0x40)) {
		FAIL("fp-signal-edit", "mxcsr=%x", mx);
		ok = 0;
	}
	for (int i = 0; i < 16; i++)
		if (xmm0[i] != 0x7a) {
			FAIL("fp-signal-edit", "xmm0[%d]=0x%02x", i, xmm0[i]);
			ok = 0;
			break;
		}
	if (ok)
		PASS("fp-signal-edit", "ucontext mxcsr+xmm0 edit");
	return ok;
}

/* ---- gp-signal-edit ----------------------------------------------------- */

static void edit_gp_handler(int sig, siginfo_t *si, void *ctxv)
{
	greg_t *g = ((ucontext_t *)ctxv)->uc_mcontext.gregs;

	(void)sig;
	(void)si;
	g[REG_RBX] = 0xa1a1a1a1a1a1a1a1UL;
	g[REG_RBP] = 0xa2a2a2a2a2a2a2a2UL;
	g[REG_R12] = 0xa3a3a3a3a3a3a3a3UL;
	g[REG_R13] = 0xa4a4a4a4a4a4a4a4UL;
	g[REG_R14] = 0xa5a5a5a5a5a5a5a5UL;
	g[REG_R15] = 0xa6a6a6a6a6a6a6a6UL;
}

static int case_gp_signal_edit(void)
{
	struct sigaction sa = { .sa_sigaction = edit_gp_handler,
			       .sa_flags = SA_SIGINFO };
	unsigned long got[6];
	static const unsigned long want[6] = {
		0xa1a1a1a1a1a1a1a1UL, 0xa2a2a2a2a2a2a2a2UL,
		0xa3a3a3a3a3a3a3a3UL, 0xa4a4a4a4a4a4a4a4UL,
		0xa5a5a5a5a5a5a5a5UL, 0xa6a6a6a6a6a6a6a6UL,
	};
	int ok = 1;
	int tid = (int)syscall(SYS_gettid);
	int pid = (int)getpid();

	sigaction(SIGUSR1, &sa, NULL);
	asm volatile(
		/* pid/tid staged in r8/r9 and the result base in r10 before
		 * the tested GPRs are touched or the stack moves. */
		"mov %[pid],%%r8d\n mov %[tid],%%r9d\n mov %[o],%%r10\n"
		"push %%rbx\n push %%rbp\n push %%r12\n push %%r13\n"
		"push %%r14\n push %%r15\n"
		"xor %%rbx,%%rbx\n xor %%rbp,%%rbp\n xor %%r12,%%r12\n"
		"xor %%r13,%%r13\n xor %%r14,%%r14\n xor %%r15,%%r15\n"
		"mov $234,%%eax\n mov %%r8d,%%edi\n mov %%r9d,%%esi\n"
		"mov $10,%%edx\n syscall\n"
		"mov $39,%%eax\n syscall\n"
		"mov %%rbx,0(%%r10)\n mov %%rbp,8(%%r10)\n"
		"mov %%r12,16(%%r10)\n mov %%r13,24(%%r10)\n"
		"mov %%r14,32(%%r10)\n mov %%r15,40(%%r10)\n"
		"pop %%r15\n pop %%r14\n pop %%r13\n pop %%r12\n"
		"pop %%rbp\n pop %%rbx\n"
		: : [pid]"m"(pid), [tid]"m"(tid), [o]"r"(got)
		: "rax","rcx","r11","rdi","rsi","rdx","r8","r9","r10",
		  "memory");
	for (int i = 0; i < 6; i++)
		if (got[i] != want[i])
			ok = 0;
	if (ok)
		PASS("gp-signal-edit", "ucontext rbx,rbp,r12-r15 edit");
	else
		FAIL("gp-signal-edit", "%lx %lx %lx %lx %lx %lx",
		     got[0], got[1], got[2], got[3], got[4], got[5]);
	return ok;
}

/* ---- fp-nested ---------------------------------------------------------- */

static volatile unsigned nested_bits;

static void nest_inner(int sig)
{
	unsigned char b[16] __attribute__((aligned(16)));

	(void)sig;
	asm volatile("movdqu %%xmm9,%0":"=m"(b));
	nested_bits |= 2;
}

static void nest_outer(int sig)
{
	struct sigaction sa = { .sa_handler = nest_inner,
			       .sa_flags = SA_NODEFER };
	unsigned char b[16] __attribute__((aligned(16)));

	(void)sig;
	sigaction(SIGUSR2, &sa, NULL);
	asm volatile("movdqu %%xmm8,%0":"=m"(b));
	tgkill_self(SIGUSR2);
	asm volatile("movdqu %%xmm8,%0":"=m"(b));
	nested_bits |= 1;
}

static int case_fp_nested(void)
{
	struct sigaction sa = { .sa_handler = nest_outer,
			       .sa_flags = SA_NODEFER };

	nested_bits = 0;
	sigaction(SIGUSR1, &sa, NULL);
	fill_state(0x7654);
	tgkill_self(SIGUSR1);
	tiny_nanosleep();
	if (nested_bits != 3 || !state_unchanged("nested")) {
		FAIL("fp-nested", "bits=%u", nested_bits);
		return 0;
	}
	PASS("fp-nested", "SA_NODEFER nested handler frames");
	return 1;
}

/* ---- fp-threads --------------------------------------------------------- */

struct worker_arg {
	int id;
	int rounds;
};

static void *mt_worker(void *arg)
{
	struct worker_arg *w = arg;

	for (int round = 0; round < w->rounds; round++) {
		cpu_set_t set;
		unsigned seed = (unsigned)((w->id << 16) ^ round ^ 0x55aa);

		CPU_ZERO(&set);
		CPU_SET((unsigned)(w->id + round) % ncpu, &set);
		sched_setaffinity(0, sizeof(set), &set);
		fill_state(seed);
		for (int k = 0; k < 6; k++) {
			struct timespec ts = { 0, 500000 };

			asm volatile("mov $39,%%eax\n syscall"
				     :::"rax","rcx","r11");
			nanosleep(&ts, NULL);
			sched_yield();
		}
		if (!state_unchanged("mt"))
			return (void *)1;
	}
	return NULL;
}

static int case_threads(int workers, int rounds)
{
	pthread_t th[32];
	struct worker_arg wa[32];
	int ok = 1;
	char name[32];

	if (workers < 1 || workers > 32) {
		FAIL("fp-threads-bad", "workers=%d", workers);
		return 0;
	}
	snprintf(name, sizeof(name), "fp-threads-%d", workers);
	for (int i = 0; i < workers; i++) {
		wa[i] = (struct worker_arg){ i, rounds };
		if (pthread_create(&th[i], NULL, mt_worker, &wa[i]) != 0) {
			FAIL(name, "pthread_create worker %d: %s", i,
			     strerror(errno));
			/* join whatever already started */
			for (int j = 0; j < i; j++)
				pthread_join(th[j], NULL);
			return 0;
		}
	}
	for (int i = 0; i < workers; i++) {
		void *rc;

		if (pthread_join(th[i], &rc) != 0 || rc != NULL)
			ok = 0;
	}
	if (ok)
		PASS(name, "%d workers x %d rounds affinity-rotate",
		     workers, rounds);
	else
		FAIL(name, "%d workers x %d rounds", workers, rounds);
	return ok;
}

/* ---- frame-churn -------------------------------------------------------- */

struct churn_arg { int rfd, wfd; };

static void *churn_blocked(void *arg)
{
	struct churn_arg *ca = arg;
	char c;

	fill_state(0xb10c);
	(void)!read(ca->rfd, &c, 1);	/* never written: parked till cancel */
	close(ca->rfd);
	close(ca->wfd);
	return NULL;
}

static int case_frame_churn(void)
{
	pthread_t blocked[8];
	struct churn_arg ca[8];
	int ok = 1, started = 0;

	for (int i = 0; i < 8; i++) {
		int p[2];
		if (pipe(p) != 0) {
			FAIL("frame-churn", "pipe: %s", strerror(errno));
			ok = 0;
			break;
		}
		ca[i].rfd = p[0];
		ca[i].wfd = p[1];
		if (pthread_create(&blocked[i], NULL, churn_blocked,
				   &ca[i]) != 0) {
			FAIL("frame-churn", "pthread_create: %s",
			     strerror(errno));
			close(p[0]);
			close(p[1]);
			ok = 0;
			break;
		}
		started++;
	}
	if (!ok) {
		for (int i = 0; i < started; i++) {
			pthread_cancel(blocked[i]);
			pthread_join(blocked[i], NULL);
		}
		return 0;
	}
	usleep(50000);
	for (int i = 0; i < 200000; i++)
		asm volatile("mov $39,%%eax\n syscall":::"rax","rcx","r11");
	for (int i = 0; i < 8; i++) {
		void *rc;
		if (pthread_cancel(blocked[i]) != 0)
			ok = 0;
		if (pthread_join(blocked[i], &rc) != 0 ||
		    rc != PTHREAD_CANCELED)
			ok = 0;
	}
	fill_state(0x0101);
	for (int i = 0; i < 1000; i++)
		asm volatile("mov $39,%%eax\n syscall":::"rax","rcx","r11");
	ok &= state_unchanged("post-churn");
	if (ok)
		PASS("frame-churn", "8 blocked owners + 200k syscalls, fds closed");
	else
		FAIL("frame-churn", "setup/cancel/join/fd cleanup failed");
	return ok;
}

/* ---- thread-exit-churn -------------------------------------------------- */

static void *short_thread(void *arg)
{
	unsigned seed = (unsigned)(uintptr_t)arg;

	fill_state(seed);
	for (int k = 0; k < 50; k++)
		asm volatile("mov $39,%%eax\n syscall":::"rax","rcx","r11");
	return NULL;
}

static long read_rss(void)
{
	FILE *f = fopen("/proc/self/status", "r");
	char line[128];
	long rss = -1;

	if (!f)
		return -1;
	while (fgets(line, sizeof(line), f))
		if (!strncmp(line, "VmRSS:", 6))
			rss = atol(line + 6);
	fclose(f);
	return rss;
}

static void report_rss(const char *tag)
{
	fprintf(stderr, "FP_RSS %s rss_kb=%ld ncpu=%u\n", tag, read_rss(), ncpu);
}

static int case_exit_churn(void)
{
	long rss0, rss1, delta;
	pthread_t th;

	rss0 = read_rss();
	report_rss("churn-before");
	for (int i = 0; i < 400; i++) {
		if (pthread_create(&th, NULL, short_thread,
				   (void *)(uintptr_t)(0x4000 + i)) != 0) {
			FAIL("thread-exit-churn", "pthread_create: %s",
			     strerror(errno));
			return 0;
		}
		if (pthread_join(th, NULL) != 0) {
			FAIL("thread-exit-churn", "pthread_join: %s",
			     strerror(errno));
			return 0;
		}
		if ((i & 15) == 0)
			sched_yield();
	}
	rss1 = read_rss();
	report_rss("churn-after");
	if (rss0 < 0 || rss1 < 0) {
		SKIP("thread-exit-churn", "VmRSS unavailable");
		return 0;
	}
	delta = rss1 - rss0;
	if (delta > 64 * 1024) {
		FAIL("thread-exit-churn", "rss grew %ld KB after 400 exits",
		     delta);
		return 0;
	}
	PASS("thread-exit-churn", "400 exited threads, rss delta %ld KB", delta);
	return 1;
}

/* ---- runner-lifetime ---------------------------------------------------- */

static int case_runner_lifetime(void)
{
	int ok = 1;

	for (int i = 0; i < 200; i++) {
		pid_t pid = fork();
		int status;

		if (pid < 0) {
			FAIL("runner-lifetime", "fork: %s", strerror(errno));
			return 0;
		}
		if (pid == 0) {
			fill_state(0x9000u + i);
			for (int k = 0; k < 20; k++)
				asm volatile("mov $39,%%eax\n syscall"
					     :::"rax","rcx","r11");
			if (!state_unchanged("short-child"))
				_exit(10);
			_exit(0);
		}
		if (waitpid(pid, &status, 0) < 0) {
			FAIL("runner-lifetime", "waitpid: %s", strerror(errno));
			return 0;
		}
		if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
			ok = 0;
	}
	if (ok)
		PASS("runner-lifetime", "200 short-lived mm lifecycles");
	else
		FAIL("runner-lifetime", "child failure");
	return ok;
}

/* ---- fork-fp ------------------------------------------------------------ */

static int clone_child_fn(void *arg)
{
	(void)arg;
	for (int k = 0; k < 100; k++)
		asm volatile("mov $39,%%eax\n syscall":::"rax","rcx","r11");
	if (!state_unchanged("clone-child"))
		_exit(20);
	_exit(0);
}

static int case_fork(void)
{
	static char stack[256 * 1024] __attribute__((aligned(16)));
	pid_t pid;
	int status;
	int ok;

	fill_state(0xf00d);
	pid = clone(clone_child_fn, stack + sizeof(stack) - 128,
		    SIGCHLD, NULL, NULL, NULL, NULL);
	if (pid < 0) {
		FAIL("fork-fp", "clone: %s", strerror(errno));
		return 0;
	}
	if (waitpid(pid, &status, 0) < 0) {
		FAIL("fork-fp", "waitpid: %s", strerror(errno));
		return 0;
	}
	ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
	ok &= state_unchanged("clone-parent");
	if (ok)
		PASS("fork-fp", "raw clone inheritance/independence");
	else
		FAIL("fork-fp", "status=%x", status);
	return ok;
}

/* ---- exec-reset --------------------------------------------------------- */

static int exec_check(void)
{
	unsigned mx = get_mxcsr();
	unsigned short fc = get_fcw();
	int rc = 0;

	/*
	 * execve resets the architectural control state: MXCSR 0x1f80 and
	 * x87 FCW 0x37f. Data registers (xmm/st) are caller-clobbered and not
	 * architecturally zeroed on Linux, so they are not asserted here.
	 */
	if (mx != 0x1f80)
		rc |= 1;
	if ((fc & 0xfff) != 0x37f)
		rc |= 2;
	return rc;
}

static int case_exec(void)
{
	pid_t pid = fork();
	int status;

	if (pid < 0) {
		FAIL("exec-reset", "fork: %s", strerror(errno));
		return 0;
	}
	if (pid == 0) {
		char *argv[] = { (char *)"/proc/self/exe",
				 (char *)"--exec-check", NULL };
		char *envp[] = { NULL };

		fill_state(0xeeee);
		execve("/proc/self/exe", argv, envp);
		_exit(30);
	}
	if (waitpid(pid, &status, 0) < 0) {
		FAIL("exec-reset", "waitpid: %s", strerror(errno));
		return 0;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		FAIL("exec-reset", "status=%x rc=%d", status,
		     WIFEXITED(status) ? WEXITSTATUS(status) : -1);
		return 0;
	}
	PASS("exec-reset", "mxcsr 0x1f80, fcw 0x37f after execve");
	return 1;
}

/* ---- ptrace-gp-fp ------------------------------------------------------- */

/*
 * Explicit stopped-phase handshake. The tracee executes two tight syscall
 * loops; the tracer edits regsets before phase 1 and restores them before
 * phase 2. The tracee samples its own register file INSIDE each loop (so the
 * compiler never allocates the sampled callee-saved GPRs), stores both
 * samples to shared memory, and validates every byte before exit.
 */
enum {
	PHASE_WAIT_EDIT = 1,	/* tracer applies full-image edits */
	PHASE_WAIT_RESTORE = 2,	/* tracee sampled edits; tracer restores */
	PHASE_DONE = 3,		/* tracee sampled restored values */
};

/*
 * Explicit stopped-phase handshake (MAP_SHARED):
 *   phase 0  tracee loops issuing getpid
 *   tracer: full-image GET + edit SET, then phase=1
 *   phase 1  tracee samples EDITED rbx/rbp/r12-r15, mxcsr, xmm0 -> s1_*,
 *            publishes sample1_done, enters loop 2
 *   tracer: verifies s1; exercises PREFIX-partial SET semantics; restores
 *            full original images; phase=2
 *   phase 2  tracee samples RESTORED values -> s2_*, sample2_done, loop 3
 *   tracer: verifies s2, phase=3, tracee exits 0 iff its s1 matched
 *
 * The tracee samples inside ONE asm block whose loops never write the
 * sampled callee-saved GPRs (they are excluded from the clobber list), so
 * compiler allocation and getpid syscalls cannot perturb them.
 */
enum { PHASE_EDIT = 1, PHASE_RESTORE = 2, PHASE_RELEASE = 3 };

struct ptrace_shared {
	volatile int phase;
	volatile int in_loop;	/* tracee set immediately before its asm loop */
	volatile int sample1_done;
	volatile int sample2_done;
	volatile int setup_bad;
	volatile unsigned s1_mx, s2_mx;
	unsigned long s1_gpr[6], s2_gpr[6];
	unsigned char s1_xmm0[16], s2_xmm0[16];
};

/* Tracer-side immutable copy of the frozen edited sample. */
static struct { unsigned long gpr[6]; unsigned mx; unsigned char xmm[16]; }
s1_frozen;

static void tracee_main(struct ptrace_shared *sh)
{
	static const unsigned long edit[6] = {
		0xd00d0001UL, 0xd00d0002UL, 0xd00d0003UL, 0xd00d0004UL,
		0xd00d0005UL, 0xd00d0006UL,
	};
	int s1_ok = 1, i;

	if (ptrace(PTRACE_TRACEME, 0, 0, 0) < 0 || raise(SIGSTOP) != 0) {
		sh->setup_bad = 1;
		_exit(40);
	}
	/* Set AFTER all libc startup/raise syscalls; the next syscall traps
	 * belong exclusively to the asm sampling loop. */
	__sync_synchronize();
	sh->in_loop = 1;

	asm volatile(
		"1: mov $39,%%eax\n syscall\n"
		"cmpl $1,%[ph]\n jl 1b\n"
		"mov %%rbx,%[g10]\n mov %%rbp,%[g11]\n"
		"mov %%r12,%[g12]\n mov %%r13,%[g13]\n"
		"mov %%r14,%[g14]\n mov %%r15,%[g15]\n"
		"stmxcsr %[m1]\n movdqu %%xmm0,%[x1]\n"
		"movl $1,%[d1]\n"
		"2: mov $39,%%eax\n syscall\n"
		"cmpl $2,%[ph]\n jl 2b\n"
		"mov %%rbx,%[g20]\n mov %%rbp,%[g21]\n"
		"mov %%r12,%[g22]\n mov %%r13,%[g23]\n"
		"mov %%r14,%[g24]\n mov %%r15,%[g25]\n"
		"stmxcsr %[m2]\n movdqu %%xmm0,%[x2]\n"
		"movl $1,%[d2]\n"
		"3: mov $39,%%eax\n syscall\n"
		"cmpl $3,%[ph]\n jl 3b\n"
		: [g10]"=m"(sh->s1_gpr[0]), [g11]"=m"(sh->s1_gpr[1]),
		  [g12]"=m"(sh->s1_gpr[2]), [g13]"=m"(sh->s1_gpr[3]),
		  [g14]"=m"(sh->s1_gpr[4]), [g15]"=m"(sh->s1_gpr[5]),
		  [g20]"=m"(sh->s2_gpr[0]), [g21]"=m"(sh->s2_gpr[1]),
		  [g22]"=m"(sh->s2_gpr[2]), [g23]"=m"(sh->s2_gpr[3]),
		  [g24]"=m"(sh->s2_gpr[4]), [g25]"=m"(sh->s2_gpr[5]),
		  [m1]"=m"(sh->s1_mx), [m2]"=m"(sh->s2_mx),
		  [x1]"=m"(sh->s1_xmm0), [x2]"=m"(sh->s2_xmm0),
		  [d1]"+m"(sh->sample1_done),
		  [d2]"+m"(sh->sample2_done)
		: [ph]"m"(sh->phase)
		: "rax","rcx","r11","memory",
		  "rbx","rbp","r12","r13","r14","r15");
	for (i = 0; i < 6; i++)
		if (sh->s1_gpr[i] != edit[i])
			s1_ok = 0;
	if (sh->s1_mx != 0x1f80)
		s1_ok = 0;
	for (i = 0; i < 16; i++)
		if (sh->s1_xmm0[i] != (unsigned char)(0x5a + i))
			s1_ok = 0;
	_exit(s1_ok ? 0 : 41);
}

/*
 * PTRACE_GET/SETREGSET always address a register set at offset 0; iov_len is
 * the number of bytes transferred from the START (writes are a prefix). There
 * is no register-offset argument. Returns 0/-errno and reports final iov_len.
 */
static int regset_io(pid_t pid, int set, unsigned type, void *buf,
		     size_t len, size_t *used)
{
	struct iovec iov = { .iov_base = buf, .iov_len = len };

	errno = 0;
	if (ptrace(set ? PTRACE_SETREGSET : PTRACE_GETREGSET, pid,
		   (void *)(unsigned long)type, &iov) < 0)
		return -errno;
	if (used)
		*used = iov.iov_len;
	return 0;
}

/* Wait while PTRACE_SYSCALL-continuing until a syscall trap, exit, or death.
 * Returns 0 on syscall stop, 1 on tracee exit/death, -1 on wait error. */
static int cont_wait(pid_t pid, int *st)
{
	if (ptrace(PTRACE_SYSCALL, pid, 0, 0) < 0)
		return -1;
	for (;;) {
		if (waitpid(pid, st, 0) < 0)
			return -1;
		if (WIFEXITED(*st) || WIFSIGNALED(*st))
			return 1;
		if (WIFSTOPPED(*st) && WSTOPSIG(*st) == (SIGTRAP | 0x80))
			return 0;
		if (ptrace(PTRACE_SYSCALL, pid, 0, 0) < 0)
			return -1;
	}
}

/* Resume through syscall traps until the shared flag is observed. */
static int wait_flag(pid_t pid, volatile int *flag, int *st)
{
	for (int n = 0; n < 512; n++) {
		if (__sync_synchronize(), *flag)
			return 0;
		int r = cont_wait(pid, st);
		if (r != 0)
			return r < 0 ? -1 : 1;
	}
	return -1;
}

static int case_ptrace(void)
{
	struct ptrace_shared *sh = MAP_FAILED;
	struct user_regs_struct *gp = NULL, *gpsave = NULL;
	unsigned char *fx = NULL, *fxsave = NULL, *xs = NULL, *xssave = NULL;
	const size_t gplen = sizeof(*gp), fxcap = 512, xscap = 4096;
	size_t gplen2 = 0, fxlen = 0, xslen = 0;
	pid_t pid = -1;
	int st = 0, rc = 0, deny = 0, e;
	const char *why = "";

	sh = mmap(NULL, 4096, PROT_READ|PROT_WRITE,
		  MAP_SHARED|MAP_ANONYMOUS, -1, 0);
	gp = calloc(1, gplen); gpsave = calloc(1, gplen);
	fx = calloc(1, fxcap); fxsave = calloc(1, fxcap);
	xs = calloc(1, xscap); xssave = calloc(1, xscap);
	if (sh == MAP_FAILED || !gp || !gpsave || !fx || !fxsave || !xs ||
	    !xssave) {
		FAIL("ptrace-gp-fp", "allocation failed");
		rc = 1; goto out;
	}
	pid = fork();
	if (pid < 0) {
		FAIL("ptrace-gp-fp", "fork: %s", strerror(errno));
		rc = 1; goto out;
	}
	if (pid == 0)
		tracee_main(sh);
	if (waitpid(pid, &st, 0) < 0 || !WIFSTOPPED(st) || sh->setup_bad) {
		FAIL("ptrace-gp-fp", "no clean initial stop");
		rc = 1; goto out;
	}
	if (ptrace(PTRACE_SETOPTIONS, pid, 0, PTRACE_O_TRACESYSGOOD) < 0) {
		SKIP("ptrace-gp-fp", "PTRACE_SETOPTIONS: %s", strerror(errno));
		goto out;
	}

	/*
	 * Apply edits at a syscall-ENTRY stop: registers written at entry
	 * are restored on return to userspace and observed by the tracee's
	 * post-syscall sampling. This kernel reports post-syscall RIP for
	 * both stops, so entry is identified by orig_rax==getpid together
	 * with the pre-dispatch rax == -ENOSYS placeholder (the exit stop
	 * carries getpid's positive pid).
	 */
	{
		int edits_applied = 0;

		for (int n = 0; n < 512 && !sh->sample1_done; n++) {
			int is_entry = 0;

			if (cont_wait(pid, &st) != 0) {
				FAIL("ptrace-gp-fp", "tracee vanished pre-sample1");
				rc = 1; goto out;
			}
			__sync_synchronize();
			if (!sh->in_loop)
				continue;
			if (regset_io(pid, 0, NT_PRSTATUS, gp, gplen, &gplen2) < 0) {
				why = "GET to identify getpid"; goto denied;
			}
			if (gp->orig_rax != 39)
				continue;
			/*
			 * On this kernel both entry and exit syscall stops
			 * report RIP past the syscall; the entry stop is
			 * identified by the -ENOSYS value the kernel plants
			 * in rax before dispatching (getpid's real return is
			 * its positive pid, observed at the exit stop).
			 */
			is_entry = ((long long)gp->rax == -38);
			if (!is_entry || edits_applied)
				continue;
			/* Full-image GETs at this entry stop. */
			if (regset_io(pid, 0, NT_PRFPREG, fx, fxcap, &fxlen) < 0 ||
			    regset_io(pid, 0, NT_X86_XSTATE, xs, xscap, &xslen) < 0) {
				why = "full GET in loop"; goto denied;
			}
			if (fxlen < 28 || xslen < 176 || xslen > xscap) {
				FAIL("ptrace-gp-fp", "regset lengths fx=%zu xs=%zu",
				     fxlen, xslen);
				rc = 1; goto out;
			}
			memcpy(gpsave, gp, gplen);
			memcpy(fxsave, fx, fxlen);
			memcpy(xssave, xs, xslen);

			gp->rbx = 0xd00d0001UL; gp->rbp = 0xd00d0002UL;
			gp->r12 = 0xd00d0003UL; gp->r13 = 0xd00d0004UL;
			gp->r14 = 0xd00d0005UL; gp->r15 = 0xd00d0006UL;
			*(unsigned *)(fx + 24) = 0x1f80;
			*(unsigned *)(xs + 24) = 0x1f80;
			for (int i = 0; i < 16; i++)
				xs[160 + i] = (unsigned char)(0x5a + i);
			if ((e = regset_io(pid, 1, NT_PRSTATUS, gp, gplen, NULL)) ||
			    (e = regset_io(pid, 1, NT_PRFPREG, fx, fxlen, NULL)) ||
			    (e = regset_io(pid, 1, NT_X86_XSTATE, xs, xslen, NULL))) {
				errno = -e; why = "full SETREGSET at entry"; goto denied;
			}
			edits_applied = 1;
			__sync_synchronize();
			sh->phase = PHASE_EDIT;
		}
		if (!sh->sample1_done) {
			FAIL("ptrace-gp-fp", "never sampled edited state");
			rc = 1; goto out;
		}
	}

	/*
	 * Characterise PREFIX-partial SET semantics precisely. Immediately
	 * before each test GET a FRESH full-image baseline (the live state),
	 * build the candidate from that baseline, attempt a short SET, and
	 * GET the whole image back:
	 *   accepted (0)  -> candidate prefix bytes present AND every byte
	 *                    outside that prefix identical to the baseline;
	 *   rejected (EINVAL/EFAULT) -> the ENTIRE image is byte-identical to
	 *                    the baseline (true atomicity, not one field).
	 * Any other errno or a partial application fails; this is not a
	 * facility denial.
	 */
	{
		unsigned char tmp[4096], base[4096], back[4096];
		const size_t fxpre = 28, xspre = 176;
		int fx_prefix_ok, xs_prefix_ok;

		if (regset_io(pid, 0, NT_PRFPREG, base, fxlen, NULL) < 0) {
			why = "fresh baseline GET NT_PRFPREG"; goto denied;
		}
		memcpy(tmp, base, fxlen);
		*(unsigned *)(tmp + 24) = 0x1f80 | 0x40;
		e = regset_io(pid, 1, NT_PRFPREG, tmp, fxpre, NULL);
		if (regset_io(pid, 0, NT_PRFPREG, back, fxlen, NULL) < 0) {
			why = "GET after FX prefix"; goto denied;
		}
		fx_prefix_ok = (e == 0);
		if (e == 0) {
			if (*(unsigned *)(back + 24) != (0x1f80 | 0x40) ||
			    memcmp(back, base, 24) ||
			    memcmp(back + fxpre, base + fxpre, fxlen - fxpre)) {
				FAIL("ptrace-prefix", "FX prefix non-atomic/retained");
				rc = 1; goto out;
			}
		} else if (e == -EINVAL || e == -EFAULT) {
			if (memcmp(back, base, fxlen)) {
				FAIL("ptrace-prefix", "FX reject was not atomic");
				rc = 1; goto out;
			}
		} else {
			errno = -e; why = "FX prefix SET"; goto denied;
		}

		if (regset_io(pid, 0, NT_X86_XSTATE, base, xslen, NULL) < 0) {
			why = "fresh baseline GET NT_X86_XSTATE"; goto denied;
		}
		memcpy(tmp, base, xslen);
		for (int i = 0; i < 16; i++)
			tmp[160 + i] = (unsigned char)(0x6b + i);
		e = regset_io(pid, 1, NT_X86_XSTATE, tmp, xspre, NULL);
		if (regset_io(pid, 0, NT_X86_XSTATE, back, xslen, NULL) < 0) {
			why = "GET after XSTATE prefix"; goto denied;
		}
		xs_prefix_ok = (e == 0);
		if (e == 0) {
			if (memcmp(back + 160, tmp + 160, 16) ||
			    memcmp(back, base, 160) ||
			    memcmp(back + xspre, base + xspre, xslen - xspre)) {
				FAIL("ptrace-prefix", "XSTATE prefix non-atomic/retained");
				rc = 1; goto out;
			}
		} else if (e == -EINVAL || e == -EFAULT) {
			if (memcmp(back, base, xslen)) {
				FAIL("ptrace-prefix", "XSTATE reject was not atomic");
				rc = 1; goto out;
			}
		} else {
			errno = -e; why = "XSTATE prefix SET"; goto denied;
		}
		PASS("ptrace-prefix-semantics",
		     "FX short SET %s, XSTATE short SET %s (reject=atomic full-only)",
		     fx_prefix_ok ? "accepted+retained" : "rejected-atomic",
		     xs_prefix_ok ? "accepted+retained" : "rejected-atomic");
	}

	/*
	 * FREEZE sample 1: once the tracee published it, it must never change
	 * again regardless of the prefix tests above or the restore below.
	 */
	memcpy(s1_frozen.gpr, sh->s1_gpr, sizeof s1_frozen.gpr);
	s1_frozen.mx = sh->s1_mx;
	memcpy(s1_frozen.xmm, sh->s1_xmm0, 16);

	/*
	 * Restore ONLY the six intentionally edited GP regs into a CURRENT
	 * register image, never the stale gpsave snapshot: that captured RIP
	 * at the first sampling-loop syscall and SETREGSET-ing it would
	 * rewind RIP to loop 1 (phase is already 2), causing the tracee to
	 * overwrite sample 1 with the restored values. All control-flow
	 * registers (RIP/RSP/orig_rax/rax...) are preserved from a fresh
	 * current GET. FP/XSTATE are merged into current full images too.
	 */
	{
		struct user_regs_struct cur;
		unsigned char fxcur[512], xscur[4096];
		size_t fxnow = 0, xsnow = 0;

		if (regset_io(pid, 0, NT_PRSTATUS, &cur, gplen, NULL) < 0 ||
		    regset_io(pid, 0, NT_PRFPREG, fxcur, sizeof fxcur, &fxnow) < 0 ||
		    regset_io(pid, 0, NT_X86_XSTATE, xscur, sizeof xscur, &xsnow) < 0) {
			why = "fresh GET before merge restore"; goto denied;
		}
		cur.rbx = gpsave->rbx; cur.rbp = gpsave->rbp;
		cur.r12 = gpsave->r12; cur.r13 = gpsave->r13;
		cur.r14 = gpsave->r14; cur.r15 = gpsave->r15;
		if ((e = regset_io(pid, 1, NT_PRSTATUS, &cur, gplen, NULL)) < 0) {
			errno = -e; why = "merge SET NT_PRSTATUS"; goto denied;
		}
		/* restore only the edited FP fields into the CURRENT images */
		*(unsigned *)(fxcur + 24) = *(unsigned *)(fxsave + 24);
		*(unsigned *)(xscur + 24) = *(unsigned *)(fxsave + 24);
		memcpy(xscur + 160, fxsave + 160, 16);
		if ((e = regset_io(pid, 1, NT_PRFPREG, fxcur, fxnow, NULL)) < 0 ||
		    (e = regset_io(pid, 1, NT_X86_XSTATE, xscur, xsnow, NULL)) < 0) {
			errno = -e; why = "merge SET FP/XSTATE"; goto denied;
		}
	}
	sh->phase = PHASE_RESTORE;
	if (wait_flag(pid, &sh->sample2_done, &st) != 0) {
		FAIL("ptrace-gp-fp", "tracee did not deliver restored sample");
		rc = 1; goto out;
	}
	sh->phase = PHASE_RELEASE;
	/* Drain remaining syscall stops (the final getpid) until _exit. */
	for (;;) {
		int r = cont_wait(pid, &st);
		if (r < 0) {
			FAIL("ptrace-gp-fp", "final resume/wait failed");
			rc = 1; goto out;
		}
		if (r == 1)
			break;
	}
	if (!WIFEXITED(st) || WEXITSTATUS(st) != 0) {
		FAIL("ptrace-gp-fp",
		     "tracee status=%x s1_gpr=%016lx,%016lx,%016lx,%016lx,%016lx,%016lx s1_mx=%08x",
		     st,
		     sh->s1_gpr[0], sh->s1_gpr[1], sh->s1_gpr[2],
		     sh->s1_gpr[3], sh->s1_gpr[4], sh->s1_gpr[5], sh->s1_mx);
		rc = 1; goto out;
	}
	/* Sample 1 frozen: it must still equal its post-edit snapshot after
	 * all prefix tests, the merge restore and both sampling loops. */
	if (memcmp(sh->s1_gpr, s1_frozen.gpr, sizeof s1_frozen.gpr) ||
	    sh->s1_mx != s1_frozen.mx ||
	    memcmp(sh->s1_xmm0, s1_frozen.xmm, 16)) {
		FAIL("ptrace-gp-fp", "frozen sample 1 was overwritten");
		rc = 1; goto out;
	}
	/* Sample 2 equals the pre-edit originals saved at the first edit. */
	if (sh->s2_gpr[0] != gpsave->rbx || sh->s2_gpr[1] != gpsave->rbp ||
	    sh->s2_gpr[2] != gpsave->r12 || sh->s2_gpr[3] != gpsave->r13 ||
	    sh->s2_gpr[4] != gpsave->r14 || sh->s2_gpr[5] != gpsave->r15 ||
	    sh->s2_mx != *(unsigned *)(fxsave + 24) ||
	    memcmp(sh->s2_xmm0, fxsave + 160, 16)) {
		FAIL("ptrace-gp-fp", "restored sample differs from originals");
		rc = 1; goto out;
	}
	PASS("ptrace-gp-fp",
	     "full SETREGSET gpr/fx/xs + prefix-partial retained bytes "
	     "(gpr=%zu fx=%zu xs=%zu), edited+restored samples exact",
	     gplen2, fxlen, xslen);
	goto out;

denied:
	/* Only a real error against a valid buffer is a facility denial. */
	deny = 1;
	(void)kill(pid, SIGKILL);
	waitpid(pid, &st, 0);
	SKIP("ptrace-gp-fp", "%s: %s", why, strerror(errno));
out:
	if (pid > 0 && !deny && (waitpid(pid, &st, WNOHANG) == 0)) {
		(void)kill(pid, SIGKILL);
		waitpid(pid, &st, 0);
	}
	free(gp); free(gpsave); free(fx); free(fxsave); free(xs); free(xssave);
	if (sh != MAP_FAILED)
		munmap(sh, 4096);
	return rc == 0 && !deny;
}


/* ---- driver ------------------------------------------------------------- */

int main(int argc, char **argv)
{
	if (argc == 2 && !strcmp(argv[1], "--exec-check"))
		return exec_check();

	ncpu = (unsigned)sysconf(_SC_NPROCESSORS_ONLN);
	if (ncpu > 32)
		ncpu = 32;
	if (!ncpu)
		ncpu = 1;

	case_gp_syscall();
	case_fp_syscalls();
	case_fp_signal_preserve();
	case_fp_signal_edit();
	case_gp_signal_edit();
	case_fp_nested();
	case_threads(1, 200);
	case_threads(4, 200);
	case_frame_churn();
	case_exit_churn();
	case_runner_lifetime();
	case_fork();
	case_exec();
	case_ptrace();

	fprintf(stderr, "FP_PROBE_END failures=%d incomplete=%d\n",
		failures, incomplete);
	if (failures)
		return 1;
	if (incomplete)
		return 2;
	return 0;
}
