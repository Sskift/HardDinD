# 0024 — v11 NV2081_BINAPI opaque-blob forwarding

Source returned after owner v11 verified the char-fd event fix
(ALLOC/FREE_OS_EVENT succeed 4/4 rc0/status0). cuInit still 100 due to
RM_CONTROL 0x20810108 paramsSize=992 EOPNOTSUPP (plus a separately reported
0x80028b). This round implements only the proven BINAPI operation.

## Proof (pinned 550.120 + actual native record)
- Class: `cl2081.h` NV2081_BINAPI, empty NV2081_ALLOC_PARAMETERS; native
  trace allocates the object (handle 0x5c000004) and issues control
  0x20810108 on it (v11 report.json: client 0xc1d558c0, object
  0x5c000004, cmd 0x20810108, flags 0, params=0x7fffdb12dcb0, size=992,
  rc0/status0).
- Dispatch: `rmapi/binary_api.c binapiControl_IMPL` forwards
  (cmd, pParams, paramsSize) verbatim to the physical RM (`pRmApi->Control`)
  on GSP or `NV_RM_RPC_API_CONTROL` on vGPU; non-GSP returns NV_OK. The host
  never dereferences nested pointers inside the blob.
- Copy bounds: `rmapi/param_copy.c` copies the buffer as an opaque packet
  bounded at RMAPI_PARAM_COPY_MAX_PARAMS_SIZE (1 MiB); object-class binding
  is enforced server-side, so no bridge class table or object-reuse special
  case is needed.

## Implementation
`gpu_rm_control_binapi`: exact command 0x20810108 only (no wildcard);
bounces the 32-byte NVOS54 header plus the opaque child (size from the
snapshot header) through the 128-byte stub payload, with the tighter bridge
bound child ≤ 16384−48. No pointer rewriting. Out-of-range child pointer
becomes a non-null host-invalid ~0ULL so the host reports its native copy
fault; copyout occurs on success and is skipped on host copy/ioctl failure
(generic RMAPI policy). Flags/size are driver-validated.

## Explicitly not done
- 0x80028b (interface 0x80 cmd 0x28b): absent from pinned ctrl0080 open
  headers; catalog marks it opaque-unknown. No proof -> stays fail-closed.
- 0x20808159 / 0x20808162 / 0x20810108 other forms unchanged.
- DETACH_IDS is implemented but was never exercised in an actual trace
  (owner correction); no measured detach claim.

## Verification (bounded, no VM/GPU by author)
- build exit 0, zero warnings, ≤4 jobs, global EX; stub code 1830/4096.
- 9/9 unittest models (new binapi_abi_model decodes the captured header and
  checks bounds/dispatch), 8/8 seccomp combos, stub relocation/size check.
- 0024 patch applies to immutable baseline v4 and round-trips byte-identical.

## Hashes
- kernel linux: `f157a3d6ae6a47a5f2155a4c6ffdcb3039852303899a4f1f5320d93aaa04b8aa`
- gpu.c: `3f06efd1a4048513a2dbdcd58762a5031abd4f24e2ff831b1f63fc30aa049714`
- patch: `894167c1cc02b67c66d359e940fb0eb01aaf544d182b7d240cf19cf613a7e72a`
- identity dir `.work/identity-v11/`; model limitation: the fixture proves
  header decode/bounds from the native record, not live GSP blob semantics
  (owner-only actual run).
