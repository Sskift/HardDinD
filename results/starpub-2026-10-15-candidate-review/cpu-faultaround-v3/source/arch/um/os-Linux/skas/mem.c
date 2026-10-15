// SPDX-License-Identifier: GPL-2.0
/*
 * Copyright (C) 2021 Benjamin Berg <benjamin@sipsolutions.net>
 * Copyright (C) 2002 - 2007 Jeff Dike (jdike@{addtoit,linux.intel}.com)
 */

#include <stddef.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <mem_user.h>
#include <mem.h>
#include <init.h>
#include <as-layout.h>
#include <mm_id.h>
#include <os.h>
#include <ptrace_user.h>
#include <registers.h>
#include <skas.h>
#include <stub-futex.h>
#include <linux/futex.h>
#include <sysdep/ptrace.h>
#include <sysdep/stub.h>
#include "../internal.h"

extern char __syscall_stub_start[];

void syscall_stub_dump_error(struct mm_id *mm_idp)
{
	struct stub_data *proc_data = (void *)mm_idp->stack;
	struct stub_syscall *sc;

	if (proc_data->syscall_data_len < 0 ||
	    proc_data->syscall_data_len >= ARRAY_SIZE(proc_data->syscall_data))
		panic("Syscall data was corrupted by stub (len is: %d, expected maximum: %d)!",
			proc_data->syscall_data_len,
			mm_idp->syscall_data_len);

	sc = &proc_data->syscall_data[proc_data->syscall_data_len];

	printk(UM_KERN_ERR "%s : length = %d, last offset = %d",
		__func__, mm_idp->syscall_data_len,
		proc_data->syscall_data_len);
	printk(UM_KERN_ERR "%s : stub syscall type %d failed, return value = 0x%lx\n",
		__func__, sc->syscall, proc_data->err);

	print_hex_dump(UM_KERN_ERR, "    syscall data: ", 0,
		       16, 4, sc, sizeof(*sc), 0);

	if (using_seccomp) {
		printk(UM_KERN_ERR "%s: FD map num: %d", __func__,
		       mm_idp->syscall_fd_num);
		print_hex_dump(UM_KERN_ERR,
				"    FD map: ", 0, 16,
				sizeof(mm_idp->syscall_fd_map[0]),
				mm_idp->syscall_fd_map,
				sizeof(mm_idp->syscall_fd_map), 0);
	}
}

static inline unsigned long *check_init_stack(struct mm_id * mm_idp,
					      unsigned long *stack)
{
	if (stack == NULL) {
		stack = (unsigned long *) mm_idp->stack + 2;
		*stack = 0;
	}
	return stack;
}

static unsigned long syscall_regs[MAX_REG_NR];

static int __init init_syscall_regs(void)
{
	get_safe_registers(syscall_regs, NULL);

	syscall_regs[REGS_IP_INDEX] = STUB_CODE +
		((unsigned long) stub_syscall_handler -
		 (unsigned long) __syscall_stub_start);
	syscall_regs[REGS_SP_INDEX] = STUB_DATA +
		offsetof(struct stub_data, sigstack) +
		sizeof(((struct stub_data *) 0)->sigstack) -
		sizeof(void *);

	return 0;
}

__initcall(init_syscall_regs);

