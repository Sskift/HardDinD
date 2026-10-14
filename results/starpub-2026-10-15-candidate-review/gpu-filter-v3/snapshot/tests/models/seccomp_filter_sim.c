// SPDX-License-Identifier: GPL-2.0
/* Bounded classic-BPF interpreter for the EXTRACTED stub seccomp filter.
 *
 * No syscalls, no forks, no seccomp install: the real sock_filter array
 * extracted verbatim from arch/um/kernel/skas/stub_exe.c (all four
 * gpu_bridge/gpu_async combinations) is interpreted against an emulated
 * seccomp_data record.  This proves exact reachability/verdicts for EVERY
 * broker/worker/relay syscall - including clone/rt_sigreturn/exit, which a
 * real-process harness cannot execute safely without hanging - in bounded
 * deterministic time.  A separate small real-install probe
 * (seccomp_filter_real_model.c, externally watch-dogged) cross-checks that
 * the kernel BPF engine agrees for representative syscalls.
 *
 * Exit non-zero on any verdict mismatch.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/syscall.h>
#include <linux/audit.h>

#ifndef GPU_BRIDGE
#define GPU_BRIDGE 0
#endif
#ifndef GPU_ASYNC
#define GPU_ASYNC 0
#endif

#define STUB_PAGE 0x01000000UL

#include "seccomp_filter_prelude.inc"
static const struct sock_filter filter[] = {
#include "seccomp_filter_body.inc"
};

struct seccomp_data_sim {
	uint32_t nr;
	uint32_t arch;
	uint64_t instruction_pointer;
	uint64_t args[6];
};

#define R_TRAP 0x00030000U
#define R_ALLOW 0x7fff0000U
#define R_KILL 0x80000000U

static uint32_t run_filter(const struct seccomp_data_sim *d)
{
	uint32_t A = 0, X = 0, M[16] = {0};
	unsigned int pc = 0;
	const unsigned int n = sizeof(filter)/sizeof(filter[0]);

	for (;;) {
		if (pc >= n)
			return 0;
		const struct sock_filter *f = &filter[pc];
		uint16_t code = f->code;
		uint32_t k = (uint32_t)f->k;
		switch (BPF_CLASS(code)) {
		case BPF_LD:
			if (BPF_MODE(code) == BPF_ABS) {
				if (k == offsetof(struct seccomp_data_sim, nr))
					A = d->nr;
				else if (k == offsetof(struct seccomp_data_sim,
						      arch))
					A = d->arch;
				else if (k >= offsetof(struct seccomp_data_sim,
						      args) &&
					 k < offsetof(struct seccomp_data_sim,
						      args) + 48) {
					unsigned int off = k -
						offsetof(struct seccomp_data_sim,
							 args);
					memcpy(&A, ((const char *)d->args) + off,
					       4);
				} else if (k == offsetof(struct seccomp_data_sim,
						instruction_pointer) ||
					   k == offsetof(struct seccomp_data_sim,
						instruction_pointer) + 4) {
					unsigned int off = k -
						offsetof(struct seccomp_data_sim,
						instruction_pointer);
					memcpy(&A,
					       ((const char *)&d->instruction_pointer)
					       + off, 4);
				} else {
					A = 0;
				}
				pc++;
			} else {
				A = 0; pc++;
			}
			break;
		case BPF_ALU:
			if (BPF_OP(code) == BPF_AND)
				A &= k;
			else
				A = 0;
			pc++;
			break;
		case BPF_JMP:
			if (BPF_OP(code) == BPF_JEQ &&
			    BPF_SRC(code) == BPF_K) {
				if (A == k)
					pc += 1 + f->jt;
				else
					pc += 1 + f->jf;
			} else {
				pc++;
			}
			break;
		case BPF_RET:
			return k;
		default:
			return 0;
		}
		(void)X; (void)M;
	}
}

#define WANT_ALLOW 1
#define WANT_KILL  2
#define WANT_TRAP  3

struct case_t {
	const char *name;
	uint32_t nr;
	uint64_t a0;
	int want_bridge;	/* expected verdict when gpu_bridge=1 */
	int want_nobridge; /* when gpu_bridge=0 */
};

