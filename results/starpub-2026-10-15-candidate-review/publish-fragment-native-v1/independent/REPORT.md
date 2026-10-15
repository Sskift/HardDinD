# Independent native regression review — HardDinD IPv6 fragmentation (round 14)

Reviewer: independent parallel agent. Work dir:
`/root/harddind-rebuild/arkclaude/round14/ipv6-fragment-review` (only this dir
written; frozen owner snapshot, publisher tree, `/root/udock`, host config and
other agents' sources untouched). No VM/GPU/Docker/perf; no network or host
installs.

## 1. Verdict

Both owner findings are **CONFIRMED by concrete native counterexamples** that
call the actual frozen production functions (`ip6_frag_input`,
`ip6_frag_slowtimo`) of the frozen patched libslirp — no reimplementation.
The two additional cheap checks (max payload, overlap/reserved) also
reproduce real defects. A reviewer-built minimal fix-probe makes the same
binary go 76/76 green under release and ASan/UBSan builds.

| # | Owner item | Frozen file:line | Result |
|---|---|---|---|
| 1 | Atomic fragment memmove overwrites `fh->nxt` before final use | `src/ip6_frag.c:159-184` | **CONFIRMED** |
| 2 | 64-bit `expire_ms` vs 32-bit `curtime`: no expiry past 2^32 ms (~49.7 d) | `src/ip6_frag.c:59,64,215-216`; `src/slirp.c:51,689,702` | **CONFIRMED** |
| 3 | `IP6_FRAG_MAX_PAYLOAD` = 65535−40 wrongly limits payload to 65495 | `src/ip6_frag.h:30-31`, check `src/ip6_frag.c:200` | **CONFIRMED** |
| 4 | Partial-identical overlap zombies; conflicting first keeps stale queue; terminal-end overwrite | `src/ip6_frag.c:221-254,271-273` | **CONFIRMED** |
| 5 | Nonzero reserved byte/bits dropped; RFC 8200 §4.5 says ignored on reception | `src/ip6_frag.c:167-172` | **CONFIRMED** |
| — | Hop limit invented as 64 (owner §7) | `src/ip6_frag.c:140` | **CONFIRMED (minor)** |
| — | Output chunk arithmetic `(1500-40-8)&~7 = 1448`, generated reserved zero | `src/ip6_frag.c:317-319,359-365` | **Verified correct non-bug control** |

## 2. Identity, scope and method

- Frozen candidate: `/root/harddind-rebuild/publish-fragment-owner-review-v1`
  (owner `snapshot.json`). Copied read-only into `inputs/slirp-frozen`;
  **all 71 `slirp-source/*` files hash-match `snapshot.json`** (0 mismatch),
  patch `3a0106c2…fc2a`.
- Provenance: frozen source == pristine upstream
  `libslirp-v4.6.1.tar.bz2` (`3ee27347…a70e`, matches `sources.lock.json`)
  with patch `0001` applied fuzz=0, plus the meson-generated
  `libslirp-version.h` (`"4.6.1"`). Verified by extract+patch+`diff -r`
  (`evidence/` path; only difference is that generated header).
- Build: same flags as the publisher's `build-libslirp.sh` (sequential gcc,
  `-O2 -g -fPIC`, glib headers extracted from the cached, checksum-verified
  dev deb `67d6c9ff…2f61`, linked against host `libglib-2.0.so.0`).
- **Scope label (per task):** the harness (`tests/frag_repro.c`) links the
  compiled static library and drives the real `Slirp`/`mbuf` structs directly
  into the production functions with an injected `SlirpCb.clock_get_ns` (no
  real sleep; every run ≈7 ms). It does **not** exercise `ip6_input()`
  dispatch integration, `slirp_pollfds_poll()` timer gating, sockets, L2
  peers, a guest or Docker. The existing publish L2 tests prove library edge
  handling only; this run likewise proves nothing guest-end-to-end.
- Bounds: one EX `flock /root/harddind-rebuild/kernel-build.lock` acquired by
  the build script itself (no nested flock; inherited-FD aware), every
  compile `taskset -c 0-3`, per-command 60 s cap, total compile phase
  ≈135 s (<180 s cap). No process was killed; nothing hung; post-run `/proc`
  scan shows zero lock holders and no leftover gcc/harness PIDs.