static inline long do_syscall_stub(struct mm_id *mm_idp)
{
	struct stub_data *proc_data = (void *)mm_idp->stack;
	int n, i;
	int err, pid = mm_idp->pid;

	/* Inform process how much we have filled in. */
	proc_data->syscall_data_len = mm_idp->syscall_data_len;
	proc_data->syscall_fd_num = mm_idp->syscall_fd_num;

	if (using_seccomp) {
		proc_data->restart_wait = 1;
		wait_stub_done_seccomp(mm_idp, 0, 1);
	} else {
		n = ptrace_setregs(pid, syscall_regs);
		if (n < 0) {
			printk(UM_KERN_ERR "Registers -\n");
			for (i = 0; i < MAX_REG_NR; i++)
				printk(UM_KERN_ERR "\t%d\t0x%lx\n", i, syscall_regs[i]);
			panic("%s : PTRACE_SETREGS failed, errno = %d\n",
			      __func__, -n);
		}

		err = ptrace(PTRACE_CONT, pid, 0, 0);
		if (err)
			panic("Failed to continue stub, pid = %d, errno = %d\n",
			      pid, errno);

		wait_stub_done(pid);
	}

	/*
	 * proc_data->err will be negative if there was an (unexpected) error.
	 * In that case, syscall_data_len points to the last executed syscall,
	 * otherwise it will be zero (but we do not need to rely on that).
	 */
	if (proc_data->err < 0) {
		syscall_stub_dump_error(mm_idp);

		/* Store error code in case someone tries to add more syscalls */
		mm_idp->syscall_data_len = proc_data->err;
	} else {
		mm_idp->syscall_data_len = 0;
	}

	if (using_seccomp)
		mm_idp->syscall_fd_num = 0;

	return mm_idp->syscall_data_len;
}

int syscall_stub_flush(struct mm_id *mm_idp)
{
	int res;

	if (mm_idp->syscall_data_len == 0)
		return 0;

	/* If an error happened already, report it and reset the state. */
	if (mm_idp->syscall_data_len < 0) {
		res = mm_idp->syscall_data_len;
		mm_idp->syscall_data_len = 0;
		return res;
	}

	res = do_syscall_stub(mm_idp);
	mm_idp->syscall_data_len = 0;

	return res;
}

struct stub_syscall *syscall_stub_alloc(struct mm_id *mm_idp)
{
	struct stub_syscall *sc;
	struct stub_data *proc_data = (struct stub_data *) mm_idp->stack;

	if (mm_idp->syscall_data_len > 0 &&
	    mm_idp->syscall_data_len == ARRAY_SIZE(proc_data->syscall_data))
		do_syscall_stub(mm_idp);

	if (mm_idp->syscall_data_len < 0) {
		/* Return dummy to retain error state. */
		sc = &proc_data->syscall_data[0];
	} else {
		sc = &proc_data->syscall_data[mm_idp->syscall_data_len];
		mm_idp->syscall_data_len += 1;
	}
	memset(sc, 0, sizeof(*sc));

	return sc;
}

static struct stub_syscall *syscall_stub_get_previous(struct mm_id *mm_idp,
						      int syscall_type,
						      unsigned long virt)
{
	if (mm_idp->syscall_data_len > 0) {
		struct stub_data *proc_data = (void *) mm_idp->stack;
		struct stub_syscall *sc;

		sc = &proc_data->syscall_data[mm_idp->syscall_data_len - 1];

		if (sc->syscall == syscall_type &&
		    sc->mem.addr + sc->mem.length == virt)
			return sc;
	}

	return NULL;
}

static int get_stub_fd(struct mm_id *mm_idp, int fd)
{
	int i;

	/* Find an FD slot (or flush and use first) */
	if (!using_seccomp)
		return fd;

	/* Already crashed, value does not matter */
	if (mm_idp->syscall_data_len < 0)
		return 0;

	/* Find existing FD in map if we can allocate another syscall */
	if (mm_idp->syscall_data_len <
	    ARRAY_SIZE(((struct stub_data *)NULL)->syscall_data)) {
		for (i = 0; i < mm_idp->syscall_fd_num; i++) {
			if (mm_idp->syscall_fd_map[i] == fd)
				return i;
		}

		if (mm_idp->syscall_fd_num < STUB_MAX_FDS) {
			i = mm_idp->syscall_fd_num;
			mm_idp->syscall_fd_map[i] = fd;

			mm_idp->syscall_fd_num++;

			return i;
		}
	}

	/* FD map full or no syscall space available, continue after flush */
	do_syscall_stub(mm_idp);
	mm_idp->syscall_fd_map[0] = fd;
	mm_idp->syscall_fd_num = 1;

	return 0;
}

