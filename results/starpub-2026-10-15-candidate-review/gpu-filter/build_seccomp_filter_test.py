#!/usr/bin/env python3
"""Extract the REAL sock_filter body from stub_exe.c and build the probe.

Pulls the array between 'struct sock_filter filter[] = {' and the matching
'};', substitutes kernel-side macros for a host userspace compile, and
builds tests/models/seccomp_filter_model for all four gpu_bridge/gpu_async
combinations.  Any reachability regression (unreachable close/mmap/clone,
write(fd>=3) killed, relay syscalls blocked) fails the generated probes.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
STUB_EXE = REPO / ".work/linux-7.2.10/arch/um/kernel/skas/stub_exe.c"
MODEL = REPO / "tests/models/seccomp_filter_model.c"
OUTDIR = REPO / ".work/seccomp-filter-test"

HEADER = r'''/* Auto-generated from stub_exe.c - do not edit. */
#include <stddef.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/syscall.h>
#include <linux/audit.h>
#define STUB_MMAP_NR __NR_mmap
#define UM_SECCOMP_ARCH_NATIVE AUDIT_ARCH_X86_64
/* Runtime page address (same harness as the round7 reviewer probe): the
 * filter is built on the stack, so non-constant immediates are emitted by
 * the compiler from the live struct value. */
extern unsigned long g_stub_page;
#define STUB_PAGE_ADDR g_stub_page
#define GPU_BRIDGE_CONST GPU_BRIDGE
#define GPU_ASYNC_CONST GPU_ASYNC
'''


def extract_body(text):
    m = re.search(r"struct sock_filter filter\[\]\s*=\s*\{(.*?)\n\s*\};",
                  text, re.S)
    if not m:
        raise SystemExit("filter array not found in stub_exe.c")
    return m.group(1)


def main():
    if not STUB_EXE.exists():
        print(f"missing {STUB_EXE}; apply the patch series first",
              file=sys.stderr)
        return 2
    body = extract_body(STUB_EXE.read_text())
    # BPF_K immediates must be compile-time constants: bind the real
    # init_data fields to probe macros/constants.
    body = body.replace("(init_data.stub_start)", "(STUB_PAGE_ADDR)")
    body = body.replace("init_data.gpu_bridge", "GPU_BRIDGE_CONST")
    body = body.replace("init_data.gpu_async", "GPU_ASYNC_CONST")
    OUTDIR.mkdir(parents=True, exist_ok=True)
    inc = OUTDIR / "seccomp_filter_body.inc"
    inc.write_text(body)
    (OUTDIR / "seccomp_filter_prelude.inc").write_text(HEADER)
    cc = os.environ.get("HOSTCC", "gcc")
    failed = []
    for bridge in (0, 1):
        for async_ in (0, 1):
            dst = OUTDIR / f"probe-b{bridge}-a{async_}"
            cmd = [cc, "-std=gnu11", "-Wall", "-O2",
                   f"-DGPU_BRIDGE={bridge}", f"-DGPU_ASYNC={async_}",
                   "-I", str(OUTDIR), "-I", str(MODEL.parent),
                   "-o", str(dst), str(MODEL)]
            subprocess.run(cmd, check=True)
            r = subprocess.run([str(dst)], capture_output=True, text=True)
            print(r.stdout.strip())
            if r.returncode != 0:
                print(r.stderr, file=sys.stderr)
                failed.append((bridge, async_))
    if failed:
        print("FAILED combinations:", failed, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
