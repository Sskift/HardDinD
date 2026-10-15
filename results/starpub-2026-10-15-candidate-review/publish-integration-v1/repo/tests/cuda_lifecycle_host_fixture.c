/*
 * Executable negative-path fixture for the CUDA lifecycle probe's pure,
 * non-CUDA host logic.  It includes the actual probe translation unit
 * (main renamed at compile time) and exercises:
 *
 *   1. validate_outputs() with clean data and with malformed device data:
 *      NaN, +/-infinity, negative and out-of-range floats must fail
 *      deterministically, must never reach the float->unsigned conversion
 *      (built with -fsanitize=undefined,address), and the checksum must
 *      accumulate only validated bounded expected values;
 *   2. cycle_seed(): explicit role/proc/worker/round/cycle namespace,
 *      run-wide uniqueness, and a printed table cross-checked against the
 *      Python mirror in gpu_lifecycle_checks.py;
 *   3. format_child_line(): the worst-case report (MAX_CYCLES records,
 *      INT_MIN rc values, UINT32_MAX counters, longest integers) must fit
 *      CHILD_LINE_MAX, and an undersized buffer must fail closed (-1)
 *      without truncation or an overrun;
 *   4. parse_child_line(): truncated, count-mismatched and garbage wire
 *      reports must be rejected; the worst-case report round-trips.
 *
 * This fixture never calls dlopen/cuInit and needs no GPU.  Build and run
 * through tests/run_host_fixtures.sh (bounded, under kernel-build.lock).
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <limits.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>

#define main cuda_lifecycle_probe_main
#include "../src/cuda_lifecycle_probe.c"
#undef main

static int failures;

#define CHECK(cond, ...) do { \
    if (!(cond)) { printf("{\"case\":\"fixture_assert\",\"ok\":false,\"at\":%d}\n", __LINE__); \
                   failures++; } } while (0)

static void fill_inputs(float *ha, float *hb, unsigned n, uint32_t seed)
{
    unsigned j;
    for (j = 0; j < n; j++) {
        ha[j] = pattern_value(j, seed, 0);
        hb[j] = pattern_value(j, seed, 1);
    }
}

static void case_validate(const char *name, unsigned n, uint32_t seed,
                          unsigned corrupt_index, float corrupt_value)
{
    float *ha = malloc(n * sizeof(float));
    float *hb = malloc(n * sizeof(float));
    float *hc = malloc(n * sizeof(float));
    uint32_t good_m = 0, good_s = 0, m = 0, s = 0;
    unsigned j;
    int rc_good, rc_bad;

    CHECK(ha && hb && hc, "alloc");
    fill_inputs(ha, hb, n, seed);
    for (j = 0; j < n; j++)
        hc[j] = ha[j] + hb[j];
    rc_good = validate_outputs(ha, hb, hc, n, &good_m, &good_s);
    /* Malformed device data: one element replaced, inputs untouched. */
    hc[corrupt_index] = corrupt_value;
    rc_bad = validate_outputs(ha, hb, hc, n, &m, &s);
    printf("{\"case\":\"validate_%s\",\"clean_rc\":%d,\"bad_rc\":%d,"
           "\"mismatches\":%u,\"csum\":%u,\"clean_csum\":%u,"
           "\"csum_excludes_corrupt\":%s}\n",
           name, rc_good, rc_bad, m, s, good_s,
           (rc_bad == 1 && m == 1 &&
            s == good_s - (uint32_t)(unsigned)(ha[corrupt_index]
                                              + hb[corrupt_index]))
               ? "true" : "false");
    CHECK(rc_good == 0, "clean must validate");
    CHECK(rc_bad == 1 && m == 1, "corrupt element must fail exactly once");
    free(ha); free(hb); free(hc);
}

static void case_validate_all_bad(unsigned n, uint32_t seed)
{
    float *ha = malloc(n * sizeof(float));
    float *hb = malloc(n * sizeof(float));
    float *hc = malloc(n * sizeof(float));
    uint32_t m = 0, s = 0;
    unsigned j;
    int rc;

    CHECK(ha && hb && hc, "alloc");
    fill_inputs(ha, hb, n, seed);
    for (j = 0; j < n; j++)
        hc[j] = NAN;
    rc = validate_outputs(ha, hb, hc, n, &m, &s);
    printf("{\"case\":\"validate_all_nan\",\"rc\":%d,\"mismatches\":%u,"
           "\"csum\":%u}\n", rc, m, s);
    CHECK(rc == 1 && m == n && s == 0, "all-NaN: csum must stay zero");
    free(ha); free(hb); free(hc);
}

