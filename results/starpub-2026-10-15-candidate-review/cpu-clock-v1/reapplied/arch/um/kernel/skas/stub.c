// SPDX-License-Identifier: GPL-2.0
/*
 * Copyright (C) 2021 Benjamin Berg <benjamin@sipsolutions.net>
 */

#include <sysdep/stub.h>

#include <linux/futex.h>
#include <sys/socket.h>
#include <errno.h>
#include <linux/sched.h>
#include <linux/prctl.h>

/*
 * Known security issues
 *
 * Userspace can jump to this address to execute *any* syscall that is
 * permitted by the stub. As we will return afterwards, it can do
 * whatever it likes, including:
 * - Tricking the kernel into handing out the memory FD
 * - Using this memory FD to read/write all physical memory
 * - Running in parallel to the kernel processing a syscall
 *   (possibly creating data races?)
 * - Blocking e.g. SIGALRM to avoid time based scheduling
 *
 * To avoid this, the permitted location for each syscall needs to be
 * checked for in the SECCOMP filter (which is reasonably simple). Also,
 * more care will need to go into considerations how the code might be
 * tricked by using a prepared stack (or even modifying the stack from
 * another thread in case SMP support is added).
 *
 * As for the SIGALRM, the best counter measure will be to check in the
 * kernel that the process is reporting back the SIGALRM in a timely
 * fashion.
 */
#ifdef __x86_64__
void stub_thread_start(struct stub_data *d);

void __attribute__((used, noreturn)) __section(".__syscall_stub")
stub_thread_start(struct stub_data *d)
{
	struct { void *sp; int flags; unsigned long size; } alt = {
		.sp = d, .size = STUB_DATA_PAGES * UM_KERN_PAGE_SIZE,
	};
	unsigned long long mask = 0;

	/* clone has a private file table and signal dispositions, but shared VM. */
	if (stub_syscall2(__NR_dup2, d->child_socket, 0) < 0 ||
	    stub_syscall3(__NR_close_range, 1, ~0U, 0) < 0 ||
	    stub_syscall2(__NR_sigaltstack, (unsigned long)&alt, 0) < 0 ||
	    stub_syscall2(__NR_prctl, PR_SET_PDEATHSIG, SIGKILL) < 0 ||
	    stub_syscall4(__NR_rt_sigprocmask, SIG_SETMASK,
			  (unsigned long)&mask, 0, sizeof(mask)) < 0)
		stub_syscall1(__NR_exit, 40);

	/* This particular stub syscall deliberately traps for initial handoff. */
	stub_syscall0(__NR_getpid);
	stub_syscall1(__NR_exit, 41);
	__builtin_unreachable();
}

static __always_inline long stub_clone(unsigned long addr)
{
	long result;
	register unsigned long data asm("r9") = addr;
	asm volatile("syscall\n\t"
		     "test %%rax, %%rax\n\t"
		     "jnz 1f\n\t"
		     "mov %%r9, %%rdi\n\t"
		     "call stub_thread_start\n\t"
		     "ud2\n"
		     "1:"
		     : "=a" (result), "+r" (data)
		     : "0" (__NR_clone), "D" (CLONE_VM | CLONE_PARENT | SIGCHLD),
		       "S" (addr + STUB_THREAD_PAGES * UM_KERN_PAGE_SIZE)
		     : "rcx", "r11", "memory");
	return result;
}
#endif