int map(struct mm_id *mm_idp, unsigned long virt, unsigned long len, int prot,
	int phys_fd, unsigned long long offset)
{
	struct stub_syscall *sc;

	/* Compress with previous syscall if that is possible */
	sc = syscall_stub_get_previous(mm_idp, STUB_SYSCALL_MMAP, virt);
	if (sc && sc->mem.prot == prot &&
	    sc->mem.offset == MMAP_OFFSET(offset - sc->mem.length)) {
		int prev_fd = sc->mem.fd;

		if (using_seccomp)
			prev_fd = sc->mem.fd == STUB_MEMFD_SLOT ?
				mm_idp->cached_phys_fd : mm_idp->syscall_fd_map[sc->mem.fd];

		if (phys_fd == prev_fd) {
			sc->mem.length += len;
			return 0;
		}
	}

	if (parallel_userspace) {
		/* Brokers never run guest instructions. Keep the stable physical
		 * memory FD there instead of sending and closing it on every fault.
		 * Runners still receive only their control socket and close all
		 * other inherited descriptors during bootstrap.
		 */
		if (mm_idp->cached_phys_fd != phys_fd) {
			int slot = get_stub_fd(mm_idp, phys_fd);
			sc = syscall_stub_alloc(mm_idp);
			sc->syscall = STUB_SYSCALL_MEMFD;
			sc->mem.fd = slot;
			mm_idp->cached_phys_fd = phys_fd;
		}
		phys_fd = STUB_MEMFD_SLOT;
	} else {
		phys_fd = get_stub_fd(mm_idp, phys_fd);
	}

	sc = syscall_stub_alloc(mm_idp);
	sc->syscall = STUB_SYSCALL_MMAP;
	sc->mem.addr = virt;
	sc->mem.length = len;
	sc->mem.prot = prot;
	sc->mem.fd = phys_fd;
	sc->mem.offset = MMAP_OFFSET(offset);

	return 0;
}

int unmap(struct mm_id *mm_idp, unsigned long addr, unsigned long len)
{
	struct stub_syscall *sc;

	/* Compress with previous syscall if that is possible */
	sc = syscall_stub_get_previous(mm_idp, STUB_SYSCALL_MUNMAP, addr);
	if (sc) {
		sc->mem.length += len;
		return 0;
	}

	sc = syscall_stub_alloc(mm_idp);
	sc->syscall = STUB_SYSCALL_MUNMAP;
	sc->mem.addr = addr;
	sc->mem.length = len;

	return 0;
}

/* Caller serializes the broker and pins its virtual CPU. */
int start_userspace_cpu(struct mm_id *broker, struct mm_id *child, unsigned long addr)
{
	struct stub_data *data = (void *)child->stack;
	struct stub_data *parent = (void *)broker->stack;
	struct stub_syscall *sc;
	unsigned long long offset;
	int sockets[2], fd, err;
	extern unsigned int uml_seccomp_spin_limit;

	if (socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0, sockets))
		return -errno;
	child->sock = sockets[0];
	data->spin_limit = uml_seccomp_spin_limit;
	fd = phys_mapping(uml_to_phys((void *)child->stack), &offset);
	map(broker, addr, STUB_THREAD_PAGES * UM_KERN_PAGE_SIZE,
	    UM_PROT_READ | UM_PROT_WRITE, fd, offset);
	err = syscall_stub_flush(broker);
	if (err)
		goto fail;
	fd = get_stub_fd(broker, sockets[1]);
	sc = syscall_stub_alloc(broker);
	sc->syscall = STUB_SYSCALL_CLONE;
	sc->mem.addr = addr;
	sc->mem.fd = fd;
	parent->clone_pid = -1;
	err = syscall_stub_flush(broker);
	if (err)
		goto fail;
	child->pid = parent->clone_pid;
	if (child->pid < 2) {
		err = -ECHILD;
		goto fail;
	}
	close(sockets[1]);
	wait_stub_done_seccomp(child, 1, 1);
	return child->pid > 0 && data->err == 0 ? 0 : -ECHILD;
fail:
	close(sockets[0]);
	close(sockets[1]);
	child->sock = 0;
	return err;
}