static void case_seed_table(void)
{
    /* Fixed tuples; the Python test re-derives the same table. */
    static const uint32_t tuples[][5] = {
        {ROLE_SOLO, 0, 0, 0, 0},
        {ROLE_SOLO, 0, 3, 1, 7},
        {ROLE_SHARED, 0, 0, 0, 0},
        {ROLE_SHARED, 0, 3, 0, 7},
        {ROLE_MULTICARD, 0, 2, 0, 4},
        {ROLE_CHILD, 1, 0, 0, 0},       /* child seq 0, cycle 0 */
        {ROLE_CHILD, 2, 0, 0, 0},       /* child seq 1, cycle 0 */
        {ROLE_CHILD, 5, 0, 0, 1},
    };
    uint32_t all[1024];
    size_t nall = 0;
    unsigned i, j, k;
    int distinct_tuples = 1;

    printf("{\"case\":\"seed_table\",\"seeds\":[");
    for (i = 0; i < sizeof(tuples) / sizeof(tuples[0]); i++) {
        uint32_t v = cycle_seed(tuples[i][0], tuples[i][1], tuples[i][2],
                                tuples[i][3], tuples[i][4]);
        printf("%s%u", i ? "," : "", v);
    }
    printf("]}\n");

    /* Enumerate the default request matrix: no two cycles anywhere,
     * including across exec children, may share a pattern. */
    for (uint32_t role = ROLE_SOLO; role <= ROLE_CHILD; role++) {
        uint32_t procs = role == ROLE_CHILD ? 4u : 1u;
        for (uint32_t proc = 0; proc < procs; proc++) {
            uint32_t p = role == ROLE_CHILD ? proc + 1 : proc;
            for (uint32_t worker = 0; worker < 4; worker++)
                for (uint32_t c = 0; c < 8; c++)
                    all[nall++] = cycle_seed(role, p, worker, 0, c);
        }
    }
    for (i = 0; i < nall; i++)
        for (j = i + 1; j < nall; j++)
            if (all[i] == all[j]) distinct_tuples = 0;
    for (k = 0; k < nall; k++)
        CHECK(all[k] != 0, "seed must never be zero");
    printf("{\"case\":\"seed_matrix\",\"cycles\":%zu,\"unique\":%s}\n",
           nall, distinct_tuples ? "true" : "false");
    CHECK(distinct_tuples, "seed namespace must be globally unique");
}

static struct cycle_result worst_cycles[MAX_CYCLES];

static void build_worst_cycles(void)
{
    int i;
    for (i = 0; i < MAX_CYCLES; i++) {
        struct cycle_result *c = &worst_cycles[i];
        memset(c, 0, sizeof(*c));
        c->idx = 4294967295u;
        c->seed = 4294967295u;
        c->alloc_rc[0] = c->alloc_rc[1] = c->alloc_rc[2] = INT_MIN;
        c->htod_rc = c->launch_rc = c->sync_rc = c->dtoh_rc = INT_MIN;
        c->free_rc[0] = c->free_rc[1] = c->free_rc[2] = INT_MIN;
        c->mismatches = 4294967295u;
        c->csum = 4294967295u;
        c->start_ns = (-9223372036854775807ll - 1) + i;
        c->end_ns = 9223372036854775807ll;
    }
}

