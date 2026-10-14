# HardDinD published-port IPv6 / multiple-address semantics — round 6

Status: implementation complete in this directory; native and Go tests green.
**Not** declared complete: nested-Docker end-to-end and same-host performance
remain owner-serial acceptance (`scripts/run-published-ports.py` extended
with the new scenarios). No UML/dockerd/GPU/perf runs were performed by this
agent; no host device, namespace, sysctl, /root/udock or outer privilege was
changed. Pinned inputs unchanged: Moby 29.8.2-harddind.1
(commit 8af9fe3a36bab3e0351b3ab1cef1e685c8b4d03), libslirp 4.6.1
(4.6.1-1ubuntu0.2; Ubuntu ships pristine upstream 4.6.1 — no debian source
patches).

## 1. Root cause (source-level, confirmed)

Publication path: dockerd (patched Moby, inside UML, rootful) →
guest `harddind-port-agent` (HTTP over ssl0) → host `scripts/port_driver.py`
→ unix-dgram `network.ctl` → `src/network_backend.c` (pinned libslirp) binds
the real host socket; in-guest iptables DNAT + docker-proxy reach the
container.

1. Host port driver advertised/accepted only `tcp4,udp4`, rejected IPv6 and
   pinned every child to 10.0.2.15 (`scripts/port_driver.py` old lines 48-61).
2. Moby's rlkclient collapsed **every** host address to one
   `networkDriver.childIP` when `disallowLoopbackChildIP` was set, so the
   guest-side logical allocator could not distinguish host IPs and `tcp6`
   bindings were silently dropped (`nat/mapper_linux.go` filters unsupported
   entries; later AddPort failures become `RootlesskitUnsupported`).
3. Backend used the v4-only `slirp_add_hostfwd` and never enabled slirp's v6
   stack. No kernel change is needed (`CONFIG_IPV6=y`, `CONFIG_IP6_NF_NAT=y`
   are already present in the pinned kernel).

### Owner-confirmed defects found in review and how they are fixed

- **Prefix assignment is not address-block assignment.** The earlier guest
  init added `10.0.2.16/28` and `fd00::100:0/112` and the daemon immediately
  referenced 10.0.2.17 / fd00::100:1, which Linux never assigned — bind() in
  the guest would have failed. Fixed: **exact** secondary addresses are now
  added/removed at runtime via netlink from inside dockerd (see §2.2). The
  guest init only assigns the two static primaries.
- **IPv6 pool arithmetic bug** (`[12]byte{fd,...,1,0}` put the hextet in the
  wrong position, generating fd00::100:0:1 outside the validated /112) is
  fixed and additionally guarded: every generated candidate is asserted to
  lie in the same `fd00::100:0/112` constant the host driver validates
  (`harddindDynAlias` panic guard + unit coverage).
- A packet-only L2 emulator is explicitly **not** treated as proof of guest
  ownership (see §4 and `tests/publish/README.md`). The decisive ownership
  path is real netlink + real dockerd, covered by Go tests and owner Docker
  acceptance.

## 2. Implementation

### 2.1 Backend `src/network_backend.c`

- `SlirpConfig` v3 with `in6_enabled=true`, `vprefix_addr6=fd00::/64`,
  `vhost6=fd00::2`, `vnameserver6=fd00::3`.
- Control grammar: `add|create|del <tcp|udp> <hostaddr> <hostport> <guestport>
  [guestaddr]`; IPv4/IPv6 literals are family-auto-detected, so the historical
  5-field v4 grammar (used by `scripts/harddind.py` for the API forward and by
  manual `forward` calls) stays byte-compatible. New 6th field carries the
  exact guest alias for both families.
- Uses `slirp_add_hostxfwd`/`slirp_remove_hostxfwd`; v6 always with
  `SLIRP_HOSTFWD_V6ONLY` (matches Moby's allocator: families independent;
  no dual-stack wildcard hijack). Rejects IPv4-mapped IPv6 and scoped/zone
  literals at the text boundary.
- Tracking keyed `(family, udp, concrete host addr, host port)`; `MAX_FORWARDS`
  raised 256→4096. The real host bind remains the authoritative conflict
  arbiter (slirp uses SO_REUSEADDR only; no REUSEPORT): wildcard/specific and
  outer-holder conflicts fail in **both** request orders, TCP and UDP.
- `list` JSON keeps old fields and adds `family`, `guest_address`.

### 2.2 Exact guest aliases with reference-counted lifetime — Moby patch 0003

`moby/patches/0003-publish-address-family.patch` (gated on
`HARDDIND_PORT_DRIVER_STATE` + port driver name `harddind-slirp`; upstream
behavior unchanged elsewhere):

