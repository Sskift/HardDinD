// SPDX-License-Identifier: GPL-2.0
/* Bounded REAL kernel seccomp cross-check for the extracted stub filter.
 *
 * Installs the actual extracted filter in a forked process and executes a
 * SMALL set of representative syscalls from a matching instruction page:
 *   - write(fd0): the owner async-completion channel,
 *   - write(fd>=3): the relay notify channel (bridge on),
 *   - close(fd): backbone reachability,
 *   - getpid: the deliberate TRAP.
 * clone/rt_sigreturn are deliberately NOT executed here (they cannot be
 * made safely bounded); the interpreter model (seccomp_filter_sim.c) proves
 * their verdict without executing them.
 *
 * The parent runs an EXTERNAL watchdog (no in-filter alarm dependency):
 * SIGKILL the whole child group after 3 s.  The Python driver additionally
 * puts each child in its own session and kills/reaps the group on timeout.
 *
 * Child exits: 5 = filter ALLOWED and syscall returned;
 *              6 = SECCOMP_RET_TRAP caught (getpid);
 *              SIGSYS 31 = SECCOMP_RET_KILL_PROCESS.
 */
#define _GNU_SOURCE
#ifndef SA_RESTORER
#define SA_RESTORER 0x04000000
#endif
#include <stdio.h>
#include <stddef.h>
#include <stdint.h>
#include <unistd.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/syscall.h>
#include <linux/audit.h>
#include <ucontext.h>

#ifndef GPU_BRIDGE
#define GPU_BRIDGE 0
#endif
#ifndef GPU_ASYNC
#define GPU_ASYNC 0
#endif

static unsigned long g_stub_page;
#define EXIT5_OFF 2
#define EXIT7_OFF 18

static void build_page(void)
{
	unsigned char *p = (void *)g_stub_page, n = 0;
	/* 0: syscall; the request number is forced into %rax by the "a"
	 * constraint of the caller (no mov in the page to overwrite it). */
	p[n++]=0x0f; p[n++]=0x05;
	/* 2: exit(5) - ALLOWED syscalls return here */
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc7; p[n++]=5; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc0; p[n++]=60; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x0f; p[n++]=0x05;
	/* 18: exit(7) - TRAP recovery tail */
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc7; p[n++]=7; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc0; p[n++]=60; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x0f; p[n++]=0x05;
	/* 34: on-page rt_sigreturn restorer: RIP stays inside the gated
	 * stub page, so the filter's IP gate accepts signal return exactly
	 * like UML's stub_signal_restorer. */
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc0; p[n++]=15; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x0f; p[n++]=0x05;
}
#define RESTORER_OFF 34

/* TRAP handler: issue NO syscall (that would re-enter the filter from a
 * non-stub RIP); redirect the saved context into the exit7 tail. */
static void on_sigsys(int sig, siginfo_t *si, void *ucv)
{
	(void)sig; (void)si;
	ucontext_t *uc = ucv;
	uc->uc_mcontext.gregs[REG_RIP] = (long long)(g_stub_page + EXIT7_OFF);
	uc->uc_mcontext.gregs[REG_RAX] = 60;
	uc->uc_mcontext.gregs[REG_RDI] = 7;
}

static void from_page(long nr, long a, long b)
{
	/* "a"(nr) pins the request to %rax; gcc loads the call target into
	 * another register.  The page begins with syscall.  b supplies the
	 * write buffer (rsi); it is harmless for the other probes. */
	register long rdi asm("rdi") = a;
	register long rsi asm("rsi") = b;
	asm volatile("callq *%[fn]"
		     : : [fn]"r"(g_stub_page), "a"(nr), "r"(rdi), "r"(rsi)
		     : "rcx","r11","memory");
}

struct t { const char *name; long nr,a; int want; }; /* 1 allow, 3 kill, 2 trap */

int main(void)
{
	void *m = mmap(NULL, 4096, PROT_READ|PROT_WRITE|PROT_EXEC,
		       MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
	if (m == MAP_FAILED)
		return 2;
	g_stub_page = (unsigned long)m;
	build_page();

#include "seccomp_filter_prelude.inc"
	const struct sock_filter f[] = {
#include "seccomp_filter_body.inc"
	};
	static const char byte = 'e';
	struct t T[] = {
		{"getpid", __NR_getpid, 0, 2},
		{"write(fd0)", __NR_write, 0,
		 (!GPU_BRIDGE && !GPU_ASYNC) ? 3 : 1},
		{"write(fd3)", __NR_write, 3, GPU_BRIDGE ? 1 : 3},
		{"write(fd9)", __NR_write, 9, GPU_BRIDGE ? 1 : 3},
		{"close(42)", __NR_close, 42, 1},
	};
	int fails = 0;

	for (unsigned i = 0; i < sizeof(T)/sizeof(T[0]); i++) {
		pid_t pid = fork();
		if (pid == 0) {
			setpgid(0, 0);
			/* Raw rt_sigaction with the KERNEL layout
			 * (handler,flags,restorer,mask), which differs from
			 * glibc's struct order; glibc would swap in its own
			 * vDSO restorer that fails the stub-page IP gate. */
			struct {
				void (*handler)(int, siginfo_t *, void *);
				unsigned long flags;
				void (*restorer)(void);
				uint64_t mask;
			} ksa = {
				.handler = on_sigsys,
				.flags = SA_SIGINFO | SA_RESTORER,
				.restorer = (void (*)(void))
					(g_stub_page + RESTORER_OFF),
				.mask = 0,
			};
			syscall(__NR_rt_sigaction, SIGSYS, &ksa, NULL, 8);
			prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
			if (syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, 0,
				    &(struct sock_fprog){
					.len = sizeof(f)/sizeof(f[0]),
					.filter = (struct sock_filter *)f}))
				_exit(8);
			from_page(T[i].nr, T[i].a, (long)&byte);
			_exit(5);
		}
		int st;
		/* External watchdog: never depend on a filter-gated alarm. */
		int dead = 0;
		for (int w = 0; w < 30; w++) { /* 3 s poll */
			if (waitpid(pid, &st, WNOHANG) == pid)
				break;
			usleep(100000);
			if (w == 29) {
				kill(-pid, SIGKILL); /* own pgid below */
				waitpid(pid, &st, 0);
				dead = 1;
			}
		}
		int got = dead ? 0 :
			(WIFEXITED(st) && WEXITSTATUS(st) == 5) ? 1 :
			(WIFEXITED(st) && WEXITSTATUS(st) == 7) ? 2 :
			(WIFSIGNALED(st) && WTERMSIG(st) == SIGSYS) ? 3 : 0;
		if (got != T[i].want) {
			printf("FAIL bridge=%d async=%d %-12s got=%d want=%d\n",
			       GPU_BRIDGE, GPU_ASYNC, T[i].name, got,
			       T[i].want);
			fails++;
		}
	}
	if (!fails)
		printf("seccomp real-filter model: OK bridge=%d async=%d\n",
		       GPU_BRIDGE, GPU_ASYNC);
	return fails ? 1 : 0;
}
