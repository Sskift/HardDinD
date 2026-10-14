/* Compile the actual probe layouts against the fixed NVIDIA SDK. */
#include "nvos.h"
#include "rs_access.h"
#include "class/clcb33.h"
#define main gpu_alloc_probe_main
#include "gpu_alloc_probe.c"
#undef main

_Static_assert(NV_CONFIDENTIAL_COMPUTE == 0xcb33, "allocation class");
_Static_assert(sizeof(NV_CONFIDENTIAL_COMPUTE_ALLOC_PARAMS) == 4, "allocation size");
_Static_assert(offsetof(NV_CONFIDENTIAL_COMPUTE_ALLOC_PARAMS, hClient) == 0, "client handle");
_Static_assert(sizeof(RS_ACCESS_MASK) == 4, "rights mask");
_Static_assert(NVOS64_FLAGS_FINN_SERIALIZED == 1, "serialized allocation flag");
#define FIELD(local, official, a, b) _Static_assert(offsetof(local, a) == offsetof(official, b), #a)
_Static_assert(sizeof(struct nvos21) == sizeof(NVOS21_PARAMETERS), "NVOS21 size");
_Static_assert(sizeof(struct nvos64) == sizeof(NVOS64_PARAMETERS), "NVOS64 size");
_Static_assert(sizeof(struct nvos00) == sizeof(NVOS00_PARAMETERS), "NVOS00 size");
FIELD(struct nvos21, NVOS21_PARAMETERS, root, hRoot);
FIELD(struct nvos21, NVOS21_PARAMETERS, parent, hObjectParent);
FIELD(struct nvos21, NVOS21_PARAMETERS, object, hObjectNew);
FIELD(struct nvos21, NVOS21_PARAMETERS, cls, hClass);
FIELD(struct nvos21, NVOS21_PARAMETERS, alloc, pAllocParms);
FIELD(struct nvos21, NVOS21_PARAMETERS, size, paramsSize);
FIELD(struct nvos21, NVOS21_PARAMETERS, status, status);
FIELD(struct nvos64, NVOS64_PARAMETERS, root, hRoot);
FIELD(struct nvos64, NVOS64_PARAMETERS, parent, hObjectParent);
FIELD(struct nvos64, NVOS64_PARAMETERS, object, hObjectNew);
FIELD(struct nvos64, NVOS64_PARAMETERS, cls, hClass);
FIELD(struct nvos64, NVOS64_PARAMETERS, alloc, pAllocParms);
FIELD(struct nvos64, NVOS64_PARAMETERS, rights, pRightsRequested);
FIELD(struct nvos64, NVOS64_PARAMETERS, size, paramsSize);
FIELD(struct nvos64, NVOS64_PARAMETERS, flags, flags);
FIELD(struct nvos64, NVOS64_PARAMETERS, status, status);
int main(void) { return 0; }
