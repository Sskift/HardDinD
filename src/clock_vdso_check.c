// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/sched.h>
#include <pthread.h>
#include <sched.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/auxv.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/timex.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SAMPLES 12000
#define LATENCY_CALLS 50000
#define TOLERANCE_NS 1000
#define NCLOCKS 6
static const clockid_t clocks[NCLOCKS] = {
    CLOCK_MONOTONIC, CLOCK_REALTIME, CLOCK_BOOTTIME, CLOCK_MONOTONIC_RAW,
    CLOCK_MONOTONIC_COARSE, CLOCK_REALTIME_COARSE
};
static const char *names[NCLOCKS] = {
    "monotonic", "realtime", "boottime", "monotonic_raw", "monotonic_coarse", "realtime_coarse"
};
static int (*vclock)(clockid_t, struct timespec *);
static int (*vgtod)(struct timeval *, struct timezone *);
static time_t (*vtime)(time_t *);
static int counts[3];
static cpu_set_t allowed;
static volatile int64_t sink;
static char failed[64][80], missing[64][80];
static double fast_ns[NCLOCKS + 2], slow_ns[NCLOCKS + 2];

static void emit(const char *id, int status, const char *detail)
{
    const char *states[] = {"pass", "fail", "not_tested"};
    if (status && counts[status] < 64)
        snprintf(status == 1 ? failed[counts[status]] : missing[counts[status]], 80, "%s", id);
    counts[status]++;
    printf("{\"check\":\"%s\",\"status\":\"%s\"%s%s}\n", id, states[status],
           detail && *detail ? "," : "", detail ? detail : "");
    fflush(stdout);
}

/* The exported ELF ABI works for static glibc and musl without libdl. */
static void *vdso_symbol(const char *name)
{
    uintptr_t base = getauxval(AT_SYSINFO_EHDR);
    const Elf64_Ehdr *eh = (const void *)base;
    const Elf64_Dyn *dyn = NULL;
    const Elf64_Sym *syms = NULL;
    const char *strs = NULL;
    const uint32_t *hash = NULL;
    if (!base || memcmp(eh->e_ident, ELFMAG, SELFMAG) || eh->e_ident[EI_CLASS] != ELFCLASS64)
        return NULL;
    const Elf64_Phdr *ph = (const void *)(base + eh->e_phoff);
    uintptr_t bias = base;
    for (unsigned i = 0; i < eh->e_phnum; i++)
        if (ph[i].p_type == PT_LOAD && ph[i].p_offset == 0) bias = base - ph[i].p_vaddr;
    for (unsigned i = 0; i < eh->e_phnum; i++)
        if (ph[i].p_type == PT_DYNAMIC)
            dyn = (const void *)(bias + ph[i].p_vaddr);
    if (!dyn)
        return NULL;
    for (; dyn->d_tag != DT_NULL; dyn++) {
        if (dyn->d_tag == DT_STRTAB) strs = (const void *)(bias + dyn->d_un.d_ptr);
        if (dyn->d_tag == DT_SYMTAB) syms = (const void *)(bias + dyn->d_un.d_ptr);
        if (dyn->d_tag == DT_HASH) hash = (const void *)(bias + dyn->d_un.d_ptr);
    }
    if (!syms || !strs || !hash)
        return NULL;
    for (uint32_t i = 0; i < hash[1]; i++)
        if (syms[i].st_shndx != SHN_UNDEF && !strcmp(strs + syms[i].st_name, name))
            return (void *)(bias + syms[i].st_value);
    return NULL;
}

static int64_t ns(struct timespec t) { return (int64_t)t.tv_sec * 1000000000 + t.tv_nsec; }
static int valid(struct timespec t) { return t.tv_nsec >= 0 && t.tv_nsec < 1000000000; }
static int rawclock(clockid_t c, struct timespec *t) { return syscall(SYS_clock_gettime, c, t); }

struct sample_result { int errors, affinity_errors; int64_t outside, span; };

