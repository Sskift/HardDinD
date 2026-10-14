// SPDX-License-Identifier: GPL-2.0
/* Environment sanity for real-kernel seccomp probes.
 *
 * Installs a minimal filter (arch check + unconditional ALLOW) and issues
 * getpid from a runtime-mapped executable page, exactly like the stub
 * syscalls the UML broker/worker make.  Exit 5 = the environment lets a
 * page-syscall through a real SECCOMP_RET_ALLOW; SIGSYS (or any other
 * death) = outer sandbox restrictions invalidate real-filter tests, in
 * which case the caller must SKIP (not pass, not fail) them and rely on
 * the deterministic BPF interpreter model.
 */
#define _GNU_SOURCE
#include <unistd.h>
#include <sys/types.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/syscall.h>
#include <linux/audit.h>

int main(void)
{
	void *p = mmap(NULL, 4096, PROT_READ|PROT_WRITE|PROT_EXEC,
		       MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED)
		return 2;
	unsigned char *q = p;
	int n = 0;
	q[n++]=0x0f;q[n++]=0x05;
	q[n++]=0x48;q[n++]=0xc7;q[n++]=0xc7;q[n++]=5;q[n++]=0;q[n++]=0;q[n++]=0;
	q[n++]=0x48;q[n++]=0xc7;q[n++]=0xc0;q[n++]=60;q[n++]=0;q[n++]=0;q[n++]=0;
	q[n++]=0x0f;q[n++]=0x05;
	struct sock_filter f[] = {
		{0x20,0,0,4},
		{0x15,1,0,AUDIT_ARCH_X86_64},
		{0x06,0,0,SECCOMP_RET_KILL_PROCESS},
		{0x06,0,0,SECCOMP_RET_ALLOW},
	};
	pid_t pid = fork();
	if (pid == 0) {
		prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
		if (syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, 0,
			    &(struct sock_fprog){4, f}))
			_exit(8);
		register long rax asm("rax")=39, rdi asm("rdi")=0;
		asm volatile("callq *%[fn]"
			     : : [fn]"r"(p), "r"(rax), "r"(rdi)
			     : "rcx","r11","memory");
		_exit(9);
	}
	int st;
	/* bounded external wait; SIGKILL on timeout */
	for (int w = 0; w < 30; w++) {
		if (waitpid(pid, &st, WNOHANG) == pid)
			break;
		usleep(100000);
		if (w == 29) { kill(pid, SIGKILL); waitpid(pid, &st, 0); return 3; }
	}
	return (WIFEXITED(st) && WEXITSTATUS(st) == 5) ? 0 : 4;
}