static int gpu_device_path(unsigned int index, char *path, size_t size)
{
	if (index == 255)
		snprintf(path, size, "/dev/nvidiactl");
	else if (index == 256)
		snprintf(path, size, "/dev/nvidia-uvm");
	else if (index <= 254)
		snprintf(path, size, "/dev/nvidia%u", index);
	else
		return -EINVAL;
	return 0;
}

int os_gpu_device_present(unsigned int index)
{
	char path[32];
	struct stat st;

	return !gpu_device_path(index, path, sizeof(path)) &&
		!stat(path, &st) && S_ISCHR(st.st_mode);
}

/*
 * Hand one queued stub batch to the worker without waiting for it.  This is
 * the nonblocking half of wait_stub_done_seccomp(): queued descriptors go by
 * SCM_RIGHTS and ownership of the worker changes through the futex word, but
 * the caller returns immediately instead of parking in FUTEX_WAIT.  The worker
 * answers with one completion datagram on id->sock (see stub_signal_interrupt),
 * so the guest side can sleep schedulably on a socket IRQ while the host GPU
 * driver processes the batch.
 */
static int stub_submit_async(struct mm_id *mm_idp)
{
	struct stub_data *data = (void *)mm_idp->stack;

	data->syscall_data_len = mm_idp->syscall_data_len;
	data->syscall_fd_num = mm_idp->syscall_fd_num;
	data->restart_wait = 1;
	/* Publish before the phase handoff: the worker reads gpu_async only
	 * after observing FUTEX_IN_CHILD. */
	__atomic_store_n(&data->gpu_async, 1, __ATOMIC_RELEASE);

	if (mm_idp->syscall_fd_num) {
		const char byte = 0;
		struct iovec iov = {
			.iov_base = (void *)&byte,
			.iov_len = sizeof(byte),
		};
		union {
			char data[CMSG_SPACE(sizeof(mm_idp->syscall_fd_map))];
			struct cmsghdr align;
		} ctrl;
		struct msghdr msgh = {
			.msg_iov = &iov,
			.msg_iovlen = 1,
		};
		unsigned int fds_size = sizeof(int) * mm_idp->syscall_fd_num;
		struct cmsghdr *cmsg;
		int sent;

		msgh.msg_control = ctrl.data;
		msgh.msg_controllen = CMSG_SPACE(fds_size);
		cmsg = CMSG_FIRSTHDR(&msgh);
		cmsg->cmsg_level = SOL_SOCKET;
		cmsg->cmsg_type = SCM_RIGHTS;
		cmsg->cmsg_len = CMSG_LEN(fds_size);
		memcpy(CMSG_DATA(cmsg), mm_idp->syscall_fd_map, fds_size);

		/* The socket is nonblocking; with a single outstanding batch
		 * (gpu_lock) its buffer cannot be full.  Report anything else
		 * instead of stranding the worker without its descriptors. */
		errno = 0;
		CATCH_EINTR(sent = syscall(__NR_sendmsg, mm_idp->sock,
					   &msgh, 0));
		if (sent < 0)
			return -errno;
	}

	data->signal = 0;
	if (stub_futex_handoff(&data->futex, FUTEX_IN_CHILD))
		CATCH_EINTR(syscall(__NR_futex, &data->futex,
				    FUTEX_WAKE, 1, NULL, NULL, 0));
	return 0;
}

/* Fill the shared GPU request block: optional host device open, payload copy
 * and stub entry.  On success *host_fd_out holds an FD the caller must close
 * after the descriptors have been handed to the worker. */
