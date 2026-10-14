/* Minimal shim for compiling the shared UML stub protocol header in the
 * userspace deterministic models (tests/models).  Not used by the kernel. */
#ifndef _KSHIM_COMPILER_TYPES_H
#define _KSHIM_COMPILER_TYPES_H
#include <stdint.h>
#ifndef __packed
#define __packed __attribute__((packed))
#endif
#ifndef __aligned
#define __aligned(x) __attribute__((aligned(x)))
#endif
#ifndef noinline
#define noinline __attribute__((noinline))
#endif
#endif
