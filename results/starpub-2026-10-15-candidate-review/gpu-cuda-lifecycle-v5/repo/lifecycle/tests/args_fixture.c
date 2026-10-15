/*
 * GPU-free direct-function tests for the probe's strict CLI parsing.
 *
 * Includes the real probe translation unit with main renamed, then
 * drives parse_ordinals() and the strict integer converters directly --
 * no dlopen, cuInit or GPU call occurs (a real driver is therefore not
 * required).  Covers the owner blocker where parse_ordinals() returned
 * -1 for every valid list, plus leading/trailing/repeated commas, junk,
 * overflow, duplicates and atoi/strtoul trailing-junk acceptance.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define main cuda_lifecycle_probe_main
#include "../src/cuda_cuda_lifecycle_probe.c"
#undef main

static int failures;
static int checks;

static void ok(int cond, const char *name)
{
    checks++;
    if (!cond) {
        printf("{\"case\":\"%s\",\"ok\":false}\n", name);
        failures++;
    }
}

static int ordinals_ngpu(const char *s, int count)
{
    struct config cfg;
    memset(&cfg, 0, sizeof(cfg));
    if (parse_ordinals(s, &cfg, count) != 0)
        return -1;
    return cfg.ngpu;
}

static int ordinals_equal(const char *s, int count,
                          const int *want, int want_n)
{
    struct config cfg;
    int i;
    memset(&cfg, 0, sizeof(cfg));
    if (parse_ordinals(s, &cfg, count) != 0)
        return 0;
    if (cfg.ngpu != want_n)
        return 0;
    for (i = 0; i < want_n; i++)
        if (cfg.ordinals[i] != want[i])
            return 0;
    return 1;
}

static int pint(const char *s, long lo, long hi)
{
    int v = 0;
    return parse_int_strict(s, lo, hi, &v) == 0;
}

static int pulong(const char *s, unsigned long lo, unsigned long hi)
{
    unsigned long v = 0;
    return parse_ulong_strict(s, lo, hi, &v) == 0;
}

int main(void)
{
    int one[1] = {3}, four[4] = {0, 1, 2, 3}, rev[4] = {3, 2, 1, 0};

    /* Valid ordinal lists: exact positive return (owner blocker). */
    ok(ordinals_ngpu("0", 4) == 1, "valid_single");
    ok(ordinals_ngpu("3", 4) == 1, "valid_single_last_index");
    ok(ordinals_equal("0,1,2,3", 4, four, 4), "valid_all_four");
    ok(ordinals_equal("3,2,1,0", 4, rev, 4), "valid_reversed_order");
    ok(ordinals_equal("2,0", 4, (int[]){2, 0}, 2), "valid_subset");
    ok(ordinals_ngpu("0,3", 4) == 2, "valid_pair_count");

    /* Rejected: empty / comma shape. */
    ok(ordinals_ngpu("", 4) < 0, "reject_empty_string");
    ok(ordinals_ngpu(NULL, 4) < 0, "reject_null");
    ok(ordinals_ngpu(",0", 4) < 0, "reject_leading_comma");
    ok(ordinals_ngpu("0,", 4) < 0, "reject_trailing_comma");
    ok(ordinals_ngpu("0,,1", 4) < 0, "reject_repeated_comma");
    ok(ordinals_ngpu(",", 4) < 0, "reject_only_commas");

    /* Rejected: junk / trailing garbage / signs / overflow. */
    ok(ordinals_ngpu("x", 4) < 0, "reject_junk");
    ok(ordinals_ngpu("0x1", 4) < 0, "reject_hex");
    ok(ordinals_ngpu("1x", 4) < 0, "reject_trailing_junk");
    ok(ordinals_ngpu(" 0", 4) < 0, "reject_leading_space");
    ok(ordinals_ngpu("0 ", 4) < 0, "reject_trailing_space");
    ok(ordinals_ngpu("-1", 4) < 0, "reject_negative");
    ok(ordinals_ngpu("+1", 4) < 0, "reject_plus_sign");
    ok(ordinals_ngpu("999999999999999999999999", 4) < 0,
       "reject_overflow");
    ok(ordinals_ngpu("4294967296", 4) < 0, "reject_u32_overflow");

    /* Rejected: range / duplicate. */
    ok(ordinals_ngpu("4", 4) < 0, "reject_out_of_range");
    ok(ordinals_ngpu("0,0", 4) < 0, "reject_duplicate");
    ok(ordinals_ngpu("1,2,1", 4) < 0, "reject_duplicate_later");
    ok(ordinals_ngpu("0,1,2,3,0", 4) < 0, "reject_duplicate_when_full");

    /* Bounded copy: more than MAX_ITEMS distinct valid indices. */
    {
        struct config cfg;
        char big[256];
        int i, off = 0;
        memset(&cfg, 0, sizeof(cfg));
        memset(big, 0, sizeof(big));
        for (i = 0; i < MAX_ITEMS + 2; i++)
            off += snprintf(big + off, sizeof(big) - off, "%s%d",
                            i ? "," : "", i);
        /* count must exceed the highest requested index and be large */
        ok(parse_ordinals(big, &cfg, MAX_ITEMS + 3) < 0,
           "reject_more_than_MAX_ITEMS");
    }

    /* Strict signed converter boundaries (parent range 1..32). */
    ok(pint("1", 1, MAX_ITEMS), "int_boundary_low_ok");
    ok(pint("32", 1, MAX_ITEMS), "int_boundary_high_ok");
    ok(!pint("0", 1, MAX_ITEMS), "int_below_low_rejected");
    ok(!pint("33", 1, MAX_ITEMS), "int_above_high_rejected");
    ok(!pint("", 1, MAX_ITEMS), "int_empty_rejected");
    ok(!pint("1 ", 1, MAX_ITEMS), "int_trailing_space_rejected");
    ok(!pint("1x", 1, MAX_ITEMS), "int_trailing_junk_rejected");
    ok(!pint("-1", 1, MAX_ITEMS), "int_negative_rejected");
    ok(!pint("999999999999999999999", 1, MAX_ITEMS),
       "int_overflow_rejected");
    {
        int v = -77;
        ok(parse_int_strict("8", 1, MAX_ITEMS, &v) == 0 && v == 8,
           "int_writes_value");
    }

    /* Strict unsigned converter (elements range 4096..1048576). */
    ok(pulong("4096", 4096, 1u << 20), "ulong_boundary_low_ok");
    ok(pulong("1048576", 4096, 1u << 20), "ulong_boundary_high_ok");
    ok(!pulong("4095", 4096, 1u << 20), "ulong_below_low_rejected");
    ok(!pulong("1048577", 4096, 1u << 20), "ulong_above_high_rejected");
    ok(!pulong("", 4096, 1u << 20), "ulong_empty_rejected");
    ok(!pulong("-1", 4096, 1u << 20), "ulong_negative_rejected");
    ok(!pulong("4096x", 4096, 1u << 20), "ulong_trailing_junk_rejected");
    ok(!pulong("0x1000", 4096, 1u << 20), "ulong_hex_rejected");
    ok(!pulong("999999999999999999999999", 4096, 1u << 20),
       "ulong_overflow_rejected");

    printf("{\"case\":\"fixture_summary\",\"checks\":%d,\"failures\":%d}\n",
           checks, failures);
    (void)one;
    return failures ? 1 : 0;
}
