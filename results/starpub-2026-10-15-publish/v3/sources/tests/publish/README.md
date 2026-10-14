# Native published-port probes (NOT nested-Docker acceptance)

These tests spawn the real `network-backend` binary (built from
`src/network_backend.c`, pinned libslirp 4.6.1) over a disposable AF_UNIX
seqpacket pair with a private unix-dgram control socket. Everything binds
host loopback ephemeral ports; no device, namespace, UML, dockerd or GPU is
involved.

- `test_hostfwd_semantics.py` — real kernel-adjudicated bind/conflict/removal
  matrix via the control protocol: same port on distinct local IPs,
  wildcard/specific both orders, dual-stack v6-only, tcp/udp independence,
  strict create vs idempotent add, capacity of `list`/`stats`, cleanup.
- `test_forwarded_flows.py` — live data-path probes for tcp4/tcp6/udp4/udp6,
  including TCP half-close teardown and removal. The guest endpoint is a
  **scripted L2 model** (`slirp_peer.py`: minimal ARP/NDP/IPv4/IPv6 + TCP/UDP
  echo). It proves the pinned slirp `slirp_add_hostxfwd` data path and the
  backend control glue, but it is **not** proof of real Linux guest ownership
  of the dynamic aliases: that is exercised by Moby Go tests
  (`harddind_aliases_linux_test.go`, `harddind_alias_mapper_linux_test.go`)
  and ultimately by the owner's nested-Docker run
  (`scripts/run-published-ports.py`).

Run (after `make network WORK=.work`):

```
make publish-tests
HARDDIND_NETWORK_BACKEND=/path/to/network-backend python3 -m unittest -v tests.publish.test_hostfwd_semantics
```