static struct sample_result samples(clockid_t c, int n, int migrate, int thread)
{
    struct sample_result r = {0};
    int64_t prev = INT64_MIN, prev_sys = INT64_MIN;
    int cpus[CPU_SETSIZE], ncpus = 0;
    for (int i = 0; i < CPU_SETSIZE; i++)
        if (CPU_ISSET(i, &allowed)) cpus[ncpus++] = i;
    for (int i = 0; i < n; i++) {
        struct timespec a, v, b;
        if (migrate && i % 128 == 0 && ncpus) {
            cpu_set_t one;
            CPU_ZERO(&one);
            CPU_SET(cpus[(i / 128 + thread) % ncpus], &one);
            if (sched_setaffinity(0, sizeof(one), &one)) r.affinity_errors++;
        }
        if (rawclock(c, &a) || vclock(c, &v) || rawclock(c, &b)) { r.errors++; continue; }
        int64_t x = ns(a), y = ns(v), z = ns(b);
        int64_t outside = x > y ? x - y : y > z ? y - z : 0;
        if (outside > r.outside) r.outside = outside;
        if (z - x > r.span) r.span = z - x;
        if (!valid(a) || !valid(v) || !valid(b) || outside > TOLERANCE_NS ||
            y < prev || x < prev_sys || z < x) r.errors++;
        prev = y;
        prev_sys = z;
    }
    return r;
}

static void report_sample(const char *id, struct sample_result r)
{
    char detail[256];
    snprintf(detail, sizeof(detail), "\"samples\":%d,\"errors\":%d,\"affinity_errors\":%d,"
             "\"max_outside_ns\":%" PRId64 ",\"max_bracket_ns\":%" PRId64,
             SAMPLES, r.errors, r.affinity_errors, r.outside, r.span);
    emit(id, r.errors || r.affinity_errors ? 1 : 0, detail);
}

struct worker { int index; struct sample_result result[NCLOCKS]; };
static void *thread_main(void *arg)
{
    struct worker *w = arg;
    for (int c = 0; c < NCLOCKS; c++) w->result[c] = samples(clocks[c], SAMPLES, 1, w->index);
    return NULL;
}

static int tod_samples(void)
{
    int errors = 0;
    for (int i = 0; i < SAMPLES; i++) {
        struct timeval a, v, b;
        time_t slot, ta = syscall(SYS_time, NULL), t = vtime(&slot), tb = syscall(SYS_time, NULL);
        if (t < ta || t > tb || slot != t) errors++;
        if (syscall(SYS_gettimeofday, &a, NULL) || vgtod(&v, NULL) ||
            syscall(SYS_gettimeofday, &b, NULL)) { errors++; continue; }
        int64_t x = (int64_t)a.tv_sec * 1000000 + a.tv_usec;
        int64_t y = (int64_t)v.tv_sec * 1000000 + v.tv_usec;
        int64_t z = (int64_t)b.tv_sec * 1000000 + b.tv_usec;
        if (v.tv_usec < 0 || v.tv_usec >= 1000000 || y < x - 1 || y > z + 1) errors++;
        /* time() is coarse; allow the previous second at a tick boundary. */
        ta = vtime(NULL);
        if (ta < b.tv_sec - 1 || ta > b.tv_sec + 1) errors++;
    }
    struct timezone a, b;
    if (syscall(SYS_gettimeofday, NULL, &a) || vgtod(NULL, &b) ||
        a.tz_minuteswest != b.tz_minuteswest || a.tz_dsttime != b.tz_dsttime) errors++;
    return errors;
}

static double latency(int clock, int raw, int *calls, double *low, double *high)
{
    double results[3];
    int iterations = LATENCY_CALLS;
    *calls = 0; *low = *high = -1;
    /* Sub-millisecond batches mostly measure scheduling noise on Starpub. */
    for (int rep = -1; rep < 3; rep++) {
        struct timespec start, end, t = {0};
        struct timeval tv = {0};
        int rc = 0;
        if (rawclock(CLOCK_MONOTONIC_RAW, &start)) return -1;
        for (int i = 0; i < iterations; i++) {
            if (clock >= 0) rc |= raw ? rawclock(clock, &t) : vclock(clock, &t);
            else if (clock == -1) rc |= raw ? (int)syscall(SYS_gettimeofday, &tv, NULL) : vgtod(&tv, NULL);
            else { t.tv_sec = raw ? syscall(SYS_time, NULL) : vtime(NULL); if (t.tv_sec < 0) rc = -1; }
        }
        if (rawclock(CLOCK_MONOTONIC_RAW, &end) || rc) return -1;
        sink = t.tv_sec + tv.tv_sec;
        int64_t elapsed = ns(end) - ns(start);
        if (elapsed <= 0) return -1;
        if (rep < 0) {
            if (elapsed < 50000000 && iterations < 20000000) {
                int64_t scaled = (int64_t)iterations * 50000000 / elapsed + iterations / 2;
                iterations = scaled > 20000000 ? 20000000 : (int)scaled;
                rep--;
            }
            continue;
        }
        results[rep] = (double)elapsed / iterations;
    }
    *calls = iterations;
    for (int i = 0; i < 3; i++) for (int j = i + 1; j < 3; j++)
        if (results[i] > results[j]) { double t = results[i]; results[i] = results[j]; results[j] = t; }
    *low = results[0]; *high = results[2];
    return results[1];
}

