#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/inotify.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
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

int main(int argc, char **argv) {
    if (argc != 2 && argc != 3) return 2;
    if (argc == 3) {
        unsigned uid = (unsigned)strtoul(argv[2], NULL, 10);
        if (setgroups(0, NULL) || setgid(uid) || setuid(uid)) return 3;
    }
    setbuf(stdout, NULL);
    const char *path = argv[1];
    int fd = -1, notify = -1;
    unsigned char *map = MAP_FAILED;
    char line[256], op[32];
    printf("{\"ready\":true,\"uid\":%u,\"gid\":%u}\n", getuid(), getgid());
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
        } else if (!strcmp(op, "lock")) {
            rc = flock(fd, LOCK_EX | LOCK_NB);
        } else if (!strcmp(op, "unlock")) {
            rc = flock(fd, LOCK_UN);
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
    if (fd >= 0) close(fd);
    if (notify >= 0) close(notify);
    return 0;
}
