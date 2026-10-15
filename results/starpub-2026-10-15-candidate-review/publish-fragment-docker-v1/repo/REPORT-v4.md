# REPORT-v4 — IPv6 fragmentation source-review fixes

Scope: the seven concrete findings in `OWNER-FRAGMENT-SOURCE-REVIEW.md` against
libslirp patch `3a0106c2`, plus the pre-existing v6 header-alignment UB that
the sanitizer run exposed. Minimal production fixes in the pinned libslirp
4.6.1 backport; native real-library regressions only. No VM/GPU/perf run by
this agent; owner nested-Docker acceptance remains pending. The frozen
snapshot under `/root/harddind-rebuild/publish-fragment-owner-review-v1` was
read but never modified.

## Findings and fixes

1. **Atomic-fragment header overwrite (source-proven).** The atomic path
   memmoved the payload over the fragment header and then read `fh->nxt`
   through the stale pointer. Fix in `ip6_frag_input()`: capture
   `nxt = fh->nxt` (and hop limit) **before** the memmove; write the captured
   byte into `ip6->ip_nh`. Native counterexample: `test_atomic_udp_fragment_with_non17_source_port`
   sends an atomic IPv6 fragment whose inner UDP source port is 7777 (0x1E61,
   high byte 0x1E ≠ next-header 17) over the real library+socket — under the
   old code dispatch died, echo now verifies at sizes 1/100/1444.
   `test_atomic_tcp_fragment_handshake` wraps the guest SYNACK/data in atomic
   fragment headers (nxt=6) and completes a real `socket.create_connection`
   plus data echo. The C unit asserts `ip_nh==17`, payload length, hop limit
   and preserved source port directly on the reassembled mbuf.

2. **Expiry clock truncation.** `q->expire_ms` was 64-bit full monotonic ms +
   60000 while the slow timer receives slirp's truncated 32-bit `curtime`;
   past 2^32 ms uptime (~49.7 days; StarPub ~161 days) the deadline could never
   match. Fix: all expiry state is now in slirp's **wrapping 32-bit ms clock
   domain** (`uint32_t expire_ms`, set from the same truncated clock), and
   reaping uses signed wrap-safe subtraction
   `(int32_t)(expire - curtime) <= 0`. Real-library C tests with a fully
   controlled virtual clock (no 60 s sleep): fill all 64 queues, verify the
   65th is rejected, nothing dies at TTL−1 ms, everything reaps at TTL, and
   the table refills to 64 immediately, at uptimes **2^32 ms** and
   **3·2^32 ms**, plus a case where the deadline itself crosses the 2^32
   wrap. `test_expiry` runs three variants in `native_ip6frag.c`.

3. **Reassembly size limit.** The IPv6 payload-length field excludes the
   40-byte fixed header, so the maximum fragmentable payload is **65535**,
   total packet 65575. `IP6_FRAG_MAX_PAYLOAD` corrected from 65495 to 65535;
   the fragment-mbuf cap raised from 1024 to **8192** (the exact number of
   8-byte pieces needed for a maximum packet — bounded, ~12 MiB). Native C
   tests feed 8192 fragments: **65535 payload reassembles** (spot-check first
   and last bytes, payload length, preserved hop limit/next-header) while a
   **65536 payload never completes** and leaves no completed datagram. Realistic
   maximum UDP (8 header + 65527 data = 65535) is covered by the same boundary
   with nxt=UDP. `m_inc`/mbuf growth handles the 65575-byte result.