static long gpu_request_prepare(struct mm_id *id, int operation, int fd,
				unsigned int command, unsigned long argument,
				void *payload, size_t payload_size,
				int *host_fd_out)
{
	struct stub_data *data = (void *)id->stack;
	struct stub_syscall *sc;
	int error, host_fd = -1;
	char path[32];

	*host_fd_out = -1;
	if (id->pid < 2)
		return -ECHILD;
	if (payload_size > STUB_GPU_SHADOW_PAGES * UM_KERN_PAGE_SIZE)
		return -EINVAL;
	error = syscall_stub_flush(id);
	if (error)
		return error;
	if (operation == 0) {
		error = gpu_device_path(command, path, sizeof(path));
		if (error)
			return error;
		host_fd = os_open_file(path, of_cloexec(of_rdwr(OPENFLAGS())),
				       0);
		if (host_fd < 0)
			return host_fd;
		fd = get_stub_fd(id, host_fd);
	}
	data->gpu_result = -EIO;
	if (operation == 3)
		data->gpu_fd = (int)argument;
	if (payload_size) {
		if (payload_size <= sizeof(data->gpu_payload)) {
			/* Never let stale stub bytes reach the host beyond the
			 * block the kernel actually validated. */
			memset(data->gpu_payload, 0, sizeof(data->gpu_payload));
			memcpy(data->gpu_payload, payload, payload_size);
		}
		/* Larger blocks live in gpu_shadow_pages, which the kernel
		 * fills through its own direct map: those pages are the ones
		 * mapped at STUB_GPU_SHADOW in this worker, so no copy is
		 * needed here.  The kernel never sends its direct-map address
		 * to the worker; it names STUB_GPU_SHADOW explicitly below. */
	}
	sc = syscall_stub_alloc(id);
	sc->syscall = STUB_SYSCALL_GPU;
	sc->mem.prot = operation;
	sc->mem.fd = fd;
	sc->mem.length = command;
	if (operation == GPU_OP_IOCTL_PAYLOAD) {
		sc->mem.addr = payload_size > sizeof(data->gpu_payload) ?
			STUB_GPU_SHADOW :
			(unsigned long)&data->gpu_payload[0];
	} else if (operation == GPU_OP_IOCTL_ADDR) {
		/* Explicit worker virtual address supplied by the kernel. */
		sc->mem.addr = argument;
	} else {
		sc->mem.addr = argument;
	}
	*host_fd_out = host_fd;
	return 0;
}

/* Caller owns the GPU mutex and disables guest interrupts for this handoff.
 * Operation 4 forwards a fixed-size RM parameter block: the UML kernel copies
 * it into the shared stub payload before the ioctl and back afterwards, so
 * the worker never sees a guest or kernel pointer. */
long os_gpu_command(struct mm_id *id, int operation, int fd,
		    unsigned int command, unsigned long argument,
		    void *payload, size_t payload_size)
{
	struct stub_data *data = (void *)id->stack;
	int error, host_fd = -1;

	error = gpu_request_prepare(id, operation, fd, command, argument,
				    payload, payload_size, &host_fd);
	if (error)
		return error;
	error = syscall_stub_flush(id);
	if (host_fd >= 0)
		os_close_file(host_fd);
	if (error)
		return error;
	/* nvidia nv.c skips copyout when the ioctl itself returns -EFAULT;
	 * preserve the caller's input block in that case.  Blocks larger than
	 * gpu_payload ride the STUB_GPU_SHADOW alias: the caller's buffer is
	 * the kernel direct map of those same pages, so it must NOT be touched
	 * through the worker-only STUB_GPU_SHADOW address here. */
	if (payload_size && payload_size <= sizeof(data->gpu_payload) &&
	    data->gpu_result != -EFAULT)
		memcpy(payload, data->gpu_payload, payload_size);
	return data->gpu_result;
}

/* 0017 asynchronous variant: queue the batch and wake the worker, but never
 * block this host thread in a futex.  The guest collects the result with
 * os_gpu_command_result() after the completion socket becomes readable. */
long os_gpu_command_submit(struct mm_id *id, int operation, int fd,
			   unsigned int command, unsigned long argument,
			   void *payload, size_t payload_size)
{
	int error, host_fd = -1;

	error = gpu_request_prepare(id, operation, fd, command, argument,
				    payload, payload_size, &host_fd);
	if (error) {
		if (host_fd >= 0)
			os_close_file(host_fd);
		return error;
	}
	error = stub_submit_async(id);
	/* The batch now belongs to the worker; clear the queue bookkeeping so
	 * nothing is requeued against the in-flight array.  gpu_lock prevents
	 * any reuse until the completion has been collected. */
	id->syscall_data_len = 0;
	id->syscall_fd_num = 0;
	if (host_fd >= 0)
		os_close_file(host_fd);
	return error;
}

