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
void gpu_relay_thread_start(struct gpu_relay_page *p);

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

/* GPU event relay (0024): a CLONE_THREAD child of the GPU worker.  Sharing
 * the file table lets it poll the device descriptors and worker eventfds; it
 * never issues an RM ioctl or mapping call.  The protocol mirrored here is
 * documented in stub-data.h beside struct gpu_relay_page. */
void __attribute__((used, section(".__syscall_stub")))
gpu_relay_thread_start(struct gpu_relay_page *p)
{
	struct gpu_relay_work *w = (void *)((unsigned long)p +
		2 * UM_KERN_PAGE_SIZE);
	unsigned long long mask = ~0UL, value;
	unsigned char byte;
	int notify_pending = 0;

	stub_syscall4(__NR_rt_sigprocmask, SIG_SETMASK,
		      (unsigned long)&mask, 0, sizeof(mask));

	for (;;) {
		int n = 0, flush_idx = -1, changed = 0, i, r;

		/* Retire closing slots before rebuilding the poll set: the
		 * worker fd is closed here, so a descriptor number the
		 * worker reuses afterwards can never be touched through a
		 * stale pollfd. */
		for (i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++) {
			struct gpu_relay_entry *e = &p->entry[i];

			if (e->state != GPU_RELAY_STATE_CLOSING)
				continue;
			stub_syscall1(__NR_close, e->fd);
			e->fd = -1;
			e->state = GPU_RELAY_STATE_EMPTY;
			e->ack = GPU_RELAY_ACK_CLOSED;
			e->ack_gen = e->gen;
			changed = 1;
		}

		w->pfds[n].fd = p->kick_fd;
		w->pfds[n].events = 0x001; /* POLLIN */
		w->pfds[n].revents = 0;
		n++;
		/* When a previous notify could not be queued, arm POLLOUT so
		 * the send is retried without a busy loop or a lost wakeup. */
		if (notify_pending) {
			w->pfds[n].fd = p->notify_fd;
			w->pfds[n].events = 0x004; /* POLLOUT */
			w->pfds[n].revents = 0;
			flush_idx = n;
			n++;
		}
		for (i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++) {
			struct gpu_relay_entry *e = &p->entry[i];

			w->snap_gen[i] = e->gen;
			w->last_level[i] = e->level;
			if (e->state != GPU_RELAY_STATE_ACTIVE || e->fd < 0)
				continue;
			w->slot_of[n] = (unsigned short)i;
			w->pfds[n].fd = e->fd;
			w->pfds[n].events = 0x001;
			w->pfds[n].revents = 0;
			n++;
		}

		stub_syscall3(__NR_poll, (unsigned long)w->pfds, n,
			      (unsigned long)-1);

		/* The kick socket only breaks poll early; drain every merged
		 * datagram.  Kicks are advisory and may be coalesced. */
		while (stub_syscall3(__NR_read, p->kick_fd,
				     (unsigned long)&byte, 1) > 0)
			;

		for (i = 1 + (flush_idx >= 0); i < n; i++) {
			short rev = w->pfds[i].revents;
			unsigned int slot = w->slot_of[i];
			struct gpu_relay_entry *e = &p->entry[slot];

			if (e->state != GPU_RELAY_STATE_ACTIVE ||
			    e->gen != w->snap_gen[slot])
				continue;
			if (e->kind == UML_GPU_RELAY_KIND_EVENTFD) {
				unsigned long long got = 0;

				if (rev & 0x001) {
					while (stub_syscall3(__NR_read, e->fd,
						(unsigned long)&value,
						sizeof(value)) ==
					       (long)sizeof(value))
						got += value;
					if (got) {
						e->value += got;
						e->value_gen++;
						changed = 1;
					}
				}
			} else {
				unsigned int level;

				/* Evaluate even when revents is 0: a clean
				 * poll round must clear a previously
				 * reported level (level-triggered guest
				 * semantics).  A level-ready fd keeps
				 * returning its bits, so comparing against
				 * the last publication cannot storm. */
				level = rev & (0x001 | 0x008 | 0x010 | 0x020);
				if (level != w->last_level[slot]) {
					e->level = level;
					e->level_gen++;
					changed = 1;
				}
			}
		}

		if (flush_idx >= 0 &&
		    (w->pfds[flush_idx].revents & 0x004) && !changed)
			changed = 1;
		if (!changed && !notify_pending)
			continue;
		byte = 'e';
		r = stub_syscall3(__NR_write, p->notify_fd,
				  (unsigned long)&byte, 1);
		notify_pending = r != 1;
	}
}