4. **Overlap / duplicate / first / terminal policy.** Rewrote validation so
   every check against existing state runs **before any queue mutation**:
   - exact duplicate (same offset, length, M semantics, identical bytes) is
     dropped but the queue is retained and still completes;
   - any **non-exact overlap discards the whole datagram queue** — including a
     byte-identical *partial* overlap, which the old code let stall forever;
   - a conflicting second first fragment tears down the old queue (old code
     dropped only the newcomer and kept the poisoned state);
   - two terminal fragments with inconsistent total length discard the queue;
   - a non-terminal fragment extending past an established end discards it.
   Native C tests: `test_exact_duplicate_keeps_queue`,
   `test_partial_overlap_discards`, `test_conflicting_first_discards`,
   `test_inconsistent_terminals`; Python socket-level tests
   `test_identical_partial_overlap_discards_queue` (matching overlap bytes)
   and `test_conflicting_first_fragment_discards_queue`, each followed by a
   clean datagram proving the flow is not poisoned.

5. **Reserved fields accepted.** RFC 8200 §4.5: receivers ignore the 8-bit
   reserved byte and the two reserved flag bits; the old code dropped them.
   Reception now ignores both (generated fragments still send zero).
   `test_reserved_fields_ignored_rfc8200` sends res-byte 0xAB and flag bits
   0b10 on **every** fragment and verifies full reassembly.

6. **Hop limit preserved.** Reassembly hard-coded hop limit 64; it now keeps
   the first fragment's hop limit (`q->hop_limit`), asserted in the C atomic
   and 65535 tests.

7. **Arithmetic/direction corrections.** Output fragment chunk is
   `(1500 − 40 − 8) & ~7 = 1448` bytes → 40+8+1448 = **1496-byte IP /
   1510-byte Ethernet** frames (matches the owner trace 1510+75); this report
   uses those numbers, not the earlier wrong "…+1452". Direction labels fixed:
   `ip6_input` reassembles fragments **from the guest**, `ip6_frag_output`
   emits fragments **to the guest** (the path the guest UDP reply uses).

8. **Bonus latent bug fixed: v6 TCP/UDP overlay alignment.** Under ASan/UBSan
   the first sanitized run reported `member access within misaligned address …
   requires 8 byte alignment` in pinned `tcp_input.c:310` for *every* v6 TCP
   packet. Root cause: `slirp_input()` used the IPv4-derived head padding
   (DELTA+2+14=44) for both families; the v6 `tcpiphdr` qlink then lands 4 mod
   8. Fix in `slirp.c`: family-specific head padding (v6 uses +6 → head 48);
   reassembled packets use the identical offset. v4 placement is unchanged.
   After this fix all v4/v6 TCP/UDP flows run clean under
   `-fsanitize=address,undefined`.

## Distribution CVE baseline preserved (OWNER-LIBSLIRP-DISTRIBUTION-BASE)

The production runtime links the **Ubuntu `libslirp0:amd64 4.6.1-1ubuntu0.2`**
package, whose series adds `CVE-2026-9539` (`sosendoob` urgent-data bounds:
guest-controlled `so_urgc` was capped only to 2048 but not to `sb->sb_cc`,
which could underflow `sb_cc` and leak data). The reproducible static rebuild
now applies the **byte-identical distro patch**
(`third_party/patches/0002-cve-2026-9539-urgent-data-bounds.patch`,
sha `2c0e837b…`, verified identical to
`debian/patches/CVE-2026-9539.patch` in the pinned debian tarball
`c7b4c8cd…`) on top of upstream 4.6.1 after the fragmentation patch; both
apply fuzz=0 in that order. The CVE patch touches only `src/socket.c` (ip6
input/output untouched), so the root-cause analysis and fragmentation code are
unaffected. `build-libslirp.sh` writes
`$work/libslirp-harddind/BUILD_MANIFEST.json` recording the upstream tarball,
both applied patches, package baseline, variant/sanitizers and link identity
(static archive + dynamically linked ABI-pinned `libglib-2.0.so.0`, reported
version stays `4.6.1`); the debian tarball and fixed version are pinned in
`sources.lock.json` (`network_backend.libslirp_debian`).

