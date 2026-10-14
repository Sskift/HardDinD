/* SPDX-License-Identifier: GPL-2.0
 * Deterministic RM escape ABI extraction model, round5/gpu-mapping-repair.
 *
 * Userspace compile against the pinned NVIDIA 550.120 SDK headers
 * (sources.lock.json -> nvidia_abi_reference); no GPU involved.  Pins the
 * NVOS block layouts the 0024 bridge marshals, including the distinct
 * 32-byte NVOS21 and 48-byte NVOS64 RM_ALLOC layouts (whose paramsSize
 * fields live at different offsets) and the trailing-FD wrappers.
 */
#include <stdio.h>
#include <stddef.h>
#include "nvos.h"
#include "nv-ioctl.h"
#include "nv-ioctl-numbers.h"
#include "nv-ioctl-numa.h"
#include "nv-unix-nvos-params-wrappers.h"
#include "nv_escape.h"

int main(void)
{
	printf("RM NVOS00 size=%zu status=%zu\n",
	       sizeof(NVOS00_PARAMETERS),
	       offsetof(NVOS00_PARAMETERS, status));
	printf("RM NVOS21 size=%zu pAllocParms=%zu paramsSize=%zu status=%zu\n",
	       sizeof(NVOS21_PARAMETERS),
	       offsetof(NVOS21_PARAMETERS, pAllocParms),
	       offsetof(NVOS21_PARAMETERS, paramsSize),
	       offsetof(NVOS21_PARAMETERS, status));
	printf("RM NVOS64 size=%zu pAllocParms=%zu pRightsRequested=%zu "
	       "paramsSize=%zu flags=%zu status=%zu\n",
	       sizeof(NVOS64_PARAMETERS),
	       offsetof(NVOS64_PARAMETERS, pAllocParms),
	       offsetof(NVOS64_PARAMETERS, pRightsRequested),
	       offsetof(NVOS64_PARAMETERS, paramsSize),
	       offsetof(NVOS64_PARAMETERS, flags),
	       offsetof(NVOS64_PARAMETERS, status));
	printf("RM NVOS54 size=%zu params=%zu paramsSize=%zu status=%zu\n",
	       sizeof(NVOS54_PARAMETERS),
	       offsetof(NVOS54_PARAMETERS, params),
	       offsetof(NVOS54_PARAMETERS, paramsSize),
	       offsetof(NVOS54_PARAMETERS, status));
	printf("RM NVOS02 wrapper size=%zu pMemory=%zu limit=%zu fd=%zu\n",
	       sizeof(nv_ioctl_nvos02_parameters_with_fd),
	       offsetof(nv_ioctl_nvos02_parameters_with_fd, params.pMemory),
	       offsetof(nv_ioctl_nvos02_parameters_with_fd, params.limit),
	       offsetof(nv_ioctl_nvos02_parameters_with_fd, fd));
	printf("RM NVOS33 wrapper size=%zu offset=%zu length=%zu "
	       "pLinearAddress=%zu fd=%zu\n",
	       sizeof(nv_ioctl_nvos33_parameters_with_fd),
	       offsetof(nv_ioctl_nvos33_parameters_with_fd, params.offset),
	       offsetof(nv_ioctl_nvos33_parameters_with_fd, params.length),
	       offsetof(nv_ioctl_nvos33_parameters_with_fd,
			params.pLinearAddress),
	       offsetof(nv_ioctl_nvos33_parameters_with_fd, fd));
	printf("RM NVOS56 size=%zu pOldCpuAddress=%zu pNewCpuAddress=%zu\n",
	       sizeof(NVOS56_PARAMETERS),
	       offsetof(NVOS56_PARAMETERS, pOldCpuAddress),
	       offsetof(NVOS56_PARAMETERS, pNewCpuAddress));
	printf("RM OS_EVENT size=%zu fd=%zu status=%zu\n",
	       sizeof(nv_ioctl_alloc_os_event_t),
	       offsetof(nv_ioctl_alloc_os_event_t, fd),
	       offsetof(nv_ioctl_alloc_os_event_t, Status));

	/* Escape request numbers used by the bridge dispatch. */
	printf("RM ESC RmAllocMemory=%u RmFree=%u RmControl=%u "
	       "RmAlloc=%u RmMapMemory=%u\n",
	       NV_ESC_RM_ALLOC_MEMORY, NV_ESC_RM_FREE, NV_ESC_RM_CONTROL,
	       NV_ESC_RM_ALLOC, NV_ESC_RM_MAP_MEMORY);
	printf("RM ESC RegisterFd=%u AllocOsEvent=%u FreeOsEvent=%u\n",
	       NV_ESC_REGISTER_FD, NV_ESC_ALLOC_OS_EVENT,
	       NV_ESC_FREE_OS_EVENT);
	printf("RM ESC SetNumaStatus=%u ExportToDmaBufFd=%u\n",
	       NV_ESC_SET_NUMA_STATUS, NV_ESC_EXPORT_TO_DMABUF_FD);
	return 0;
}