/* Clone the relay thread with the GPU worker's TGID and file table.  Only
 * compile-time constants and the page argument may appear here: the stub
 * page is mapped without relocations. */
static __always_inline long stub_gpu_relay_clone(unsigned long page)
{
	long result;
	register unsigned long data asm("r9") = page;
	struct gpu_relay_page *p = (struct gpu_relay_page *)page;
	unsigned long tid = page + offsetof(struct gpu_relay_page, clear_tid);
	unsigned long stack_top = page + STUB_THREAD_PAGES * UM_KERN_PAGE_SIZE;

	asm volatile("syscall\n\t"
		     "test %%rax, %%rax\n\t"
		     "jnz 1f\n\t"
		     "mov %%r9, %%rdi\n\t"
		     "call gpu_relay_thread_start\n\t"
		     "ud2\n\t"
		     "1:"
		     : "=a"(result), "+r"(data)
		     : "0" (__NR_clone),
		       "D" (CLONE_VM | CLONE_FILES | CLONE_SIGHAND |
			    CLONE_THREAD | CLONE_PARENT_SETTID |
			    CLONE_CHILD_CLEARTID),
		       "S" (stack_top),
		       "d" (tid),
		       "r" (0UL),
		       "r" (tid)
		     : "rcx", "r11", "memory");
	(void)p;
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
			else if (sc->mem.prot == GPU_OP_IOCTL_ADDR)
				/* Explicit worker VA (STUB_GPU_SHADOW); the
				 * exact ioctl request number is mem.length.
				 * Never a kernel pointer: the kernel only
				 * names fixed worker mappings here. */
				d->gpu_result = stub_syscall3(__NR_ioctl, sc->mem.fd,
					sc->mem.length, sc->mem.addr);
			else if (sc->mem.prot == GPU_OP_MMAP) {
				/* Same-VA device mmap: guest VA == host VA;
				 * RM pgoff and the UVM offset==VA rule are
				 * validated by the host driver itself. */
				res = stub_syscall6(STUB_MMAP_NR,
					sc->mem.addr, sc->mem.length,
					sc->mem.arg_prot,
					MAP_FIXED | (sc->mem.map_flags &
						    (MAP_SHARED | MAP_PRIVATE)),
					sc->mem.fd, sc->mem.offset);
				d->gpu_result = res == sc->mem.addr ? 0 : res;
			}
			else if (sc->mem.prot == GPU_OP_MUNMAP)
				/* Raw syscall result: 0 or negative errno. */
				d->gpu_result = stub_syscall2(__NR_munmap,
					sc->mem.addr, sc->mem.length);
			else if (sc->mem.prot == GPU_OP_MPROTECT)
				d->gpu_result = stub_syscall3(__NR_mprotect,
					sc->mem.addr, sc->mem.length,
					sc->mem.arg_prot);
			else if (sc->mem.prot == GPU_OP_EVENTFD)
				/* EFD_CLOEXEC (02000000) | EFD_NONBLOCK
				 * (00004000): the relay drains this fd. */
				d->gpu_fd = stub_syscall2(__NR_eventfd2, 0,
					02000000 | 00004000);
			else if (sc->mem.prot == GPU_OP_RELAY_START) {
				struct gpu_relay_page *rp =
					(void *)sc->mem.addr;
				int kick, notify;

				/* Keep private copies: every descriptor this
				 * batch received is closed at batch end. */
				kick = stub_syscall1(__NR_dup,
					fd_map[sc->mem.fd]);
				notify = stub_syscall1(__NR_dup,
					fd_map[sc->mem.length]);
				rp->kick_fd = kick;
				rp->notify_fd = notify;
				d->gpu_result = stub_gpu_relay_clone(
					sc->mem.addr);
			}
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