## 3. Finding 1 — atomic fragment next-header overwrite (CONFIRMED)

Mechanism (frozen `src/ip6_frag.c`): `fh` is set to
`m->m_data + sizeof(struct ip6)` (offset 40, lines 159-160). The atomic path
(line 175, `byte_off==0 && !more`) does

```c
memmove(m->m_data + 40, m->m_data + 48, chunk);   /* lines 179-181 */
...
ip6->ip_nh = fh->nxt;                            /* line 184: STALE POINTER */
```

The shift copies the first 8 payload bytes over the fragment header, so
`fh->nxt` then aliases **payload byte 0** — for UDP/TCP that is the high byte
of the source port. `memmove` itself is correct (payload bytes verified
intact); only the stale header read is wrong. The corrupted `ip_nh` then
hits `default: m_free(m)` in `ip6_input.c:88-111`, i.e. the packet is
silently dropped after reassembly.

Concrete counterexamples (`logs/run-frozen.stdout`), fragment-header
`nxt` carried in the wire packet vs value written into the reassembled fixed
header:

- UDP, srcport 40000 (0x9c40), `nxt=17`: observed `ip_nh=156 (0x9c)` — BUG.
- TCP, srcport 40000, `nxt=6`: observed `ip_nh=156` — BUG.
- Control: UDP srcport 4353 (0x1101) gives `ip_nh=17` by accident — proves
  the failure is data-dependent (port-driven), which is why typical test
  ports masked it. `m_len=48`, `ip_pl=8`, shifted payload all intact.

Fix: capture `uint8_t atomic_nxt = fh->nxt;` before the `memmove` and use the
captured value (fix-probe `evidence/fixprobe-ip6_frag.diff:35-47`).

## 4. Finding 2 — fragment queues never expire past 2^32 ms uptime (CONFIRMED)

`q->expire_ms` is a **full 64-bit** monotonic-ms deadline
(`ip6_frag.c:215-216`: `clock_get_ns()/SCALE_MS + 60000`; field declared
`uint64_t` in `ip6_frag.h:49`). The only production caller computes `unsigned
curtime` (`slirp.c:51`) by the same division (`slirp.c:689`, truncating to
32 bits) and passes it to `ip6_frag_slowtimo(Slirp*, unsigned)`
(`slirp.c:702`, prototype `ip6_frag.h:61`). The comparison
`if (curtime_ms >= q->expire_ms)` (`ip6_frag.c:64`) therefore compares a
zero-extended 32-bit value against a 64-bit deadline.

Injected-clock runs against the real function (harness reproduces the exact
line-689 truncation `(unsigned)(ns/1e6)`):

| Clock t0 | deadline = t0+60000 | uint32 curtime at t0+60001 | Result |
|---|---|---|---|
| 5,000,000 ms (control) | 5,060,000 | 5,060,001 | reaped (works pre-wrap) |
| 2^32 − 30,000 | 4,294,997,296 | **30,001** | **not reaped** |
| 13,900,000,000 ms (~160.9 d) | 13,900,060,000 | **1,015,158,113** | **not reaped** |

Native host uptime during review: 13,903,760–13,904,390 s
(160.92–160.93 days, i.e. ~161, logged from `/proc/uptime`) — i.e. the failing regime is
the machine's current state, not a hypothetical.

Capacity consequence (same binary): fill the 64 queues with incomplete
first fragments at the 161-day clock, advance past the 60 s TTL, run the
production timer → still 64/64 queues and 64 fragments held; new flow
refused. **Reassembly capacity is dead until process restart.** A fresh flow
accepted after reclaim does get a correct new 60 s deadline (fix-probe
branch).

Fix (either, owner accepted both):

1. consistent 64-bit clock — widen the slow-timo argument and its call to
   `uint64_t` (validated by fix-probe, `evidence/fixprobe-slirp.c.diff`); or
