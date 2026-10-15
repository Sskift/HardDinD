// SPDX-License-Identifier: GPL-2.0
/*
 * perfrun - harness plumbing for the owner-run perf supervisor.
 *
 * Minimal bounded wrapper used INSIDE the guest (the stripped Alpine root
 * ships no coreutils `timeout` applet):
 *     perfrun <seconds> <prog> [args...]
 * Runs <prog> in its own process group. On deadline the ENTIRE group
 * (leader and every fork()ed child) is terminated, escalated to SIGKILL,
 * and verified gone via a /proc process-group census before exit. This
 * binary is not measured - the benchmark prints its own clock_gettime
 * delta itself.
 *
 * Exit codes: child exit status; 124 on timeout (full group reaped);
 * 127 on exec failure; 2 on usage/wait error.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Count live (non-zombie) processes other than the leader in pgrp. */
static int group_survivors(pid_t pgid, pid_t leader)
{
	DIR *d = opendir("/proc");
	int n = 0;

	if (!d)
		return -1;
	for (struct dirent *de; (de = readdir(d));) {
		char path[300], buf[512];
		pid_t pid, pgrp;
		char state;
		FILE *f;
		int ppid;

		for (char *q = de->d_name; *q; q++)
			if (*q < '0' || *q > '9')
				goto next;
		snprintf(path, sizeof(path), "/proc/%s/stat", de->d_name);
		f = fopen(path, "r");
		if (!f)
			goto next;
		size_t got = fread(buf, 1, sizeof(buf) - 1, f);
		fclose(f);
		buf[got] = 0;
		char *rp = strrchr(buf, ')');
		if (!rp)
			goto next;
		/* after comm: ) state ppid pgrp ... */
		if (sscanf(rp + 2, "%c %d %d", &state, &ppid, &pgrp) != 3)
			goto next;
		pid = atoi(de->d_name);
		if (pgrp == pgid && pid != leader && state != 'Z')
			n++;
next:
		;
	}
	closedir(d);
	return n;
}

static void sleep_ms(long ms)
{
	struct timespec ts = { ms / 1000, (ms % 1000) * 1000 * 1000 };
	nanosleep(&ts, NULL);
}

/* Escalate until the whole group is gone. Bounded: 2 s TERM, 2 s KILL. */
static void kill_whole_group(pid_t pgid, pid_t leader)
{
	int status, leader_seen = 0;

	kill(-pgid, SIGTERM);
	for (int i = 0; i < 20; i++) {
		pid_t r = waitpid(leader, &status, WNOHANG);
		if (r == leader)
			leader_seen = 1;
		if (leader_seen && group_survivors(pgid, leader) == 0)
			return;
		sleep_ms(100);
	}
	/* Leader may still be alive or children ignored/blocked TERM. */
	kill(-pgid, SIGKILL);
	for (int i = 0; i < 40; i++) {
		pid_t r = waitpid(leader, &status, WNOHANG);
		if (r == leader)
			leader_seen = 1;
		int surv = group_survivors(pgid, leader);
		if (leader_seen && surv == 0)
			return;
		/* keep asserting SIGKILL on stragglers re-spawned meantime */
		if (i % 5 == 4)
			kill(-pgid, SIGKILL);
		sleep_ms(50);
	}
}

int main(int argc, char **argv)
{
	if (argc < 3)
		return 2;
	int deadline_s = atoi(argv[1]);
	if (deadline_s <= 0)
		return 2;

	pid_t pid = fork();
	if (pid < 0)
		return 127;
	if (pid == 0) {
		setpgid(0, 0);
		execv(argv[2], &argv[2]);
		_exit(127);
	}
	setpgid(pid, pid); /* races harmlessly with the child's own setpgid */

	struct timespec now, end;
	clock_gettime(CLOCK_MONOTONIC, &end);
	end.tv_sec += deadline_s;

	for (;;) {
		int status;
		pid_t r = waitpid(pid, &status, WNOHANG);
		if (r == pid) {
			if (WIFEXITED(status))
				return WEXITSTATUS(status);
			if (WIFSIGNALED(status))
				return 128 + WTERMSIG(status);
			return 2;
		}
		if (r < 0 && errno != EINTR)
			return 2;
		clock_gettime(CLOCK_MONOTONIC, &now);
		if (now.tv_sec > end.tv_sec ||
		    (now.tv_sec == end.tv_sec && now.tv_nsec >= end.tv_nsec))
			break;
		sleep_ms(100);
	}

	kill_whole_group(pid, pid);
	return 124;
}
