#define main lifecycle_original_main
#include "../src/cuda_lifecycle_probe.c"
#undef main
#include <limits.h>

int main(int argc, char **argv)
{
    struct cycle_result result;
    char token[512];
    int fixed = argc == 2 && !strcmp(argv[1], "--expect-fixed");
    int n = snprintf(token, sizeof(token),
        "%u,%u,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%u,%u",
        UINT32_MAX, UINT32_MAX, INT_MIN, INT_MIN, INT_MIN, INT_MIN,
        INT_MIN, INT_MIN, INT_MIN, INT_MIN, INT_MIN, INT_MIN,
        UINT32_MAX, UINT32_MAX);
    int rc = proto_cycle(token, &result);
    int preserved = rc == 0 && result.idx == UINT32_MAX &&
        result.seed == UINT32_MAX && result.alloc_rc[0] == INT_MIN &&
        result.mismatches == UINT32_MAX && result.csum == UINT32_MAX;
    printf("{\"case\":\"max_token\",\"bytes\":%d,\"rc\":%d,\"checksum\":%u,\"preserved\":%s}\n",
           n, rc, result.csum, preserved ? "true" : "false");
    int unsigned_rc = proto_cycle("4294967296,1,0,0,0,0,0,0,0,0,0,0,0,1", &result);
    int signed_rc = proto_cycle("0,1,4294967296,0,0,0,0,0,0,0,0,0,0,1", &result);
    int trailing_rc = proto_cycle("0,1,0,0,0,0,0,0,0,0,0,0,0,1,", &result);
    printf("{\"case\":\"numeric_and_field_bounds\",\"unsigned_overflow_rc\":%d,\"signed_overflow_rc\":%d,\"trailing_field_rc\":%d}\n",
           unsigned_rc, signed_rc, trailing_rc);
    if (fixed)
        return !(preserved && unsigned_rc == -1 && signed_rc == -1 && trailing_rc == -1);
    return !(!preserved && rc == 0 && unsigned_rc == 0 && signed_rc == 0 && trailing_rc == 0);
}
