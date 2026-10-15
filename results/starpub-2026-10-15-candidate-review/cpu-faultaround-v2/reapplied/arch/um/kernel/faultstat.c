// SPDX-License-Identifier: GPL-2.0
/*
 * Optional per-fault rendezvous counters for the UML seccomp thread
 * machinery.  Enabled with uml_faultstat=1; read /proc/uml_faultstat.
 * The instrumentation is bounded and off by default.
 */

#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/kstrtox.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <init.h>
#include <faultstat.h>

volatile int uml_faultstat_enabled;

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
volatile unsigned long long uml_fs_resume;
volatile unsigned long long uml_fs_resume_ns;

static int faultstat_show(struct seq_file *m, void *v)
{
	seq_puts(m,
"# UML anonymous fault rendezvous counters (machine-wide, approximate)\n"
"# segv          userspace SIGSEGV reports handled\n"
"# write_fault   successful user write faults\n"
"# window        faultaround windows entered\n"
"# window_page   neighbour faults issued inside windows\n"
"# window_skip   faultaround attempts gated off\n"
"# sync          TLB range syncs with pending work\n"
"# map           host mmap records queued\n"
"# unmap         host munmap records queued\n"
"# flush         broker stub batches completed\n"
"# flush_ns      ns spent in broker stub flushes\n"
"# resume        runner resume/trap round trips\n"
"# resume_ns     ns spent in runner resume/trap waits\n");
	seq_printf(m, "enabled %u\n", uml_faultstat_enabled);
	seq_printf(m, "segv %llu\n", uml_fs_segv);
	seq_printf(m, "write_fault %llu\n", uml_fs_write_fault);
	seq_printf(m, "window %llu\n", uml_fs_window);
	seq_printf(m, "window_page %llu\n", uml_fs_window_page);
	seq_printf(m, "window_skip %llu\n", uml_fs_window_skip);
	seq_printf(m, "sync %llu\n", uml_fs_sync);
	seq_printf(m, "map %llu\n", uml_fs_map);
	seq_printf(m, "unmap %llu\n", uml_fs_unmap);
	seq_printf(m, "flush %llu\n", uml_fs_flush);
	seq_printf(m, "flush_ns %llu\n", uml_fs_flush_ns);
	seq_printf(m, "resume %llu\n", uml_fs_resume);
	seq_printf(m, "resume_ns %llu\n", uml_fs_resume_ns);
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
"    stub flushes and runner resume round trips; read them from\n"
"    /proc/uml_faultstat.  Bounded machine-wide counters, intended for\n"
"    source-level performance attribution.\n\n");
