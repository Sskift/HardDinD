#include <stdio.h>
#include <stddef.h>
#include "class/cl0005.h"
#include "class/cl0080.h"
#include "class/cl2080.h"
#include "class/cl2081.h"
#include "class/cl503c.h"
#include "class/cl83de.h"
#include "class/cl9067.h"
#include "class/cl90f1.h"
#include "class/clb0b5sw.h"
#include "class/clcb33.h"
#include "alloc/alloc_channel.h"
#include "nvos.h"
#define S(t) printf("%-42s %3zu\n", #t, sizeof(t))
int main(void){
 S(NV_MEMORY_ALLOCATION_PARAMS);
 S(NV0005_ALLOC_PARAMETERS);
 S(NV0080_ALLOC_PARAMETERS);
 S(NV2080_ALLOC_PARAMETERS);
 S(NV2081_ALLOC_PARAMETERS);
 S(NV503C_ALLOC_PARAMETERS);
 S(NV83DE_ALLOC_PARAMETERS);
 S(NV_CTXSHARE_ALLOCATION_PARAMETERS);
 S(NV_VASPACE_ALLOCATION_PARAMETERS);
 S(NV_CHANNEL_GROUP_ALLOCATION_PARAMETERS);
 S(NV_CHANNEL_ALLOC_PARAMS);
 S(NVB0B5_ALLOCATION_PARAMETERS);
 S(NV_GR_ALLOCATION_PARAMETERS);
 S(NV_CONFIDENTIAL_COMPUTE_ALLOC_PARAMS);
 return 0;}