static void case_report_bounds(void)
{
    char line[CHILD_LINE_MAX];
    char tiny[64];
    struct child_line_input in;
    size_t prefix_len, one_cycle_len, token_len, full_len;
    int rc_full, rc_tiny;

    build_worst_cycles();
    memset(&in, 0, sizeof(in));
    in.seq = INT_MAX; in.ordinal = INT_MAX; in.elements = INT_MAX;
    in.cycles = 0;
    in.pid = INT_MAX; in.ppid = INT_MAX;
    in.allocs = in.frees = in.mismatches = 4294967295u;
    in.create_rc = in.load_rc = in.unload_rc = in.destroy_rc = INT_MIN;
    in.start_ns = -9223372036854775807ll - 1;
    in.end_ns = 9223372036854775807ll;
    in.uuid = "GPU-ffffffff-ffff-ffff-ffff-ffffffffffff";
    in.cyc = worst_cycles;
    rc_full = format_child_line(line, sizeof(line), &in);
    CHECK(rc_full == 0, "zero-cycle prefix must format");
    prefix_len = strlen(line) - 1;               /* drop newline */

    in.cycles = 1;
    rc_full = format_child_line(line, sizeof(line), &in);
    CHECK(rc_full == 0, "one-cycle report must format");
    one_cycle_len = strlen(line) - 1;
    token_len = one_cycle_len - prefix_len - 1;  /* " c=" separator */

    in.cycles = MAX_CYCLES;
    rc_full = format_child_line(line, sizeof(line), &in);
    CHECK(rc_full == 0, "MAX_CYCLES worst-case report must format");
    full_len = strlen(line);
    rc_tiny = format_child_line(tiny, sizeof(tiny), &in);
    CHECK(rc_tiny == -1, "undersized buffer must fail closed");

    printf("{\"case\":\"report_bounds\",\"prefix_len\":%zu,\"token_len\":%zu,"
           "\"full_len\":%zu,\"CHILD_LINE_MAX\":%d,\"fits\":%s,"
           "\"tiny_buffer_rejected\":%s,\"prefix_within_margin\":%s,"
           "\"token_within_margin\":%s}\n",
           prefix_len, token_len, full_len, (int)CHILD_LINE_MAX,
           (rc_full == 0 && full_len <= CHILD_LINE_MAX) ? "true" : "false",
           rc_tiny == -1 ? "true" : "false",
           prefix_len <= CHILD_PREFIX_MAX ? "true" : "false",
           token_len <= CHILD_CYCLE_TOKEN_MAX ? "true" : "false");
    CHECK(prefix_len <= CHILD_PREFIX_MAX, "prefix margin wrong");
    CHECK(token_len <= CHILD_CYCLE_TOKEN_MAX, "token margin wrong");
    CHECK(full_len <= CHILD_LINE_MAX, "report must not overrun");
}

/* parse_child_line expects the EXACT production wire form, including its
 * single terminating newline: the parent feeds the raw pipe bytes.  These
 * cases therefore never strip the newline themselves. */
static int parse_expect_fail(const char *label, char *raw, int seq)
{
    struct child_observation o;
    int rc;
    memset(&o, 0, sizeof(o));
    o.seq = seq;
    rc = parse_child_line(raw, &o);
    printf("{\"case\":\"%s\",\"rc\":%d}\n", label, rc);
    CHECK(rc == -1, label);
    return rc;
}

