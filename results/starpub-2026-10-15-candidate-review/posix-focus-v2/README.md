# POSIX focus v2: descriptor permissions and PID namespaces

The owner fixed the probe pipe lifetime from v1: retain the G2 writer until
after fork, close it in the parent, and close the reader in the child. The
virtiofsd binary remains `13ebaff57033f0a54f2ee7e708ccdc66171ed30f941f739f06b3543d1d53eb09`.
The tested probe is `c60eb1ca164f45cce23dbe9c5882c385b68eda8088baaa53ea08721017abe261`.

| Environment | Tested | Not tested | Failed |
|---|---:|---:|---:|
| Starpub native | 10 | 2 | 0 |
| Internal Docker, default profile | 11 | 1 | 0 |
| Internal Docker, namespace-enabled test | 12 | 0 | 0 |

Checks cover read/write descriptor access, chmod after opening with UID 42424,
new-open denial versus a retained descriptor, lock conflicts, and GETLK PID
identity. The nested holder's ancestor PID was 8, local PID 1, querier local
PID 2; GETLK returned 1. Sibling containers returned 0 for the invisible PID.
The namespace-enabled test adds SYS_ADMIN and disables seccomp only inside
the inner test container. Starpub's outer restrictions remain intact. Native
cannot create the nested namespace here; those two omissions are not passes.

The owner also corrected the author runner's instance/container names and
persisted every probe identity before readiness, with bounded group cleanup.
The original author runner and first preparation failure remain archived.
Seven parent commands exited 0, shutdown was normal, input hashes stayed
unchanged, and all 32 recorded groups were empty. `python3 recheck.py` checks
both immutable runs and their raw reports. No I/O performance, shared mmap or
mixed outer/guest deadlock parity follows from this result.