- New `daemon/libnetwork/internal/rlkclient/harddind_aliases_linux.go`:
  - wildcard parents `0.0.0.0`/`::` map to fixed aliases **10.0.2.16/32** and
    **fd00::10/128**; concrete host IPs get lowest-free exact secondaries from
    bounded pools: v4 **10.0.2.17..10.0.2.254** (238),
    v6 **fd00::100:1..fd00::100:ffff** (65535, asserted inside
    `fd00::100:0/112`).
  - addresses are assigned via netlink (`RTM_NEWADDR`/`RTM_DELADDR`,
    vendored vishvananda/netlink) on `$HARDDIND_PUBLISH_IFACE` (vec0), entirely
    inside the guest network namespace dockerd already administers. IPv6
    aliases use `IFA_F_NODAD` so dockerd's bind() never races DAD tentative
    state; v4 aliases are /32 (the primary /24 supplies the connected route;
    ARP/NDP answer for every local address with default arp_ignore). No
    nonlocal_bind/freebind and no outer-host changes.
  - **reference counting** across protocols (TCP+UDP on one host IP share an
    alias): exact address removed on last teardown and the slot immediately
    reusable, so sequential container churn cannot monotonically exhaust the
    pool. Unbalanced release is reported, never silently ignored.
  - startup **prune** removes stale pool/wildcard addresses left by a crashed
    daemon (primaries 10.0.2.15 / fd00::15 are never touched), then re-adds the
    wildcard aliases.
- `ChildHostIP` now reserves (and really assigns) the alias; new
  `ReleaseChildIP` drops the reference. `nat.MapPorts` tracks reservations and
  its failure path rolls back every reservation not owned by bindings (bound
  ones are released by `UnmapPorts`) — socket allocator, proxy, DNAT and host
  hostfwd failures all unwind atomically; nothing becomes `RootlesskitUnsupported`.
  Pool/netlink exhaustion is a hard container-start error, never a silent drop.
- Guest init (`guest/runtime-init`) adds only `fd00::15/64` + v6 default route
  when publishing is enabled; `guest/runtime-daemon` exports
  `HARDDIND_PUBLISH_IFACE=vec0`.

### 2.3 Host port driver `scripts/port_driver.py`

- `/v1/info` is `harddind-port-v2`, advertises `tcp4,tcp6,udp4,udp6` and
  `networkDriver.ipv6=true`.
- POST enforces proto/address-family match, rejects v4-mapped/scoped/link-local
  v6 parents, and validates that `childIP` is exactly a configured alias or
  primary. DELETE issues the matching 6-field control command.

### 2.4 Backwards compatibility

- 5-field v4 control grammar and old `list` fields unchanged.
- Moby changes are env+driver-name gated; upstream rootlesskit/builtin paths
  are byte-identical (covered by updated upstream `TestChildHostIP` and the
  bridge mock update).
- v6 only works with the matched rebuilt fixture; mismatch fails loudly rather
  than half-working.

## 3. Tests and results (all run locally, no guests)

### Native backend + pinned libslirp (`make publish-tests`, 18 tests)

`tests/publish/test_hostfwd_semantics.py` (real backend process, real host
loopback binds): distinct IPs share a port while exact binds conflict;
wildcard/specific conflicts both orders for TCP and UDP; v4/v6 wildcard
coexist and v6 is v6-only (verified by probing further binds);
::1/127.0.0.1 coexist; tcp/udp independent; strict-create vs idempotent-add;
different guest port conflict; missing-map delete semantics; malformed
families/ports rejected (v4-mapped v6, scoped literal, sctp, bad range);
`stats.forwards`, list family fields, prompt removal and shutdown socket
cleanup.

`tests/publish/test_forwarded_flows.py` (real libslirp data path with the
scripted L2 peer): live tcp4/tcp6/udp4/udp6 echo incl. 1400-byte UDP and
MSS-segmented 4096-byte TCP with half-close EOF; same host port on two
distinct IPs simultaneously; removal fails new flows promptly.

```
$ gcc -O2 -Wall -Wextra -Werror src/network_backend.c -lslirp -o .work/network-backend
$ .work/network-backend --version
4.6.1
$ make publish-tests
Ran 18 tests in 0.277s — OK
```

Debug evidence from failed attempts is preserved in `.work/`
(`debug_flow*.py`, `debug_v6.py`, instrumented libslirp copy under
`.work/libslirp-dbg/`, `harness.c`): the residual model-peer bugs found and
fixed were (a) stale overwrite of the TCP data-offset byte, (b) BB vs HH TCP
flag packing, (c) snd_nxt off-by-one, (d) v6 pseudo-header pack format,
(e) ICMPv6 hop limit 255. A standalone C harness against the real libslirp
(`.work/harness.c`) prints `HANDSHAKE OK` / `DATA DELIVERED` and confirms the
pinned library's `slirp_add_hostxfwd` SYNACK/timer behavior independently of
the Python model.

### Moby Go tests (patched 29.8.2 tree, go1.26.8, vendored, ≤4 jobs under
kernel-build.lock)

- new `harddind_aliases_linux_test.go` (fake netlink backend): startup prune
  leaves primaries/unrelated addresses untouched and assigns wildcards;
  refcount/shared-alias behavior across protos; last-reference removal and
  slot reuse; v4/v6 pool extents matching the validated CIDRs; exhaustion hard
  error with no partial state and recovery after release; transient netlink
  failure leaves no reservation; managed-alias membership table.
