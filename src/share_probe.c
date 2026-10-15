#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/inotify.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>

static double seconds(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void result(const char *op, int rc, int error) {
    printf("{\"op\":\"%s\",\"rc\":%d,\"errno\":%d}\n", op, rc, error);
}

static void interrupted(int signo) { (void)signo; }

/* POSIX record-lock analogue of timed_wait(): blocking F_SETLKW over a range. */
static void posix_timed_wait(int fd, unsigned delay_us, off_t start, off_t len) {
    const char *op = "posix-timed-wait";
    sigset_t alarm_set, old_mask, pending;
    struct sigaction action = { .sa_handler = interrupted }, old_action;
    struct itimerval timer = {0}, stopped = {0};
    if (!delay_us) { result(op, -1, EINVAL); return; }
    sigemptyset(&alarm_set); sigaddset(&alarm_set, SIGALRM);
    // CRITICAL: SIGALRM must be UNBLOCKED during F_SETLKW or the kernel keeps
    // the signal pending and never interrupts the syscall (a blocked signal
    // defers delivery).  Install the handler, ensure it can arrive, then arm.
    sigprocmask(SIG_UNBLOCK, &alarm_set, &old_mask);
    if (sigaction(SIGALRM, &action, &old_action)) { result(op, -1, errno); return; }
    printf("{\"op\":\"timed-wait\",\"armed\":true}\n"); fflush(stdout);
    timer.it_value.tv_sec = delay_us / 1000000;
    timer.it_value.tv_usec = delay_us % 1000000;
    setitimer(ITIMER_REAL, &timer, NULL);
    struct flock l = { .l_type = F_WRLCK, .l_whence = SEEK_SET,
                      .l_start = start, .l_len = len };
    int rc = fcntl(fd, F_SETLKW, &l);
    int error = rc < 0 ? errno : 0;
    setitimer(ITIMER_REAL, &stopped, NULL);
    sigaction(SIGALRM, &old_action, NULL);
    // Consume any still-pending SIGALRM so a late timer cannot reach the
    // command loop; restore the previous mask afterwards.
    sigemptyset(&pending);
    if (sigpending(&pending) == 0 && sigismember(&pending, SIGALRM)) {
        struct timespec ts = { .tv_sec = 0, .tv_nsec = 20 * 1000 * 1000 };
        sigprocmask(SIG_BLOCK, &alarm_set, NULL);
        sigtimedwait(&alarm_set, NULL, &ts);
    }
    sigprocmask(SIG_SETMASK, &old_mask, NULL);
    if (rc < 0) result(op, -1, error);
    else printf("{\"op\":\"posix-timed-wait\",\"rc\":0}\n");
}

/* A guest timer races a real blocking flock with the outer unlock.
 * Block and drain SIGALRM before returning to the command loop so a late timer
 * cannot terminate the probe or interrupt its next stdin read. Repetition also
 * handles a signal delivered before flock is entered on a loaded machine. */
static void timed_wait(int fd, unsigned delay_us) {
    const char *op = "timed-wait";
    sigset_t alarm_set, old_mask;
    struct sigaction action = { .sa_handler = interrupted }, old_action;
    struct itimerval timer = {0}, stopped = {0};
    if (!delay_us || delay_us > 1000000) { result(op, -1, EINVAL); return; }
    sigemptyset(&alarm_set); sigaddset(&alarm_set, SIGALRM);
    sigemptyset(&action.sa_mask);
    if (sigprocmask(SIG_BLOCK, &alarm_set, &old_mask)) { result(op, -1, errno); return; }
    if (sigismember(&old_mask, SIGALRM)) {
        sigprocmask(SIG_SETMASK, &old_mask, NULL);
        result(op, -1, EINVAL); return;
    }
    if (sigaction(SIGALRM, &action, &old_action)) {
        int error = errno;
        sigprocmask(SIG_SETMASK, &old_mask, NULL);
        result(op, -1, error); return;
    }
    printf("{\"op\":\"timed-wait\",\"armed\":true}\n");
    timer.it_value.tv_sec = delay_us / 1000000;
    timer.it_value.tv_usec = delay_us % 1000000;
    timer.it_interval = timer.it_value;
    int rc = setitimer(ITIMER_REAL, &timer, NULL), error = errno;
    if (!rc) {
        rc = sigprocmask(SIG_SETMASK, &old_mask, NULL); error = errno;
        if (!rc) { rc = flock(fd, LOCK_EX); error = errno; }
        if (sigprocmask(SIG_BLOCK, &alarm_set, NULL)) { rc = -1; error = errno; }
    }
    if (setitimer(ITIMER_REAL, &stopped, NULL)) { rc = -1; error = errno; }
    struct timespec now = {0};
    while (sigtimedwait(&alarm_set, NULL, &now) >= 0) {}
    if (sigaction(SIGALRM, &old_action, NULL)) { rc = -1; error = errno; }
    if (sigprocmask(SIG_SETMASK, &old_mask, NULL)) { rc = -1; error = errno; }
    result(op, rc, rc < 0 ? error : 0);
}

/* ------------------------------------------------------------------ */
/* Self-exec POSIX-lock helper.                                        */
/*                                                                     */
/* The minimal guest image contains ONLY this static binary (no         */
/* /bin/sleep), so exec-preservation tests re-exec the probe itself.   */
/* Invocation:                                                         */
/*   <probe> --posix-exec-helper <ready_fd> <release_fd> <lock_fd>     */
/*                      <timeout_ms>                                   */
/* The child takes the POSIX write lock on lock_fd BEFORE exec; the    */
/* three listed descriptors are kept non-CLOEXEC across execve.  After  */
/* a successful exec the new program image emits an explicit READY      */
/* marker over ready_fd (proving post-exec state in the SAME pid with   */
/* the SAME lock fd still open), then waits on release_fd or the hard   */
/* timeout, and exits 0.                                               */
/* ------------------------------------------------------------------ */
#define EXEC_HELPER_MARK "READY\n"
#define EXEC_HELPER_FAIL "ERR %d\n"
#define EXEC_HELPER_TIMEOUT_MS 15000
#define EXEC_READY_TIMEOUT_MS 5000

/* True and fill `out` when argv[0] is an absolute executable path. */
static int argv0_absolute(char **argv, char *out, size_t outsz) {
    const char *a0 = argv[0];
    if (!a0 || a0[0] != '/') return 0;
    size_t n = strlen(a0);
    if (n >= outsz) return 0;
    memcpy(out, a0, n + 1);
    return 1;
}

static int run_exec_helper(int argc, char **argv) {
    if (argc != 6) return 2;
    int ready_fd = atoi(argv[2]);
    int release_fd = atoi(argv[3]);
    int lock_fd = atoi(argv[4]);
    int timeout_ms = atoi(argv[5]);
    if (ready_fd < 0 || release_fd < 0 || lock_fd < 0 || timeout_ms <= 0) return 2;
    /* The lock descriptor inherited across exec must still be open: that
     * descriptor (and this process's POSIX locks on it) survived execve. */
    if (fcntl(lock_fd, F_GETFD) < 0) {
        dprintf(ready_fd, EXEC_HELPER_FAIL, EBADF);
        return 4;
    }
    if (dprintf(ready_fd, "{\"exec_helper\":1,\"pid\":%d,\"lock_fd\":%d}\n",
                getpid(), lock_fd) < 0) {
        return 5;
    }
    struct pollfd pfd = { .fd = release_fd, .events = POLLIN };
    /* Bounded wait: release byte/EOF or hard timeout, never an unbounded sleep. */
    poll(&pfd, 1, timeout_ms);
    return 0;
}

static int parallel_create(const char *path) {
    pid_t children[8];
    unsigned count = 0;
    int failed = 0;
    for (unsigned i = 0; i < 8; i++) {
        pid_t pid = fork();
        if (pid < 0) { failed = 1; break; }
        if (!pid) {
            const mode_t masks[] = {0000, 0007, 0027, 0077};
            char name[4096];
            for (unsigned j = 0; j < 64; j++) {
                mode_t mask = masks[(i + j) % 4];
                umask(mask);
                int n = snprintf(name, sizeof(name), "%s-%u-%u", path, i, j);
                if (n < 0 || (size_t)n >= sizeof(name)) _exit(4);
                int fd = open(name, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0666);
                if (fd < 0) _exit(5);
                struct stat st;
                int bad = fstat(fd, &st) || (st.st_mode & 0777) != (0666 & ~mask);
                close(fd);
                if (unlink(name) || bad) _exit(6);
            }
            _exit(0);
        }
        children[count++] = pid;
    }
    for (unsigned i = 0; i < count; i++) {
        int status;
        pid_t n;
        do { n = waitpid(children[i], &status, 0); } while (n < 0 && errno == EINTR);
        if (n < 0 || !WIFEXITED(status) || WEXITSTATUS(status)) failed = 1;
    }
    if (failed) errno = EIO;
    return failed ? -1 : 0;
}

int main(int argc, char **argv) {
    /* Self-exec helper mode: no path argument, no uid switch, no command loop. */
    if (argc >= 2 && !strcmp(argv[1], "--posix-exec-helper")) {
        return run_exec_helper(argc, argv);
    }
    if (argc != 2 && argc != 3) return 2;
    if (argc == 3) {
        unsigned uid = (unsigned)strtoul(argv[2], NULL, 10);
        if (setgroups(0, NULL) || setgid(uid) || setuid(uid)) return 3;
    }
    setbuf(stdout, NULL);
    const char *path = argv[1];
    int fd = -1, second = -1, other_fd = -1, notify = -1, child_pipe = -1;
    pid_t child = -1, exec_child = -1;
    int exec_release_fd = -1;   /* parent end: byte/EOF releases the helper */
    unsigned char *map = MAP_FAILED;
    char line[256], op[32];
    printf("{\"ready\":true,\"uid\":%u,\"gid\":%u,\"pid\":%d}\n", getuid(), getgid(), getpid());
    while (fgets(line, sizeof(line), stdin)) {
        unsigned value = 0;
        if (sscanf(line, "%31s %u", op, &value) < 1) continue;
        errno = 0;
        int rc = 0;
        if (!strcmp(op, "open") || !strcmp(op, "open-read") || !strcmp(op, "open-wronly")
            || !strcmp(op, "create")) {
            if (fd >= 0) close(fd);
            int flags = !strcmp(op, "open-read") ? O_RDONLY
                      : !strcmp(op, "open-wronly") ? O_WRONLY : O_RDWR;
            if (!strcmp(op, "create")) flags |= O_CREAT | O_EXCL;
            fd = open(path, flags | O_CLOEXEC, 0600);
            rc = fd < 0 ? -1 : 0;
        } else if (!strcmp(op, "other-open")) {
            /* Open a SECOND file in the same directory as argv[1] (works for
             * both native cwd and the container /share working dir). */
            if (other_fd >= 0) close(other_fd);
            char name2[256];
            if (sscanf(line, "%*s %255s", name2) != 1) { rc = -1; errno = EINVAL; }
            else {
                char full[512];
                char dir[300] = ".";
                char *slash = strrchr(path, '/');
                if (slash && slash != path) {
                    size_t dl = (size_t)(slash - path);
                    if (dl < sizeof(dir)) { memcpy(dir, path, dl); dir[dl] = 0; }
                }
                snprintf(full, sizeof(full), "%s/%s", dir, name2);
                other_fd = open(full, O_RDWR | O_CLOEXEC);
                rc = other_fd < 0 ? -1 : 0;
            }
        } else if (!strcmp(op, "other-close")) {
            rc = other_fd < 0 ? -1 : close(other_fd); other_fd = -1;
        } else if (!strcmp(op, "close")) {
            rc = close(fd); fd = -1;
        } else if (!strcmp(op, "stat")) {
            struct stat st;
            if (fstat(fd, &st)) result(op, -1, errno);
            else printf("{\"op\":\"stat\",\"rc\":0,\"size\":%lld,\"mode\":%u,\"uid\":%u,\"gid\":%u,\"nlink\":%lu}\n",
                        (long long)st.st_size, st.st_mode & 07777, st.st_uid, st.st_gid, (unsigned long)st.st_nlink);
            continue;
        } else if (!strcmp(op, "read")) {
            unsigned char byte;
            ssize_t n = pread(fd, &byte, 1, 0);
            if (n != 1) result(op, -1, errno);
            else printf("{\"op\":\"read\",\"rc\":0,\"byte\":%u}\n", byte);
            continue;
        } else if (!strcmp(op, "write")) {
            unsigned char byte = value;
            rc = pwrite(fd, &byte, 1, 0) == 1 ? fsync(fd) : -1;
        } else if (!strcmp(op, "map")) {
            if (map != MAP_FAILED) munmap(map, 4096);
            map = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
            rc = map == MAP_FAILED ? -1 : 0;
        } else if (!strcmp(op, "mapped")) {
            if (map == MAP_FAILED) result(op, -1, EINVAL);
            else printf("{\"op\":\"mapped\",\"rc\":0,\"byte\":%u}\n", *(volatile unsigned char *)map);
            continue;
        } else if (!strcmp(op, "mwrite")) {
            if (map == MAP_FAILED) { rc = -1; errno = EINVAL; }
            else { map[0] = value; rc = msync(map, 4096, MS_SYNC); }
        } else if (!strcmp(op, "unmap")) {
            rc = map == MAP_FAILED ? 0 : munmap(map, 4096); map = MAP_FAILED;
        } else if (!strcmp(op, "signal-ready")) {
            struct sigaction action = { .sa_handler = interrupted };
            sigemptyset(&action.sa_mask);
            rc = sigaction(SIGUSR1, &action, NULL);
        } else if (!strcmp(op, "dup") || !strcmp(op, "open-second")) {
            if (second >= 0) close(second);
            second = !strcmp(op, "dup") ? dup(fd) : open(path, O_RDWR | O_CLOEXEC);
            rc = second < 0 ? -1 : 0;
        } else if (!strcmp(op, "close-second")) {
            rc = close(second); second = -1;
        } else if (!strcmp(op, "reopen")) {
            /* Attempt a FRESH open of argv[1] with the requested mode without
             * touching the held descriptor.  Used after chmod revocation:
             * root with CAP_DAC_OVERRIDE is expected to succeed even mode000,
             * an unprivileged owner gets EACCES. */
            char mode[16] = "rdwr";
            sscanf(line, "%*s %15s", mode);
            int flags = !strcmp(mode, "rdonly") ? O_RDONLY
                      : !strcmp(mode, "wronly") ? O_WRONLY : O_RDWR;
            int nfd = open(path, flags | O_CLOEXEC);
            if (nfd < 0) {
                printf("{\"op\":\"reopen\",\"rc\":-1,\"errno\":%d}\n", errno);
            } else {
                printf("{\"op\":\"reopen\",\"rc\":0,\"errno\":0}\n");
                close(nfd);
            }
            fflush(stdout);
            continue;
        } else if (!strncmp(op, "ns-nested-lockcheck", 18)) {
            /* Nested PID-namespace GETLK identity, self-contained and bounded.
             * C unshare(NEWUSER|NEWPID) (NEWPID alone if privileged); its
             * children G1 (holder) and G2 (querier) live in the NEW pidns.
             * G2's F_GETLK reports G1's LOCAL tgid; the fork pid g1 is G1's
             * tgid in the ancestor ns.  Exact unshare errno is reported so an
             * environment that forbids pidns creation records not_tested. */
            off_t ns_start = 0, ns_len = 64;
            sscanf(line, "%*s %lld %lld", (long long *)&ns_start, (long long *)&ns_len);
            int rp[2], ready[2], release[2], g2res[2];
            if (pipe2(rp, O_CLOEXEC)) { result(op, -1, errno); continue; }
            pid_t cp = fork();
            if (cp < 0) {
                close(rp[0]); close(rp[1]); rc = -1; result(op, rc, errno);
            } else if (!cp) {
                close(rp[0]);
                int ur = unshare(CLONE_NEWUSER | CLONE_NEWPID);
                int ue = ur < 0 ? errno : 0;
                if (ur < 0 && (ue == EPERM || ue == EINVAL)) {
                    ur = unshare(CLONE_NEWPID);
                    ue = ur < 0 ? errno : 0;
                }
                if (ur < 0) {
                    dprintf(rp[1], "{\"op\":\"ns-nested-lockcheck\",\"rc\":-1,"
                            "\"unshare_errno\":%d}\n", ue);
                    close(rp[1]); _exit(0);
                }
                if (ue == 0) {
                    int mfd = open("/proc/self/setgroups", O_WRONLY);
                    if (mfd >= 0) { (void)!write(mfd, "deny", 4); close(mfd); }
                    mfd = open("/proc/self/uid_map", O_WRONLY);
                    if (mfd >= 0) { dprintf(mfd, "0 %d 1\n", geteuid()); close(mfd); }
                    mfd = open("/proc/self/gid_map", O_WRONLY);
                    if (mfd >= 0) { dprintf(mfd, "0 %d 1\n", getegid()); close(mfd); }
                }
                if (pipe2(ready, O_CLOEXEC) || pipe2(release, O_CLOEXEC)
                    || pipe2(g2res, O_CLOEXEC)) _exit(11);
                pid_t g1 = fork();
                if (g1 == 0) {
                    close(ready[0]); close(release[1]); close(g2res[0]); close(g2res[1]);
                    struct flock ll = { .l_type = F_WRLCK, .l_whence = SEEK_SET,
                                       .l_start = ns_start, .l_len = ns_len };
                    int e = fcntl(fd, F_SETLK, &ll) < 0 ? errno : 0;
                    dprintf(ready[1], "%d %d\n", e, (int)getpid());
                    if (!e) { char x; (void)!read(release[0], &x, 1); }
                    _exit(0);
                }
                close(ready[1]); close(release[0]);
                /* Keep the writer until G2 has inherited it. */
                int lock_err = 0, g1_local = -1;
                FILE *rf = fdopen(ready[0], "r");
                if (fscanf(rf, "%d %d", &lock_err, &g1_local) != 2) lock_err = EIO;
                if (lock_err) {
                    dprintf(rp[1], "{\"op\":\"ns-nested-lockcheck\",\"rc\":-1,\"lock_errno\":%d}\n",
                            lock_err);
                    (void)!write(release[1], "x", 1);
                    waitpid(g1, NULL, 0);
                    close(rp[1]); _exit(0);
                }
                pid_t g2 = fork();
                if (g2 == 0) {
                    close(ready[0]); close(release[1]); close(g2res[0]);
                    struct flock q = { .l_type = F_WRLCK, .l_whence = SEEK_SET,
                                      .l_start = ns_start, .l_len = ns_len };
                    int ge = fcntl(fd, F_GETLK, &q) < 0 ? errno : 0;
                    dprintf(g2res[1], "%d %d %d\n", ge, ge ? 0 : (int)q.l_pid, (int)getpid());
                    _exit(0);
                }
                close(g2res[1]);
                int getlk_errno = -1, getlk_pid = -1, g2_pid = -1;
                FILE *gf = fdopen(g2res[0], "r");
                if (fscanf(gf, "%d %d %d", &getlk_errno, &getlk_pid, &g2_pid) != 3)
                    getlk_errno = EIO;
                fclose(rf); fclose(gf);
                dprintf(rp[1],
                    "{\"op\":\"ns-nested-lockcheck\",\"rc\":0,\"unshare_errno\":0,"
                    "\"holder_ancestor_pid\":%d,\"holder_local_pid\":%d,"
                    "\"getlk_local_pid\":%d,\"getlk_errno\":%d,\"querier_local_pid\":%d}\n",
                    (int)g1, g1_local, getlk_pid, getlk_errno, g2_pid);
                (void)!write(release[1], "x", 1);
                waitpid(g2, NULL, 0);
                waitpid(g1, NULL, 0);
                close(rp[1]);
                _exit(0);
            } else {
                close(rp[1]);
                char buf[512];
                ssize_t n = read(rp[0], buf, sizeof(buf) - 1);
                int status = 0;
                waitpid(cp, &status, 0);
                close(rp[0]);
                if (n > 0) {
                    buf[n] = 0;
                    fputs(buf, stdout);
                    fflush(stdout);
                    continue;
                }
                printf("{\"op\":\"ns-nested-lockcheck\",\"rc\":-1,\"errno\":%d,"
                       "\"child_exited\":%s,\"raw_status\":%d}\n", EIO,
                       WIFEXITED(status) ? "true" : "false", status);
                continue;
            }

        } else if (!strcmp(op, "fork-hold")) {
            int pipes[2];
            if (child > 0) { rc = -1; errno = EBUSY; }
            else if (pipe(pipes)) rc = -1;
            else {
                child = fork();
                if (child < 0) { close(pipes[0]); close(pipes[1]); rc = -1; }
                else if (!child) {
                    close(pipes[1]);
                    char byte;
                    while (read(pipes[0], &byte, 1) < 0 && errno == EINTR) {}
                    _exit(0);
                } else { close(pipes[0]); child_pipe = pipes[1]; }
            }
        } else if (!strncmp(op, "fork-locker", 11)) {
            /* Child in the SAME pid namespace but a new files_struct (fork does
             * not share CLONE_FILES): its POSIX lock gets a distinct FUSE
             * owner.  It locks then blocks on a pipe; the reply carries the
             * child pid for GETLK attribution. */
            long long fl_start = 0, fl_len = 64;
            int fl_type = F_WRLCK;
            /* Optional "r"/"w" mode token may precede start length. */
            char tok[3][16] = {{0}};
            int ntok = sscanf(line, "%*s %15s %15s %15s", tok[0], tok[1], tok[2]);
            int nidx = 0;
            if (ntok > 0 && (!strcmp(tok[0], "r") || !strcmp(tok[0], "w"))) {
                if (!strcmp(tok[0], "r")) fl_type = F_RDLCK;
                nidx = 1;
            }
            if (ntok - nidx >= 2) {
                fl_start = strtoll(tok[nidx], NULL, 10);
                fl_len = strtoll(tok[nidx + 1], NULL, 10);
            }
            int st[2], rel[2];
            if (child > 0) { rc = -1; errno = EBUSY; }
            else if (pipe2(st, O_CLOEXEC) || pipe2(rel, O_CLOEXEC)) { rc = -1; }
            else {
                pid_t f = fork();
                if (f < 0) { rc = -1; }
                else if (!f) {
                    close(st[0]); close(rel[1]);
                    struct flock ll = { .l_type = (short)fl_type, .l_whence = SEEK_SET,
                                       .l_start = (off_t)fl_start, .l_len = (off_t)fl_len };
                    int e = fcntl(fd, F_SETLK, &ll) < 0 ? errno : 0;
                    char bb = (char)e;
                    (void)!write(st[1], &bb, 1);
                    if (!e) { char xx; (void)!read(rel[0], &xx, 1); }
                    _exit(0);
                } else {
                    close(st[1]); close(rel[0]);
                    child_pipe = rel[1];
                    child = f;
                    char bb = 0;
                    if (read(st[0], &bb, 1) != 1) { rc = -1; errno = EIO; }
                    else if (bb) { rc = -1; errno = (unsigned char)bb; }
                    else printf("{\"op\":\"fork-locker\",\"rc\":0,\"child_pid\":%d}\n", f);
                    if (bb) { int status; waitpid(f, &status, 0); child = -1; }
                    if (rc) result(op, -1, errno);
                    continue;
                }
            }
        } else if (!strcmp(op, "exec-hold") || !strcmp(op, "exec-missing")) {
            /* POSIX record locks survive execve(2).  The child takes a write
             * lock on the ACTUAL open fd and re-execs THIS SAME static binary
             * in helper mode (works in the minimal guest image, which contains
             * no /bin/sleep).  The parent asserts conflict only AFTER reading
             * a marker emitted by the new program image post-exec, same pid,
             * same lock fd verified open.  "exec-missing" execs an absent
             * target to record errno/exit status. */
            off_t lock_start = 0, lock_len = 64;
            sscanf(line, "%*s %lld %lld", (long long *)&lock_start, (long long *)&lock_len);
            int missing = !strcmp(op, "exec-missing");
            int ready_pipe[2], release_pipe[2];
            if (fd < 0) { result(op, -1, EBADF); continue; }
            if (pipe2(ready_pipe, O_CLOEXEC) || pipe2(release_pipe, O_CLOEXEC)) {
                result(op, -1, errno); continue;
            }
            pid_t f = fork();
            if (f < 0) {
                close(ready_pipe[0]); close(ready_pipe[1]);
                close(release_pipe[0]); close(release_pipe[1]);
                result(op, -1, errno); continue;
            }
            if (!f) {
                /* Child. Drop parent-only pipe ends. */
                close(ready_pipe[0]);
                close(release_pipe[1]);
                /* CLOEXEC cleared ONLY on the actual lock fd + helper pipes. */
                int keep_fds[3] = { fd, ready_pipe[1], release_pipe[0] };
                for (unsigned i = 0; i < 3; i++) {
                    int fl = fcntl(keep_fds[i], F_GETFD);
                    if (fl >= 0) fcntl(keep_fds[i], F_SETFD, fl & ~FD_CLOEXEC);
                }
                struct flock ll = { .l_type = F_WRLCK, .l_whence = SEEK_SET,
                                   .l_start = lock_start, .l_len = lock_len };
                if (fcntl(fd, F_SETLK, &ll) < 0) {
                    dprintf(ready_pipe[1],
                        "{\"exec_failed\":true,\"stage\":\"lock\",\"errno\":%d}\n", errno);
                    _exit(1);
                }
                char selfpath[4096] = {0};
                char fallback[4096];
                ssize_t rn = readlink("/proc/self/exe", selfpath, sizeof(selfpath) - 1);
                const char *target;
                if (!missing && rn > 0) target = selfpath;
                else if (!missing && argv0_absolute(argv, fallback, sizeof(fallback))) target = fallback;
                else target = "/no-such-posix-exec-helper-zzz";
                char c_ready[16], c_release[16], c_lockfd[16], c_timeout[16];
                snprintf(c_ready, sizeof(c_ready), "%d", ready_pipe[1]);
                snprintf(c_release, sizeof(c_release), "%d", release_pipe[0]);
                snprintf(c_lockfd, sizeof(c_lockfd), "%d", fd);
                snprintf(c_timeout, sizeof(c_timeout), "%d", EXEC_HELPER_TIMEOUT_MS);
                char *helper_argv[] = {
                    (char *)target, (char *)"--posix-exec-helper",
                    c_ready, c_release, c_lockfd, c_timeout, NULL
                };
                execv(target, helper_argv);
                int e = errno;
                dprintf(ready_pipe[1],
                    "{\"exec_failed\":true,\"stage\":\"exec\",\"errno\":%d}\n", e);
                _exit(127);
            }
            /* Parent. */
            close(ready_pipe[1]);
            close(release_pipe[0]);
            exec_release_fd = release_pipe[1];
            char marker[512];
            struct pollfd rpfd = { .fd = ready_pipe[0], .events = POLLIN };
            int pr = poll(&rpfd, 1, EXEC_READY_TIMEOUT_MS);
            ssize_t got = 0;
            size_t total = 0;
            if (pr > 0) {
                while (total + 1 < sizeof(marker)) {
                    got = read(ready_pipe[0], marker + total, 1);
                    if (got <= 0) break;
                    total++;
                    if (marker[total - 1] == '\n') break;
                }
                marker[total] = 0;
            }
            close(ready_pipe[0]);
            if (pr <= 0 || got <= 0) {
                int status = 0;
                kill(f, SIGKILL);
                waitpid(f, &status, 0);
                printf("{\"op\":\"%s\",\"rc\":-1,\"errno\":%d,\"child_pid\":%d,"
                       "\"exited\":%s,\"signaled\":%s,\"exit_code\":%d,\"term_sig\":%d,\"raw_status\":%d}\n",
                       op, pr == 0 ? ETIMEDOUT : EIO, f,
                       WIFEXITED(status) ? "true" : "false",
                       WIFSIGNALED(status) ? "true" : "false",
                       WIFEXITED(status) ? WEXITSTATUS(status) : -1,
                       WIFSIGNALED(status) ? WTERMSIG(status) : -1, status);
                close(exec_release_fd); exec_release_fd = -1;
                fflush(stdout);
                continue;
            }
            int failed = strstr(marker, "\"exec_failed\":true") != NULL;
            int m_errno = 0, m_pid = -1, m_lockfd = -1;
            char *pe;
            if ((pe = strstr(marker, "\"errno\":")) != NULL) m_errno = atoi(pe + 8);
            if ((pe = strstr(marker, "\"pid\":")) != NULL) m_pid = atoi(pe + 6);
            if ((pe = strstr(marker, "\"lock_fd\":")) != NULL) m_lockfd = atoi(pe + 10);
            int status = 0;
            if (failed) {
                int waited = 0;
                for (unsigned i = 0; i < 50; i++) {
                    if (waitpid(f, &status, WNOHANG) == f) { waited = 1; break; }
                    struct timespec ts = { .tv_nsec = 2 * 1000 * 1000 };
                    nanosleep(&ts, NULL);
                }
                if (!waited) { kill(f, SIGKILL); waitpid(f, &status, 0); }
                close(exec_release_fd); exec_release_fd = -1;
                printf("{\"op\":\"%s\",\"rc\":-1,\"errno\":%d,\"child_pid\":%d,"
                       "\"exited\":%s,\"signaled\":%s,\"exit_code\":%d,\"term_sig\":%d,\"raw_status\":%d}\n",
                       op, m_errno ? m_errno : EIO, f,
                       WIFEXITED(status) ? "true" : "false",
                       WIFSIGNALED(status) ? "true" : "false",
                       WIFEXITED(status) ? WEXITSTATUS(status) : -1,
                       WIFSIGNALED(status) ? WTERMSIG(status) : -1, status);
                fflush(stdout);
                continue;
            }
            exec_child = f;
            printf("{\"op\":\"%s\",\"rc\":0,\"errno\":0,\"child_pid\":%d,\"reported_pid\":%d,"
                   "\"lock_fd\":%d,\"post_exec\":true}\n",
                   op, f, m_pid, m_lockfd);
            fflush(stdout);
            continue;
        } else if (!strcmp(op, "exec-reap")) {
            /* Controlled release, then exact-pid bounded reap with status. */
            if (exec_child <= 0) { result(op, -1, EINVAL); continue; }
            if (exec_release_fd >= 0) { (void)!write(exec_release_fd, "x", 1); }
            int status = 0, reaped = 0;
            for (unsigned i = 0; i < 250; i++) {   /* up to 5 s */
                if (waitpid(exec_child, &status, WNOHANG) == exec_child) { reaped = 1; break; }
                struct timespec ts = { .tv_nsec = 20 * 1000 * 1000 };
                nanosleep(&ts, NULL);
            }
            if (!reaped) { kill(exec_child, SIGKILL); waitpid(exec_child, &status, 0); }
            if (exec_release_fd >= 0) { close(exec_release_fd); exec_release_fd = -1; }
            pid_t reaped_pid = exec_child;
            exec_child = -1;
            int good = reaped && WIFEXITED(status) && WEXITSTATUS(status) == 0;
            printf("{\"op\":\"exec-reap\",\"rc\":%d,\"errno\":0,\"child_pid\":%d,"
                   "\"exited\":%s,\"signaled\":%s,\"exit_code\":%d,\"term_sig\":%d,\"raw_status\":%d}\n",
                   good ? 0 : -1, reaped_pid,
                   WIFEXITED(status) ? "true" : "false",
                   WIFSIGNALED(status) ? "true" : "false",
                   WIFEXITED(status) ? WEXITSTATUS(status) : -1,
                   WIFSIGNALED(status) ? WTERMSIG(status) : -1, status);
            fflush(stdout);
            continue;

        } else if (!strcmp(op, "fork-close")) {
            /* POSIX locks are not inherited: the forked child may close its
             * inherited descriptors without releasing the parent's locks. */
            pid_t f = fork();
            if (f < 0) { rc = -1; }
            else if (!f) {
                if (fd >= 0) close(fd);
                if (second >= 0) close(second);
                _exit(0);
            } else {
                int status;
                pid_t done = waitpid(f, &status, 0);
                if (done != f || !WIFEXITED(status) || WEXITSTATUS(status)) { rc = -1; errno = EIO; }
            }
        } else if (!strcmp(op, "release-child")) {
            if (child <= 0) { rc = -1; errno = ECHILD; }
            else {
                close(child_pipe); child_pipe = -1;
                int status;
                rc = waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status) ? 0 : -1;
                child = -1;
            }
        } else if (!strcmp(op, "lock") || !strcmp(op, "lock-shared") || !strcmp(op, "wait") || !strcmp(op, "lock-second")) {
            int operation = !strcmp(op, "lock-shared") ? LOCK_SH : LOCK_EX;
            if (strcmp(op, "wait")) operation |= LOCK_NB;
            rc = flock(!strcmp(op, "lock-second") ? second : fd, operation);
        } else if (!strcmp(op, "timed-wait")) {
            timed_wait(fd, value);
            continue;
        } else if (!strcmp(op, "unlock")) {
            rc = flock(fd, LOCK_UN);
        } else if (!strcmp(op, "posix-lock") || !strcmp(op, "posix-unlock")) {
            struct flock lock = { .l_type = !strcmp(op, "posix-lock") ? F_WRLCK : F_UNLCK,
                                  .l_whence = SEEK_SET, .l_start = 0, .l_len = 64 };
            /* Optional arguments select an arbitrary range; defaults keep
             * compatibility with the original fixed-range command. */
            if (!strcmp(op, "posix-unlock")) {
                long long ustart = 0, ulen = 64;
                if (sscanf(line, "%*s %lld %lld", &ustart, &ulen) == 2) {
                    lock.l_start = (off_t)ustart;
                    lock.l_len = (off_t)ulen;
                }
            }
            rc = fcntl(fd, F_SETLK, &lock);
        } else if (!strncmp(op, "posix-", 6)) {
            /* Rich byte-range interface. A trailing '2' targets the second
             * open/dup fd; -w variants block in F_SETLKW; -cur/-end resolve
             * the range against the file offset/EOF like fcntl whence. */
            char name[32];
            snprintf(name, sizeof(name), "%s", op + 6);
            int target_fd = fd;
            size_t nlen = strlen(name);
            /* Trailing 'y' routes to the other-file descriptor (cross-file
             * ABBA cases); trailing '2' routes to the dup/second descriptor. */
            if (nlen > 1 && name[nlen - 1] == 'y') {
                name[nlen - 1] = '\0';
                target_fd = other_fd;
            } else if (nlen && name[nlen - 1] == '2') {
                name[nlen - 1] = '\0';
                target_fd = second;
            }
            long long a1 = 0, a2 = 0, a3 = 0;
            int nargs = sscanf(line, "%*s %lld %lld %lld", &a1, &a2, &a3);
            struct flock range;
            memset(&range, 0, sizeof(range));
            if (!strcmp(name, "rlock") || !strcmp(name, "wlock") ||
                !strcmp(name, "rlock-w") || !strcmp(name, "wlock-w") ||
                !strcmp(name, "unlock") || !strcmp(name, "timed-wait") ||
                !strcmp(name, "wlock-cur") || !strcmp(name, "wlock-end")) {
                if ((!strcmp(name, "timed-wait") && (nargs < 3 || target_fd < 0))
                    || (nargs < 2 || target_fd < 0)) {
                    rc = -1; errno = (nargs < (strcmp(name,"timed-wait")?2:3)) ? EINVAL : EBADF;
                }
                else if (!strcmp(name, "timed-wait")) {
                    /* args: start length delay_us */
                    posix_timed_wait(target_fd, (unsigned)a3, (off_t)a1, (off_t)a2);
                    continue;
                }
                else {
                    int blocking = 0;
                    if (!strcmp(name, "rlock")) range.l_type = F_RDLCK;
                    else if (!strcmp(name, "wlock")) range.l_type = F_WRLCK;
                    else if (!strcmp(name, "rlock-w")) { range.l_type = F_RDLCK; blocking = 1; }
                    else if (!strcmp(name, "wlock-w")) { range.l_type = F_WRLCK; blocking = 1; }
                    else if (!strcmp(name, "unlock")) range.l_type = F_UNLCK;
                    if (!strcmp(name, "wlock-cur")) {
                        /* Real SEEK_CUR: establish and verify a known file
                         * position, then lock relative to it with an explicit
                         * signed delta (a3, default 0). */
                        off_t pos = lseek(target_fd, (off_t)a1, SEEK_SET);
                        if (pos != (off_t)a1) { rc = -1; errno = EINVAL; }
                        else {
                            range.l_type = F_WRLCK;
                            range.l_whence = SEEK_CUR;
                            range.l_start = nargs >= 3 ? (off_t)a2 : 0;
                            range.l_len = nargs >= 3 ? (off_t)a3 : (off_t)a2;
                            rc = fcntl(target_fd, F_SETLK, &range);
                        }
                    } else if (!strcmp(name, "wlock-end")) {
                        /* Real SEEK_END: signed offset from EOF resolved by the
                         * kernel/FUSE client, no lseek pre-resolution. */
                        range.l_type = F_WRLCK;
                        range.l_whence = SEEK_END;
                        range.l_start = (off_t)a1;
                        range.l_len = (off_t)a2;
                        rc = fcntl(target_fd, F_SETLK, &range);
                    } else {
                        range.l_whence = SEEK_SET;
                        range.l_start = (off_t)a1;
                        range.l_len = (off_t)a2;
                    }
                    rc = fcntl(target_fd, blocking ? F_SETLKW : F_SETLK, &range);
                }
            } else if (!strcmp(name, "getlk")) {
                if (nargs < 3 || target_fd < 0 || (a1 != F_RDLCK && a1 != F_WRLCK)) {
                    result(op, -1, target_fd < 0 ? EBADF : EINVAL);
                    continue;
                }
                range.l_type = (short)a1;
                range.l_whence = SEEK_SET;
                range.l_start = (off_t)a2;
                range.l_len = (off_t)a3;
                rc = fcntl(target_fd, F_GETLK, &range);
                if (!rc)
                    printf("{\"op\":\"posix-getlk\",\"rc\":0,\"type\":%d,\"start\":%lld,\"len\":%lld,\"pid\":%d}\n",
                           range.l_type, (long long)range.l_start, (long long)range.l_len, range.l_pid);
                else result(op, -1, errno);
                continue;
            } else {
                rc = -1; errno = EINVAL;
            }
        } else if (!strcmp(op, "setxattr")) {
            rc = fsetxattr(fd, "user.harddind", "verified", 8, 0);
        } else if (!strcmp(op, "getxattr")) {
            char data[64];
            ssize_t n = fgetxattr(fd, "user.harddind", data, sizeof(data));
            if (n < 0) result(op, -1, errno);
            else printf("{\"op\":\"getxattr\",\"rc\":0,\"expected_value\":%s,\"outer_value\":%s}\n",
                        n == 8 && !memcmp(data, "verified", 8) ? "true" : "false",
                        n == 8 && !memcmp(data, "outer-ok", 8) ? "true" : "false");
            continue;
        } else if (!strcmp(op, "watch")) {
            if (notify >= 0) close(notify);
            notify = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
            rc = notify < 0 ? -1 : inotify_add_watch(notify, path, IN_MODIFY | IN_ATTRIB | IN_MOVE_SELF | IN_DELETE_SELF);
            if (rc >= 0) rc = 0;
        } else if (!strcmp(op, "events")) {
            char data[4096] __attribute__((aligned(8)));
            ssize_t n = read(notify, data, sizeof(data));
            if (n < 0 && errno != EAGAIN) result(op, -1, errno);
            else printf("{\"op\":\"events\",\"rc\":0,\"bytes\":%zd}\n", n < 0 ? 0 : n);
            continue;
        } else if (!strcmp(op, "parallel-create")) {
            rc = parallel_create(path);
        } else if (!strcmp(op, "bench-lock")) {
            double start = seconds(); unsigned done = 0;
            for (; done < value; done++) {
                if (flock(fd, LOCK_EX | LOCK_NB) || flock(fd, LOCK_UN)) { rc = -1; break; }
            }
            double elapsed = seconds() - start;
            printf("{\"op\":\"bench-lock\",\"rc\":%d,\"errno\":%d,\"iterations\":%u,\"seconds\":%.9f}\n",
                   rc, rc < 0 ? errno : 0, done, elapsed);
            continue;
        } else if (!strcmp(op, "bench-read") || !strcmp(op, "bench-write")) {
            const size_t chunk = 65536;
            unsigned char data[65536]; memset(data, 0x5a, sizeof(data));
            size_t total = (size_t)value * 1024 * 1024;
            double start = seconds(); size_t done = 0; int writing = !strcmp(op, "bench-write");
            if (writing && ftruncate(fd, total)) { result(op, -1, errno); continue; }
            for (; done < total; done += chunk) {
                ssize_t n = writing ? pwrite(fd, data, chunk, done) : pread(fd, data, chunk, done);
                if (n != (ssize_t)chunk) { rc = -1; break; }
                if (!writing) for (size_t i = 0; i < chunk; i++) if (data[i] != 0x5a) { rc = -1; errno = EILSEQ; break; }
                if (rc) break;
            }
            if (!rc && writing) rc = fsync(fd);
            int error = errno; double elapsed = seconds() - start;
            printf("{\"op\":\"%s\",\"rc\":%d,\"errno\":%d,\"bytes\":%zu,\"seconds\":%.9f,\"MiB_per_second\":%.3f}\n",
                   op, rc, error, done, elapsed, done / 1048576.0 / elapsed);
            continue;
        } else if (!strcmp(op, "quit")) break;
        else { rc = -1; errno = EINVAL; }
        result(op, rc, rc < 0 ? errno : 0);
    }
    if (map != MAP_FAILED) munmap(map, 4096);
    if (child_pipe >= 0) close(child_pipe);
    if (child > 0) waitpid(child, NULL, 0);
    /* Release and reap a still-live exec helper on quit (hard bounded). */
    if (exec_child > 0) {
        if (exec_release_fd >= 0) { (void)!write(exec_release_fd, "x", 1); }
        int status = 0, reaped = 0;
        for (unsigned i = 0; i < 50; i++) {
            if (waitpid(exec_child, &status, WNOHANG) == exec_child) { reaped = 1; break; }
            struct timespec ts = { .tv_nsec = 100 * 1000 * 1000 };
            nanosleep(&ts, NULL);
        }
        if (!reaped) kill(exec_child, SIGKILL);
    }
    if (exec_release_fd >= 0) close(exec_release_fd);
    if (second >= 0) close(second);
    if (fd >= 0) close(fd);
    if (notify >= 0) close(notify);
    return 0;
}