static void mutations(int enabled)
{
    struct timespec saved, begin, now, restore;
    struct timex old = {0}, tx = {0};
    if (!enabled) {
        emit("clock_settime", 2, "\"reason\":\"requires --mutate-clock in a disposable guest\"");
        emit("adjtimex", 2, "\"reason\":\"requires --mutate-clock in a disposable guest\"");
        return;
    }
    if (rawclock(CLOCK_REALTIME, &saved) || rawclock(CLOCK_MONOTONIC, &begin)) {
        emit("clock_settime", 1, "\"reason\":\"cannot save clocks\"");
    } else {
        now = saved; now.tv_sec += 2;
        if (syscall(SYS_clock_settime, CLOCK_REALTIME, &now)) {
            emit("clock_settime", errno == EPERM || errno == EACCES ? 2 : 1,
                 "\"reason\":\"clock_settime denied\"");
        } else {
            int errors = 0;
            for (int c = 0; c < NCLOCKS; c++) errors += samples(clocks[c], SAMPLES, 0, 0).errors;
            errors += tod_samples();
            int rc = rawclock(CLOCK_MONOTONIC, &now);
            int64_t restored = ns(saved) + ns(now) - ns(begin);
            restore.tv_sec = restored / 1000000000;
            restore.tv_nsec = restored % 1000000000;
            if (rc || syscall(SYS_clock_settime, CLOCK_REALTIME, &restore)) errors++;
            for (int c = 0; c < NCLOCKS; c++) errors += samples(clocks[c], SAMPLES, 0, 0).errors;
            emit("clock_settime", errors ? 1 : 0, "\"restoration_attempted\":true");
        }
    }
    if (syscall(SYS_adjtimex, &old) < 0) { emit("adjtimex", 2, "\"reason\":\"query denied\""); return; }
    tx.modes = ADJ_FREQUENCY;
    tx.freq = old.freq + (old.freq > 0 ? -1 : 1) * 50 * 65536;
    if (syscall(SYS_adjtimex, &tx) < 0) {
        emit("adjtimex", errno == EPERM || errno == EACCES ? 2 : 1, "\"reason\":\"adjustment denied\"");
        return;
    }
    int errors = 0;
    struct timespec delay = {.tv_nsec = 100000000};
    nanosleep(&delay, NULL);
    for (int c = 0; c < NCLOCKS; c++) errors += samples(clocks[c], SAMPLES, 0, 0).errors;
    errors += tod_samples();
    tx.modes = ADJ_FREQUENCY; tx.freq = old.freq;
    if (syscall(SYS_adjtimex, &tx) < 0) errors++;
    for (int c = 0; c < NCLOCKS; c++) errors += samples(clocks[c], SAMPLES, 0, 0).errors;
    emit("adjtimex", errors ? 1 : 0, "\"restoration_attempted\":true");
}

static int deny_clocks(void)
{
    struct sock_filter f[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clock_gettime, 3, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_gettimeofday, 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_time, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)
    };
    struct sock_fprog prog = {.len = sizeof(f) / sizeof(f[0]), .filter = f};
    return prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) || prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog);
}

/* Called only in a disposable child: the filter remains installed. */
static int clock_path(void)
{
    struct timespec ts;
    struct timeval tv;
    int fast = 0, trapped = 0;
    if (deny_clocks()) return 77;
    for (int c = 0; c < NCLOCKS; c++) {
        int rc = vclock(clocks[c], &ts);
        fast += rc == 0; trapped += rc == -EPERM;
    }
    int rc = vgtod(&tv, NULL);
    fast += rc == 0; trapped += rc == -EPERM;
    time_t t = vtime(NULL);
    fast += t > 0; trapped += t == -EPERM;
    return fast == NCLOCKS + 2 ? 0 : trapped == NCLOCKS + 2 ? 2 : 1;
}

