# Actual CUDA lifecycle probe — v2 (owner source-review blocker fixes)

Status: **source-only delivery; no GPU/native/VM/Docker run by the
author.** Strict compile, direct-function fixture and parser/model tests
pass (GPU-free, bounded EX). Parent calibrates natively and then runs
inside Docker with the native baseline as `--native-reference`.

v1 is frozen verbatim under `v1-frozen/` (source/checker/tests/runner/
docs + `SHA256SUMS`); nothing there is modified. All v2 work is inside
`cuda-lifecycle/`.

## Known facts (explicit, not acceptance claims)

- **gpu-completion-owner-v1** patched kernel **b8a58b85** — the fixed
  GPU async completion path — and its 8/8 four-GPU async matrix passed.
  Source **0028** is still under review/controls.
- **mapping-owner-v14 ordinary internal Docker CUDA** succeeded on four
  A100s but is historical diagnostic evidence; it is **not** final
  acceptance.
- This v2 probe is built on the v14-fixed `cuda_probe.c` ABI and the
  proven lifecycle transport, but has itself not yet been executed on
  hardware by the author.

## Blocker fixes

1. **`parse_ordinals` always rejected valid lists.** The return was
   `cfg->ngpu?-1:-1` — failure for every non-empty list. Rewritten with
   manual tokenization: returns 0 on success; rejects empty string,
   leading/trailing/repeated commas (strtok_r silently collapsed these),
   junk/trailing chars, spaces/`+` signs, signed/unsigned overflow,
   out-of-range indices and duplicates; bounds `ngpu` to `MAX_ITEMS`.
   Direct coverage in `tests/args_fixture.c` (45 checks) compiled against
   the real translation unit — no driver needed.
2. **Unbounded default copy.** The count loop copied `count` ordinals
   into the fixed `cfg.ordinals[MAX_ITEMS]` array with no bound. The
   probe now fails before copying when `cuDeviceGetCount` exceeds
   `MAX_ITEMS`.
3. **`--iterations` vs `--cycles`.** Usage advertised `--iterations`
   but parsing only accepted `--cycles`. `--iterations` is now the
   documented flag; `--cycles` remains as a deprecated alias (the child
   wire protocol keeps `--cycles`). All numeric parent and child
   arguments use new strict converters `parse_int_strict` /
   `parse_ulong_strict` (whole-string, explicit first-digit check,
   errno + range): no atoi trailing junk, no silent wrap/zero. Valid
   boundary and malformed argv are tested on the real binary (malformed
   parent args rejected at rc 2 before any GPU call; child boundary
   values 1/32 cycles and 4096/1 MiB elements reach gate-EOF rc 3;
   malformed child values rc 2). A child-mode bug that rejected its own
   `--child` flag was also fixed.
4. **Native-reference peer parity.** `cuda_lifecycle_checks.py
   --native-reference <native.jsonl>`: strict UUID/order equality with
   the baseline inventory, full 12-pair matrix required in both logs,
   and per-pair `can_access` comparison — **guest false where native is
   true fails** ("parity regression"), guest true where native is false
   fails; accessible pairs must complete byte-checked HtoD/DtoD/Dtoh
   transfers in both phases; unsupported pairs must carry null transfer
   fields and never count as transfer passes. Without the flag, an
   honest all-false local matrix remains an accepted local diagnostic
   (unchanged v1 behavior). Owner always supplies native-before/after.
5. **Lock/runner honesty.** `run-native.sh` now acquires the global EX
   lock **once with `flock -w 180`** and holds it across compile, the
   GPU-free mock suite **and** the native GPU run + checker (the v1
   wrapper released before the GPU phase, allowing owner-experiment
   contention). No 20×900 s retry loop; a busy lock fails after 180 s
   for an explicit operator rerun. Nesting is forbidden via the
   `CUDA_LIFECYCLE_LOCK_HELD` guard (rc 2). The parent may bypass the
   wrapper and run steps directly, documented in the script header.

## GPU-free verification (single bounded EX acquisition, ≤4 jobs, ≤180 s)

- strict compile of probe and `args_fixture.c`, `gcc rc=0`,
  byte-reproducible probe (`build/compile.log`);
- direct-function args fixture: **45 checks, 0 failures**
  (`build/args-fixture.log`);
- **46** Python tests OK (`build/selftest.log`): v1 evidence tests plus
  native-reference parity (identical/native-true-guest-false/
  guest-true-native-false/UUID mismatch/incomplete matrix/missing
  native transfer/unreadable reference/honest-all-false) and real-binary
  strict-argv tests;
- nesting guard verified (rc 2); child boundary argv verified on the
  binary (gate-EOF rc 3).
- No CUDA/GPU/VM/Docker invocation by the author.

## Hashes (sha256)

```
f4a3e46422de5272752e2bc4e28c80f94a74e963d1c0c516ef4e8b6b2cf9631b  src/cuda_cuda_lifecycle_probe.c
bbff94bc834994b73762535fd98316003ede067c1d0f37621c916dfdba89d7f6  scripts/cuda_lifecycle_checks.py
fac1dfd5887933205641b54f7439d5eb5a596f71f23180d8ac8b95f4093a7506  tests/test_cuda_lifecycle_checks.py
a63fa88afebfc4520839a9a9e4a52fa72a4f285e933a18ddfa0c920480292c63  tests/args_fixture.c
9fb8033a82b2d35151c6a9dc2b10dd8628189ad14b8f9626d3022ebb7630998a  run-native.sh
646a40317aa43e87f30fdf4e7be0a908b134ca25163a1d0909003ca3a56cce57  docs/cuda-12.4-peer-api.txt
3b58bf033661215137383a7422f52ab29dff1b965a4e64f22b8dcc05692422b5  build/cuda-lifecycle-probe
14151f86497b4b358539eaf7123dc25b319c3362ece121d2b9031b8d91175918  build/cuda-lifecycle-args-fixture
38dd2d4d3dd0992129922dc22a4141cd66ab12cb965c19246c530a3b091647ce  v1-frozen/SHA256SUMS
```

## Owner execution

```
# native four-A100 host (single 180s-bounded EX hold; writes
# build/native.jsonl which is the parity baseline)
cuda-lifecycle/run-native.sh build/native.jsonl

# guest via the existing scripts/run-gpu-cuda.py transport, then:
python3 scripts/cuda_lifecycle_checks.py <guest.jsonl> \
    --uuids <native uuid csv> \
    --native-reference build/native.jsonl
# rerun native after the guest and diff inventory/baseline
```

Acceptance: checker `ok:true` native and guest — full inventory, balanced
8/4/4 phases with positive overlap, complete peer matrix where every
native-accessible pair is guest-accessible and byte-checked, honest
unsupported records only where native is also false, exit 0 and clean
owned cleanup.
