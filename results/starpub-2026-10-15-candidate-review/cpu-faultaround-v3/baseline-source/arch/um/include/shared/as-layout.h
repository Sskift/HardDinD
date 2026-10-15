/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Copyright (C) 2007 Jeff Dike (jdike@{addtoit,linux.intel}.com)
 */

#ifndef __START_H__
#define __START_H__

#include <generated/asm-offsets.h>

/*
 * Stolen from linux/const.h, which can't be directly included since
 * this is used in userspace code, which has no access to the kernel
 * headers.  Changed to be suitable for adding casts to the start,
 * rather than "UL" to the end.
 */

/* Some constant macros are used in both assembler and
 * C code.  Therefore we cannot annotate them always with
 * 'UL' and other type specifiers unilaterally.  We
 * use the following macros to deal with this.
 */
#define STUB_START stub_start
#define STUB_CODE STUB_START
#define STUB_DATA (STUB_CODE + UM_KERN_PAGE_SIZE)
#define STUB_DATA_PAGES 2
/* Each CPU owns two data/signal pages and two bootstrap stack pages.
 * Slot 64 is the per-mm GPU worker, slot 65 its event relay thread (the
 * first two pages hold the shared relay table, the last two are its
 * stack) and slot 66 is the 16 KiB bounce block for forwarded RM/UVM
 * ioctl arguments.  The kernel always refers to slot 66 by its worker
 * virtual address (STUB_GPU_SHADOW); its kernel direct map is a distinct
 * address and is never written into a worker syscall argument. */
#define STUB_THREAD_SLOTS 67
#define STUB_GPU_DATA STUB_THREAD_DATA(64)
#define STUB_GPU_RELAY STUB_THREAD_DATA(65)
#define STUB_GPU_SHADOW STUB_THREAD_DATA(66)
#define STUB_GPU_SHADOW_PAGES 4
#define STUB_THREAD_PAGES 4
#define STUB_THREAD_DATA(cpu) (STUB_DATA + (STUB_DATA_PAGES + (cpu) * STUB_THREAD_PAGES) * UM_KERN_PAGE_SIZE)
#define STUB_SIZE ((1 + STUB_DATA_PAGES + STUB_THREAD_SLOTS * STUB_THREAD_PAGES) * UM_KERN_PAGE_SIZE)
#define STUB_END (STUB_START + STUB_SIZE)

#ifndef __ASSEMBLER__

#include <sysdep/ptrace.h>

struct task_struct;
extern struct task_struct *cpu_tasks[];

extern unsigned long long physmem_size;

extern unsigned long high_physmem;
extern unsigned long uml_physmem;
extern unsigned long uml_reserved;
extern unsigned long end_vm;
extern unsigned long start_vm;

extern unsigned long brk_start;

extern unsigned long stub_start;

extern int linux_main(int argc, char **argv, char **envp);
extern void uml_finishsetup(void);

struct siginfo;
extern void (*sig_info[])(int, struct siginfo *si, struct uml_pt_regs *, void *);

#endif

#endif