static int path_probe(const char *id, int expected)
{
    fflush(stdout);
    pid_t child = fork();
    if (!child) _exit(clock_path());
    int status, mode = 1;
    if (child > 0 && waitpid(child, &status, 0) == child && WIFEXITED(status)) mode = WEXITSTATUS(status);
    int result = mode == 77 ? 2 : mode == 1 ? 1 :
                 expected ? (mode == (expected == 1 ? 0 : 2) ? 0 : 1) : mode == 0 ? 0 : 2;
    char detail[256], line[512];
    unsigned seq = 0, clock_mode = 0;
    if (expected) {
        FILE *maps = fopen("/proc/self/maps", "r");
        while (maps && fgets(line, sizeof(line), maps)) {
            unsigned long start;
            if (strstr(line, "[vvar]") && sscanf(line, "%lx-", &start) == 1) {
                /* Diagnostic for this candidate's pinned vdso_clock ABI. */
                volatile const unsigned *data = (const void *)start;
                seq = data[0]; clock_mode = data[1];
                break;
            }
        }
        if (maps) fclose(maps);
    }
    snprintf(detail, sizeof(detail), "\"path\":\"%s\",\"candidate_seq\":%u,\"candidate_clock_mode\":%u",
             mode == 0 ? "trap_free" : mode == 2 ? "syscall" : "unavailable_or_mixed", seq, clock_mode);
    emit(id, result, detail);
    return mode;
}

static int read_offsets(int64_t *mono, int64_t *boot)
{
    FILE *f = fopen("/proc/self/timens_offsets", "r");
    char name[32]; long long sec, nsec;
    int seen = 0;
    if (!f) return -1;
    while (fscanf(f, "%31s %lld %lld", name, &sec, &nsec) == 3) {
        if (!strcmp(name, "monotonic")) { *mono = sec * 1000000000 + nsec; seen |= 1; }
        if (!strcmp(name, "boottime")) { *boot = sec * 1000000000 + nsec; seen |= 2; }
    }
    fclose(f);
    return seen == 3 ? 0 : -1;
}

static int namespace_switches(int parent, int target, int64_t mono, int64_t boot)
{
    /* Bracket each namespaced read with raw reads in the original namespace.
       Repeated setns also proves that already-faulted vvar pages are replaced. */
    for (int rep = 0; rep < 8; rep++) for (int c = 0; c < NCLOCKS; c++) {
        struct timespec a, v, b;
        int64_t delta = clocks[c] == CLOCK_BOOTTIME ? boot :
            clocks[c] == CLOCK_MONOTONIC || clocks[c] == CLOCK_MONOTONIC_RAW ||
            clocks[c] == CLOCK_MONOTONIC_COARSE ? mono : 0;
        if (setns(parent, CLONE_NEWTIME) || rawclock(clocks[c], &a) ||
            setns(target, CLONE_NEWTIME) || vclock(clocks[c], &v) ||
            setns(parent, CLONE_NEWTIME) || rawclock(clocks[c], &b)) return 1;
        if (!valid(v) || ns(v) < ns(a) + delta - TOLERANCE_NS ||
            ns(v) > ns(b) + delta + TOLERANCE_NS) return 1;
    }
    if (tod_samples()) return 1;
    return setns(target, CLONE_NEWTIME) ? 1 : 0;
}

static int namespace_child(void)
{
    int parent = open("/proc/self/ns/time", O_RDONLY);
    int64_t mono = 0, boot = 0;
    if (parent < 0 || read_offsets(&mono, &boot)) return 77;
    if (unshare(CLONE_NEWTIME)) return errno == EPERM || errno == EINVAL || errno == ENOSYS ? 77 : 1;
    int fd = open("/proc/self/timens_offsets", O_WRONLY);
    const char offsets[] = "monotonic 123 456789000\nboottime 321 123456000\n";
    if (fd < 0) return 1;
    int ok = write(fd, offsets, sizeof(offsets) - 1) == (ssize_t)sizeof(offsets) - 1;
    close(fd);
    if (!ok) return 1;
    pid_t child = fork();
    if (child < 0) return 1;
    if (!child) {
        int target = open("/proc/self/ns/time", O_RDONLY);
        if (target < 0 || namespace_switches(parent, target,
                    123456789000LL - mono, 321123456000LL - boot)) _exit(1);
        for (int c = 0; c < NCLOCKS; c++) if (samples(clocks[c], SAMPLES, 0, 0).errors) _exit(1);
        if (tod_samples()) _exit(1);
        int mode = clock_path();
        _exit(mode == 77 ? 78 : mode);
    }
    int status;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) return 1;
    return WEXITSTATUS(status);
}

