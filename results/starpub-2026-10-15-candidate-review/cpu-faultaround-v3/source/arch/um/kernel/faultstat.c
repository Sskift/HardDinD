// SPDX-License-Identifier: GPL-2.0
/*
 * Optional per-fault rendezvous counters for the UML seccomp thread
 * machinery.  Enabled with uml_faultstat=1; read /proc/uml_faultstat.
 *
 * The instrumentation is bounded, purely observational and off by
 * default: it never returns an error and never alters the result of a
 * mapping/syscall path.  Timestamps use the host CLOCK_MONOTONIC via the
 * os layer (same clock the os-Linux side uses), never the coarse guest
 * jiffies clock.
 */

#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/kstrtox.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <init.h>
#include <os.h>
#include <faultstat.h>

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

unsigned long long uml_fs_clock_ns(void)
{
	/* os_nsecs() is host CLOCK_MONOTONIC; shared by kernel- and
	 * os-Linux-compiled callers so timestamps are comparable. */
	return (unsigned long long)os_nsecs();
}

static int faultstat_show(struct seq_file *m, void *v)
{
	/*
	 * Torn-free point reads of each counter.  The set is not a
	 * multi-counter transaction; machine-wide counters from several
	 * vCPUs can interleave between lines, which the header states.
	 */
	seq_puts(m,
"# UML fault rendezvous counters (machine-wide, approximate; one ns clock:\n"
"# host CLOCK_MONOTONIC via os_nsecs)\n"
"# trap          userspace() rounds (runner resume until next trap)\n"
"# segv          rounds whose report was SIGSEGV\n"
"# write_fault   successful user write faults\n"
"# window        faultaround windows entered\n"
"# window_page   neighbour faults issued inside windows\n"
"# window_skip   faultaround attempts gated off\n"
"# sync          TLB range syncs with pending work\n"
"# map           host mmap records queued\n"
"# unmap         host munmap records queued\n"
"# flush         broker stub batches completed\n"
"# flush_ns      ns spent inside broker stub flushes\n"
"# run           userspace() rounds timed\n"
"# run_ns        ns per round INCLUDING guest user execution until the\n"
"#               next trap; NOT pure fault-handling overhead\n");
	seq_printf(m, "enabled %u\n", __atomic_load_n(&uml_faultstat_enabled,
						      __ATOMIC_ACQUIRE));
	seq_printf(m, "trap %llu\n", uml_fs_read(&uml_fs_trap));
	seq_printf(m, "segv %llu\n", uml_fs_read(&uml_fs_segv));
	seq_printf(m, "write_fault %llu\n", uml_fs_read(&uml_fs_write_fault));
	seq_printf(m, "window %llu\n", uml_fs_read(&uml_fs_window));
	seq_printf(m, "window_page %llu\n", uml_fs_read(&uml_fs_window_page));
	seq_printf(m, "window_skip %llu\n", uml_fs_read(&uml_fs_window_skip));
	seq_printf(m, "sync %llu\n", uml_fs_read(&uml_fs_sync));
	seq_printf(m, "map %llu\n", uml_fs_read(&uml_fs_map));
	seq_printf(m, "unmap %llu\n", uml_fs_read(&uml_fs_unmap));
	seq_printf(m, "flush %llu\n", uml_fs_read(&uml_fs_flush));
	seq_printf(m, "flush_ns %llu\n", uml_fs_read(&uml_fs_flush_ns));
	seq_printf(m, "run %llu\n", uml_fs_read(&uml_fs_run));
	seq_printf(m, "run_ns %llu\n", uml_fs_read(&uml_fs_run_ns));
	return 0;
}

static int faultstat_open(struct inode *inode, struct file *file)
{
	return single_open(file, faultstat_show, NULL);
}

static const struct proc_ops faultstat_proc_ops = {
	.proc_open	= faultstat_open,
	.proc_read	= seq_read,
	.proc_lseek	= seq_lseek,
	.proc_release	= single_release,
};

static int __init faultstat_init(void)
{
	proc_create("uml_faultstat", 0400, NULL, &faultstat_proc_ops);
	return 0;
}
__initcall(faultstat_init);

static int __init uml_faultstat_setup(char *line, int *add)
{
	unsigned int enable;

	*add = 0;
	if (kstrtouint(line, 10, &enable) || enable > 1) {
		pr_warn("uml_faultstat requires 0 or 1\n");
		return 0;
	}
	uml_faultstat_enabled = enable;
	return 0;
}
__uml_setup("uml_faultstat=", uml_faultstat_setup,
"uml_faultstat=<0|1>\n"
"    Default 0.  Count anonymous fault rendezvous, TLB syncs, broker\n"
"    stub flushes and runner rounds; read them from /proc/uml_faultstat.\n"
"    When disabled no clock is read and no atomic is executed; timings\n"
"    use host CLOCK_MONOTONIC ns (os_nsecs), not guest jiffies.\n\n");