2. wrap-safe 32-bit clock — store the deadline as `uint32_t` in slirp's
   truncated clock domain and expire with signed subtraction
   `(int32_t)(expire_ms - curtime_ms) <= 0`; sound because TTL 60 s ≪ 2^31 ms
   and the slow timer fires every 500 ms.

(The publisher's in-flight `.work` tree implements option 2 — see §8.)

## 5. Finding 3 — max payload off by the 40-byte header (CONFIRMED)

IPv6 Payload Length counts everything after the 40-byte fixed header, so the
largest fragmentable payload is **65535** (max UDP datagram 65527). Frozen
`ip6_frag.h:30-31` sets `IP6_FRAG_MAX_PAYLOAD = 65535 - sizeof(ip6) = 65495`
and `ip6_frag.c:200` drops `end > 65495`. Direct two-fragment reassembly
results: 65495 accepted; **65496 and 65535 dropped** (should accept); 65536
correctly dropped. Fix: `#define IP6_FRAG_MAX_PAYLOAD 65535u` (fix-probe does
exactly this; 65575-byte reassembly mbuf allocates fine).

Related bound: with non-final fragments at the minimum legal 8-byte chunk, a
65535 payload needs 8192 fragments, exceeding the global
`IP6_FRAG_MAX_FRAGMENTS = 1024` cap — raising only the payload limit leaves
such datagrams un-reassemblable. The publisher's in-flight tree raises the
cap to 8192 (~12 MiB worst case); recommend keeping that change with the
payload fix.

## 6. Finding 4 — overlap classification / terminal consistency (CONFIRMED)

RFC 8200 §4.5 overlap rule: an **exact duplicate** (same offset, same length,
identical bytes) is discarded while the queue is retained; **every other
overlap** — including a partial overlap whose intersecting bytes happen to
match — must discard the whole reassembly. Frozen behavior from direct calls:

- partial identical overlap A[0,16) M=1 then B[8,24) M=0 with bytes 8..15
  identical: no conflict detected, B is not an exact dup, both entries are
  inserted ordered by offset; the contiguity check can then never close
  (`f->off != expect`) → a **zombie queue held until TTL** (and past TTL on a
  >49.7-day uptime box, per finding 2).
- conflicting first fragment (same off/len, different bytes): the early
  `if (q->have_first) goto drop` at `ip6_frag.c:222-224` frees only the
  incoming fragment and **keeps the stale queue**; feeding the remainder
  then reassembles the rejected-first datagram anyway.
- exact-duplicate first fragment: retained/ignored correctly today (the
  end-observed behavior matches RFC, but only by accident of the goto);
  content-conflicting middle fragment: whole-queue discard already correct
  (control).
- two terminal fragments with different total ends: `q->end` is overwritten
  (`ip6_frag.c:271-273`); a gap sequence A[0,8) M=1, B[16,24) M=0 end=24,
  C[24,32) M=0, then a closer at [8,16) reassembles a 32-byte datagram that
  had two last fragments — should be rejected.

Fix: strip headers, then classify overlap against queued fragments **before
any queue/first mutation** — conflict → discard whole queue; non-exact
intersection → discard whole queue; exact dup → free incoming, retain queue —
and only then set `have_first/nxt/vtf/hl`; reject a terminal whose `end`
differs from an established `q->end` (fix-probe
`evidence/fixprobe-ip6_frag.diff:51-138`; all five overlap scenarios green).

## 7. Finding 5 — reserved fields, plus hop-limit note

- RFC 8200 §4.5: the fragment-header `Reserved` byte and the two `Res` flag
  bits are "initialized to zero for fragmentation and **ignored on
  reception**". Frozen `ip6_frag.c:167-172` drops any packet carrying either
  nonzero; an atomic packet with `res=0xab, Res=0b11` is dropped (BUG) and is
  accepted by the fix-probe. Output generation already zeroes both
  (`fh->res = 0`, `off_flags` only uses offset/M bits) — asserted natively in
  the output test.
- Reassembled fixed header hardcodes `ip6->ip_hl = 64` (`ip6_frag.c:140`);
  first-fragment hop limit 37 comes out as 64. Fix-probe stores/restores it;
  owner §7 already lists this.

## 8. Publisher fixed-candidate status

At review end the publisher's fix is **still in-flight and mutable**:
`arkclaude/round6/publish-compat/.work/slirp-work/src/ip6_frag.{c,h}`
(mtime 23:49/23:41), with `dbg65535`/`native_ip6frag` scratch binaries; the
shipped patch under `third_party/patches/` is still the frozen
`3a0106c2…fc2a` and no new report exists. I therefore did **not** freeze or
build it (per "do not use publisher mutable rebuilt binary without freezing
identity"). Read-only inspection shows it addresses findings 1-5 and the
hop-limit item using the wrap-safe-32-bit expiry option, bumps
`MAX_FRAGMENTS` to 8192, and adds a non-terminal-extends-past-`end` check;
my same harness, run in its default 32-bit-curtime mode against a frozen copy
when the publisher declares ready, is the intended rerun (expected RC 0
without special flags).

## 9. Fix validation (reviewer-built minimal fix-probe, not publisher code)

`inputs/slirp-fixprobe` is my frozen copy with the minimal §3-§7 edits; diffs
in `evidence/fixprobe-*.diff`. Same harness, same counterexamples:

| Build | Binary | Result |
|---|---|---|
| frozen, release | `build-frozen/frag_repro` | RC=1, checks=76, **failures=16, bug_conditions_observed=16** |
| fix-probe, release (`-DFRAG_SLOWTIMO_64`) | `build-fixprobe/frag_repro_fixed` | **RC=0, checks=76, failures=0** |
| frozen, ASan+UBSan `-O1` | `build-asan-frozen/frag_repro_asan` | RC=1, same 16; **zero sanitizer findings** |
| fix-probe, ASan+UBSan | `build-asan-fixprobe/frag_repro_asan_fixed` | **RC=0, 76/76; zero sanitizer findings** |

Direct `ip6_frag_output` controls (boundary stub `if_output`, clearly labeled):
1500-byte packet passes unfragmented; 1540 → two frames 1496 (40+8+**1448**)
and 100, offsets 0 M=1 / 1448 M=0 (`0x05a8`), shared id, reserved zero;
65535 payload → 46 frames (45×1448 + 375) all ≤ MTU. This confirms the
owner's correction that the 40+8+1452 arithmetic claim was wrong.

## 10. Artifacts, raw logs, cleanup

- Harness/build: `tests/frag_repro.c`, `tests/build_and_link.sh`.
- Raw logs with exit statuses: `logs/run-frozen.{stdout,stderr,rc}`,
  `logs/run-fixprobe.*`, `logs/run-asan-frozen.*`, `logs/run-asan-fixprobe.*`,
  build logs + `build-*/compile.log`. First post-teardown-crash harness run
  (my harness bug, fixed before results) preserved under
  `evidence/frozen-first-run/` (first complete frozen run with the earlier
  smaller case set: 60 checks/14 failures; the only crash was an in-harness
  teardown bug fixed before that run, apport pipe core pattern, no core file).
- Fix diffs: `evidence/fixprobe-ip6_frag.diff`,
  `evidence/fixprobe-ip6_frag.h.diff`, `evidence/fixprobe-slirp.c.diff`.
- Hashes: `MANIFEST.sha256` (185 entries, before/after hashes of every
  input, source, binary and log) and `manifest.json` (run metadata, pinned
  hashes, snapshot verification 71/71, exit statuses).
- Cleanup: no timed-out or hung process occurred after the harness was fixed
  (all exits recorded; one early segfault was an in-harness teardown bug,
  apport pipe core pattern, no core file left); final `/proc/*/fd` scan shows
  no holder of `kernel-build.lock` and no leftover gcc/taskset/harness
  process; no broad process killing used.

## 11. Not claimed

No guest/end-to-end, nested Docker, L2-peer or performance validation; no
RFC-full-compliance claim (extension-header chains before the fragment
header, ICMPv6 PTB and unimplemented extensions are not covered here); the
fix-probe is a minimal validation of fix direction in the reviewer's folder,
not an integrated/accepted patch; nested-Docker scheduling stays with the
owner/main parent.