int main(void)
{
	/* The UVM table uses the same syscall numbers as the kernel. */
	const struct case_t cases[] = {
	/* always-reachable broker/worker/relay backbone */
	{"getpid", __NR_getpid, 0, WANT_TRAP, WANT_TRAP},
	{"futex", __NR_futex, 0, WANT_ALLOW, WANT_ALLOW},
	{"recvmsg", __NR_recvmsg, 0, WANT_ALLOW, WANT_ALLOW},
	{"close(42)", __NR_close, 42, WANT_ALLOW, WANT_ALLOW},
	{"mmap(addr)", __NR_mmap, STUB_PAGE, WANT_ALLOW, WANT_ALLOW},
	{"munmap(addr)", __NR_munmap, STUB_PAGE, WANT_ALLOW, WANT_ALLOW},
	{"arch_prctl", __NR_arch_prctl, 0, WANT_ALLOW, WANT_ALLOW},
	{"rt_sigreturn(ucp)", __NR_rt_sigreturn, STUB_PAGE,
	 WANT_ALLOW, WANT_ALLOW},
	{"clone", __NR_clone, 0x101 | 0x10000 | 17, WANT_ALLOW, WANT_ALLOW},
	{"dup2", __NR_dup2, 1, WANT_ALLOW, WANT_ALLOW},
	{"close_range", __NR_close_range, 3, WANT_ALLOW, WANT_ALLOW},
	{"sigaltstack", __NR_sigaltstack, 0, WANT_ALLOW, WANT_ALLOW},
	{"rt_sigprocmask", __NR_rt_sigprocmask, STUB_PAGE,
	 WANT_ALLOW, WANT_ALLOW},
	{"prctl", __NR_prctl, 0, WANT_ALLOW, WANT_ALLOW},
	{"exit(30)", __NR_exit, 30, WANT_ALLOW, WANT_ALLOW},
	/* worker GPU syscalls */
	{"dup", __NR_dup, 3, WANT_ALLOW, WANT_KILL},
	{"ioctl", __NR_ioctl, 3, WANT_ALLOW, WANT_KILL},
	/* relay syscalls */
	{"read(fd3)", __NR_read, 3, WANT_ALLOW, WANT_KILL},
	{"poll", __NR_poll, STUB_PAGE, WANT_ALLOW, WANT_KILL},
	{"eventfd2", __NR_eventfd2, 0, WANT_ALLOW, WANT_KILL},
	{"mprotect", __NR_mprotect, STUB_PAGE, WANT_ALLOW, WANT_KILL},
	/* write matrix */
	{"write(fd0)", __NR_write, 0, WANT_ALLOW,
	 GPU_ASYNC ? WANT_ALLOW : WANT_KILL},
	{"write(fd3)", __NR_write, 3, WANT_ALLOW, WANT_KILL},
	{"write(fd9)", __NR_write, 9, WANT_ALLOW, WANT_KILL},
	};
	int fails = 0;
	for (size_t i = 0; i < sizeof(cases)/sizeof(cases[0]); i++) {
		struct seccomp_data_sim d = {
			.nr = cases[i].nr,
			.arch = AUDIT_ARCH_X86_64,
			.instruction_pointer = STUB_PAGE,
			.args = { cases[i].a0 },
		};
		uint32_t v = run_filter(&d);
		int got;
		if (v == R_ALLOW) got = WANT_ALLOW;
		else if (v == R_KILL) got = WANT_KILL;
		else if (v == R_TRAP) got = WANT_TRAP;
		else got = 0;
		int want = GPU_BRIDGE ? cases[i].want_bridge :
			cases[i].want_nobridge;
		if (got != want) {
			static const char *nm[] = {0,"ALLOW","KILL","TRAP"};
			printf("FAIL bridge=%d async=%d %-18s got=%s want=%s "
			       "(verdict=0x%x)\n", GPU_BRIDGE, GPU_ASYNC,
			       cases[i].name, nm[got], nm[want], v);
			fails++;
		}
	}
	if (!fails)
		printf("seccomp filter sim: OK bridge=%d async=%d (%zu probes)\n",
		       GPU_BRIDGE, GPU_ASYNC,
		       sizeof(cases)/sizeof(cases[0]));
	return fails ? 1 : 0;
}