- new `harddind_alias_mapper_linux_test.go` (real OS ephemeral allocator +
  fake pdc): two distinct host IPs reserve/release balanced; simulated
  hostfwd bind conflict rolls back ALL guest reservations; port allocation
  failure rolls reservations back too.
- upstream `TestChildHostIP` adapted to the new signature; bridge
  `port_mapping_linux_test.go` mock updated; full `dockerd` compiles.

```
go test ./daemon/libnetwork/internal/rlkclient ./daemon/libnetwork/portmappers/nat  → ok/ok
go vet  ./daemon/libnetwork/internal/rlkclient ./daemon/libnetwork/portmappers/nat ./daemon/libnetwork/drivers/bridge → clean (incl. all test files)
go build ./cmd/dockerd → build OK (.work/dockerd.verify, 130 MB)
```

**Accurate status of bridge package tests (do not treat as passed):** this
agent never got a PASS from `go test ./daemon/libnetwork/drivers/bridge`.
Two filtered runs (`-run 'TestPortMappingConfig|TestPortMappingV6Config|
TestAddPortMappings'`, GOMAXPROCS=4/-p2, exclusive kernel-build lock held)
hung for the full 600s Go timeout while holding the global EX build lock
(full logs preserved verbatim in `.work/evidence/bridge-test-attempt1.log`
and `.work/evidence/go-tests-final.log`). A filtered run against the
**pristine** tree failed immediately with `Failed to enter netns: operation
not permitted` (`context_unix.go:94`), i.e. the package's netns setup cannot
run in this sandbox; owner flagged the lock contention in
OWNER-TEST-BOUNDS.md, so these tests were not retried again. Mock/interface
compatibility is proven only by `go vet` type-checking all test files and
successful compilation of the test binary; the bridge tests must be run in
the owner's guest-capable environment and are an explicit acceptance input,
not a pass claimed here.

### Python unit tests (`python3 -m unittest discover -s tests`)

31 tests OK, including the rewritten `tests/test_port_driver.py`
(v4+v6 validation, wildcard→fixed-alias defaults, distinct parents/children,
pool extents, v2 info advertisement, framed transport error recovery).

## 4. Owner test entry points (serial scheduling)

1. Rebuild artifacts (owner): `make network`, `make moby`, `make port-agent`,
   then `make runtime-root` (guest init/daemon changed) into a fresh work dir.
2. Real nested Docker: `python3 scripts/run-published-ports.py <work> --name
   <name>` — added scenarios `ipv6_tcp_udp_publish`,
   `distinct_host_ips_same_port`, `wildcard_specific_conflict_v4`,
   `wildcard_specific_conflict_v6` (both bind orders, atomic failed start,
   surviving service, post-rm release), alongside all existing scenarios.
3. Native regression (no guest): `make network && make publish-tests`.
4. Go regression: build script `scripts/build-moby.sh` compiles/runs the patch
   series; focused `go test` commands in §3.

## 5. Reproducibility / hashes

- Pinned libslirp upstream tarball (recorded in `sources.lock.json` as
  `network_backend.libslirp_source`, verified against the Ubuntu
  4.6.1-1ubuntu0.2 packaging which ships zero source patches):
  `third_party/libslirp-v4.6.1.tar.bz2`
  sha256 `3ee27347764c629e10228f7002dd22104e9cc21623d565dc0e7e57ed1b63a70e`.
- `moby/patches/0003-publish-address-family.patch`
  sha256 `5a76bcdc9ecdf2f4ac8dc81daf15ca0833cd9a9c6081759142af68ebc3e08417`.
- Local verification binaries (workspace-local, not shipped):
  network-backend
  `647339bbf17b2ed09da6affff87a8531573f414f706b95cbcc29668b78cb381c`,
  patched dockerd
  `4305d9f8b11e94281287dffdc1e9c54e3763c0e3e9ea9d0af8f76addec9aff81`.
- Rebuild: `gcc -O2 -Wall -Wextra -Werror src/network_backend.c -lslirp`;
  Moby via `bash scripts/build-moby.sh <work>` (applies 0001-0003 with
  `patch --fuzz=0`, go1.26.8, vendored).
- The instrumented libslirp/peer debug trail used during diagnosis is kept
  under `.work/` (`harness.c`, `libslirp-dbg/`, `debug_*.py`).

## 6. Remaining limitations (not claimed complete)

- No real nested Docker create/stop/restart/conflict/IPv4+IPv6/TCP+UDP run and
- no same-host performance numbers by this agent; outer global-IP reachability,
  Swarm ingress, abrupt host/guest recovery remain untested.
- Distinct-parent alias pools are bounded per dockerd lifetime (238 v4; 65535
  v6). Exhaustion is a loud error; slots reuse on teardown and the pool resets
  at daemon start. Very long-running daemons that cycled >238 *simultaneously
  alive* distinct v4 parent IPs would need a wider v4 secondary design.
- Scoped/link-local IPv6 parent addresses (no zone in Docker syntax) are
  rejected explicitly rather than remapped.
- The packet model in `tests/publish` proves slirp/backend behavior only;
  Linux guest ownership of netlink-added aliases is demonstrated structurally
  (netlink API + Go tests) and must be confirmed by the owner Docker runs.
