"""Exercise the actual adaptive C timer with a deterministic clock."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('cc'), 'requires a C compiler')
class TimerTests(unittest.TestCase):
    def test_calibration_is_excluded_and_fast_batches_grow(self):
        include = pathlib.Path(__file__).resolve().parents[1] / 'bench'
        code = r'''
#define clock_gettime fake_clock_gettime
#include "common.h"
static double tick;
int fake_clock_gettime(clockid_t id, struct timespec *ts) {
    (void)id; ts->tv_sec = (time_t)tick; ts->tv_nsec = (long)((tick - ts->tv_sec) * 1e9); return 0;
}
int main(void) {
    struct batch_timer t = batch_timer();
    tick = 0.001; require(batch_next(&t, 0.1) && t.size == 2 && !t.measuring, "double first batch");
    tick = 0.003; require(batch_next(&t, 0.1) && t.size == 4 && !t.measuring, "double second batch");
    tick = 0.023; require(batch_next(&t, 0.1) && t.measuring && t.elapsed == 0 && t.reads == 0, "exclude calibration");
    tick = 0.028; require(batch_next(&t, 0.1) && t.size == 8 && t.reads == 1, "adapt measured batch");
    tick = 0.15; require(!batch_next(&t, 0.1) && t.elapsed > 0.126 && t.elapsed < 0.128, "measured elapsed");
    require(t.calibration_reads == 3 && t.reads == 2, "read counts"); return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / 'timer.c'; source.write_text(code)
            binary = pathlib.Path(tmp) / 'timer'
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(include), str(source), '-o', str(binary)], check=True)
            subprocess.run([binary], check=True)
