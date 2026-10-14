// SPDX-License-Identifier: GPL-2.0
/* Actual extracted-filter probe for the 0024 stub seccomp policy.
 *
 * The BPF filter body is #included from files extracted verbatim from
 * arch/um/kernel/skas/stub_exe.c by build_seccomp_filter_test.py for each
 * gpu_bridge/gpu_async combination.  The probe installs the real filter in
 * a forked host process and issues every broker/worker/relay syscall FROM
 * an executable page the IP gate matches (same emulation as the round7
 * reviewer model).  A SIGSYS handler distinguishes SECCOMP_RET_TRAP
 * (getpid: caught, exit 6) from SECCOMP_RET_KILL_PROCESS (handler cannot
 * run: signal 31 with the process killed).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stddef.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <string.h>
#include <errno.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/poll.h>
#include <ucontext.h>
#include <sys/syscall.h>
#include <linux/audit.h>

#ifndef GPU_BRIDGE
#define GPU_BRIDGE 0
#endif
#ifndef GPU_ASYNC
#define GPU_ASYNC 0
#endif

static unsigned long g_stub_page;

static void build_page(void)
{
	unsigned char *p = (void *)g_stub_page, n = 0;
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc0; p[n++]=0; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x0f; p[n++]=0x05;
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc7; p[n++]=5; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x48; p[n++]=0xc7; p[n++]=0xc0; p[n++]=60; p[n++]=0; p[n++]=0; p[n++]=0;
	p[n++]=0x0f; p[n++]=0x05;
}

static void from_page(long nr, long a, long b, long c, long d, long e)
{
	register long rax asm("rax")=nr, rdi asm("rdi")=a, rsi asm("rsi")=b,
		rdx asm("rdx")=c, r10 asm("r10")=d, r8 asm("r8")=e;
	asm volatile("callq *%[fn]"
		     : : [fn]"r"(g_stub_page), "r"(rax), "r"(rdi), "r"(rsi),
			 "r"(rdx), "r"(r10), "r"(r8)
		     : "rcx","r9","r11","memory");
}

#define WANT_ALLOW 1	/* exited 5 */
#define WANT_TRAP 2	/* SIGSYS caught (exit 6): getpid TRAP */
#define WANT_KILL 3	/* SIGSYS killed despite handler */
#define WANT_PASS 4	/* anything but filter KILL (e.g. rt_sigreturn) */

struct test { const char *name; long nr,a,b,c,d,e; int want; };

static void on_sigsys(int sig) { (void)sig; _exit(6); }

/* A rt_sigreturn with a garbage frame is undefined and can spin; build a
 * minimal valid ucontext that resumes into the page's exit(5) tail. */
static void do_safe_rt_sigreturn(void)
{
	/* Tail offset: syscall(2)+ret-adjacent; find mov $5,%rdi. */
	unsigned char *q = (unsigned char *)g_stub_page;
	long tail = 0;
	for (long i = 0; i < 24; i++)
		if (q[i] == 0x48 && q[i+1] == 0xc7 && q[i+2] == 0xc7) {
			tail = i; break;
		}
	static unsigned long stk[8192] __attribute__((aligned(16)));
	ucontext_t uc;
	memset(&uc, 0, sizeof(uc));
	uc.uc_mcontext.gregs[REG_RIP] = (long long)(g_stub_page + tail);
	uc.uc_mcontext.gregs[REG_RSP] =
		(long long)(unsigned long)(stk + 8192 - 128);
	uc.uc_mcontext.gregs[REG_RBP] = uc.uc_mcontext.gregs[REG_RSP];
	uc.uc_stack.ss_sp = stk;
	uc.uc_stack.ss_size = sizeof(stk);
	uc.uc_stack.ss_flags = 0;
	from_page(__NR_rt_sigreturn, (long)&uc, 8, 0, 0, 0);
}

