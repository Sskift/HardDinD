
#if __BITS_PER_LONG > 32
			/* [0] Load upper 32bit of instruction pointer from seccomp_data */
			BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
				 (offsetof(struct seccomp_data, instruction_pointer) + 4)),

			/* [1] Jump forward 3 instructions if the upper address is not identical */
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (STUB_PAGE_ADDR) >> 32, 0, 3),
#endif
			/* [2] Load lower 32bit of instruction pointer from seccomp_data */
			BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
				 (offsetof(struct seccomp_data, instruction_pointer))),

			/* [3] Mask out lower bits */
			BPF_STMT(BPF_ALU | BPF_AND | BPF_K, 0xfffff000),

			/* [4] Jump to [6] if the lower bits are not on the expected page */
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (STUB_PAGE_ADDR) & 0xfffff000, 1, 0),

			/* [5] Trap call, allow */
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),

			/* [6,7] Check architecture */
			BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
				 offsetof(struct seccomp_data, arch)),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K,
				 UM_SECCOMP_ARCH_NATIVE, 1, 0),

			/* [8] Kill (for architecture check) */
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),

			/* [9] Load syscall number */
			BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
				 offsetof(struct seccomp_data, nr)),

			/* Device syscalls remain unavailable unless explicitly enabled. */
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_dup, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, GPU_BRIDGE_CONST ?
				 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_ioctl, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, GPU_BRIDGE_CONST ?
				 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
			/* Each match is followed by its action: no fragile jump counts. */
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_getpid, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_futex, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_recvmsg, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
		/* GPU bridge relay thread syscalls (gated on gpu_bridge; with the
		 * bridge off each pair KILLs): read() drains the kick socket and
		 * worker eventfds, poll() waits on worker descriptors, eventfd2()
		 * creates RM OS events and mprotect() propagates guest protection
		 * changes. */
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_read, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, GPU_BRIDGE_CONST ?
			 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_poll, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, GPU_BRIDGE_CONST ?
			 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_eventfd2, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, GPU_BRIDGE_CONST ?
			 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_mprotect, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, GPU_BRIDGE_CONST ?
			 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
		/* write(): with the bridge on, the relay notify datagram goes to
		 * a runtime-dup descriptor (not expressible as a BPF immediate),
		 * so the stub instruction page is the boundary and write is
		 * allowed like read/poll.  With the bridge off the owner rule
		 * stands: only the 0017 completion byte on fd 0 (and only with
		 * gpu_async) survives the args[0] gate.  write falls INTO the
		 * gate (jt); every other syscall jumps OVER it (jf=5) to the
		 * close/mmap/... chain; both gate RETs are terminal. */
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_write,
			 GPU_BRIDGE_CONST ? 0 : 1, 5),
		BPF_STMT(BPF_RET | BPF_K, GPU_BRIDGE_CONST ?
			 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
		BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
			 offsetof(struct seccomp_data, args[0])),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, GPU_ASYNC_CONST ?
			 SECCOMP_RET_ALLOW : SECCOMP_RET_KILL_PROCESS),
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_close, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, STUB_MMAP_NR, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_munmap, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
#ifdef __i386__
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_set_thread_area, 0, 1),
#else
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_arch_prctl, 0, 1),
#endif
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_rt_sigreturn, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_dup2, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_close_range, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_sigaltstack, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_rt_sigprocmask, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_prctl, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_exit, 0, 1),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
			BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),