/* Probe passes the same arguments to native/A/B. Inspect the independent boot
   configuration, never the vvar value or measured path, to choose expectations. */
static int boot_expectation(int *guest)
{
    FILE *f = fopen("/proc/cmdline", "r");
    char line[16384], *save;
    int enabled = 0;
    *guest = 0;
    if (!f) return -1;
    int ok = fgets(line, sizeof(line), f) != NULL;
    fclose(f);
    if (!ok) return -1;
    for (char *p = strtok_r(line, " \n", &save); p; p = strtok_r(NULL, " \n", &save)) {
        if (!strcmp(p, "init=/sbin/harddind-runtime-init")) *guest = 1;
        const char prefix[] = "harddind_vdso_clock=";
        if (!strncmp(p, prefix, sizeof(prefix) - 1)) enabled = !strcmp(p + sizeof(prefix) - 1, "1");
    }
    return *guest && !enabled ? 2 : 1;
}

int main(int argc, char **argv)
{
    int mutate = 0, expect_fallback = 0, expected = 0, initial_time = 0;
    int expect_boot = 0, mutate_guest = 0, guest = 0, initial_path = 77, ns_rc = 77;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--mutate-clock")) mutate = 1;
        else if (!strcmp(argv[i], "--expect-timens-fallback")) expect_fallback = 1;
        else if (!strcmp(argv[i], "--initial-time")) initial_time = 1;
        else if (!strcmp(argv[i], "--expect-fast")) expected = 1;
        else if (!strcmp(argv[i], "--expect-syscall")) expected = 2;
        else if (!strcmp(argv[i], "--expect-boot")) expect_boot = 1;
        else if (!strcmp(argv[i], "--mutate-guest")) mutate_guest = 1;
        else { fprintf(stderr, "usage: %s [--mutate-clock|--mutate-guest] [--expect-timens-fallback] [--expect-fast|--expect-syscall|--expect-boot] [--initial-time]\n", argv[0]); return 2; }
    }
    if (expect_boot || mutate_guest) {
        int boot = boot_expectation(&guest);
        if (boot < 0) { emit("boot.expectation", 1, NULL); goto summary; }
        if (expect_boot) expected = boot;
        if (mutate_guest && guest) mutate = 1;
        emit("boot.expectation", 0, guest ? "\"environment\":\"guest\"" : "\"environment\":\"native\"");
    }
    /* In a privileged --pid=host guest container, explicitly test PID 1's
       time namespace without changing the container runtime's defaults. */
    if (initial_time) {
        int fd = open("/proc/1/ns/time", O_RDONLY);
        int rc = fd < 0 ? -1 : setns(fd, CLONE_NEWTIME);
        if (fd >= 0) close(fd);
        emit("timens.enter_initial", rc ? 1 : 0, NULL);
        if (rc) goto summary;
    }
    vclock = vdso_symbol("__vdso_clock_gettime");
    vgtod = vdso_symbol("__vdso_gettimeofday");
    vtime = vdso_symbol("__vdso_time");
    if (!vclock || !vgtod || !vtime) { emit("vdso.symbols", 1, "\"reason\":\"missing ELF symbols\""); goto summary; }
    emit("vdso.symbols", 0, NULL);
    initial_path = path_probe("path.initial", expected);
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) { emit("affinity", 1, NULL); goto summary; }
    for (int c = 0; c < NCLOCKS; c++) report_sample(names[c], samples(clocks[c], SAMPLES, 0, 0));
    pthread_t threads[4]; struct worker workers[4] = {0}; int started = 0;
    for (int i = 0; i < 4; i++) {
        workers[i].index = i;
        if (pthread_create(&threads[i], NULL, thread_main, &workers[i])) { emit("threads.create", 1, NULL); break; }
        started++;
    }
    for (int i = 0; i < started; i++) {
        if (pthread_join(threads[i], NULL)) { emit("threads.join", 1, NULL); continue; }
        for (int c = 0; c < NCLOCKS; c++) {
            char id[80]; snprintf(id, sizeof(id), "thread%d.%s", i, names[c]);
            report_sample(id, workers[i].result[c]);
        }
    }
    emit("migration.coverage", started != 4 ? 1 : CPU_COUNT(&allowed) > 1 ? 0 : 2,
         CPU_COUNT(&allowed) > 1 ? "\"threads\":4" : "\"reason\":\"only one allowed CPU\"");
    emit("gettimeofday_time", tod_samples() ? 1 : 0, NULL);
    struct timespec ts;
    errno = 0;
    int rc = rawclock(-12345, &ts), err = errno;
    emit("invalid_clock", rc == -1 && vclock(-12345, &ts) == -err ? 0 : 1, NULL);
    mutations(mutate);
    path_probe("path.after_mutations", expected);
    fflush(stdout);
    pid_t child = fork(); int status = 0;
    ns_rc = 1;
    if (!child) _exit(namespace_child());
    if (child > 0 && waitpid(child, &status, 0) == child && WIFEXITED(status)) ns_rc = WEXITSTATUS(status);
    emit("timens.semantics", ns_rc == 77 ? 2 : ns_rc == 0 || ns_rc == 2 || ns_rc == 78 ? 0 : 1,
         ns_rc == 77 ? "\"reason\":\"unshare CLONE_NEWTIME unavailable\"" :
         "\"setns_switches\":144,\"monotonic_offset_ns\":123456789000,\"boottime_offset_ns\":321123456000");
    int ns_expected = expect_fallback ? 2 : expected == 2 ? 2 : 0;
    emit("timens.path", ns_rc == 77 || ns_rc == 78 ? 2 : ns_rc == ns_expected ? 0 : 1,
         ns_rc == 0 ? "\"path\":\"trap_free\"" : ns_rc == 2 ? "\"path\":\"syscall\"" : NULL);
    path_probe("path.after_timens", expected);
    for (int c = 0; c < NCLOCKS + 2; c++) {
        char id[80], detail[512];
        int clock = c < NCLOCKS ? clocks[c] : c == NCLOCKS ? -1 : -2;
        int fast_calls, slow_calls;
        double fast_low, fast_high, slow_low, slow_high;
        double fast = latency(clock, 0, &fast_calls, &fast_low, &fast_high);
        double slow = latency(clock, 1, &slow_calls, &slow_low, &slow_high);
        fast_ns[c] = fast; slow_ns[c] = slow;
        snprintf(id, sizeof(id), "latency.%s", c < NCLOCKS ? names[c] : c == NCLOCKS ? "gettimeofday" : "time");
        snprintf(detail, sizeof(detail), "\"vdso_ns\":%.3f,\"syscall_ns\":%.3f,"
                 "\"vdso_calls\":%d,\"syscall_calls\":%d,\"repetitions\":3,"
                 "\"vdso_samples_ns\":[%.3f,%.3f,%.3f],\"syscall_samples_ns\":[%.3f,%.3f,%.3f]",
                 fast, slow, fast_calls, slow_calls, fast_low, fast, fast_high, slow_low, slow, slow_high);
        emit(id, fast > 0 && slow > 0 ? 0 : 1, detail);
    }
