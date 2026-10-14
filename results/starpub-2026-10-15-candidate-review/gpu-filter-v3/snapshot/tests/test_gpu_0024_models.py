#!/usr/bin/env python3
"""Deterministic model tests for the 0024 GPU mapping repair.

These compile and run small userspace models against the pinned NVIDIA
550.120 headers (sources.lock.json -> nvidia_abi_reference) and the shared
UML stub headers.  They never touch a GPU; they pin ABI sizes/offsets and
the relay/VMA protocol invariants the kernel implementation relies on.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
MODELS = REPO / "tests" / "models"
SHARED = REPO / ".work" / "linux-7.2.10" / "arch" / "um" / "include" / "shared"

DEFAULT_REF = "/root/harddind-rebuild/arkclaude/reference/nvidia-550.120"
NV_REF = pathlib.Path(os.environ.get("NV_ABI_REF", DEFAULT_REF))


def have_ref():
    return (NV_REF / "src/common/sdk/nvidia/inc/nvos.h").is_dir() or \
        (NV_REF / "src/common/sdk/nvidia/inc/nvos.h").exists()


class AbiModels(unittest.TestCase):
    def run_model(self, src, expect, includes):
        cc = os.environ.get("HOSTCC", "gcc")
        with tempfile.TemporaryDirectory() as d:
            binary = pathlib.Path(d) / "model"
            subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra",
                            *sum((["-I", str(i)] for i in includes), []),
                            "-o", str(binary), str(src)],
                           check=True, capture_output=True)
            out = subprocess.run([str(binary)], check=True,
                                 capture_output=True, text=True).stdout
        expected = (MODELS / expect).read_text()
        self.assertEqual(sorted(out.splitlines()),
                         sorted(expected.splitlines()))

    @unittest.skipUnless(have_ref(), "pinned NVIDIA reference tree missing")
    def test_rm_block_layouts(self):
        inc = [NV_REF / "src/common/sdk/nvidia/inc",
               NV_REF / "src/nvidia/arch/nvalloc/unix/include"]
        self.run_model(MODELS / "rm_abi_model.c", "rm_abi_expected.txt", inc)

    @unittest.skipUnless(have_ref(), "pinned NVIDIA reference tree missing")
    def test_uvm_command_table(self):
        inc = [NV_REF / "kernel-open/nvidia-uvm",
               NV_REF / "src/common/sdk/nvidia/inc",
               NV_REF / "kernel-open/common/inc"]
        self.run_model(MODELS / "uvm_abi_model.c", "uvm_abi_expected.txt", inc)

    @unittest.skipUnless(have_ref(), "pinned NVIDIA reference tree missing")
    def test_control_flat_sizes_root(self):
        inc = [NV_REF / "src/common/sdk/nvidia/inc"]
        self.run_model(MODELS / "ctl_abi_model.c", "ctl_abi_expected.txt", inc)

    @unittest.skipUnless(have_ref(), "pinned NVIDIA reference tree missing")
    def test_control_flat_sizes_subdevice(self):
        inc = [NV_REF / "src/common/sdk/nvidia/inc"]
        self.run_model(MODELS / "ctl2080_abi_model.c",
                       "ctl2080_abi_expected.txt", inc)

    @unittest.skipUnless(shutil.which("gcc"), "gcc required")
    def test_relay_protocol(self):
        self.assertTrue(SHARED.is_dir(),
                        "private patched kernel tree missing; apply series")
        cc = os.environ.get("HOSTCC", "gcc")
        with tempfile.TemporaryDirectory() as d:
            binary = pathlib.Path(d) / "relay"
            subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra",
                            "-DUM_KERN_PAGE_SIZE=4096",
                            "-I", str(MODELS / "kshim"),
                            "-I", str(SHARED),
                            "-I", str(SHARED / "skas"),
                            "-o", str(binary),
                            str(MODELS / "relay_protocol_model.c")],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)

    @unittest.skipUnless(shutil.which("gcc"), "gcc required")
    def test_vma_lifetime(self):
        cc = os.environ.get("HOSTCC", "gcc")
        with tempfile.TemporaryDirectory() as d:
            binary = pathlib.Path(d) / "vma"
            subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra",
                            "-o", str(binary),
                            str(MODELS / "vma_lifetime_model.c")],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