static void case_protocol_parser(void)
{
    char line[CHILD_LINE_MAX];
    struct child_line_input in;
    struct child_observation o;
    char *cut;
    int rc;

    build_worst_cycles();
    memset(&in, 0, sizeof(in));
    in.seq = 7; in.ordinal = 2; in.elements = 262144; in.cycles = MAX_CYCLES;
    in.pid = 1234; in.ppid = 5678;
    in.allocs = in.frees = MAX_CYCLES * 3u; in.mismatches = 0;
    in.uuid = "GPU-01020304-0506-0708-090a-0b0c0d0e0f10";
    in.cyc = worst_cycles;
    /* Make the round-tripped line semantically valid: zero rcs, monotonic
     * timestamps inside the reported worker interval. */
    for (int i = 0; i < MAX_CYCLES; i++) {
        worst_cycles[i].idx = (uint32_t)i;
        worst_cycles[i].alloc_rc[0] = worst_cycles[i].alloc_rc[1] = 0;
        worst_cycles[i].alloc_rc[2] = 0;
        worst_cycles[i].htod_rc = worst_cycles[i].launch_rc = 0;
        worst_cycles[i].sync_rc = worst_cycles[i].dtoh_rc = 0;
        worst_cycles[i].free_rc[0] = worst_cycles[i].free_rc[1] = 0;
        worst_cycles[i].free_rc[2] = 0;
        worst_cycles[i].mismatches = 0;
        worst_cycles[i].seed = (uint32_t)(1000 + i);
        worst_cycles[i].csum = 7;
        worst_cycles[i].role = ROLE_CHILD;
        worst_cycles[i].proc = 8;          /* seq 7 + 1 */
        worst_cycles[i].start_ns = 1000 + (long long)i * 10;
        worst_cycles[i].end_ns = 1009 + (long long)i * 10;
    }
    in.start_ns = worst_cycles[0].start_ns;
    in.end_ns = worst_cycles[MAX_CYCLES - 1].end_ns;
    rc = format_child_line(line, sizeof(line), &in);
    CHECK(rc == 0, "fixture line build");
    /* Raw newline-terminated line goes straight to the production parser. */
    memset(&o, 0, sizeof(o));
    o.seq = in.seq;
    rc = parse_child_line(line, &o);
    int identity_ok = (rc == 0 && o.cycles == MAX_CYCLES && o.seq == 7
                       && o.ordinal == 2 && o.cycle[3].proc == 8
                       && o.cycle[3].role == ROLE_CHILD
                       && o.cycle[3].start_ns == 1030
                       && o.cycle[3].end_ns == 1039
                       && o.cycle[MAX_CYCLES - 1].idx == MAX_CYCLES - 1);
    printf("{\"case\":\"parse_max_cycles\",\"rc\":%d,\"cycles\":%d,"
           "\"identity_restored\":%s}\n", rc, o.cycles,
           identity_ok ? "true" : "false");
    CHECK(identity_ok, "max-cycle report must parse with identity");

    /* Missing terminating newline: the raw production form requires it. */
    {
        char copy[CHILD_LINE_MAX];
        snprintf(copy, sizeof(copy), "%s", line);
        copy[strlen(copy) - 1] = '\0';
        parse_expect_fail("parse_missing_newline", copy, 7);
    }
    /* Interior newline / trailing junk after the record. */
    {
        char copy[CHILD_LINE_MAX];
        snprintf(copy, sizeof(copy), "%s", line);
        memcpy(copy + strlen(copy) - 1, "x\nx\n", 4);
        parse_expect_fail("parse_trailing_junk", copy, 7);
    }
    /* Truncated before any cycle tokens: claims MAX_CYCLES, gives zero. */
    {
        char copy[CHILD_LINE_MAX];
        cut = strstr(line, " c=");
        CHECK(cut != NULL, "fixture contains cycles");
        snprintf(copy, sizeof(copy), "%.*s\n", (int)(cut - line), line);
        parse_expect_fail("parse_truncated", copy, 7);
    }
    /* Cycle count mismatch: advertise one more cycle than present. */
    {
        char copy[CHILD_LINE_MAX];
        snprintf(copy, sizeof(copy), "%s", line);
        char *cy = strstr(copy, "cy=32 ");
        CHECK(cy != NULL, "fixture advertises cy=32");
        memcpy(cy, "cy=33 ", 6);
        parse_expect_fail("parse_count_mismatch", copy, 7);
    }
    /* Report routing: seq in the record must match the pipe owner. */
    {
        char copy[CHILD_LINE_MAX];
        char *sq = strstr(line, "seq=7 ");
        CHECK(sq != NULL, "fixture contains seq");
        snprintf(copy, sizeof(copy), "%s", line);
        sq = strstr(copy, "seq=7 ");
        memcpy(sq, "seq=6 ", 6);
        parse_expect_fail("parse_wrong_seq", copy, 7);
    }
    /* Unknown and duplicated header keys. */
    {
        char copy[CHILD_LINE_MAX];
        snprintf(copy, sizeof(copy), "%s", line);
        char *rcp = strstr(copy, " rc=0 ");
        CHECK(rcp != NULL, "fixture contains rc");
        memcpy(rcp, " qx=0 ", 6);
        parse_expect_fail("parse_unknown_key", copy, 7);
    }
    {
        char copy[CHILD_LINE_MAX];
        char suffix[CHILD_LINE_MAX];
        snprintf(copy, sizeof(copy), "%s", line);
        char *p = strstr(copy, " uuid=");
        size_t at = (size_t)(p - copy);
        /* Separate destination buffers: src and dst of the substitution
         * must not overlap (owner v4 hardening). */
        snprintf(suffix, sizeof(suffix), "%s", p + 1);
        snprintf(copy + at, sizeof(copy) - at, "rc=1 %s", suffix);
        parse_expect_fail("parse_duplicate_key", copy, 7);
    }
    /* Malformed UUID. */
    {
        char copy[CHILD_LINE_MAX];
        snprintf(copy, sizeof(copy), "%s", line);
        char *u = strstr(copy, "uuid=GPU-");
        memcpy(u + 5, "GPU-zzzzzzzz-0000", 16);
        parse_expect_fail("parse_bad_uuid", copy, 7);
    }
    /* Cycle token: unsigned overflow, signed overflow, trailing comma,
     * missing field, non-monotonic timestamps. */
    {
        const char *bad_tokens[] = {
            "4294967296,1,0,0,0,0,0,0,0,0,0,0,0,1,100,101",
            "0,1,2147483648,0,0,0,0,0,0,0,0,0,0,1,100,101",
            "9223372036854775808,1,0,0,0,0,0,0,0,0,0,0,0,1,100,101",
            "0,1,0,0,0,0,0,0,0,0,0,0,0,1,101,100",
            "0,1,0,0,0,0,0,0,0,0,0,0,0,1,-1,100",
            NULL
        };
        for (int t = 0; bad_tokens[t]; t++) {
            char copy[CHILD_LINE_MAX];
            char label[48];
            char *tp;
            snprintf(copy, sizeof(copy), "%s", line);
            tp = strstr(copy, " c=");
            CHECK(tp, "fixture cycle anchor");
            snprintf(tp, sizeof(copy) - (size_t)(tp - copy),
                     " c=%s\n", bad_tokens[t]);
            snprintf(label, sizeof(label), "parse_bad_cycle_%d", t);
            parse_expect_fail(label, copy, 7);
        }
    }
    /* Token-level strictness is also covered by the dedicated
     * owner_protocol_regression binary (field count / comma shape). */

    /* Garbage / wrong prefix. */
    parse_expect_fail("parse_garbage",
                      "LCYC1 garbage-without-keys\n", 7);
}

