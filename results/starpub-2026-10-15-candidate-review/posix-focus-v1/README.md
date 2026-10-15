# POSIX focus v1: probe pipe failure

The unchanged `13ebaff5…` virtiofsd backend passed the new descriptor-mode,
permission-revocation, same-namespace and sibling-container PID checks. Native
reported 10 tested / 2 not tested; default Docker 11 / 1. The explicitly
namespace-enabled inner container failed its nested PID check (11 tested,
1 failure). These are functional observations, not performance acceptance.

The probe closed `g2res[1]` before forking the querier, so no result could be
written. Its recorded `EIO`, querier PID -1 and GETLK PID -1 are a probe
failure, not evidence of a backend defect. All seven parent commands and raw
reports are retained. The guest stopped normally and 32 recorded groups were
empty. See [v2](../posix-focus-v2/README.md) for the repaired pipe lifetime.
