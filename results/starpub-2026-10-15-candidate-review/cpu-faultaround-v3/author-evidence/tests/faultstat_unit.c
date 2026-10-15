// SPDX-License-Identifier: GPL-2.0
/*
 * faultstat_unit - host-side function test for the REAL counter header
 * shipped in the kernel patch (arch/um/include/shared/faultstat.h).
 *
 * It verifies the default-off overhead contract at function level:
 *   - disabled: uml_fs_clock_ns() is never called by begin/end and no
 *     counter is modified by inc/add (no write-side atomic on that path);
 *   - enabled:  brackets accumulate real clock deltas, non-monotonic
 *     samples are discarded;
 *   - torn-free snapshot helper works against concurrent writers;
 *   - an observational wrapper mirroring tlb.c fs_map() cannot alter the
 *     wrapped operation's result/errno.
 *
 * The companion static check (run_faultstat_checks.py) proves the same
 * properties at source level for the actual tlb.c/process.c call sites.
 * This file never executes inside the guest kernel; it is a unit test of
 * the shared inline contract.  Exit 0 pass, 1 fail.
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <faultstat.h>

/* Definitions normally supplied by arch/um/kernel/faultstat.c. */
volatile int uml_faultstat_enabled;
volatile unsigned long long uml_fs_trap;
volatile unsigned long long uml_fs_segv;
volatile unsigned long long uml_fs_write_fault;
volatile unsigned long long uml_fs_window;
volatile unsigned long long uml_fs_window_page;
volatile unsigned long long uml_fs_window_skip;
volatile unsigned long long uml_fs_sync;
volatile unsigned long long uml_fs_map;
volatile unsigned long long uml_fs_unmap;
volatile unsigned long long uml_fs_flush;
volatile unsigned long long uml_fs_flush_ns;
volatile unsigned long long uml_fs_run;
volatile unsigned long long uml_fs_run_ns;

static unsigned long long mock_clock_value;
static unsigned long long mock_clock_calls;

/* Stands in for the production os_nsecs()-backed implementation. */
unsigned long long uml_fs_clock_ns(void)
{
	mock_clock_calls++;
	return mock_clock_value;
}

static int failures;

/* Every line this program prints is one complete JSON object. */
static void jput_escaped(const char *str)
{
	for (; *str; str++) {
		unsigned char c = (unsigned char)*str;

		switch (c) {
		case '"':
		case '\\':
			putchar('\\');
			break;
		case '\n':
			fputs("\\n", stdout);
			continue;
		case '\r':
			fputs("\\r", stdout);
			continue;
		case '\t':
			fputs("\\t", stdout);
			continue;
		}
		putchar((int)c);
	}
}

static void jemit(const char *cond, const char *detail)
{
	fputs("{\"check_fail\":\"", stdout);
	jput_escaped(cond);
	fputs("\",\"detail\":\"", stdout);
	jput_escaped(detail);
	fputs("\"}\n", stdout);
	fflush(stdout);
}

#define EXPECT(cond, ...) do {						\
	if (!(cond)) {							\
		char d[160];						\
		snprintf(d, sizeof(d), __VA_ARGS__);			\
		jemit(#cond, d);					\
		failures++;						\
	} } while (0)

static void zero_counters(void)
{
	uml_fs_trap = uml_fs_segv = uml_fs_write_fault = 0;
	uml_fs_window = uml_fs_window_page = uml_fs_window_skip = 0;
	uml_fs_sync = uml_fs_map = uml_fs_unmap = 0;
	uml_fs_flush = uml_fs_flush_ns = uml_fs_run = uml_fs_run_ns = 0;
}