static __always_inline int syscall_handler(struct stub_data *d, int fd_map[STUB_MAX_FDS])
{
	int i;
	unsigned long res;
	int fd;

	for (i = 0; i < d->syscall_data_len; i++) {
		struct stub_syscall *sc = &d->syscall_data[i];

		if (sc->syscall == STUB_SYSCALL_GPU) {
#ifdef __x86_64__
			/* 0017 diagnostic only (gpu_async_debug=2): a bounded
			 * host-side stall before the ioctl, used to prove the
			 * guest keeps scheduling while this worker is busy.
			 * ~3e8 TSC cycles is about 0.1 s at 3 GHz; the exact
			 * duration depends on the host clock.  Never enabled
			 * by default. */
			if (d->gpu_debug & 0x2) {
				unsigned long long start, now;
				unsigned int low, high;

				asm volatile("rdtsc" : "=a"(low), "=d"(high));
				start = ((unsigned long long)high << 32) | low;
				do {
					asm volatile("rdtsc" : "=a"(low), "=d"(high));
					now = ((unsigned long long)high << 32) | low;
				} while (now - start < 300000000ULL);
			}
#endif
			/* Expected device errno must not poison the mapping queue. */
			if (sc->mem.prot == 0)
				d->gpu_result = stub_syscall1(__NR_dup, fd_map[sc->mem.fd]);
			else if (sc->mem.prot == 1)
				d->gpu_result = stub_syscall3(__NR_ioctl, sc->mem.fd,
					sc->mem.length, sc->mem.addr);
			else if (sc->mem.prot == 2)
				d->gpu_result = stub_syscall1(__NR_close, sc->mem.fd);
			else if (sc->mem.prot == 3)
				d->gpu_result = stub_syscall3(__NR_ioctl, sc->mem.fd,
					sc->mem.length, (unsigned long)&d->gpu_fd);
			else if (sc->mem.prot == 4)
				/* Fixed NVOS00/21/64 block in shared stub memory. */
				d->gpu_result = stub_syscall3(__NR_ioctl, sc->mem.fd,
					sc->mem.length,
					(unsigned long)&d->gpu_payload[0]);
			else
				d->gpu_result = -EOPNOTSUPP;
			continue;
		}

		switch (sc->syscall) {
		case STUB_SYSCALL_MEMFD:
			res = stub_syscall2(__NR_dup2, fd_map[sc->mem.fd], STUB_MEMFD_HOST_FD);
			if (res != STUB_MEMFD_HOST_FD) {
				d->err = res;
				d->syscall_data_len = i;
				return -1;
			}
			d->memfd_cached = 1;
			break;
		case STUB_SYSCALL_MMAP:
			if (d->memfd_cached && sc->mem.fd == STUB_MEMFD_SLOT)
				fd = STUB_MEMFD_HOST_FD;
			else if (fd_map)
				fd = fd_map[sc->mem.fd];
			else
				fd = sc->mem.fd;

			res = stub_syscall6(STUB_MMAP_NR,
					    sc->mem.addr, sc->mem.length,
					    sc->mem.prot,
					    MAP_SHARED | MAP_FIXED,
					    fd, sc->mem.offset);
			if (res != sc->mem.addr) {
				d->err = res;
				d->syscall_data_len = i;
				return -1;
			}
			break;
#ifdef __x86_64__
		case STUB_SYSCALL_CLONE: {
			struct stub_data *child = (void *)sc->mem.addr;
			child->child_socket = fd_map[sc->mem.fd];
			d->clone_pid = stub_clone(sc->mem.addr);
			if (d->clone_pid < 0) {
				d->err = d->clone_pid;
				d->syscall_data_len = i;
				return -1;
			}
			break;
		}
#endif
		case STUB_SYSCALL_MUNMAP:
			res = stub_syscall2(__NR_munmap,
					    sc->mem.addr, sc->mem.length);
			if (res) {
				d->err = res;
				d->syscall_data_len = i;
				return -1;
			}
			break;
		default:
			d->err = -95; /* EOPNOTSUPP */
			d->syscall_data_len = i;
			return -1;
		}
	}

	d->err = 0;
	d->syscall_data_len = 0;

	return 0;
}

void __section(".__syscall_stub")
stub_syscall_handler(void)
{
	syscall_handler(get_stub_data(), NULL);

	trap_myself();
}

void __section(".__syscall_stub")
stub_signal_interrupt(int sig, siginfo_t *info, void *p)
{
	struct stub_data *d = ((ucontext_t *)p)->uc_stack.ss_sp;
	char rcv_data;
	union {
		char data[CMSG_SPACE(sizeof(int) * STUB_MAX_FDS)];
		struct cmsghdr align;
	} ctrl = {};
	struct iovec iov = {
		.iov_base = &rcv_data,
		.iov_len = 1,
	};
	struct msghdr msghdr = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
		.msg_control = &ctrl,
		.msg_controllen = sizeof(ctrl),
	};
	ucontext_t *uc = p;
	struct cmsghdr *fd_msg;
	int *fd_map;
	int num_fds;
	/* 0017: set only by the frame that finishes a submitted batch, so a
	 * nested signal re-entering this handler cannot send the completion. */
	int notify = 0;
	long res;

	d->signal = sig;
	d->si_offset = (unsigned long)info - (unsigned long)&d->sigstack[0];
	d->mctx_offset = (unsigned long)&uc->uc_mcontext - (unsigned long)&d->sigstack[0];

