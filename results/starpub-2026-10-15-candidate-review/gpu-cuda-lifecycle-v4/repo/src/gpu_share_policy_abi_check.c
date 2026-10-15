/*
 * Compile-time ABI cross-check for the 0xd04 transport: build this against
 * the fixed NVIDIA 550.120 SDK headers (read-only reference in
 * sources.lock.json, nvidia_abi_reference commit 5e52edb2034d) and against
 * the probe's private ABI definitions.  Any layout drift fails the build.
 *
 * gcc -O2 -Wall -Wextra -Werror \
 *     -I<nvidia-550.120>/src/common/sdk/nvidia/inc \
 *     -I<nvidia-550.120>/kernel-open/common/inc \
 *     gpu_share_policy_abi_check.c -o gpu-share-policy-abi-check
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "rs_access.h"
#include "ctrl/ctrl0000/ctrl0000client.h"

/* Mirror of the transport/probe definition. */
struct local_share_policy {
	uint32_t target;
	uint32_t accessMask;
	uint16_t type;
	uint8_t  action;
	uint8_t  padding;
};

_Static_assert(sizeof(RS_SHARE_POLICY) == 12,
	       "driver RS_SHARE_POLICY must be 12 bytes");
_Static_assert(offsetof(RS_SHARE_POLICY, target) == 0, "target offset");
_Static_assert(offsetof(RS_SHARE_POLICY, accessMask) == 4,
	       "accessMask offset");
_Static_assert(offsetof(RS_SHARE_POLICY, type) == 8, "type offset");
_Static_assert(offsetof(RS_SHARE_POLICY, action) == 10, "action offset");

_Static_assert(sizeof(struct local_share_policy) ==
	       sizeof(RS_SHARE_POLICY), "local/driver size mismatch");
_Static_assert(offsetof(struct local_share_policy, target) ==
	       offsetof(RS_SHARE_POLICY, target), "target mismatch");
_Static_assert(offsetof(struct local_share_policy, accessMask) ==
	       offsetof(RS_SHARE_POLICY, accessMask), "mask mismatch");
_Static_assert(offsetof(struct local_share_policy, type) ==
	       offsetof(RS_SHARE_POLICY, type), "type mismatch");
_Static_assert(offsetof(struct local_share_policy, action) ==
	       offsetof(RS_SHARE_POLICY, action), "action mismatch");

_Static_assert(sizeof(NV0000_CTRL_CLIENT_SET_INHERITED_SHARE_POLICY_PARAMS)
	       == 12, "0xd04 params must be exactly one RS_SHARE_POLICY");
_Static_assert(NV0000_CTRL_CMD_CLIENT_SET_INHERITED_SHARE_POLICY == 0xd04,
	       "0xd04 command number");

/* rs_access.h: share types and action flags used by the probe. */
_Static_assert(RS_SHARE_TYPE_NONE == 0, "");
_Static_assert(RS_SHARE_TYPE_ALL == 1, "");
_Static_assert(RS_SHARE_TYPE_OS_SECURITY_TOKEN == 2, "");
_Static_assert(RS_SHARE_TYPE_CLIENT == 3, "");
_Static_assert(RS_SHARE_TYPE_PID == 4, "");
_Static_assert(RS_SHARE_TYPE_SMC_PARTITION == 5, "");
_Static_assert(RS_SHARE_TYPE_GPU == 6, "");
_Static_assert(RS_SHARE_TYPE_FM_CLIENT == 7, "");
_Static_assert(RS_SHARE_TYPE_MAX == 8, "");
_Static_assert(RS_SHARE_ACTION_FLAG_REVOKE == 0x1, "");
_Static_assert(RS_SHARE_ACTION_FLAG_REQUIRE == 0x2, "");
_Static_assert(RS_SHARE_ACTION_FLAG_COMPOSE == 0x4, "");
_Static_assert(RS_ACCESS_DUP_OBJECT == 0 && RS_ACCESS_NICE == 1 &&
	       RS_ACCESS_DEBUG == 2 && RS_ACCESS_COUNT == 3, "");

int main(void)
{
	return 0;
}