/* Collect a batch previously submitted with os_gpu_command_submit().  Called
 * only after the completion IRQ (or the worker-death wake); reports -ECHILD
 * for a dead worker or the batch's stub error if the worker rejected it. */
long os_gpu_command_result(struct mm_id *id, void *payload,
			   size_t payload_size)
{
	struct stub_data *data = (void *)id->stack;

	if (id->pid < 2)
		return -ECHILD;
	/* Pairs with the worker's ACQ_REL claim on gpu_async before the
	 * completion write(); socket delivery already orders the stores, the
	 * fence is here for readers that only see the wakeup. */
	__atomic_thread_fence(__ATOMIC_ACQUIRE);
	if (data->err < 0)
		return data->err;
	/* See os_gpu_command: large blocks are observed by the caller through
	 * the shadow's kernel direct map, never through the worker VA. */
	if (payload_size && payload_size <= sizeof(data->gpu_payload) &&
	    data->gpu_result != -EFAULT)
		memcpy(payload, data->gpu_payload, payload_size);
	return data->gpu_result;
}

/* GPU mapping bridge (0024).  Every helper here runs serialized by the
 * per-mm gpu_lock; mmap/munmap/mprotect touch only this GPU worker's own
 * queue, while os_gpu_pages_map/unmap additionally require the broker
 * serialization (sync_tlb_lock) the caller in gpu_start provides. */

long os_gpu_mmap(struct mm_id *id, int host_fd, unsigned long addr,
		 unsigned long len, int prot, int map_flags,
		 unsigned long offset)
{
	struct stub_data *data = (void *)id->stack;
	struct stub_syscall *sc;
	int error;

	error = syscall_stub_flush(id);
	if (error)
		return error;
	data->gpu_result = -EIO;
	sc = syscall_stub_alloc(id);
	sc->syscall = STUB_SYSCALL_GPU;
	sc->mem.prot = GPU_OP_MMAP;
	sc->mem.addr = addr;
	sc->mem.length = len;
	sc->mem.offset = offset;
	sc->mem.fd = host_fd;
	sc->mem.map_flags = map_flags;
	sc->mem.arg_prot = prot;
	error = syscall_stub_flush(id);
	return error ? error : data->gpu_result;
}

long os_gpu_munmap(struct mm_id *id, unsigned long addr, unsigned long len)
{
	struct stub_data *data = (void *)id->stack;
	struct stub_syscall *sc;
	int error;

	error = syscall_stub_flush(id);
	if (error)
		return error;
	data->gpu_result = -EIO;
	sc = syscall_stub_alloc(id);
	sc->syscall = STUB_SYSCALL_GPU;
	sc->mem.prot = GPU_OP_MUNMAP;
	sc->mem.addr = addr;
	sc->mem.length = len;
	error = syscall_stub_flush(id);
	return error ? error : data->gpu_result;
}

long os_gpu_mprotect(struct mm_id *id, unsigned long addr,
		     unsigned long len, int prot)
{
	struct stub_data *data = (void *)id->stack;
	struct stub_syscall *sc;
	int error;

	error = syscall_stub_flush(id);
	if (error)
		return error;
	data->gpu_result = -EIO;
	sc = syscall_stub_alloc(id);
	sc->syscall = STUB_SYSCALL_GPU;
	sc->mem.prot = GPU_OP_MPROTECT;
	sc->mem.addr = addr;
	sc->mem.length = len;
	sc->mem.arg_prot = prot;
	error = syscall_stub_flush(id);
	return error ? error : data->gpu_result;
}

/* Create a non-blocking, close-on-exec eventfd inside the GPU worker.  The
 * RM caches the descriptor at ALLOC_OS_EVENT time, so it must belong to the
 * worker file table. */
int os_gpu_eventfd(struct mm_id *id)
{
	struct stub_data *data = (void *)id->stack;
	struct stub_syscall *sc;
	int error;

	error = syscall_stub_flush(id);
	if (error)
		return error;
	data->gpu_fd = -1;
	sc = syscall_stub_alloc(id);
	sc->syscall = STUB_SYSCALL_GPU;
	sc->mem.prot = GPU_OP_EVENTFD;
	error = syscall_stub_flush(id);
	if (error)
		return error;
	return data->gpu_fd < 0 ? -EIO : data->gpu_fd;
}

