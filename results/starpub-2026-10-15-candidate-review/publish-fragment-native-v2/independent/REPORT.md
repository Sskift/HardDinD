# REPORT-v2 — portable behavioral re-review of the frozen fixed IPv6-frag candidate

Reviewer: independent native regression agent (round 14). Work dir:
`/root/harddind-rebuild/arkclaude/round14/ipv6-fragment-review/v2`.
v1 artifacts (`../REPORT.md`, `../tests/`) are preserved unchanged. The owner
folders were used read-only; everything was copied into `v2/` before use.
No VM/GPU/Docker/perf, no network installs, no other authors' sources touched.

## 1. Trigger and bottom line

Owner froze the final publisher source plus the CVE patch at
`/root/harddind-rebuild/publish-fragment-native-owner-v1` (patches
`0001` sha `8faab399…5ed0`, `0002-cve-2026-9539` sha `2c0e837b…13d7`).
The author's own 12 direct tests pass. My **unmodified v1 harness**, run by
both the owner (`release-independent-tests.stdout`, exit 1) and reproduced
here, fails **4 of 75 checks with bug_conditions_observed=0** against that
tree.

**Both parent-identified failures are confirmed review-harness bugs, not
candidate bugs.** A new representation-independent v2 harness passes the
frozen fixed candidate with **108/108 checks, RC 0** in both release and
ASan/UBSan builds, while the same unmodified v2 binary (one production clock
ABI, no macros) fails **20 checks / fires 17 BUG-CONFIRMED conditions,
RC 1**, against the original frozen bad tree — every original defect is still
caught. No new real candidate defect was found this round.

| Group (pgid) | Candidate | Variant | Result |
|---|---|---|---|
| rel-fixed (2906972) | frozen fixed | release `-O2` | **RC 0 — checks=108 failures=0 bug=0** |
| rel-bad (2907194) | original frozen bad | release `-O2` | RC 1 — checks=102 failures=20 **bug=17** |
| asan-fixed (2916477) | frozen fixed | ASan+UBSan `-O1` | **RC 0 — 108/108, zero sanitizer findings** |
| asan-bad (2916804) | original frozen bad | ASan+UBSan `-O1` | RC 1 — failures=20 **bug=17**, zero sanitizer findings |

Bad control executes 102 rather than 108 checks because the capacity test
**safely skips 6 refill/fresh-TTL dependent steps once reclaim is proven
broken** (`SKIP dependent refill/fresh-TTL steps` in the log); their
semantics are independently covered by `test_fresh_ttl_after_reclaim` and
the second capacity cycle, which do run on the fixed tree.

## 2. The two v1 harness bugs (confirmed, with evidence)

Owner run preserved at `v2/evidence/owner-v1-run/release-independent-tests.stdout`
(sha `487d9cd8…2270`); the harness the owner ran is byte-identical to my v1
`../tests/frag_repro.c` (both sha `4bb8ddef…f62ab`, matching
`owner-status.json → independent_before`).

**HB-1 — representation assertion instead of behavior (2 failures).**
v1 `test_expiry` read the queue's internal `expire_ms` field and demanded
`deadline == t0 + 60000` as a *full 64-bit* value. The supported fix
(`build/…/src/ip6_frag.h:57` `uint32_t expire_ms`; `ip6_frag.c:59-74`)
intentionally stores the deadline in slirp's truncated, wrap-safe 32-bit ms
clock domain and expires with `(int32_t)(expire_ms - curtime_ms) <= 0`.
- owner stdout line 36: at t0 = 2³²−30000 the stored deadline is `30000`
  (wrapped), not `4294997296` → FAIL;
- line 42: at 13.9×10⁹ ms the stored deadline is `1015158112`, not
  `139000060000` → FAIL.
The behavioral check immediately under each (queue reaped at t0+60000)
**passed** — expiry actually worked; only the internal-format expectation
was wrong.

