/*
 * Device-free native assertion for the RAW PTRACE_SETREGSET short-write path
 * (OWNER-RAW-REGSET-REVIEW). This deliberately does NOT use the probe's
 * regset_io() helper (which normalises the return to -errno and only writes
 * iov_len on success); it calls ptrace() directly so it can assert the true
 * kernel ABI:
 *
 *   - the raw syscall return on a rejected short SET is -1 (with errno set),
 *   - struct iovec.iov_len is initialised to the requested length and, on
 *     this 5.15 host, is left UNCHANGED after the failed call, so the
 *     post-call value equals the requested length.
 *
 * If a future/other kernel changes either fact, this exits non-zero and
 * prints the OBSERVED values rather than relying on a zero sentinel. It
 * creates its own traced child (no /dev, no VM) and is meant to run bounded
 * under the global EX lock.
 */
#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

static unsigned char buf[8192];

struct tc {
	unsigned type;
	size_t len;
	const char *tag;
};

int main(void)
{
	pid_t pid = fork();

	if (pid == 0) {
		if (ptrace(PTRACE_TRACEME, 0, 0, 0) < 0)
			_exit(50);
		raise(SIGSTOP);
		_exit(0);
	}

	int st = 0, fails = 0;
	if (waitpid(pid, &st, 0) < 0 || !WIFSTOPPED(st)) {
		fprintf(stderr, "no initial stop\n");
		return 1;
	}
	if (ptrace(PTRACE_SETOPTIONS, pid, 0, PTRACE_O_TRACESYSGOOD) < 0) {
		fprintf(stderr, "SETOPTIONS unavailable: %s\n", strerror(errno));
		kill(pid, SIGKILL);
		waitpid(pid, &st, 0);
		return 2;	/* facility denial, not an ABI failure */
	}
	ptrace(PTRACE_SYSCALL, pid, 0, 0);
	waitpid(pid, &st, 0);

	struct iovec g = { .iov_base = buf, .iov_len = sizeof buf };
	if (ptrace(PTRACE_GETREGSET, pid, NT_X86_XSTATE, &g) < 0) {
		fprintf(stderr, "GETREGSET XSTATE: %s\n", strerror(errno));
		kill(pid, SIGKILL);
		waitpid(pid, &st, 0);
		return 2;
	}

	const struct tc cases[] = {
		{ NT_PRFPREG, 28,  "FX28"  },
		{ NT_PRFPREG, 32,  "FX32"  },
		{ NT_PRFPREG, 256, "FX256" },
		{ NT_X86_XSTATE, 172, "XS172" },
		{ NT_X86_XSTATE, 176, "XS176" },
		{ NT_X86_XSTATE, 520, "XS520" },
	};
	for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		struct iovec v = { .iov_base = buf, .iov_len = cases[i].len };
		errno = 0;
		long rc = ptrace(PTRACE_SETREGSET, pid,
				 (void *)(unsigned long)cases[i].type, &v);
		int en = errno;
		int ok = (rc == -1 && v.iov_len == cases[i].len);
		printf("RAW %s req=%zu raw_rc=%ld errno=%d(%s) post_iov=%zu %s\n",
		       cases[i].tag, cases[i].len, rc, en, strerror(en),
		       v.iov_len, ok ? "OK" : "UNEXPECTED");
		if (!ok)
			fails++;
	}

	kill(pid, SIGKILL);
	waitpid(pid, &st, 0);
	if (fails) {
		fprintf(stderr, "RAW-REGSET: %d unexpected observations\n",
			fails);
		return 3;
	}
	printf("ptrace-raw-regset OK: rejected SET raw_rc=-1 and "
	       "post_iov_len==requested_len for all 6 lengths\n");
	return 0;
}
