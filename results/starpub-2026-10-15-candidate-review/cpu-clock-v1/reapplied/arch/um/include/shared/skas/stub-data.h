/* SPDX-License-Identifier: GPL-2.0 */
/*

 * Copyright (C) 2015 Thomas Meyer (thomas@m3y3r.de)
 * Copyright (C) 2005 Jeff Dike (jdike@karaya.com)
 */

#ifndef __STUB_DATA_H
#define __STUB_DATA_H

#include <linux/compiler_types.h>
#include <as-layout.h>
#include <sysdep/tls.h>
#include <sysdep/stub-data.h>
#include <mm_id.h>

#include <stub-futex.h>

struct stub_init_data {
	int seccomp;
	int gpu_bridge;
	/* Permit the GPU completion-byte write(); 0017 async GPU wait. */
	int gpu_async;

	unsigned long stub_start;

	int stub_code_fd;
	unsigned long stub_code_offset;
	int stub_data_fd;
	unsigned long stub_data_offset;

	unsigned long signal_handler;
	unsigned long signal_restorer;
};

#define STUB_NEXT_SYSCALL(s) \
	((struct stub_syscall *) (((unsigned long) s) + (s)->cmd_len))

enum stub_syscall_type {
	STUB_SYSCALL_UNSET = 0,
	STUB_SYSCALL_MMAP,
	STUB_SYSCALL_MUNMAP,
	STUB_SYSCALL_CLONE,
	STUB_SYSCALL_MEMFD,
	STUB_SYSCALL_GPU,
};

#define STUB_MEMFD_SLOT (-1)
#define STUB_MEMFD_HOST_FD 1

/* Fixed RM ioctl block exchanged by STUB_SYSCALL_GPU operation 4.  It lives
 * inside the shared stub pages, so the GPU worker never dereferences a guest
 * pointer or a UML kernel address. */
#define STUB_GPU_PAYLOAD_SIZE 128

struct stub_syscall {
	struct {
		unsigned long addr;
		unsigned long length;
		unsigned long offset;
		int fd;
		int prot;
	} mem;

	enum stub_syscall_type syscall;
};

struct stub_data {
	long err;
	long clone_pid;
	long gpu_result;
	int gpu_fd;
	/* 0017: kernel sets gpu_async before handing a batch to the worker;
	 * the worker clears it while sending the one completion byte.  The
	 * two bytes fit in the padding before the 128-byte payload, so
	 * neither gpu_payload nor syscall_data offsets change. */
	unsigned char gpu_async;
	unsigned char gpu_debug;
	int child_socket;
	int memfd_cached;
	int syscall_fd_num;

	int syscall_data_len;

	/* Bounded RM blocks, including a snapshotted 40-byte child. */
	unsigned char gpu_payload[STUB_GPU_PAYLOAD_SIZE] __aligned(64);

	/* Reserve header space for the 128-byte GPU payload. */
	struct stub_syscall syscall_data[(UM_KERN_PAGE_SIZE - 320) / sizeof(struct stub_syscall)] __aligned(16);

	/* data shared with signal handler (only used in seccomp mode) */
	short restart_wait;
	unsigned int futex;
	unsigned int spin_limit;
	int signal;
	unsigned short si_offset;
	unsigned short mctx_offset;

	/* seccomp architecture specific state restore */
	struct stub_data_arch arch_data;

	/* Stack for our signal handlers and for calling into . */
	unsigned char sigstack[UM_KERN_PAGE_SIZE] __aligned(UM_KERN_PAGE_SIZE);
};

#endif
