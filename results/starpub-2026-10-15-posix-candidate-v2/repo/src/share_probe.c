#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
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
    if (argc != 2 && argc != 3) return 2;
    if (argc == 3) {
        unsigned uid = (unsigned)strtoul(argv[2], NULL, 10);
        if (setgroups(0, NULL) || setgid(uid) || setuid(uid)) return 3;
    }
    setbuf(stdout, NULL);
    const char *path = argv[1];
    int fd = -1, second = -1, notify = -1, child_pipe = -1;
    pid_t child = -1;
    unsigned char *map = MAP_FAILED;
    char line[256], op[32];
    printf("{\"ready\":true,\"uid\":%u,\"gid\":%u,\"pid\":%d}\n", getuid(), getgid(), getpid());
    while (fgets(line, sizeof(line), stdin)) {
        unsigned value = 0;
        if (sscanf(line, "%31s %u", op, &value) < 1) continue;
        errno = 0;
        int rc = 0;
        if (!strcmp(op, "open") || !strcmp(op, "open-read") || !strcmp(op, "create")) {
            if (fd >= 0) close(fd);
            int flags = !strcmp(op, "open-read") ? O_RDONLY : O_RDWR;
            if (!strcmp(op, "create")) flags |= O_CREAT | O_EXCL;
            fd = open(path, flags | O_CLOEXEC, 0600);
            rc = fd < 0 ? -1 : 0;
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
            if (nlen && name[nlen - 1] == '2') {
                name[nlen - 1] = '\0';
                target_fd = second;
            }
            long long a1 = 0, a2 = 0, a3 = 0;
            int nargs = sscanf(line, "%*s %lld %lld %lld", &a1, &a2, &a3);
            struct flock range;
            memset(&range, 0, sizeof(range));
            if (!strcmp(name, "rlock") || !strcmp(name, "wlock") ||
                !strcmp(name, "rlock-w") || !strcmp(name, "wlock-w") ||
                !strcmp(name, "unlock") ||
                !strcmp(name, "wlock-cur") || !strcmp(name, "wlock-end")) {
                if (nargs < 2 || target_fd < 0) { rc = -1; errno = nargs < 2 ? EINVAL : EBADF; }
                else {
                    int blocking = 0;
                    if (!strcmp(name, "rlock")) range.l_type = F_RDLCK;
                    else if (!strcmp(name, "wlock")) range.l_type = F_WRLCK;
                    else if (!strcmp(name, "rlock-w")) { range.l_type = F_RDLCK; blocking = 1; }
                    else if (!strcmp(name, "wlock-w")) { range.l_type = F_WRLCK; blocking = 1; }
                    else if (!strcmp(name, "unlock")) range.l_type = F_UNLCK;
                    if (!strcmp(name, "wlock-cur") || !strcmp(name, "wlock-end")) {
                        range.l_type = F_WRLCK;
                        lseek(target_fd, 0, SEEK_END);
                        lseek(target_fd, a1, !strcmp(name, "wlock-cur") ? SEEK_SET : SEEK_END);
                        range.l_whence = SEEK_CUR;
                        range.l_start = 0;
                        range.l_len = (off_t)a2;
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
    if (second >= 0) close(second);
    if (fd >= 0) close(fd);
    if (notify >= 0) close(notify);
    return 0;
}