/* SIGPIPE/EPIPE: the probe ignores SIGPIPE (same disposition main()
 * installs) and write_pipe_byte() must return -1 with errno=EPIPE on a
 * pipe whose reader has closed, instead of terminating the process. */
static void case_closed_pipe_write(void)
{
    int p[2];
    int rc, saved;
    void (*old)(int) = signal(SIGPIPE, SIG_IGN);

    CHECK(pipe(p) == 0, "pipe");
    close(p[0]);
    errno = 0;
    rc = write_pipe_byte(p[1], 'g');
    saved = errno;
    int ok = (rc == -1 && saved == EPIPE);
    printf("{\"case\":\"closed_pipe_write\",\"rc\":%d,\"errno\":%d,"
           "\"epipe\":%s}\n", rc, saved, ok ? "true" : "false");
    CHECK(ok, "closed pipe write must surface EPIPE, never SIGPIPE");
    close(p[1]);
    signal(SIGPIPE, old);

    /* A live pipe still works. */
    {
        int q[2];
        unsigned char byte = 0;
        ssize_t got;
        CHECK(pipe(q) == 0, "pipe2");
        signal(SIGPIPE, SIG_IGN);
        CHECK(write_pipe_byte(q[1], 'R') == 0, "live pipe write");
        do { got = read(q[0], &byte, 1); } while (got < 0 && errno == EINTR);
        CHECK(got == 1 && byte == 'R', "live pipe byte round trip");
        close(q[0]); close(q[1]);
    }
}

int main(void)
{
    unsigned n = 4096;
    uint32_t seed = cycle_seed(ROLE_SOLO, 0, 9, 0, 0);

    case_validate("nan", n, seed, 3, NAN);
    case_validate("plus_inf", n, seed, 4, INFINITY);
    case_validate("minus_inf", n, seed, 5, -INFINITY);
    case_validate("negative", n, seed, 6, -1.0f);
    case_validate("oob_large", n, seed, 7, 3.4e38f);
    case_validate("oob_above_uint32", n, seed, 8, 1e30f);
    case_validate_all_bad(256, seed ^ 0xabcdu);
    case_seed_table();
    case_report_bounds();
    case_protocol_parser();
    case_closed_pipe_write();
    printf("{\"case\":\"fixture_summary\",\"failures\":%d}\n", failures);
    return failures ? 1 : 0;
}
