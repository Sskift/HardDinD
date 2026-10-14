#include <stdio.h>
#include <stddef.h>
#include "ctrl/ctrl2080.h"
#include "ctrl/ctrl0000/ctrl0000gpu.h"
#include "ctrl/ctrl0000/ctrl0000system.h"
#include "ctrl/ctrl0000/ctrl0000syncgpuboost.h"
#include "ctrl/ctrl0000/ctrl0000client.h"
#include "ctrl/ctrl0080/ctrl0080gpu.h"
#include "ctrl/ctrl0080/ctrl0080fb.h"
#include "ctrl/ctrl0080/ctrl0080host.h"
#include "ctrl/ctrl0080/ctrl0080fifo.h"
#include "ctrl/ctrl0080/ctrl0080perf.h"
#include "ctrl/ctrl503c.h"
#include "ctrl/ctrl83de/ctrl83dedebug.h"
#include "ctrl/ctrl90e6.h"
#include "ctrl/ctrla06c.h"
#include "ctrl/ctrlc36f.h"
#include "ctrl/ctrlcb33.h"
#define P(t) printf("%-55s %zu\n", #t, sizeof(t))
int main(void){
P(NV0000_CTRL_GPU_GET_ATTACHED_IDS_PARAMS);
P(NV0000_CTRL_GPU_GET_ID_INFO_V2_PARAMS);
P(NV0000_CTRL_GPU_GET_PCI_INFO_PARAMS);
P(NV0000_CTRL_GPU_GET_UUID_FROM_GPU_ID_PARAMS);
P(NV0000_CTRL_SYSTEM_GET_FABRIC_STATUS_PARAMS);
P(NV0000_CTRL_SYSTEM_GET_P2P_CAPS_MATRIX_PARAMS);
P(NV0000_CTRL_SYSTEM_GET_FEATURES_PARAMS);
P(NV0000_CTRL_GPU_GET_PROBED_IDS_PARAMS);
P(NV0000_CTRL_GPU_ATTACH_IDS_PARAMS);
P(NV0000_CTRL_GPU_GET_MEMOP_ENABLE_PARAMS);
P(NV0000_SYNC_GPU_BOOST_GROUP_INFO_PARAMS);
P(NV0000_CTRL_CLIENT_GET_ADDR_SPACE_TYPE_PARAMS);
P(NV0080_CTRL_GPU_GET_NUM_SUBDEVICES_PARAMS);
P(NV0080_CTRL_GPU_GET_VIRTUALIZATION_MODE_PARAMS);
P(NV0080_CTRL_GPU_GET_CLASSLIST_V2_PARAMS);
P(NV0080_CTRL_FB_GET_CAPS_V2_PARAMS);
P(NV0080_CTRL_HOST_GET_CAPS_V2_PARAMS);
P(NV0080_CTRL_PERF_CUDA_LIMIT_CONTROL_PARAMS);
return 0;}
