/* Fixed-work counterparts of app.compile, micro.forkexec and micro.pipe. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "core cost line=%d %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
#define VALUES 8192
struct snapshot {
    char names[VALUES][80];
    uint64_t values[VALUES];
    unsigned int count;
    int faults, profile;
    uint64_t khz;
};
static struct snapshot *first, *last;

static uint64_t now(void) {
    struct timespec ts;
    CHECK(!clock_gettime(CLOCK_MONOTONIC, &ts));
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}
static void snapshot_file(struct snapshot *s, const char *file, const char *prefix, int *enabled) {
    FILE *f = fopen(file, "r");
    if (!f) { CHECK(errno == ENOENT); return; }
    char line[256], name[64], extra;
    uint64_t value;
    while (fgets(line, sizeof(line), f)) {
        if (line[0] == '#') continue;
        CHECK(sscanf(line, "%63s %" SCNu64 " %c", name, &value, &extra) == 2);
        if (!strcmp(name, "enabled")) { CHECK(value <= 1); *enabled = (int)value; continue; }
        if (!strcmp(name, "tsc_khz")) { s->khz = value; continue; }
        if (!strcmp(name, "sample_period")) { CHECK(value == 256); continue; }
        CHECK(s->count < VALUES && strlen(name) == strspn(name, "abcdefghijklmnopqrstuvwxyz0123456789_"));
        int n = snprintf(s->names[s->count], sizeof(s->names[0]), "%s%s", prefix, name);
        CHECK(n > 0 && n < (int)sizeof(s->names[0]));
        s->values[s->count++] = value;
    }
    CHECK(!ferror(f) && !fclose(f));
}
static void snapshot(struct snapshot *s) {
    snapshot_file(s, "/proc/uml_faultstat", "fault_", &s->faults);
    snapshot_file(s, "/proc/uml_syscall_profile", "", &s->profile);
}
static void wait_ok(pid_t child) {
    int status;
    pid_t result;
    do { result = waitpid(child, &status, 0); } while (result < 0 && errno == EINTR);
    CHECK(result == child && WIFEXITED(status) && !WEXITSTATUS(status));
}
static void command(char *const args[]) {
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) { execv(args[0], args); perror("execv"); _exit(127); }
    wait_ok(child);
}
static void shell(const char *text) {
    char *args[] = {"/bin/sh", "-ec", (char *)text, NULL};
    command(args);
}
static void transfer(int fd, void *buffer, size_t len, int writing) {
    char *p = buffer;
    while (len) {
        ssize_t n = writing ? write(fd, p, len) : read(fd, p, len);
        if (n < 0 && errno == EINTR) continue;
        CHECK(n > 0); p += n; len -= (size_t)n;
    }
}
static void pipe_work(uint64_t count) {
    int a[2], b[2]; CHECK(!pipe(a) && !pipe(b));
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        CHECK(!close(a[1]) && !close(b[0]));
        for (uint64_t i = 0; i < count; i++) {
            uint64_t word;
            transfer(a[0], &word, sizeof(word), 0); CHECK(word == i);
            transfer(b[1], &word, sizeof(word), 1);
        }
        CHECK(!close(a[0]) && !close(b[1])); _exit(0);
    }
    CHECK(!close(a[0]) && !close(b[1]));
    for (uint64_t i = 0; i < count; i++) {
        uint64_t word = i, reply;
        transfer(a[1], &word, sizeof(word), 1);
        transfer(b[0], &reply, sizeof(reply), 0); CHECK(reply == word);
    }
    CHECK(!close(a[1]) && !close(b[0])); wait_ok(child);
}
static double cpu_seconds(const struct rusage *r) {
    return r->ru_utime.tv_sec + r->ru_stime.tv_sec +
        (r->ru_utime.tv_usec + r->ru_stime.tv_usec) / 1e6;
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "child")) return 0;
    CHECK(argc >= 2 && argc <= 4);
    const char *mode = argv[1];
    CHECK(!strcmp(mode, "compile") || !strcmp(mode, "forkexec") || !strcmp(mode, "pipe"));
    char *end = NULL;
    uint64_t count = argc >= 3 ? strtoull(argv[2], &end, 10) : 1;
    CHECK(count && count <= 10000000 && (argc < 3 || (*argv[2] && !*end)));
    if (!strcmp(mode, "compile")) {
        CHECK(count == 1 && !chdir("/work"));
        shell("rm -rf lua; mkdir lua; tar -xzf /bench/lua.tar.gz -C lua --strip-components=1");
    }
    /* Shared, prefaulted snapshots avoid adding observer COW faults after fork. */
    size_t bytes = 2 * sizeof(*first);
    first = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    CHECK(first != MAP_FAILED); last = first + 1;
#ifdef __linux__
    CHECK(!madvise(first, bytes, MADV_DONTFORK));
#endif
    for (size_t i = 0; i < bytes; i += 4096) ((volatile unsigned char *)first)[i] = 0;
    ((volatile unsigned char *)first)[bytes - 1] = 0;
    struct rusage self_a, self_b, child_a, child_b;
    snapshot(first);
    CHECK(!getrusage(RUSAGE_SELF, &self_a) && !getrusage(RUSAGE_CHILDREN, &child_a));
    uint64_t started = now();
    if (!strcmp(mode, "compile")) {
        shell("cd lua; make -j4 generic > /work/compile.log 2>&1 || { cat /work/compile.log >&2; exit 1; }");
    } else if (!strcmp(mode, "forkexec")) {
        char *args[] = {argc == 4 ? argv[3] : argv[0], "child", NULL};
        for (uint64_t i = 0; i < count; i++) command(args);
    } else pipe_work(count);
    uint64_t elapsed = now() - started;
    CHECK(!getrusage(RUSAGE_SELF, &self_b) && !getrusage(RUSAGE_CHILDREN, &child_b));
    snapshot(last);
    if (!strcmp(mode, "compile")) {
        shell("lua/src/lua -e 'local n=0; for i=1,10000 do n=n+i end; assert(n==50005000); assert(_VERSION==\"Lua 5.4\")'; lua/src/luac -p /bench/verify.lua");
        shell("rm -rf lua");
    }
    CHECK(elapsed && first->count == last->count && first->faults == last->faults &&
          first->profile == last->profile && first->khz == last->khz);
    printf("{\"schema\":\"hd.core-cost/1\",\"mode\":\"%s\",\"verified\":true,\"operations\":%" PRIu64
           ",\"seconds\":%.9f,\"cpu_seconds\":%.6f,\"minor_faults\":%ld,\"major_faults\":%ld,"
           "\"faults_enabled\":%d,\"profile_enabled\":%d,\"tsc_khz\":%" PRIu64 ",\"counters\":{",
           mode, count, elapsed / 1e9, cpu_seconds(&self_b) + cpu_seconds(&child_b) -
           cpu_seconds(&self_a) - cpu_seconds(&child_a), self_b.ru_minflt + child_b.ru_minflt -
           self_a.ru_minflt - child_a.ru_minflt, self_b.ru_majflt + child_b.ru_majflt -
           self_a.ru_majflt - child_a.ru_majflt, last->faults, last->profile, last->khz);
    int printed = 0;
    for (unsigned int i = 0; i < first->count; i++) {
        CHECK(!strcmp(first->names[i], last->names[i]) && last->values[i] >= first->values[i]);
        uint64_t delta = last->values[i] - first->values[i];
        if (delta) printf("%s\"%s\":%" PRIu64, printed++ ? "," : "", first->names[i], delta);
    }
    puts("}}");
    return 0;
}
