/* Auto-generated from stub_exe.c - do not edit. */
#include <stddef.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/syscall.h>
#include <linux/audit.h>
#define STUB_MMAP_NR __NR_mmap
#define UM_SECCOMP_ARCH_NATIVE AUDIT_ARCH_X86_64
/* Fixed stub page: both models mmap MAP_FIXED at exactly this address, so
 * the BPF_K immediate is a compile-time constant. */
#if defined(FILTER_RUNTIME_PAGE)
/* Real-process model: automatic array, BPF_K bound to the runtime page. */
extern unsigned long g_stub_page;
#define STUB_PAGE_ADDR g_stub_page
#else
#define STUB_PAGE_ADDR 0x01000000UL
#endif
#define GPU_BRIDGE_CONST GPU_BRIDGE
#define GPU_ASYNC_CONST GPU_ASYNC