static void test_disabled_no_clock_no_writes(void)
{
	unsigned long long t0;
	int i;

	uml_faultstat_enabled = 0;
	zero_counters();
	mock_clock_value = 1000;
	mock_clock_calls = 0;

	for (i = 0; i < 1000000; i++) {
		uml_fs_inc(&uml_fs_map);
		uml_fs_add(&uml_fs_window_page, 3);
	}
	t0 = uml_fs_time_begin();
	EXPECT(t0 == 0, "disabled begin must return 0, got %llu", t0);
	mock_clock_value = 2000;
	uml_fs_time_end(&uml_fs_flush_ns, t0);

	EXPECT(mock_clock_calls == 0,
	       "disabled path read the clock %llu times", mock_clock_calls);
	EXPECT(uml_fs_map == 0 && uml_fs_window_page == 0 &&
	       uml_fs_flush_ns == 0,
	       "disabled path wrote counters (%llu/%llu/%llu)",
	       uml_fs_map, uml_fs_window_page, uml_fs_flush_ns);
}

static void test_enabled_durations(void)
{
	unsigned long long t0;

	uml_faultstat_enabled = 1;
	zero_counters();
	mock_clock_calls = 0;
	mock_clock_value = 1000;
	t0 = uml_fs_time_begin();
	EXPECT(t0 == 1000, "begin t0=%llu", t0);
	EXPECT(mock_clock_calls == 1, "begin clock calls=%llu",
	       mock_clock_calls);
	mock_clock_value = 2500;
	uml_fs_time_end(&uml_fs_flush_ns, t0);
	EXPECT(uml_fs_flush_ns == 1500, "flush_ns=%llu", uml_fs_flush_ns);

	/* Non-monotonic sample: must be discarded, not wrapped. */
	mock_clock_value = 1000;
	t0 = uml_fs_time_begin();
	mock_clock_value = 999;
	uml_fs_time_end(&uml_fs_flush_ns, t0);
	EXPECT(uml_fs_flush_ns == 1500,
	       "non-monotonic sample leaked, sum=%llu", uml_fs_flush_ns);

	uml_fs_inc(&uml_fs_sync);
	uml_fs_add(&uml_fs_run, 4);
	EXPECT(uml_fs_sync == 1 && uml_fs_run == 4, "plain counters");
}

/* Exact model of the tlb.c fs_map wrapper contract. */
static int model_observed_map(int op_result)
{
	int ret = op_result;

	uml_fs_inc(&uml_fs_map);
	return ret;
}

static void test_errno_propagation(void)
{
	int rc;
	static const int codes[] = { 0, -5, -12, -14, -100 };
	unsigned i;

	uml_faultstat_enabled = 1;
	zero_counters();
	for (i = 0; i < sizeof(codes) / sizeof(codes[0]); i++) {
		rc = model_observed_map(codes[i]);
		EXPECT(rc == codes[i],
		       "wrapper changed result %d -> %d", codes[i], rc);
	}
	EXPECT(uml_fs_map == 5, "map records=%llu", uml_fs_map);
}

static volatile int snapshot_stop;

static void *writer_thread(void *arg)
{
	(void)arg;
	while (!snapshot_stop)
		uml_fs_inc(&uml_fs_segv);
	return NULL;
}

static void test_snapshot(void)
{
	pthread_t th[2];
	unsigned long long prev = 0;
	int i;

	uml_faultstat_enabled = 1;
	uml_fs_segv = 0;
	snapshot_stop = 0;
	for (i = 0; i < 2; i++)
		if (pthread_create(&th[i], NULL, writer_thread, NULL)) {
			EXPECT(0, "pthread_create failed");
			snapshot_stop = 1;
			return;
		}
	for (i = 0; i < 1000000; i++) {
		unsigned long long v = uml_fs_read(&uml_fs_segv);

		EXPECT(v >= prev, "snapshot went backwards %llu < %llu",
		       v, prev);
		prev = v;
	}
	snapshot_stop = 1;
	for (i = 0; i < 2; i++)
		pthread_join(th[i], NULL);
	EXPECT(uml_fs_read(&uml_fs_segv) >= prev, "final snapshot");
}

int main(void)
{
	test_disabled_no_clock_no_writes();
	test_enabled_durations();
	test_errno_propagation();
	test_snapshot();
	if (failures) {
		printf("{\"test\":\"faultstat_unit\",\"result\":\"FAIL\","
		       "\"failures\":%d}\n", failures);
		return 1;
	}
	printf("{\"test\":\"faultstat_unit\",\"result\":\"PASS\"}\n");
	return 0;
}
