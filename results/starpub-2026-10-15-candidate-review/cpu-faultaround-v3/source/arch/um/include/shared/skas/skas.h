/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Copyright (C) 2002 - 2007 Jeff Dike (jdike@{addtoit,linux.intel}.com)
 */

#ifndef __SKAS_H
#define __SKAS_H

#include <sysdep/ptrace.h>

extern int using_seccomp;
extern int seccomp_threads;
extern int seccomp_broker_cache;
void cleanup_broker_cache(void);
#define parallel_userspace (using_seccomp && seccomp_threads)
struct mm_id *current_mm_enter(void);
void current_mm_exit(struct mm_id *id);
struct mm_struct;
void gpu_bridge_teardown(struct mm_struct *mm);

void alarm_current_mm(void);

extern void new_thread_handler(void);
extern void handle_syscall(struct uml_pt_regs *regs);
extern unsigned long current_stub_stack(void);
extern struct mm_id *current_mm_id(void);
extern void current_mm_sync(void);
void initial_jmpbuf_lock(void);
void initial_jmpbuf_unlock(void);

#endif
