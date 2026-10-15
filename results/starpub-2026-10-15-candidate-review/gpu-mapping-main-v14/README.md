# Main build selector matches the tested mapping-v14 source

On Starpub, the owner extracted the original patched-file subset from the
fixed Linux 7.2.10 archive and ran the actual main
`scripts/apply-kernel-patches.py --gpu-mapping-experiment` selector. The
complete default + async GPU + 0027 + mapping-0024 series applied successfully.
All **33 tracked source files** exactly matched the owner-built kernel that
passed [four-card Docker CUDA](../gpu-mapping-v14/README.md). The main runtime
config also matched that candidate's input. The command exited 0 and its
process group was empty.

The frozen selector, build script, Makefile, all patches, source manifest,
command output and comparison are retained. This binds the new optional main
target to the actual tested source; it is not a claim that every GPU ABI or
performance requirement is satisfied. No second full kernel build or repeated
CUDA run is represented here. `python3 recheck.py` checks the frozen binding.
