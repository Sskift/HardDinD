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
 * pointer or a UML kernel address.  Blocks larger than 128 bytes use the
 * 16 KiB STUB_GPU_SHADOW bounce area (largest fixed 550.120 production
 * payload is UVM_MAP_EXTERNAL_ALLOCATION at 9264 bytes). */
#define STUB_GPU_PAYLOAD_SIZE 128

/* STUB_SYSCALL_GPU operations carried in mem.prot. */
#define GPU_OP_OPEN			0
#define GPU_OP_IOCTL			1
#define GPU_OP_CLOSE			2
#define GPU_OP_REGISTER_FD		3
#define GPU_OP_IOCTL_PAYLOAD		4	/* addr := &gpu_payload */
#define GPU_OP_MMAP			5
#define GPU_OP_MUNMAP			6
#define GPU_OP_EVENTFD			7
#define GPU_OP_RELAY_START		8
#define GPU_OP_IOCTL_ADDR		9	/* addr := explicit worker VA */
#define GPU_OP_MPROTECT			10

/*
 * Event relay protocol between the UML kernel and the GPU worker's relay
 * thread (first two pages of the STUB_GPU_RELAY slot).  All state transitions
 * happen under the per-mm gpu_lock on the kernel side; the relay thread is
 * the only writer of the ack/result fields.
 *
 * Slot generations close the descriptor-reuse race: the kernel snapshots
 * gen when publishing a slot and ignores results carrying an older gen, and
 * the worker fd is only closed after the relay acknowledges it excluded the
 * slot from its poll set (state GPU_RELAY_STATE_CLOSED at the current gen).
 * Eventfd counters are monotonically accumulated by the relay and delivered
 * as deltas by the kernel, so a full notify socket or an extra kick cannot
 * lose counts; device readiness is reported only on level transitions which
 * gives the guest level-triggered poll semantics without a relay busy loop.
 */
#define UML_GPU_RELAY_MAX_ENTRIES	192
#define UML_GPU_RELAY_KIND_DEVICE	1
#define UML_GPU_RELAY_KIND_EVENTFD	2
#define GPU_RELAY_STATE_EMPTY		0
#define GPU_RELAY_STATE_ACTIVE		1
#define GPU_RELAY_STATE_CLOSING		2
/* relay -> kernel acknowledgement */
#define GPU_RELAY_ACK_NONE		0
#define GPU_RELAY_ACK_EXCLUDED		1	/* slot left out of the poll set */
#define GPU_RELAY_ACK_CLOSED		2	/* worker fd has been closed */
#define GPU_RELAY_MAGIC			0x52475055U /* "UPGR" */

struct gpu_relay_entry {
	/* kernel -> relay (first 16 bytes) */
	int			fd;		/* worker fd, -1 if empty */
	unsigned int		gen;		/* bumped per publication */
	unsigned char		kind;		/* UML_GPU_RELAY_KIND_* */
	unsigned char		state;		/* GPU_RELAY_STATE_* */
	unsigned char		ack;		/* GPU_RELAY_ACK_* (relay->k) */
	unsigned char		pad0;
	unsigned int		ack_gen;
	unsigned long long	value;		/* EVENTFD: accumulated count */
	/* relay -> kernel */
	unsigned int		level;		/* DEVICE: current poll mask */
	unsigned int		level_gen;	/* bumped on level change */
	unsigned int		value_gen;	/* bumped after accumulation */
	unsigned int		sticky;		/* reserved: latched errors */
};

struct gpu_relay_page {
	unsigned int		magic;
	volatile unsigned int	stop;
	int			kick_fd;	/* relay polls/read */
	int			notify_fd;	/* relay writes one byte */
	int			clear_tid;	/* CLONE_CHILD_CLEARTID word */
	int			pad2;
	struct gpu_relay_entry	entry[UML_GPU_RELAY_MAX_ENTRIES];
};

/* Relay poll workspace.  The table fills the first two slot pages; the
 * workspace lives at the start of the remaining two pages (fixed worker
 * VA, no relocations) and the relay stack grows down from the slot top, so
 * the relay function keeps a tiny frame. */
struct gpu_pollfd {
	int			fd;
	short			events;
	short			revents;
};
struct gpu_relay_work {
	struct gpu_pollfd	pfds[UML_GPU_RELAY_MAX_ENTRIES + 2];
	unsigned short		slot_of[UML_GPU_RELAY_MAX_ENTRIES + 2];
	unsigned int		snap_gen[UML_GPU_RELAY_MAX_ENTRIES];
	unsigned int		last_level[UML_GPU_RELAY_MAX_ENTRIES];
};

struct stub_syscall {
	struct {
		unsigned long addr;
		unsigned long length;
		unsigned long offset;
		int fd;
		int prot;
		/* GPU_OP_MMAP: exact host prot/map flags, kept out of prot so
		 * the mapping broker's own MMAP opcodes stay unchanged. */
		int map_flags;
		int arg_prot;
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
