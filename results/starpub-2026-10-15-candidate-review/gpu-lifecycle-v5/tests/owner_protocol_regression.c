/*
 * Owner protocol hardening regression for the strict child wire token.
 * Includes the real probe translation unit and drives proto_cycle()
 * directly with edge values: the maximum-width legal token must survive
 * exactly, while overflow (unsigned / signed / int64 timestamp), field
 * count, trailing/leading/duplicate-comma and non-monotonic inputs must
 * all be rejected.  Bounded, GPU-free; built by run_host_fixtures.sh.
 */
#define main lifecycle_probe_regression_main
#include "../src/cuda_lifecycle_probe.c"
#undef main
#include <limits.h>
#include <stdio.h>

static int failures;

#define EXPECT(cond, name) do { \
    if (!(cond)) { printf("{\"case\":\"%s\",\"ok\":false}\n", name); \
                   failures++; } } while (0)

static int parse(const char *tok, struct cycle_result *r)
{
    return proto_cycle(tok, r);
}

int main(void)
{
    struct cycle_result r;
    char token[CHILD_CYCLE_TOKEN_MAX + 1];
    int n, rc;

    /* Maximum-width legal token: u32 maxes, INT_MIN rcs, INT64 times. */
    n = snprintf(token, sizeof(token),
        "%u,%u,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%u,%u,%lld,%lld",
        UINT32_MAX, UINT32_MAX, INT_MIN, INT_MIN, INT_MIN, INT_MIN,
        INT_MIN, INT_MIN, INT_MIN, INT_MIN, INT_MIN, INT_MIN,
        UINT32_MAX, UINT32_MAX, 1LL, 9223372036854775807LL);
    rc = parse(token, &r);
    int preserved = rc == 0 && r.idx == UINT32_MAX && r.seed == UINT32_MAX
        && r.alloc_rc[0] == INT_MIN && r.htod_rc == INT_MIN
        && r.free_rc[2] == INT_MIN && r.mismatches == UINT32_MAX
        && r.csum == UINT32_MAX && r.start_ns == 1
        && r.end_ns == INT64_MAX;
    printf("{\"case\":\"max_token\",\"bytes\":%d,\"rc\":%d,"
           "\"checksum\":%u,\"preserved\":%s}\n", n, rc, r.csum,
           preserved ? "true" : "false");
    EXPECT(preserved, "max_token");
    EXPECT((size_t)n <= CHILD_CYCLE_TOKEN_MAX, "max_token_margin");

    struct { const char *tok; const char *name; } bad[] = {
        {"4294967296,1,0,0,0,0,0,0,0,0,0,0,0,1,1,2", "unsigned_overflow_idx"},
        {"1,4294967296,0,0,0,0,0,0,0,0,0,0,0,1,1,2", "unsigned_overflow_seed"},
        {"0,1,2147483648,0,0,0,0,0,0,0,0,0,0,1,1,2", "signed_overflow_rc"},
        {"0,1,-2147483649,0,0,0,0,0,0,0,0,0,0,1,1,2", "signed_underflow_rc"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,4294967296,1,1,2", "unsigned_overflow_mm"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,0,4294967296,1,2", "unsigned_overflow_csum"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,0,1,9223372036854775808,2",
         "int64_overflow_start"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,0,1,-1,2", "negative_start"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,0,1,2,1", "non_monotonic"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,0,1,1,2,", "trailing_comma"},
        {",0,1,0,0,0,0,0,0,0,0,0,0,0,1,1,2", "leading_comma"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,0,1,1", "missing_field"},
        {"0,1,0,0,0,0,0,0,0,0,0,0,0,1,1,2,3", "extra_field"},
        {"0,1,0,0,0x1,0,0,0,0,0,0,0,0,1,1,2", "non_numeric"},
        {"", "empty_token"},
    };
    int all_rejected = 1;
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        int x = parse(bad[i].tok, &r);
        if (x != -1) {
            printf("{\"case\":\"%s\",\"rc\":%d}\n", bad[i].name, x);
            all_rejected = 0;
        }
    }
    printf("{\"case\":\"numeric_and_field_bounds\",\"all_rejected\":%s}\n",
           all_rejected ? "true" : "false");
    EXPECT(all_rejected, "numeric_and_field_bounds");

    printf("{\"case\":\"protocol_regression_summary\",\"failures\":%d}\n",
           failures);
    return failures ? 1 : 0;
}