restart_wait:
	if (stub_futex_handoff(&d->futex, FUTEX_IN_KERN)) {
		do {
			res = stub_syscall3(__NR_futex, (unsigned long)&d->futex,
					    FUTEX_WAKE, 1);
		} while (res == -EINTR);
	}

	/* 0017 asynchronous GPU completion: report only AFTER parking in
	 * FUTEX_IN_KERN.  The kernel must not publish the next batch (and its
	 * FUTEX_IN_CHILD handoff) before this phase exchange is visible, or
	 * the exchange below would clobber it.  Claiming the flag with
	 * ACQ_REL orders every gpu_result/gpu_payload store before the
	 * datagram; the kernel sleeps schedulably on the socket IRQ instead
	 * of blocking in the handoff futex with guest interrupts disabled.
	 * A notification that cannot be delivered is unrecoverable: exit so
	 * the SIGCHLD reaper fails the waiter instead of leaving it asleep. */
	if (notify) {
		notify = 0;
		if (__atomic_exchange_n(&d->gpu_async, 0, __ATOMIC_ACQ_REL)) {
			const char done = 0;
			long sent;

			do {
				sent = stub_syscall3(__NR_write, 0,
						     (unsigned long)&done, 1);
			} while (sent == -EINTR);
			if (sent != 1)
				stub_syscall1(__NR_exit, 72);
		}
	}

	res = 0;
#ifdef __x86_64__
	/* Kernel is concurrently consuming this request; never spin unbounded. */
	for (unsigned int spin = 0; spin < d->spin_limit && spin < 10000; spin++) {
		if (stub_futex_phase(&d->futex) != FUTEX_IN_KERN)
			break;
		asm volatile("pause" ::: "memory");
	}
#endif
	while (stub_futex_phase(&d->futex) == FUTEX_IN_KERN) {
		if (!stub_futex_prepare_wait(&d->futex, FUTEX_IN_KERN))
			break;
		res = stub_syscall4(__NR_futex, (unsigned long)&d->futex,
				    FUTEX_WAIT, FUTEX_IN_KERN | STUB_FUTEX_WAITING, 0);
		if (res < 0 && res != -EINTR && res != -EAGAIN)
			break;
	}

	if (res < 0 && res != -EAGAIN && res != -EINTR) {
		d->err = res;
		stub_syscall1(__NR_exit, 70);
	}

	if (d->syscall_data_len) {
		/* Read passed FDs (if any) */
		num_fds = 0;
		fd_map = NULL;
		if (d->syscall_fd_num) {
			/* recvmsg changes these fields; brokers reuse this frame. */
			msghdr.msg_controllen = sizeof(ctrl);
			msghdr.msg_flags = 0;
			do {
				res = stub_syscall3(__NR_recvmsg, 0, (unsigned long)&msghdr, 0);
			} while (res == -EINTR);
			fd_msg = msghdr.msg_control;
			fd_map = (void *)CMSG_DATA(fd_msg);
			if (res == iov.iov_len && msghdr.msg_controllen >= CMSG_LEN(0) &&
			    fd_msg->cmsg_level == SOL_SOCKET && fd_msg->cmsg_type == SCM_RIGHTS)
				num_fds = (fd_msg->cmsg_len - CMSG_LEN(0)) / sizeof(int);
			if (res < 0 || (msghdr.msg_flags & MSG_CTRUNC) ||
			    num_fds != d->syscall_fd_num || num_fds > STUB_MAX_FDS) {
				d->err = res < 0 ? res : -EMSGSIZE;
				stub_syscall1(__NR_exit, 71);
			}
		}

		/* Try running queued syscalls. */
		res = syscall_handler(d, fd_map);

		while (num_fds) {
			int passed_fd = fd_map[--num_fds];
			if (!d->memfd_cached || passed_fd != STUB_MEMFD_HOST_FD)
				stub_syscall2(__NR_close, passed_fd, 0);
		}
	} else {
		res = 0;
	}

	if (res < 0 || d->restart_wait) {
		/* Report SIGSYS if we restart. */
		d->signal = SIGSYS;
		d->restart_wait = 0;
		/* 0017: this frame completed queued work; its parking pass
		 * sends the async completion byte (if this batch used it). */
		notify = 1;

		goto restart_wait;
	}

	/* Restore arch dependent state that is not part of the mcontext */
	stub_seccomp_restore_state(&d->arch_data);

	/* Return so that the host modified mcontext is restored. */
}

void __section(".__syscall_stub")
stub_signal_restorer(void)
{
	/* We must not have anything on the stack when doing rt_sigreturn */
	stub_syscall0(__NR_rt_sigreturn);
}
