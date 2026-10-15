# 0005 shared POSIX locks — report v8 (focus runner correctness)

Addresses all points in `OWNER-V7-RUNNER-REVIEW.md`. **No backend, 0005
patch, probe, or standard-runner changes**: virtiofsd
`13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09`, 0005
`a7ffeca5fd5b847879d92b0f160035c88eee5440d6c95dd9b62b8c44ad51c37c`,
share_probe.c `66dc754a…`, run-posix-native.py and run-shared-locks.py all
byte-identical to v6/v7. Only `scripts/run-posix-focus.py` changed
(`6d198abb…`). All native checks under global `kernel-build.lock`, hard GNU
timeouts, ≤4 jobs; no guest/GPU/perf. Cleanup is by recorded pid/process
group/container name — no name-based kills. Old 47-case suite not re-run (no
source it consumes changed).

## Fixes

1. **Same-pidns oracle bug (the concrete correctness defect).** The v7 case
   used two `Probe(mode=docker)` instances for
   `getlk_same_pidns_exact_child_tgid`, but separate `docker run` containers
   are sibling pid namespaces — an exact-pid expectation there is impossible.
   The case now holds AND queries inside ONE probe (parent forks the holder,
   parent queries `posix-getlk`), so it is valid identically in native and
   docker modes; the reported pid is asserted equal to the fork child pid and
   the command identity is recorded. The cross-container check is a distinct
   case (`getlk_sibling_container_pid_zero`) that asserts **pid 0**; in native
   mode it is recorded `not_tested` with the reason and the native control
   pointer (exact tgid is already proven in the same-ns case / standard
   matrix) — it is never mislabeled tested.

2. **Guest transport path.** Docker transport now matches
   `scripts/harddind.py` CLI conventions exactly:
   `python3 harddind.py <work> <name> docker run --rm -i --mount … <image>
   /share-probe …` with the work directory passed explicitly (no host `/root`
   path assumptions, no bare `docker`). An optional
   `--pid container:<owned holder>` hook exists for explicit shared-pidns
   testing if the owner prefers that over the intra-probe fork query.

3. **Constructor cleanup + incremental identity/output.** `Probe.__init__`
   wraps Popen+readiness in try/except and calls its own `close()` on any
   failure (a spawn/readiness failure between `Popen` and `__enter__` can no
   longer leak). Each probe records `mode/uid/container/pid/pgid/path/cmd`;
   every case result (including a `_runner_error`) is flushed to a `.tmp` and
   atomically renamed to the output JSON immediately, with `complete:false`,
   so setup/teardown failures leave a persisted partial record; the final
   write sets `complete:true`. `close()` uses the stored Popen handle and its
   own process group with bounded wait then SIGKILL fallback; forced shutdown
   is surfaced (not silently asserted).

4. **Nested pidns gating.** `not_tested` is returned ONLY for exact
   unshare `EPERM`(1)/`EACCES`(13). Malformed responses, lock failures, and
   any other errno (or a missing `unshare_errno`) now produce `fail` —
   classifier covered explicitly. For the guest run the runner adds, only to
   the INTERNAL guest container when `POSIX_FOCUS_ALLOW_NS=1` is set in its
   environment, `--cap-add SYS_ADMIN --security-opt seccomp=unconfined`; outer
   host permissions/seccomp are never modified. The baseline default denial is
   recorded separately in `baseline_limitation`.

## Native bounded result

`evidence/v8/posix-focus-native-v8.json`, identical across 3 stability runs
(`focus-v8-s{1,2,3}.json`): **10 tested / 2 not_tested / 0 fail**.

- tested: fd-mode gates ×4 (RDONLY/WONLY × read/write → EBADF/OK), chmod-000
  held-fd lock, unpriv reopen EACCES ×2, held-lock conflict, root
  CAP_DAC_OVERRIDE informational, same-pidns exact child tgid (single probe).
- not_tested with exact evidence: sibling containers (requires docker);
  nested pidns (`unshare_errno:1` EPERM in this seccomp host).

Constructor self-cleanup was verified with real spawns (failed init reaps; a
live `cat` launched through the same handle is waited on with no zombie), and
identity `/proc/<pid>/exe` scans show no lingering probes after runs. Python
unit tests pass; build lock released.

## Owner guest invocation (not run by author)

```sh
# rebuilt static probe is already in <work>/share-probe (v6 hash e8193146…);
# no backend rebuild needed (binary 13ebaff… unchanged).
POSIX_FOCUS_ALLOW_NS=1 \
  timeout --kill-after=20 200 python3 scripts/run-posix-focus.py <work> \
    --mode docker --image <imported-harddind-image> \
    --out <work>/results/posix-focus-docker.json
```

Expected guest outcome: same-ns exact tgid (both modes), sibling containers →
pid 0, nested NEWUSER|NEWPID → holder local 1 / querier local 2 / GETLK local
1 / ancestor fork pid. If the guest denies namespace creation despite the
cap/seccomp flags, the nested case records `not_tested` with the exact errno;
anything else is `fail`.

## Hashes (`evidence/v8/stage8-hashes.txt`)

- run-posix-focus.py `6d198abb997daac5b34f416fa61aecb18ee6c388ba63495f7757272d4215581b1`
- unchanged: share_probe.c `66dc754a…`, probe `e8193146…`,
  run-posix-native.py `fe9a5a5f…`, run-shared-locks.py `dc843526…`,
  virtiofsd `13ebaff5…`, 0005 `a7ffeca5…`

## Remaining limitation

Mixed host/guest blocking deadlock cycles remain undetectable from userspace
polling. No performance, isolation, or overall Docker completion claim.