summary:
    printf("{\"check\":\"summary\",\"status\":\"%s\",\"pass\":%d,\"fail\":%d,\"not_tested\":%d",
           counts[1] ? "fail" : counts[2] ? "not_tested" : "pass", counts[0], counts[1], counts[2]);
    printf(",\"expected\":%d,\"path\":\"%s\",\"timens_path\":\"%s\",\"latency\":{", expected,
           initial_path == 0 ? "trap_free" : initial_path == 2 ? "syscall" : "unavailable_or_mixed",
           ns_rc == 0 ? "trap_free" : ns_rc == 2 ? "syscall" : "not_tested_or_failed");
    for (int c = 0; c < NCLOCKS + 2; c++)
        printf("%s\"%s\":{\"vdso_ns\":%.3f,\"syscall_ns\":%.3f}", c ? "," : "",
               c < NCLOCKS ? names[c] : c == NCLOCKS ? "gettimeofday" : "time", fast_ns[c], slow_ns[c]);
    printf("},\"failed_checks\":[");
    for (int i = 0; i < counts[1] && i < 64; i++) printf("%s\"%s\"", i ? "," : "", failed[i]);
    printf("],\"not_tested_checks\":[");
    for (int i = 0; i < counts[2] && i < 64; i++) printf("%s\"%s\"", i ? "," : "", missing[i]);
    printf("]}\n");
    return counts[1] ? 1 : 0;
}