int main(void)
{
	g_stub_page = (unsigned long)mmap(NULL, 4096,
			PROT_READ|PROT_WRITE|PROT_EXEC,
			MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
	if (g_stub_page == (unsigned long)-1) return 2;
	build_page();

#include "seccomp_filter_prelude.inc"
	struct sock_filter f[] = {
#include "seccomp_filter_body.inc"
	};
	unsigned int len = sizeof(f)/sizeof(f[0]);
	int sv[2];
	if (pipe2(sv, O_NONBLOCK)) return 2;
	static char byte = 'e';
	struct pollfd pfd = { .fd = sv[0], .events = POLLIN };

	struct test T[] = {
	/* broker/worker bootstrap and batch syscalls - ALWAYS allowed */
	{"getpid(TRAP)", __NR_getpid, 0,0,0,0,0, WANT_TRAP},
	{"futex", __NR_futex, 0,0,0,0,0, WANT_ALLOW},
	{"recvmsg", __NR_recvmsg, 0,0,0,0,0, WANT_ALLOW},
	{"close(42)", __NR_close, 42,0,0,0,0, WANT_ALLOW},
	{"mmap(addr)", __NR_mmap, (long)g_stub_page,4096,3,0x12,-1, WANT_ALLOW},
	{"munmap", __NR_munmap, (long)g_stub_page,4096,0,0,0, WANT_ALLOW},
	{"arch_prctl", __NR_arch_prctl, 0,0,0,0,0, WANT_ALLOW},
	{"rt_sigreturn", -1,0,0,0,0,0, WANT_PASS},
	{"clone", __NR_clone, 0,0,0,0,0, WANT_PASS},
	{"dup2", __NR_dup2, 1,1,0,0,0, WANT_ALLOW},
	{"close_range", __NR_close_range, 3,9,0,0,0, WANT_ALLOW},
	{"sigaltstack", __NR_sigaltstack, 0,0,0,0,0, WANT_ALLOW},
	{"rt_sigprocmask", __NR_rt_sigprocmask, 0,0,0,8,0, WANT_ALLOW},
	{"prctl", __NR_prctl, 0,0,0,0,0, WANT_ALLOW},
	{"exit(30)", __NR_exit, 30,0,0,0,0, WANT_ALLOW},
	/* worker GPU syscalls - gated on bridge */
	{"dup", __NR_dup, 3,0,0,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	{"ioctl", __NR_ioctl, 3,0,0,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	/* relay thread syscalls */
	{"read", __NR_read, sv[0],(long)g_stub_page,8,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	{"poll", __NR_poll, (long)&pfd,1,0,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	{"eventfd2", __NR_eventfd2, 0,0,0,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	{"mprotect", __NR_mprotect, (long)g_stub_page,4096,3,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	/* write rules: fd0 needs async when bridge off; fd>=3 needs bridge */
	{"write(fd0)", __NR_write, 0,(long)&byte,1,0,0,
	 (!GPU_BRIDGE && !GPU_ASYNC) ? WANT_KILL : WANT_ALLOW},
	{"write(fd3)", __NR_write, 3,(long)&byte,1,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	{"write(notify-fd9)", __NR_write, 9,(long)&byte,1,0,0,
	 GPU_BRIDGE ? WANT_ALLOW : WANT_KILL},
	};
	int failures = 0, n = sizeof(T)/sizeof(T[0]);

	for (int i = 0; i < n; i++) {
		pid_t pid = fork();
		if (pid == 0) {
			struct sigaction sa = { .sa_handler = on_sigsys };
			sigaction(SIGSYS, &sa, NULL);
			prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
			if (syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, 0,
				    &(struct sock_fprog){len, f}))
				_exit(8);
			if (T[i].nr == -1) {
				do_safe_rt_sigreturn();
				_exit(5);
			}
			from_page(T[i].nr, T[i].a, T[i].b, T[i].c,
				  T[i].d, T[i].e);
			_exit(5);
		}
		int st;
		waitpid(pid, &st, 0);
		int got;
		if (WIFEXITED(st) && WEXITSTATUS(st) == 5)
			got = WANT_ALLOW;
		else if (WIFEXITED(st) && WEXITSTATUS(st) == 6)
			got = WANT_TRAP;
		else if (WIFSIGNALED(st) && WTERMSIG(st) == SIGSYS)
			got = WANT_KILL;
		else
			got = WANT_PASS;
		int ok;
		switch (T[i].want) {
		case WANT_PASS:
			ok = got != WANT_KILL; break;
		case WANT_ALLOW:
			ok = got == WANT_ALLOW; break;
		default:
			ok = got == T[i].want;
		}
		if (!ok) {
			static const char *names[] = {0,"ALLOW","TRAP","KILL","PASS"};
			printf("FAIL bridge=%d async=%d %-18s got=%s want=%s\n",
			       GPU_BRIDGE, GPU_ASYNC, T[i].name,
			       names[got], names[T[i].want]);
			failures++;
		}
	}
	if (failures == 0)
		printf("seccomp filter model: OK bridge=%d async=%d (%d probes)\n",
		       GPU_BRIDGE, GPU_ASYNC, n);
	return failures ? 1 : 0;
}