**HB-2 — invalid `#else` capacity schedule (2 failures).**
v1 `test_capacity_recovery` conditionally compiled its correct assertions
behind `FRAG_SLOWTIMO_64`; the default-32-bit `#else` branch submitted id
1065 after reclaim (a correct implementation accepts it and opens a queue
with a **fresh** 60 s TTL), then id 1066 (two live queues), yet expected
exactly one queue and immediate reaping at the same timestamp
(owner stdout lines 54-55: "new flow accepted…" FAIL, "wedged table never
reclaims (got 2)" FAIL). No correct per-queue TTL implementation can satisfy
that order. v1's own report (§8) predicted the default 32-bit build would
return RC 0 "without special flags" — that prediction was wrong, and the
`FRAG_SLOWTIMO_64` gate is itself proof v1 was not implementation-neutral.

These are logged here as **review harness defects**. The frozen candidate
behavior they pointed at is correct.

## 3. v2 design — one production ABI, behavioral only, expectations strengthened

`v2/tests/frag_repro_v2.c` (diff vs v1: `v2/evidence/harness-v1-to-v2.diff`):

- **Single production clock path for every candidate.** The harness calls
  exactly what `slirp.c:51/689/702` passes:
  `uint32_t curtime = (uint32_t)(clock_ns/1000000);
  ip6_frag_slowtimo(s, curtime);`
  No `FRAG_SLOWTIMO_64`, no widened call ABI, and the queue's `expire_ms`
  representation is never read. The same source/binary therefore tests both
  the wrap-safe-32-bit fixed code and a hypothetical consistent-64-bit code.
- **Assertions are on the injected monotonic timeline only:** alive at
  t0+59999 (early-reap BUG if gone), reaped at t0+60000 (retain BUG if held),
  at four clock positions: pre-wrap 5,000,000 ms; deadline straddling the
  first wrap (2³²−30000); actual host uptime 13,900,000,000 ms (~161 d =
  3.24 wraps; `/proc/uptime` logged at run time); explicit multi-wrap
  10×2³²+123,456,789 ms (~497 d).
- **New meaningful checks (76 → 108 checks; nothing removed or weakened):**
  exact TTL boundary (59999 survives / 60000 reaps) at every position;
  two queues born 30 s apart expire 30 s apart across rollover; a flow
  admitted exactly at reclaim survives on its own fresh 60 s and dies at
  t+120 s; capacity test fills 64, verifies the 65th refusal while full,
  64 survivors at −1 ms, full reclaim at TTL, then **refills 64 without
  restart and confirms the second generation survives on its fresh TTL and
  reaps 60 s later**; atomic path now also asserts hop-limit preservation;
  added an exact-retransmit-of-terminal-fragment case plus a conflicting
  terminal-retransmit discard case (guards the fixed code's new
  same-offset/M-agreement classification; both behave on both trees).
- All v1 functional checks retained verbatim in spirit: atomic UDP/TCP nh
  overwrite (+ accidental-match control), payload bounds 65495/65496/65535/
  65536, partial identical overlap, conflicting first, exact duplicate,
  content conflict, two-terminals consistency, reserved-byte/bits ignored,
  first-fragment hop limit, and output chunking 1496/100 (1448 payload),
  zero reserved bits, 65535 → 46 fragments.

## 4. Discrimination evidence (bad control still catches the real old bugs)

From `v2/logs/rel-bad.tests.stdout` (identical results under ASan):

- **Pre-wrap TTL case passes on the bad tree**, all four rollover TTL cases
  fail with BUG-CONFIRMED (straddle-wrap, 161-day uptime, 10-wrap; per-queue
  deadlines and fresh-TTL-across-reclaim also fail) — the harness
  discriminates the actual defect rather than failing unconditionally. No
  "reaped 1 ms BEFORE TTL" alarm ever fires on either tree (no over-eager
  expiry assumptions).
- Atomic UDP nxt 17→0x9c and TCP nxt 6→0x9c BUGs; 65496/65535 payload
  rejections; partial-identical zombie; conflicting-first stale reassembly;
  terminal-end overwrite; reserved drop; invented hop limit 64 — 17 BUG
  conditions total (full list in the raw log). The 20 FAIL lines are each a
  direct or cascading consequence of those defects (e.g. once qA is wrongly
  retained, qB's count-based observations cascade; documented per case in
  the log), never a standalone expectation mismatch.
- Fixed tree: 108/108 ok, zero BUG, zero FAIL, both builds, zero
  ASan/UBSan/LeakSan output.

## 5. Fixed candidate source inspection (no new candidate bug found)

Reviewed `ip6_frag.c`/`ip6_frag.h` and the other changed files in the frozen
stage (5 files differ from the original: `ip6_frag.{c,h}`, `slirp.c`,
`socket.c`, generated `libslirp-version.h`; full diff
`v2/evidence/fixed-vs-bad-source.diff`):

- expiry: wrap-safe 32-bit domain is internally consistent (store now truncated
  +60000; signed subtraction; TTL 60 s ≪ 2³¹ ms) and matches the unchanged
  production call (`slirp.c:689-702` still passes file-scope `unsigned`).
- overlap classification happens before queue mutation: conflict → whole
  queue discard; exact duplicate (offset/len/M/data) → drop copy, keep queue;
  any other intersection → discard; second terminal with a different end →
  discard; non-terminal past established end → discard. All asserted natively.
- `slirp.c` adds a v6-specific ingress head pad (6 vs 2; head 48) matching
  `frag_reassemble`'s 48-byte mbuf offset for 8-byte qlink alignment — the
  reassembly path returns 8-aligned mbufs; exercised by every reassembly case
  under UBSan with zero alignment findings.
- `IP6_FRAG_MAX_FRAGMENTS` raised 1024→8192 so a legal minimum-8-byte-granular
  65535-octet datagram (8192 pieces) is representable; global bound retained.
- `0002-cve-2026-9539` (cap `so_urgc` to available receive data in
  `sosendoob`, `socket.c`) is compiled into both fixed archives but is a TCP
  urgent-data path not exercised by this fragment harness — stated, not
  claimed as tested here.

Identity/provenance (verified, not assumed): the copied fixed tree matches
the owner manifest **61/61**; the bad tree matches v1 `snapshot.json`
**71/71**; pristine 4.6.1 (`3ee27347…a70e`) + frozen patches with fuzz=0
reproduces both trees byte-for-byte apart from the meson-generated version
header (`v2/evidence/provenance.txt`, `"4.6.1"` unchanged).

## 6. Execution bounds, lock discipline and cleanup

- One EX `flock /root/harddind-rebuild/kernel-build.lock` acquisition per
  build+test group (`v2/tests/run_group.sh` opens fd 9 once, compiles and
  tests while held, releases on group exit; `build_v2.sh` verifies the
  inherited FD inode and never re-locks — no nested flock).
- Every gcc/link/test: `taskset -c 0-3`; per-file compile cap 60 s; test cap
  60 s; group wall time bounded by `setsid -w` under the 180 s limit
  (observed groups 2-16 s; one group waited 50 s behind a parallel agent and
  still finished well within bounds). Compilation reused frozen-source
  objects for harness-only edits (mtime guard; per-variant object dirs).
- Each group ran in its own recorded session/pgid (table §1); post-run
  `ps -g <pgid>` shows **0 surviving members** for all four; final sweep:
  no holder of the build lock, no leftover gcc/harness processes. Nothing was
  killed; no cores produced (apport pipe pattern).
- Inputs/outputs: all under `v2/`; owner tree never modified. Raw logs:
  `v2/logs/<group>.{build.stdout,build.stderr,tests.stdout,tests.stderr,rc,group.log,pgid}`
  and per-dir `compile.log`. Hashes: `v2/MANIFEST-v2.sha256` (330 entries,
  all verified against disk) with run metadata in `v2/manifest-v2.json`.

## 7. Not claimed

Same scope as v1: direct calls into the actual compiled production functions
on real Slirp/mbuf state with an injected clock (no real sleep) — not a
guest, L2-peer, nested-Docker or performance result; no full RFC 8200
compliance claim (extension-header chains, ICMPv6 PTB, jumbograms remain
uncovered); the CVE fix is compiled but not behaviorally exercised here;
owner still schedules nested-Docker acceptance.