**Build status note:** the `.work/network-backend*` binaries listed below were
compiled earlier in this revision from bare upstream + the fragmentation patch
and therefore predate the CVE integration; they were used for the bounded
sanitizer/native regression runs. Artifacts built after this report via
`scripts/build-network.sh` include both patches (manifest proves it). No
rebuild was run after the CVE integration because owner build activity held
the global EX lock (gpu-instrumentation-owner-v3 / round4 / build-mmap-candidate);
per OWNER-NESTED-LOCK-STOP the work stayed source-only and the previously
failed outer-flock attempt is preserved.

## Tests actually run (bounded, global EX lock held, ≤4 jobs)

| suite | binary | result |
|---|---|---|
| C fragment units (release), 12 cases incl. virtual-clock expiry/65535/atomic | `.work/native_ip6frag` | ALL PASS |
| C fragment units under ASan/UBSan | `.work/native_ip6frag-asan` | ALL PASS, 0 findings |
| Python real-backend flows (release): 36 tests | `.work/network-backend` | OK |
| Python flows under ASan/UBSan: 29 tests (hostfwd, forwarded flows, 14 fragmentation scenarios, 4000-record inventory) | `.work/network-backend-asan` | OK, 0 AddressSanitizer/runtime-error findings |
| canonical `scripts/build-network.sh` end-to-end from pinned tarball + fuzz=0 patch | `.work/official-v4/network-backend` + its native_ip6frag | exit 0; 24-test smoke OK |
| patch applies fuzz=0 to pristine 4.6.1 and reproduces the working tree byte-for-byte | `third_party/patches/0001-ipv6-fragmentation.patch` | verified |

No full nested Docker/GPU/perf tests were run by this agent (owner-controlled;
owner GPU v14 held the global build lock during part of this work). A bounded
clean-work `make network` run waited on the owner-held lock and was terminated
by the tool timeout (exit 144, no compile ran) — `.work/final-network.log`
retains that record; the identical script path had already been verified in
`.work/official-v4`.

## Limitations (not claimed)

- Extension headers (Hop-by-Hop/Destination/Routing/AH/ESP) are not parsed;
  only the fixed-header next-header chain is handled. Jumbograms (hop-by-hop
  payload-length extension) and ICMPv6 fragmentation-required signalling for
  forwarded paths are not implemented.
- Atomic fragments larger than one MTU are correctly rejected with ICMPv6
  packet-too-big by the pre-existing length check (RFC-correct).
- Owner nested-Docker acceptance of the v6 UDP 1453+ matrix remains the final
  gate; this revision changes only the pinned libslirp, not the guest/Moby side.

## Hashes (workspace-local verification artifacts)

- libslirp patch 0001 `8faab399ba6f8bb7165c4b96093046a6625e206c4dadccb1465ade09216a5ed0`
- CVE patch 0002 `2c0e837b6f1774f261862805390a6ab43b4e4239c6892c43feb5895f2c8013d7`
  (identical to `c7b4c8cd…` debian series)
- release backend `169c5cf88b8ce16d14d2a342a856d45b09f151c5d843926590ed08fd5c9c399b`
- ASan backend `7687609df3e807de54dbaa271e040e92170dab3c99224d24911c67b20c94411e`
- C units release `bb261270812c7af0a6eec3eee5fc0a8ff45b8c47fc03c72a15b6dbb770763248`;
  ASan `4cb4138d724f6bbd4562392f8b4c308b57c8181c1fc9d1955f98d95748c61e5f`
- sources `ip6_frag.c c909ee71…`, `ip6_frag.h ead1cdb9…`, `slirp.c a4d4ceb9…`,
  `ip6_input.c 7b8d2a19…`; tests `native_ip6frag.c 60ed52e1…`,
  `test_ipv6_fragmentation.py 35f2850c…`.

Reproduce: `bash scripts/build-network.sh <work>` (release; also builds/runs
the C units) or `bash scripts/build-network.sh <work> asan`; Python
regressions `make publish-tests` / `make publish-tests-asan`.