/* Map kernel-allocated relay/shadow pages into the broker at the fixed stub
 * VAs.  GPU workers are CLONE_VM children of the broker, so pages mapped
 * here before the worker clone exists are inherited at the same address.
 * Caller holds the broker's sync_tlb_lock around map+flush and the
 * subsequent start_userspace_cpu clone. */
int os_gpu_pages_map(struct mm_id *broker, unsigned long pages,
		     unsigned long host_addr, unsigned long count_pages)
{
	unsigned long long offset;
	int fd;

	fd = phys_mapping(uml_to_phys((void *)pages), &offset);
	if (map(broker, host_addr, count_pages * UM_KERN_PAGE_SIZE,
		 UM_PROT_READ | UM_PROT_WRITE, fd, offset))
		return -EINVAL;
	return syscall_stub_flush(broker);
}

/* Only used when GPU startup fails before the worker exists. */
int os_gpu_pages_unmap(struct mm_id *broker, unsigned long host_addr,
		       unsigned long count_pages)
{
	unmap(broker, host_addr, count_pages * UM_KERN_PAGE_SIZE);
	return syscall_stub_flush(broker);
}

/* Start the GPU worker event relay thread.  Two non-blocking AF_UNIX
 * datagram socketpairs connect the relay to the UML kernel: a kick the
 * kernel writes to break the relay out of poll(), and a notify socket the
 * relay writes one datagram to whenever the relay table gained data.  The
 * relay dups the passed descriptors to its own fixed numbers, so the
 * per-batch SCM_RIGHTS copies being closed at batch end cannot steal them. */
int os_gpu_relay_start(struct mm_id *id, int *kernel_kick_fd,
		       int *kernel_notify_fd)
{
	struct stub_data *data = (void *)id->stack;
	struct stub_syscall *sc;
	int sv_kick[2] = { -1, -1 }, sv_notify[2] = { -1, -1 };
	int slot_kick, slot_notify, error;

	if (socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK,
		       0, sv_kick) ||
	    socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK,
		       0, sv_notify)) {
		error = -errno;
		goto fail;
	}
	error = syscall_stub_flush(id);
	if (error)
		goto fail;
	slot_kick = get_stub_fd(id, sv_kick[1]);
	slot_notify = get_stub_fd(id, sv_notify[1]);
	data->gpu_result = -EIO;
	sc = syscall_stub_alloc(id);
	sc->syscall = STUB_SYSCALL_GPU;
	sc->mem.prot = GPU_OP_RELAY_START;
	sc->mem.addr = STUB_GPU_RELAY;
	sc->mem.fd = slot_kick;
	sc->mem.length = slot_notify;
	error = syscall_stub_flush(id);
	if (error || data->gpu_result < 2) {
		if (!error)
			error = -ECHILD;
		goto fail;
	}
	os_close_file(sv_kick[1]);
	os_close_file(sv_notify[1]);
	*kernel_kick_fd = sv_kick[0];
	*kernel_notify_fd = sv_notify[0];
	return 0;
fail:
	if (sv_kick[0] >= 0)
		os_close_file(sv_kick[0]);
	if (sv_kick[1] >= 0)
		os_close_file(sv_kick[1]);
	if (sv_notify[0] >= 0)
		os_close_file(sv_notify[0]);
	if (sv_notify[1] >= 0)
		os_close_file(sv_notify[1]);
	return error;
}

void os_gpu_relay_kick(int fd)
{
	const char byte = 'k';

	/* The socket is non-blocking; kicks are level hints (the relay always
	 * re-evaluates the whole table after waking), so a merged or failed
	 * write cannot lose state. */
	if (fd >= 0 && write(fd, &byte, 1) < 0) {
		/* Merged or failed kicks cannot lose state: the relay always
		 * re-evaluates the whole table after waking. */
	}
}
