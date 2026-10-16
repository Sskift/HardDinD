"""Shared file effects ported from archive/evidence-20261015."""
import errno
import fcntl
import os
import json
import mmap
import math
import time
import struct

from suites.functional.peer import Peer as ProbePeer

# Guest->outer inotify is checked with an outer UID 1001 watcher only: outer root
# shares its 128 inotify instances with every root process on the Starpub host.
NAMES = ['read_initial', 'outer_write_open_fd', 'outer_write_reopen', 'outer_truncate_fstat', 'outer_chmod_fstat', 'guest_write_outer_read', 'mmap_outer_to_guest', 'mmap_guest_to_outer', 'replace_open_inode', 'unlink_open_inode', 'xattr_both_directions', 'outer_lock_blocks_guest', 'guest_lock_blocks_outer', 'outer_inotify_to_guest', 'user_permission_denied', 'user_create_owner', 'read_only_bind', 'outer_inotify_to_guest_uid1001', 'guest_inotify_to_outer_uid1001', 'parallel_creation_masks', 'outer_metadata_churn', 'outer_replace_churn', 'outer_negative_lookup', 'outer_mtime', 'outer_permission_revoked', 'outer_mmap_window', 'outer_attr_window', 'outer_read_window', 'outer_mapped_write', 'notification_mmap_latency', 'outer_directory_inotify', 'outer_readdir_preserved_mtime']


NAMES += ['outer_hardlink_alias', 'outer_rename_unwatched', 'outer_before_first_lookup',
          'outer_watch_exhaustion', 'outer_notify_overflow', 'outer_mapped_remap', 'outer_mapped_scale', 'outer_mapped_read', 'mapped_flock_lifetime']

NAMES += ['read_buffer', 'hostfs_ceiling', 'metadata_batch', 'outer_directory_permission', 'outer_lookup_race', 'exclusive_create']


METADATA_BATCH = r'''
#define _DEFAULT_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) exit(2);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
static void snapshot(const char *phase, double elapsed) {
    glob_t paths = {0};
    printf("phase %s %.9f\n", phase, elapsed);
    usleep(20000); /* Drain asynchronous release/forget, outside the timed phase. */
    if (!glob("/sys/fs/virtiofs/*/mqs/*/profile", 0, NULL, &paths)) {
        for (size_t i = 0; i < paths.gl_pathc; i++) {
            FILE *f = fopen(paths.gl_pathv[i], "r");
            char line[512];
            if (!f) exit(3);
            printf("queue %s\n", paths.gl_pathv[i]);
            while (fgets(line, sizeof(line), f)) fputs(line, stdout);
            if (ferror(f) && errno != EOPNOTSUPP) exit(4);
            fclose(f);
            char latency[512];
            snprintf(latency, sizeof(latency), "%.*s/latency",
                     (int)(strrchr(paths.gl_pathv[i], '/') - paths.gl_pathv[i]), paths.gl_pathv[i]);
            f = fopen(latency, "r");
            if (f) {
                while (fgets(line, sizeof(line), f)) fputs(line, stdout);
                if (ferror(f) && errno != EOPNOTSUPP) exit(4);
                fclose(f);
            }
        }
    }
    globfree(&paths);
    fflush(stdout);
}
int main(int argc, char **argv) {
    enum { N = 256 };
    if (argc != 2 || chdir(argv[1])) return 5;
    char a[32], b[32];
    snapshot("before", 0);
    const char *phases[] = {"create", "stat", "rename", "unlink"};
    for (int phase = 0; phase < 4; phase++) {
        double start = now();
        for (int i = 0; i < N; i++) {
            snprintf(a, sizeof(a), "f%06d", i);
            snprintf(b, sizeof(b), "g%06d", i);
            if (phase == 0) {
                int fd = open(a, O_CREAT | O_EXCL | O_WRONLY, 0600);
                if (fd < 0 || write(fd, &i, sizeof(i)) != sizeof(i) || close(fd)) return 6;
            } else if (phase == 1) {
                struct stat st;
                if (stat(a, &st) || st.st_size != sizeof(i) || (st.st_mode & 0777) != 0600) return 7;
            } else if (phase == 2) {
                if (rename(a, b)) return 8;
            } else if (unlink(b)) return 9;
        }
        snapshot(phases[phase], now() - start);
    }
    DIR *dir = opendir(".");
    if (!dir) return 10;
    struct dirent *entry;
    while ((entry = readdir(dir)))
        if (entry->d_name[0] != '.') return 11;
    return closedir(dir) ? 12 : 0;
}
'''


def metadata_profile(text):
    """Subtract cumulative queue counters; reject truncated or regressing data."""
    phases, current, queue, path = {}, None, None, None
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == 'phase':
            name, elapsed = fields[1:]
            if name in phases:
                raise ValueError('duplicate phase: ' + name)
            current = phases[name] = {'seconds': float(elapsed), 'queues': {}, 'transport': {}, 'traces': []}
            queue, path = None, None
        elif fields[0] == 'queue':
            path = fields[1]
            queue = current['queues'].setdefault(path, {})
        elif fields[0] == 'work':
            if (path is None or len(fields) not in (6, 9) or fields[3] != 'kick'
                    or (len(fields) == 9 and fields[6] != 'notify')):
                raise ValueError('malformed transport row: ' + line)
            notifications = fields[7:9] if len(fields) == 9 else ['0', '0']
            current['transport'][path] = tuple(map(int, fields[1:3] + fields[4:6] + notifications))
        elif fields[0] == 'trace':
            if path is None or len(fields) != 8:
                raise ValueError('malformed latency trace: ' + line)
            trace = list(map(int, fields[1:]))
            if trace[0] not in (1, 3) or min(trace[1:]) <= 0 or trace[2:] != sorted(trace[2:]):
                raise ValueError('invalid latency timestamps: ' + line)
            current['traces'].append(trace)
        elif fields[0].isdigit():
            if queue is None or len(fields) not in (8, 9, 11):
                raise ValueError('malformed opcode row: ' + line)
            opcode = int(fields[0])
            if opcode in queue:
                raise ValueError('duplicate opcode row: ' + line)
            queue[opcode] = tuple(map(int, fields[1:])) + (0,) * (11 - len(fields))
        else:
            raise ValueError('unknown profile row: ' + line)
    if list(phases) != ['before', 'create', 'stat', 'rename', 'unlink']:
        raise ValueError('incomplete metadata phases')
    result, previous, transport = {}, phases['before']['queues'], phases['before']['transport']
    for name in ('create', 'stat', 'rename', 'unlink'):
        phase = phases[name]
        if not previous.keys() <= phase['queues'].keys() or any(
                not counters.keys() <= phase['queues'][path].keys() for path, counters in previous.items()):
            raise ValueError('profile counters disappeared')
        if not transport.keys() <= phase['transport'].keys():
            raise ValueError('transport counters disappeared')
        rows = {}
        for path, counters in phase['queues'].items():
            for opcode, values in counters.items():
                old = previous.get(path, {}).get(opcode, (0,) * 10)
                delta = tuple(a - b for a, b in zip(values, old))
                if min(delta) < 0:
                    raise ValueError('profile counter regressed')
                if any(delta):
                    rows[f'{path}:{opcode}'] = dict(zip(
                        ('submitted', 'completed', 'total_ns', 'enqueue_ns',
                         'device_ns', 'complete_ns', 'reply_bytes', 'background',
                         'wake_count', 'wake_ns'), delta))
        timing = {}
        for path, values in phase['transport'].items():
            delta = tuple(a - b for a, b in zip(values, transport.get(path, (0,) * 6)))
            if min(delta) < 0:
                raise ValueError('transport counter regressed')
            timing[path] = dict(zip(('work_count', 'work_delay_ns', 'kick_count', 'kick_ns',
                                     'notify_count', 'notify_ns'), delta))
        result[name] = {'operations': 256, 'seconds': phase['seconds'], 'opcodes': rows,
                        'transport': timing, 'traces': phase['traces']}
        previous = phase['queues']
        transport = phase['transport']
    return result


def metadata_batch(c, share):
    import shutil
    import subprocess
    binary = build_probe(c, 'metadata-batch', METADATA_BATCH)
    shutil.copy2(binary, share / 'batch')
    native = share / 'native-files'
    native.mkdir()
    result = subprocess.run([str(binary), str(native)], capture_output=True, text=True,
                            timeout=c.remaining(30))
    assert result.returncode == 0, result.stderr
    c.write('native_metadata_phases: ' + json.dumps(metadata_profile(result.stdout), sort_keys=True))
    assert not list(native.iterdir()), 'native metadata workload left files behind'
    directory = share / 'files'
    directory.mkdir()
    output = c.d('run', '--rm', '--mount', f'type=bind,src={share},dst=/share',
                 'hd-functional:local', '/share/batch', '/share/files')
    profile = metadata_profile(output)
    c.write('metadata_phases: ' + json.dumps(profile, sort_keys=True))
    if 'virtiofs.profile_requests=1' in c.ctx.instance_options.get('kernel_args', []):
        assert any(row['opcodes'] for row in profile.values()), 'kernel profile was not readable'
        assert all(row['traces'] for row in profile.values()), 'kernel latency samples were not readable'
    assert not list(directory.iterdir()), 'metadata workload left files behind'


def hostfs_ceiling(c, share):
    """Diagnostic only: the official workload on native, virtiofs, and hostfs."""
    import shutil
    import subprocess
    from pipeline import bench, runs
    binary = bench.bench_root() / 'root/bench/workload'
    shutil.copy2(binary, share / 'workload')
    for metric in ('fs.bind_metadata', 'disk.bind_read'):
        for rep in range(2):
            for mode in ('native', 'virtiofs', 'hostfs'):
                directory = share / (metric + '-' + str(rep) + '-' + mode)
                directory.mkdir()
                before = runs.throttle()
                if mode == 'native':
                    result = subprocess.run([str(binary), metric, '1', '--workdir', str(directory)],
                                            capture_output=True, text=True, timeout=c.remaining(90))
                    assert result.returncode == 0, result.stderr
                    output = result.stdout
                else:
                    script = 'exec /share/workload "$2" 1 --workdir "$1"'
                    target = '/share/' + directory.name
                    if mode == 'hostfs':
                        target = str(directory)
                        script = ('mkdir /host; mount -t hostfs -o "$1" none /host; '
                                  'test "$(stat -f -c %t /host)" = c0ffee; '
                                  'exec /share/workload "$2" 1 --workdir /host')
                    output = c.d('run', '--rm', *(['--privileged'] if mode == 'hostfs' else []),
                                 '--mount', f'type=bind,src={share},dst=/share',
                                 'hd-functional:local', 'sh', '-ec', script, 'sh', target, metric,
                                 timeout=90)
                row = {'metric': metric, 'rep': rep, 'mode': mode, **bench.parse_output(output)}
                after = runs.throttle()
                if before and after and after[0] > before[0]:
                    row['throttled'] = (after[1] - before[1]) / (after[0] - before[0])
                c.write('hostfs_ceiling: ' + json.dumps(row, sort_keys=True))
                assert not list(directory.iterdir()), 'workload left data behind'


READ_BUFFER = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#define MIB (1024UL * 1024)
static void require(int ok, const char *what) {
    if (!ok) { perror(what); exit(2); }
}
static uint64_t ns(void) {
    struct timespec ts;
    require(!clock_gettime(CLOCK_MONOTONIC, &ts), "clock");
    return (uint64_t)ts.tv_sec * 1000000000 + (uint64_t)ts.tv_nsec;
}
int main(int argc, char **argv) {
    require(argc == 2, "usage");
    size_t page = (size_t)sysconf(_SC_PAGESIZE);
    unsigned char *expected = malloc(MIB);
    unsigned char *buffer = mmap(NULL, MIB + page * 2, PROT_READ | PROT_WRITE,
                                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    require(expected && buffer != MAP_FAILED, "allocate");
    for (size_t i = 0; i < MIB; i++) expected[i] = (unsigned char)(i * 131 + i / 251);
    memset(buffer, 0x53, MIB + page * 2);
    int fd = open(argv[1], O_CREAT | O_EXCL | O_RDWR, 0600);
    require(fd >= 0 && write(fd, expected, MIB) == MIB && !fsync(fd), "prepare");
    for (size_t size = 64 * 1024; size <= MIB; size *= 16) {
        for (int rep = -1; rep < 3; rep++) {
            size_t iterations = 256 * MIB / size;
            uint64_t started = ns();
            for (size_t i = 0; i < iterations; i++)
                require(pread(fd, buffer + 17, size, 0) == (ssize_t)size, "pread");
            uint64_t elapsed = ns() - started;
            require(!memcmp(expected, buffer + 17, size), "read content");
            require(buffer[16] == 0x53 && buffer[size + 17] == 0x53, "buffer canaries");
            if (rep >= 0) printf("{\"size\":%zu,\"rep\":%d,\"bytes\":%zu,\"ns\":%llu}\n",
                size, rep, iterations * size, (unsigned long long)elapsed);
        }
    }
    unsigned char *edge = mmap(NULL, 3 * page, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    require(edge != MAP_FAILED && !mprotect(edge + page, page, PROT_NONE), "guard");
    require(pread(fd, edge, 2 * page, 0) == (ssize_t)page, "partial read");
    require(!memcmp(expected, edge, page), "partial content");
    errno = 0;
    require(pread(fd, edge + page, page, 0) == -1 && errno == EFAULT, "guard EFAULT");
    require(!mprotect(edge, 2 * page, PROT_READ), "readonly");
    errno = 0;
    require(pread(fd, edge, 2 * page, 0) == -1 && errno == EFAULT, "readonly EFAULT");
    require(!mprotect(edge, 2 * page, PROT_READ | PROT_WRITE), "writable");
    memset(edge, 0x59, 2 * page);
    pid_t child = fork();
    require(child >= 0, "fork");
    if (!child) {
        require(pread(fd, edge, 2 * page, 0) == (ssize_t)(2 * page) &&
                !memcmp(expected, edge, 2 * page), "COW child");
        _exit(0);
    }
    int status;
    require(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status), "wait");
    for (size_t i = 0; i < 2 * page; i++) require(edge[i] == 0x59, "COW parent");
    require(!close(fd) && !unlink(argv[1]), "cleanup");
    require(!munmap(edge, 3 * page) && !munmap(buffer, MIB + 2 * page), "unmap");
    free(expected);
    puts("{\"edges\":true}");
    return 0;
}
'''


def read_buffer_results(output):
    rows = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
    if len(rows) != 7 or rows[-1] != {'edges': True}:
        raise ValueError('incomplete read-buffer checks')
    expected = [(size, rep) for size in (65536, 1048576) for rep in range(3)]
    if [(row.get('size'), row.get('rep')) for row in rows[:-1]] != expected:
        raise ValueError('missing read-buffer size or repetition')
    for row in rows[:-1]:
        if any(type(row[key]) is not int for key in ('size', 'rep', 'bytes', 'ns')):
            raise ValueError('invalid read-buffer field type')
        if row['bytes'] != 256 * 1048576 or type(row['ns']) is not int or row['ns'] <= 0:
            raise ValueError('invalid read-buffer measurement')
        row['us_per_mib'] = row['ns'] / 1000 / 256
    return rows[:-1]


def read_buffer(c, share):
    import shutil
    import subprocess
    binary = build_probe(c, 'read-buffer', READ_BUFFER)
    shutil.copy2(binary, share / 'reader')
    for mode in ('native', 'overlay', 'virtiofs'):
        if mode == 'native':
            result = subprocess.run([str(binary), str(share / 'native-data')], capture_output=True,
                                    text=True, timeout=c.remaining(60))
            assert result.returncode == 0, result.stderr
            output = result.stdout
        else:
            path = '/tmp/data' if mode == 'overlay' else '/share/data'
            output = c.d('run', '--rm', '--mount', f'type=bind,src={share},dst=/share',
                         'hd-functional:local', '/share/reader', path, timeout=90)
        c.write('read_buffer: ' + json.dumps({'mode': mode, 'rows': read_buffer_results(output)}, sort_keys=True))


def lookup_race(c, share):
    import shutil
    binary = build_probe(c, 'tree-probe', TREE_PROBE)
    shutil.copy2(binary, share / 'tree-probe')
    for mode in ('native', 'docker'):
        directory = share / mode
        directory.mkdir()
        path = directory / 'race'
        samples = []
        with TreePeer(c, mode, directory, binary) as peer:
            for _ in range(8):
                peer.ask('race race')
                for _ in range(512):
                    path.write_bytes(b'old')
                    path.unlink()
                started = time.time_ns()
                path.write_bytes(b'new-data' * 1024)
                samples.append(peer.window('stat race', {'rc': 0, 'size': 8192}, started))
                path.unlink()
        check_windows(c, mode, samples)


EXCLUSIVE_CREATE = r'''
#define _DEFAULT_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2 || chdir(argv[1])) return 2;
    umask(0027);
    int fd = open("fresh", O_CREAT | O_EXCL | O_RDWR, 0666);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || (st.st_mode & 0777) != 0640 ||
        st.st_uid != geteuid() || st.st_gid != getegid()) return 3;
    if (write(fd, "keep", 4) != 4 || close(fd)) return 4;
    if (mkdir("directory", 0755) || symlink("absent", "dangling") ||
        symlink("fresh", "link")) return 5;
    const char *names[] = {"fresh", "directory", "dangling", "link"};
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        fd = open(names[i], O_CREAT | O_EXCL | O_TRUNC | O_RDWR, 0666);
        if (fd >= 0 || errno != EEXIST) return 6;
    }
    if (!lstat("absent", &st) || errno != ENOENT) return 7;
    char data[4];
    fd = open("fresh", O_RDONLY);
    if (fd < 0 || read(fd, data, sizeof(data)) != 4 || memcmp(data, "keep", 4) || close(fd)) return 8;
    fd = open("link", O_CREAT | O_RDWR, 0666);
    if (fd < 0 || read(fd, data, sizeof(data)) != 4 || memcmp(data, "keep", 4) || close(fd)) return 9;
    if (mkdir("readonly", 0755)) return 10;
    fd = open("readonly/exists", O_CREAT | O_EXCL | O_RDWR, 0600);
    if (fd < 0 || close(fd) || chmod("readonly", 0555)) return 11;
    fd = open("readonly/exists", O_CREAT | O_EXCL | O_RDWR, 0600);
    if (fd >= 0 || errno != EEXIST) return 12;
    if (geteuid() != 0) {
        fd = open("readonly/absent", O_CREAT | O_EXCL | O_RDWR, 0600);
        if (fd >= 0 || errno != EACCES) return 13;
    }
    if (chmod("readonly", 0755) || unlink("readonly/exists") || rmdir("readonly")) return 14;
    if (unlink("fresh") || unlink("link") || unlink("dangling") || rmdir("directory")) return 15;
    puts("exclusive create: ok");
    return 0;
}
'''


def exclusive_create(c, share):
    import shutil
    import subprocess
    binary = build_probe(c, 'exclusive-create', EXCLUSIVE_CREATE)
    shutil.copy2(binary, share / 'exclusive-create')
    for mode in ('native', 'root', 'user'):
        directory = share / mode
        directory.mkdir(mode=0o777)
        os.chmod(directory, 0o777)
        if mode == 'native':
            result = subprocess.run([str(binary), str(directory)], capture_output=True, text=True,
                                    timeout=c.remaining(10))
            c.write(result.stdout + result.stderr)
            assert result.returncode == 0, result
        else:
            output = c.d('run', '--rm', '--user', '1001:1001' if mode == 'user' else '0:0',
                         '--mount', f'type=bind,src={share},dst=/share', 'hd-functional:local',
                         '/share/exclusive-create', '/share/' + mode)
            assert output.strip() == 'exclusive create: ok', output
        assert not list(directory.iterdir()), mode


WINDOW_PROBE = r'''
#include <dirent.h>
#include <stdint.h>
#include <time.h>
#include <poll.h>
#include <sys/inotify.h>
static long long realtime_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_REALTIME, &t)) exit(90);
    return (long long)t.tv_sec * 1000000000 + t.tv_nsec;
}
/* Fixed numeric fields keep the protocol independent of a JSON parser. */
static int window_command(char *line, const char *path, int *held,
                          unsigned char *map, size_t map_size, int notify, int tree) {
    if (!strcmp(line, "clock\n")) {
        long long start = realtime_ns(), end = realtime_ns();
        printf("{\"rc\":0,\"start_ns\":%lld,\"end_ns\":%lld}\n", start, end);
        return 1;
    }
    if (!strncmp(line, "race ", 5)) {
        char name[256]; struct stat st;
        if (sscanf(line + 5, "%255s", name) != 1) exit(94);
        puts("{\"rc\":0}");
        struct pollfd input = {0, POLLIN, 0};
        while (poll(&input, 1, 0) == 0) {
            if (stat(name, &st) && errno != ENOENT) exit(95);
        }
        return 1;
    }
    if (strncmp(line, "poll ", 5)) return 0;
    long long deadline, expected[8]; unsigned mask;
    char *command = strchr(line, '|'), op[32], arg[256] = ".";
    if (!command || sscanf(line, "poll %lld %u %lld %lld %lld %lld %lld %lld %lld %lld",
        &deadline, &mask, &expected[0], &expected[1], &expected[2], &expected[3],
        &expected[4], &expected[5], &expected[6], &expected[7]) != 10) exit(91);
    if (sscanf(command + 1, "%31s %255s", op, arg) < 1) exit(92);
    unsigned count = 0; int matched = 0;
    do {
        struct sample { long long start, end, value[8]; } row = {0}, *r = &row;
        count++;
        r->start = realtime_ns();
        struct stat st; int fd = *held, temporary = 0, rc = 0, error = 0;
        const char *name = tree ? arg : path;
        if (!strcmp(op, "mapped")) {
            size_t offset = tree ? strtoull(arg, NULL, 10) : 0;
            if (map == MAP_FAILED || offset >= map_size) { rc = -1; error = EINVAL; }
            else r->value[2] = *(volatile unsigned char *)(map + offset);
        } else if (!strcmp(op, "event")) {
            static char pending[65536]; static size_t used;
            char *slash = strchr(arg, '/');
            if (!slash) exit(96);
            unsigned wanted = (unsigned)strtoul(arg, NULL, 10);
            ssize_t n = read(notify, pending + used, sizeof(pending) - used);
            if (n > 0) used += n;
            else if (n < 0 && errno != EAGAIN) { rc = -1; error = errno; }
            size_t pos = 0;
            while (used - pos >= sizeof(struct inotify_event)) {
                struct inotify_event *e = (void *)(pending + pos);
                size_t len = sizeof(*e) + e->len;
                if (len > used - pos || e->mask & IN_Q_OVERFLOW) exit(97);
                if ((e->mask & wanted) && e->len && !strcmp(e->name, slash + 1)) {
                    r->value[6] = e->cookie; r->value[7] = 1;
                    memmove(pending + pos, pending + pos + len, used - pos - len);
                    used -= len; break;
                }
                pos += len;
            }
        } else if (!strcmp(op, "events")) {
            char data[4096]; ssize_t n = read(notify, data, sizeof(data));
            if (n < 0 && errno != EAGAIN) { rc = -1; error = errno; }
            r->value[7] = n > 0;
        } else if (!strcmp(op, "list")) {
            struct dirent **entries;
            int n = scandir(name, &entries, NULL, alphasort);
            if (n < 0) { rc = -1; error = errno; }
            else {
                uint64_t hash = 0;
                for (int i = 0; i < n; i++) {
                    const unsigned char *s = (void *)entries[i]->d_name;
                    if (strcmp((void *)s, ".") && strcmp((void *)s, "..")) {
                        do { hash = (hash * 131 + *s) & INT64_MAX; } while (*s++);
                    }
                    free(entries[i]);
                }
                free(entries); r->value[7] = (long long)hash;
            }
        } else {
            if (!strcmp(op, "reopen") || !strcmp(op, "open") || !strcmp(op, "reopened")) {
                if (strcmp(op, "reopen") && *held >= 0) { close(*held); *held = -1; }
                fd = open(name, O_RDWR | O_CLOEXEC);
                if (!strcmp(op, "reopen")) temporary = 1;
                else *held = fd;
                if (fd < 0) { rc = -1; error = errno; }
            } else if (tree && !strcmp(op, "read")) {
                fd = open(name, O_RDONLY | O_CLOEXEC); temporary = 1;
                if (fd < 0) { rc = -1; error = errno; }
            }
            if (!rc) {
                rc = tree && !strcmp(op, "stat") ? stat(name, &st) : fstat(fd, &st);
                if (rc) error = errno;
                else {
                    r->value[3] = st.st_size; r->value[4] = st.st_mode & 07777;
                    r->value[5] = (long long)st.st_mtim.tv_sec * 1000000000 + st.st_mtim.tv_nsec;
                    if (!strcmp(op, "read") || !strcmp(op, "held") || !strcmp(op, "held-found") || !strcmp(op, "reopened")) {
                        unsigned char byte;
                        if (pread(fd, &byte, 1, 0) != 1) { rc = -1; error = errno ? errno : EIO; }
                        else r->value[2] = byte;
                    }
                }
            }
            if (temporary && fd >= 0) close(fd);
            if (!strcmp(op, "held-found")) r->value[6] = stat(arg, &st);
        }
        r->value[0] = rc; r->value[1] = error;
        r->end = realtime_ns();
        matched = 1;
        for (int i = 0; i < 8; i++) if ((mask & (1u << i)) && expected[i] != r->value[i]) matched = 0;
        int last = matched || r->end >= deadline || count == 10001;
        printf("{\"start_ns\":%lld,\"end_ns\":%lld,\"value\":[", r->start, r->end);
        for (int j = 0; j < 8; j++) printf("%s%lld", j ? "," : "", r->value[j]);
        printf("],\"last\":%s}\n", last ? "true" : "false");
        if (last) break;
        struct timespec pause = {0, 100000};
        nanosleep(&pause, NULL);
    } while (count < 10001);
    return 1;
}
'''

WINDOW_FIELDS = ('rc', 'errno', 'byte', 'size', 'mode', 'mtime_ns', 'found_rc', 'names')


def clock_offset(samples):
    """NTP midpoint estimate from the least delayed exchange."""
    assert samples, 'missing clock exchanges'
    for a, b, g, h in samples:
        assert all(isinstance(v, int) and not isinstance(v, bool) for v in (a, b, g, h))
        assert a <= b and g <= h and b - a >= h - g, 'invalid clock exchange'
    a, b, g, h = min(samples, key=lambda s: s[1] - s[0] - (s[3] - s[2]))
    offset = ((g - a) + (h - b)) // 2
    result = {'offset_ns': offset, 'rtt_ns': b - a - (h - g), 'samples': samples}
    assert abs(offset) <= 500_000, result
    return result


class WindowPeer(ProbePeer):
    def __init__(self, c, mode, path, uid=None, readonly=False):
        import pathlib
        import shutil
        code = (pathlib.Path(__file__).parent / 'helpers/share_probe.c').read_text()
        code = code.replace('int main(int argc, char **argv) {', WINDOW_PROBE + '\nint main(int argc, char **argv) {')
        code = code.replace('char line[256], op[32];', 'char line[1024], op[32];')
        marker = 'while (fgets(line, sizeof(line), stdin)) {'
        code = code.replace(marker, marker + '\n        if (window_command(line, path, &fd, map, 4096, notify, 0)) continue;')
        binary = build_probe(c, 'window-peer', code)
        if not (path.parent / 'window-peer').exists():
            shutil.copy2(binary, path.parent / 'window-peer')
        self.c, self.mode = c, mode
        self.buffer, self.closed, self.expected_kill = b'', False, False
        c.serial += 1
        self.name = c.object('container', 'window-' + str(c.serial)) if mode == 'docker' else 'native-window'
        if mode == 'native':
            self.proc = c.spawn(command=[str(binary), path.name, *([str(uid)] if uid is not None else [])], cwd=path.parent)
        else:
            self.proc = c.spawn('run', '--rm', '-i', '--name', self.name, '--label', c.label,
                *(['--user', f'{uid}:{uid}'] if uid is not None else []),
                '--mount', f'type=bind,src={path.parent},dst=/share' + (',readonly' if readonly else ''),
                'hd-functional:local', '/share/window-peer', '/share/' + path.name)
        try:
            assert self.receive().get('ready'), 'window probe startup failed'
            self.calibrate()
        except BaseException:
            self.close()
            raise

    def calibrate(self):
        samples = []
        for _ in range(9):
            a = time.time_ns()
            self.send('clock')
            row = self.receive()
            b = time.time_ns()
            samples.append((a, b, row['start_ns'], row['end_ns']))
        self.offset = clock_offset(samples)['offset_ns']
        self.c.write('coherence_clock: ' + json.dumps({'mode': self.mode, **clock_offset(samples)}))

    def window(self, command, expected, started_ns, ended_ns=None):
        ended_ns = time.time_ns() if ended_ns is None else ended_ns
        deadline = ended_ns + 1_000_000_000
        self.begin_window(command, expected, deadline)
        return self.finish_window(started_ns, ended_ns, deadline)

    def begin_window(self, command, expected, deadline):
        self.window_mask = sum(1 << WINDOW_FIELDS.index(k) for k in expected)
        self.window_values = [expected.get(k, 0) for k in WINDOW_FIELDS]
        self.send(f'poll {deadline + self.offset} {self.window_mask} ' +
                  ' '.join(map(str, self.window_values)) + '|' + command)

    def finish_window(self, started_ns, ended_ns, deadline, initial=()):
        rows = list(initial)
        while True:
            left = (deadline - time.time_ns()) / 1e9
            try:
                assert left > 0, 'host 1s deadline'
                row = self.receive(timeout=left)
            except (TimeoutError, AssertionError) as error:
                raise AssertionError({'deadline': 'host 1s', 'reads': len(rows),
                    'last_read': rows[-1] if rows else None,
                    'staleness_ms': max(0, rows[-1]['start_ns'] - self.offset - ended_ns) / 1e6 if rows else None,
                    'error': str(error)}) from error
            rows.append(row)
            if row['last']:
                break
        result = change_window(rows, self.window_values, self.window_mask, started_ns, ended_ns, self.offset)
        self.c.write('coherence_sample: ' + json.dumps({'mode': self.mode, **result}, sort_keys=True))
        return result


def name_hash(names):
    result = 0
    for name in sorted(names):
        for byte in os.fsencode(name) + b'\0':
            result = (result * 131 + byte) & ((1 << 63) - 1)
    return result


def expect_metadata(row, size, mode):
    assert row.get('rc') == 0 and (row.get('size'), row.get('mode')) == (size, mode), row


def metadata_churn(peer, path, rounds=32):
    """Measure from before truncate/chmod until both new attributes are read."""
    samples = []
    for i in range(rounds):
        size, mode = 4096 + i, (0o640 if i % 2 else 0o600)
        peer.ask('stat')
        started = time.time_ns()
        os.truncate(path, size)
        os.chmod(path, mode)
        ended = time.time_ns()
        samples.append(peer.window('stat', {'rc': 0, 'size': size, 'mode': mode}, started, ended))
    return samples


def change_window(reads, expected, mask, started_ns, ended_ns, offset_ns=0):
    """Score stale observations in the peer clock, excluding channel latency."""
    assert started_ns <= ended_ns and reads, 'missing change or observations'
    stale, last_stale, previous, fresh = 0, 0, None, False
    for row in reads:
        start, end, value = row['start_ns'] - offset_ns, row['end_ns'] - offset_ns, row['value']
        assert start <= end and (previous is None or previous <= start), row
        assert len(value) == len(expected), row
        assert not fresh, 'peer kept reading after a fresh observation'
        previous = end
        fresh = all(not mask & (1 << i) or v == expected[i] for i, v in enumerate(value))
        if not fresh:
            stale += 1
            last_stale = max(0, start - ended_ns)
    result = {'origin': 'outer_change_end', 'change_start_ns': started_ns, 'change_end_ns': ended_ns,
              'first_access': stale == 0, 'stale_reads': stale, 'staleness_ms': last_stale / 1e6,
              'fresh': fresh, 'last_read': reads[-1], 'offset_ns': offset_ns}
    assert fresh and last_stale <= 50_000_000 and previous <= ended_ns + 1_000_000_000, result
    return result


def window_summary(samples):
    assert samples, 'missing coherence samples'
    assert all(row.get('origin') == 'outer_change_end' and row.get('fresh') is True
               and isinstance(row.get('first_access'), bool) for row in samples), samples
    windows = sorted(row['staleness_ms'] for row in samples)
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) and v >= 0 for v in windows), windows
    return {'origin': 'outer_change_end', 'rounds': len(samples),
            'first_access_misses': sum(not row['first_access'] for row in samples),
            'samples_ms': [row['staleness_ms'] for row in samples],
            'p50_staleness_ms': windows[(len(windows) - 1) // 2],
            'p95_staleness_ms': windows[(95 * len(windows) + 99) // 100 - 1],
            'max_staleness_ms': windows[-1]}


def check_windows(c, mode, samples):
    result = dict(mode=mode, **window_summary(samples))
    c.write('coherence_window: ' + json.dumps(result, sort_keys=True))
    assert result['p95_staleness_ms'] <= 10 and result['max_staleness_ms'] <= 50, result
    return result


def notification_probe():
    """Record permitted outer notification mechanisms without requiring privilege."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    result = {}
    for name, function, args in [
            ('fanotify_fd', 'fanotify_init', (3, os.O_RDONLY)),
            ('fanotify_fid', 'fanotify_init', (0xe03, os.O_RDONLY)),
            ('inotify', 'inotify_init1', (os.O_CLOEXEC | os.O_NONBLOCK,))]:
        fn = getattr(libc, function)
        fd = fn(*args)
        result[name] = {'available': fd >= 0, 'errno': ctypes.get_errno() if fd < 0 else 0}
        if fd >= 0:
            os.close(fd)
    return result


def build_probe(c, name, code):
    from pipeline import build
    flags = ['-static', '-O2', '-Wall', '-Wextra', '-Werror']
    kind = 'share-' + name
    key = build.key_of(kind + '/1', code, flags, c.suite.bins.name)
    def compile_probe(tmp, _final):
        source = tmp / 'waiter.c'
        source.write_text(code)
        build.run(['gcc', *flags, source, '-o', tmp / 'waiter'],
                  log_file=tmp / 'build.log', timeout=90)
    return build.artifact(kind, key, compile_probe) / 'waiter'

def mmap_delivery(c, share):
    """Begin guest polling before the outer write."""
    for mode in ('native', 'docker'):
        path = share / ('data-' + mode)
        path.write_bytes(b'A' * 8192)
        with WindowPeer(c, mode, path) as peer, path.open('r+b', buffering=0) as outer:
            peer.ask('open');peer.ask('map')
            samples = []
            for value in range(66, 98):
                deadline = time.time_ns() + 1_000_000_000
                peer.begin_window('mapped', {'rc': 0, 'byte': value}, deadline)
                armed = peer.receive(timeout=1)
                assert not armed['last'] and armed['value'][2] == value - 1, armed
                started = time.time_ns()
                os.pwrite(outer.fileno(), bytes([value]), 0)
                ended = time.time_ns()
                samples.append(peer.finish_window(started, ended, ended + 1_000_000_000, [armed]))
            peer.ask('unmap')
        check_windows(c, mode, samples)


def inotify_records(data):
    records, offset = [], 0
    while len(data) - offset >= 16:
        wd, mask, cookie, size = struct.unpack_from('=iIII', data, offset)
        if size > 256:
            raise ValueError('oversized inotify name')
        if len(data) - offset < 16 + size:
            break
        raw = data[offset + 16:offset + 16 + size]
        if size and not raw.endswith(b'\0'):
            raise ValueError('unterminated inotify name')
        records.append({'wd': wd, 'mask': mask, 'cookie': cookie,
                        'name': os.fsdecode(raw.split(b'\0', 1)[0])})
        offset += 16 + size
    return records, data[offset:]


def directory_events(c, share):
    import shutil
    binary = build_probe(c, 'tree-probe', TREE_PROBE)
    shutil.copy2(binary, share / 'tree-probe')
    for mode in ('native', 'docker'):
        directory = share / mode
        directory.mkdir(mode=0o755)
        samples = []
        with TreePeer(c, mode, directory, binary, uid=1001) as peer:
            peer.ask('watch .')
            def event(mask, name, started, ended):
                row = peer.window(f'event {mask}/{name}', {'rc': 0, 'names': 1}, started, ended)
                samples.append(row)
                return row['last_read']['value'][6]
            for i in range(8):
                original, renamed = directory / ('original-' + str(i)), directory / ('renamed-' + str(i))
                started = time.time_ns()
                original.write_bytes(b'content')
                ended = time.time_ns()
                event(0x100, original.name, started, ended)
                event(0x2, original.name, started, ended)
                started = time.time_ns()
                os.chmod(original, 0o640)
                ended = time.time_ns()
                event(0x4, original.name, started, ended)
                started = time.time_ns()
                original.rename(renamed)
                ended = time.time_ns()
                moved_from = event(0x40, original.name, started, ended)
                moved_to = event(0x80, renamed.name, started, ended)
                assert moved_from and moved_from == moved_to, (moved_from, moved_to)
                started = time.time_ns()
                renamed.unlink()
                ended = time.time_ns()
                event(0x200, renamed.name, started, ended)
        check_windows(c, mode, samples)


TREE_PROBE = r'''
#define _GNU_SOURCE
#include <grp.h>
#include <sys/inotify.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if ((argc != 2 && argc != 3) || chdir(argv[1])) return 2;
    if (argc == 3) {
        unsigned uid = strtoul(argv[2], NULL, 10);
        if (setgroups(0, NULL) || setgid(uid) || setuid(uid)) return 13;
    }
    setbuf(stdout, NULL);
    puts("{\"ready\":true}");
    char line[512], op[32], path[256];
    int held = -1, notify = -1;
    unsigned char *mapping = MAP_FAILED;
    size_t map_size = 0;
    while (fgets(line, sizeof(line), stdin)) {
        if (!strcmp(line, "quit\n")) return 0;
        if (sscanf(line, "%31s %255s", op, path) != 2) return 3;
        if (!strcmp(op, "watch")) {
            notify = inotify_init1(IN_CLOEXEC | IN_NONBLOCK);
            int rc = notify < 0 ? -1 : inotify_add_watch(notify, path,
                IN_CREATE | IN_DELETE | IN_MODIFY | IN_ATTRIB | IN_MOVED_FROM | IN_MOVED_TO);
            printf("{\"rc\":%d,\"errno\":%d}\n", rc < 0 ? -1 : 0, rc < 0 ? errno : 0);
        } else if (!strcmp(op, "map") || !strcmp(op, "mapall") || !strcmp(op, "private")) {
            int fd = open(path, O_RDWR);
            struct stat st;
            if (fd < 0 || fstat(fd, &st)) return 7;
            map_size = !strcmp(op, "mapall") ? (size_t)st.st_size : 4096;
            mapping = mmap(NULL, map_size, PROT_READ | PROT_WRITE,
                           !strcmp(op, "private") ? MAP_PRIVATE : MAP_SHARED, fd, 0);
            close(fd);
            if (mapping == MAP_FAILED) return 8;
            puts("{\"rc\":0}");
        } else if (!strcmp(op, "remap")) {
            size_t size = strtoull(path, NULL, 10);
            if (mapping == MAP_FAILED || size != 8192) return 9;
            unsigned char *next = mremap(mapping, map_size, size, MREMAP_MAYMOVE);
            if (next == MAP_FAILED) return 10;
            mapping = next; map_size = size;
            puts("{\"rc\":0}");
        } else if (!strcmp(op, "mapped")) {
            size_t offset = strtoull(path, NULL, 10);
            if (mapping == MAP_FAILED || offset >= map_size) return 11;
            printf("{\"rc\":0,\"byte\":%u}\n", *(volatile unsigned char *)(mapping + offset));
        } else if (!strcmp(op, "unmap")) {
            if (mapping == MAP_FAILED || munmap(mapping, map_size)) return 12;
            mapping = MAP_FAILED;
            puts("{\"rc\":0}");
        } else if (!strcmp(op, "hold") || !strcmp(op, "held")) {
            if (!strcmp(op, "hold")) {
                if (held >= 0) close(held);
                held = open(path, O_RDONLY);
            }
            struct stat st; unsigned char byte = 0;
            if (held < 0 || fstat(held, &st) || pread(held, &byte, 1, 0) != 1)
                printf("{\"rc\":-1,\"errno\":%d}\n", errno);
            else printf("{\"rc\":0,\"size\":%lld,\"mode\":%u,\"byte\":%u}\n",
                        (long long)st.st_size, st.st_mode & 0777, byte);
        } else if (!strcmp(op, "fault")) {
            uint64_t action = strtoull(path, NULL, 10);
            int fd = open(".", O_RDONLY | O_DIRECTORY);
            int rc = fd < 0 ? -1 : ioctl(fd, _IOW('H', 0xd5, uint64_t), &action);
            int error = errno;
            if (fd >= 0) close(fd);
            printf("{\"rc\":%d,\"errno\":%d}\n", rc, rc ? error : 0);
        } else if (!strcmp(op, "list")) {
            DIR *dir = opendir(path);
            if (!dir) { printf("{\"rc\":-1,\"errno\":%d}\n", errno); continue; }
            printf("{\"rc\":0,\"names\":[");
            struct dirent *e; int comma = 0;
            while ((e = readdir(dir))) {
                if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
                /* Test paths use only ASCII letters, digits, dots, and hyphens. */
                if (strspn(e->d_name, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-") != strlen(e->d_name)) return 4;
                printf("%s\"%s\"", comma ? "," : "", e->d_name); comma = 1;
            }
            closedir(dir); puts("]}");
        } else if (!strcmp(op, "stat") || !strcmp(op, "read")) {
            struct stat st; unsigned char byte = 0; int rc = stat(path, &st), error = errno;
            if (!rc && !strcmp(op, "read")) {
                int fd = open(path, O_RDONLY);
                if (fd < 0) { rc = -1; error = errno; }
                else { if (read(fd, &byte, 1) != 1) { rc = -1; error = errno; } close(fd); }
            }
            if (rc) printf("{\"rc\":-1,\"errno\":%d}\n", error);
            else printf("{\"rc\":0,\"size\":%lld,\"mode\":%u,\"mtime_ns\":%lld,\"ino\":%llu,\"byte\":%u}\n",
                (long long)st.st_size, st.st_mode & 0777,
                (long long)st.st_mtim.tv_sec * 1000000000 + st.st_mtim.tv_nsec,
                (unsigned long long)st.st_ino, byte);
        } else return 5;
    }
    return 6;
}
'''


TREE_PROBE = TREE_PROBE.replace('int main(int argc, char **argv) {', WINDOW_PROBE + '\nint main(int argc, char **argv) {').replace(
    'while (fgets(line, sizeof(line), stdin)) {',
    'while (fgets(line, sizeof(line), stdin)) {\n        if (window_command(line, NULL, &held, mapping, map_size, notify, 1)) continue;')


class TreePeer(WindowPeer):
    def __init__(self, c, mode, directory, binary, uid=None):
        self.c, self.mode = c, mode
        self.buffer, self.closed, self.expected_kill = b'', False, False
        c.serial += 1
        self.name = c.object('container', 'tree-' + str(c.serial)) if mode == 'docker' else 'native-tree'
        if mode == 'native':
            self.proc = c.spawn(command=[str(binary), str(directory), *([str(uid)] if uid is not None else [])])
        else:
            self.proc = c.spawn('run', '--rm', '-i', '--name', self.name, '--label', c.label,
                                '--mount', f'type=bind,src={directory.parent},dst=/share',
                                'hd-functional:local', '/share/tree-probe', '/share/' + directory.name,
                                *([str(uid)] if uid is not None else []))
        try:
            assert self.receive().get('ready') is True, 'tree probe startup failed'
            self.calibrate()
        except BaseException:
            self.close()
            raise


def fault_control(c, action):
    """Fault injection is reachable only in the outer instance directory."""
    import socket
    paths = list(c.ctx.instance.glob('*.sock.notify-control'))
    assert len(paths) == 1, paths
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(c.remaining(1))
        client.connect(str(paths[0]))
        client.sendall(bytes([action]))
        data = b''
        while len(data) < 4:
            chunk = client.recv(4 - len(data))
            assert chunk, 'notification control disconnected'
            data += chunk
    error, = struct.unpack('<i', data)
    assert error == 0, {'fault': action, 'errno': error}
    c.write('notification_fault: ' + str(action))


def namespace_windows(c, share, name):
    import shutil
    binary = build_probe(c, 'tree-probe', TREE_PROBE)
    shutil.copy2(binary, share / 'tree-probe')
    modes = ('docker', 'native') if name in ('outer_watch_exhaustion', 'outer_notify_overflow') else ('native', 'docker')
    bound_failures = []
    for mode in modes:
        directory = share / mode
        directory.mkdir()
        samples = []
        with TreePeer(c, mode, directory, binary) as peer:
            if name == 'outer_mapped_remap':
                for mapping_kind in ('map', 'private'):
                    path = directory / 'mapped'
                    path.write_bytes(b'A' * 8192)
                    peer.ask(mapping_kind + ' mapped')
                    assert peer.ask('mapped 0')['byte'] == 65
                    peer.ask('remap 8192')
                    assert peer.ask('mapped 4096')['byte'] == 65
                    with path.open('r+b', buffering=0) as file:
                        with mmap.mmap(file.fileno(), 8192, access=mmap.ACCESS_WRITE) as mapped:
                            for i in range(32):
                                started = time.time_ns()
                                mapped[4096] = 66 + i
                                samples.append(peer.window('mapped 4096', {'rc': 0, 'byte': 66 + i}, started))
                    peer.ask('unmap .')
            elif name == 'outer_mapped_scale':
                for size in (4096, 1 << 20, 8 << 20, 32 << 20):
                    path = directory / 'mapped'
                    path.write_bytes(b'A' * size)
                    peer.ask('mapall mapped')
                    samples = []
                    with path.open('r+b', buffering=0) as file:
                        with mmap.mmap(file.fileno(), size, access=mmap.ACCESS_WRITE) as mapped:
                            for i in range(32):
                                offset = (i * 4096) % size
                                peer.ask('mapped ' + str(offset))
                                started = time.time_ns()
                                mapped[offset] = 66 + i
                                samples.append(peer.window('mapped ' + str(offset), {'rc': 0, 'byte': 66 + i}, started))
                    peer.ask('unmap .')
                    c.write('mapped_bytes: ' + str(size))
                    try:
                        check_windows(c, mode, samples)
                    except AssertionError as error:
                        bound_failures.append({'mode': mode, 'mapped_bytes': size, 'error': str(error)})
                continue
            elif name == 'outer_mtime':
                path = directory / 'file'
                path.write_bytes(b'timestamp')
                for i in range(32):
                    peer.ask('stat file')
                    expected = (1700000000 + i) * 1000000000 + i
                    started = time.time_ns()
                    os.utime(path, ns=(expected, expected))
                    samples.append(peer.window('stat file', {'rc': 0, 'mtime_ns': expected}, started))
            elif name == 'outer_readdir_preserved_mtime':
                listing = lambda: sorted(peer.ask('list .')['names'])
                assert listing() == []
                stamp = directory.stat()
                original, renamed = directory / 'original', directory / 'renamed'
                for _ in range(16):
                    for change, expected in ((lambda: original.write_bytes(b'content'), ['original']),
                                             (lambda: original.rename(renamed), ['renamed']),
                                             (renamed.unlink, [])):
                        started = time.time_ns()
                        change()
                        os.utime(directory, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                        samples.append(peer.window('list .', {'rc': 0, 'names': name_hash(expected)}, started))
            elif name == 'outer_hardlink_alias':
                watched, other = directory / 'watched', directory / 'unwatched'
                watched.mkdir();other.mkdir()
                path = watched / 'file'
                path.write_bytes(b'A' * 8192)
                alias = other / 'alias'
                os.link(path, alias)
                for i in range(32):
                    peer.ask('read watched/file')
                    started = time.time_ns()
                    alias.write_bytes(bytes([66 + i]) * (4096 + i))
                    os.chmod(alias, 0o600 if i % 2 else 0o640)
                    ended = time.time_ns()
                    samples.append(peer.window('read watched/file', {'rc': 0, 'byte': 66 + i, 'size': 4096 + i,
                                                        'mode': 0o600 if i % 2 else 0o640}, started, ended))
            elif name == 'outer_rename_unwatched':
                watched = directory / 'watched'
                watched.mkdir()
                for i in range(16):
                    other = directory / ('unwatched-' + str(i))
                    other.mkdir()
                    source, destination = other / 'file', watched / 'file'
                    source.write_bytes(bytes([66 + i]) * 8192)
                    assert peer.ask('stat watched/file', ok=False)['errno'] == errno.ENOENT
                    started = time.time_ns()
                    source.rename(destination)
                    ended = time.time_ns()
                    samples.append(peer.window('read watched/file', {'rc': 0, 'byte': 66 + i}, started, ended))
                    started = time.time_ns()
                    destination.rename(source)
                    samples.append(peer.window('stat watched/file', {'rc': -1, 'errno': errno.ENOENT}, started))
            elif name in ('outer_watch_exhaustion', 'outer_notify_overflow'):
                # The daemon control socket is outside the exported tree.
                # Overflow injects an actual IN_Q_OVERFLOW record into the decoder.
                if name == 'outer_notify_overflow':
                    for j in range(256):
                        filename = 'warm-' + str(j)
                        (directory / filename).write_bytes(b'warm')
                        peer.ask('read ' + filename)
                for i in range(32):
                    if mode == 'docker' and name == 'outer_watch_exhaustion':
                        fault_control(c, 1)
                    try:
                        prefix = ''
                        if name == 'outer_watch_exhaustion':
                            prefix = 'unwatched-' + str(i) + '/'
                            (directory / prefix).mkdir()
                        filename = prefix + 'file-' + str(i)
                        path = directory / filename
                        path.write_bytes(b'A' * 8192)
                        peer.ask('hold ' + filename)
                        absent = prefix + 'missing-' + str(i)
                        assert peer.ask('stat ' + absent, ok=False)['errno'] == errno.ENOENT
                        if mode == 'docker' and name == 'outer_notify_overflow':
                            fault_control(c, 3)
                        started = time.time_ns()
                        path.write_bytes(bytes([66 + i]) * (4096 + i))
                        os.chmod(path, 0o600)
                        (directory / absent).write_bytes(b'present')
                        ended = time.time_ns()
                        if mode == 'docker' and name == 'outer_notify_overflow':
                            fault_control(c, 2)
                        samples.append(peer.window('held-found ' + absent, {'rc': 0, 'byte': 66 + i,
                            'size': 4096 + i, 'mode': 0o600, 'found_rc': 0}, started, ended))
                    finally:
                        if mode == 'docker':
                            fault_control(c, 0)
            elif name == 'outer_replace_churn':
                path = directory / 'file'
                path.write_bytes(b'A' * 8192)
                for i in range(32):
                    peer.ask('read file')
                    replacement = directory / 'replacement'
                    replacement.write_bytes(bytes([66 + i]) * (8192 + i))
                    os.chmod(replacement, 0o640)
                    started = time.time_ns()
                    replacement.replace(path)
                    ended = time.time_ns()
                    samples.append(peer.window('read file', {'rc': 0, 'byte': 66 + i, 'size': 8192 + i, 'mode': 0o640}, started, ended))
            elif name == 'outer_before_first_lookup':
                for i in range(32):
                    filename = 'fresh-' + str(i)
                    path = directory / filename
                    path.write_bytes(b'A' * 8192)
                    started = time.time_ns()
                    path.write_bytes(bytes([66 + i]) * (4096 + i))
                    os.chmod(path, 0o600)
                    ended = time.time_ns()
                    samples.append(peer.window('read ' + filename, {'rc': 0, 'byte': 66 + i, 'size': 4096 + i, 'mode': 0o600}, started, ended))
            else:
                raise AssertionError(name)
        check_windows(c, mode, samples)
    assert not bound_failures, bound_failures


def run(c, name):
    share = c.ctx.share / c.id
    share.mkdir(parents=True, exist_ok=True)
    os.chmod(share, 0o755)
    if name == 'mapped_flock_lifetime':
        for mode in ('native', 'docker'):
            path = share / mode
            path.write_bytes(b'A' * 8192)
            with ProbePeer(c, mode, path) as first, ProbePeer(c, mode, path) as second:
                first.ask('open');first.ask('map');first.ask('lock')
                second.ask('open');second.ask('map')
                with path.open('r+b') as outer:
                    blocked = False
                    try:
                        fcntl.flock(outer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        blocked = True
                    assert blocked, 'outer flock ignored guest lock'
                    first.ask('unmap');first.ask('close')
                    fcntl.flock(outer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(outer, fcntl.LOCK_UN)
                assert second.ask('mapped')['byte'] == 65
                second.ask('unmap')
        return
    if name == 'read_buffer':
        return read_buffer(c, share)
    if name == 'hostfs_ceiling':
        return hostfs_ceiling(c, share)
    if name == 'metadata_batch':
        return metadata_batch(c, share)
    if name == 'outer_lookup_race':
        return lookup_race(c, share)
    if name == 'exclusive_create':
        return exclusive_create(c, share)
    if name == 'outer_directory_inotify':
        return directory_events(c, share)
    if name == 'notification_mmap_latency':
        return mmap_delivery(c, share)
    if name in ('outer_readdir_preserved_mtime', 'outer_mtime', 'outer_hardlink_alias',
                'outer_rename_unwatched', 'outer_before_first_lookup', 'outer_replace_churn',
                'outer_watch_exhaustion', 'outer_notify_overflow', 'outer_mapped_remap', 'outer_mapped_scale'):
        return namespace_windows(c, share, name)
    def Peer(mode, path, uid=None, readonly=False):
        return WindowPeer(c, mode, path, uid=uid, readonly=readonly)
    def host_write(path,byte):
        with path.open('r+b', buffering=0) as f:
            f.write(bytes([byte]))
    def expect_byte(peer,op,byte):
        row=peer.ask(op);assert row.get('byte')==byte,row
    def one(mode,name):
        path=share/(mode+'-'+name);path.write_bytes(b'A'*8192);os.chmod(path,0o666)
        uid=1001 if name in ['user_permission_denied','user_create_owner','outer_inotify_to_guest_uid1001','outer_permission_revoked','outer_directory_permission'] else None
        if mode == 'native' and name == 'outer_inotify_to_guest':
            uid = 1001  # Root's inotify-instance quota is shared with unrelated host jobs.
        if name=='user_permission_denied':os.chmod(path,0o600)
        if name=='user_create_owner':
            path.unlink();os.chmod(share,0o777)
        try:
            with Peer(mode,path,uid,readonly=name=='read_only_bind') as peer:
                if name=='user_permission_denied':
                    row=peer.ask('open',ok=False);assert row['rc']==-1 and row['errno']==errno.EACCES,row;return
                if name=='user_create_owner':
                    peer.ask('create');peer.ask('write 66');st=path.stat()
                    assert (st.st_uid,st.st_gid,st.st_mode&0o777)==(1001,1001,0o600),st
                    assert path.read_bytes()==b'B';return
                if name=='read_only_bind':
                    if mode=='native':
                        peer.ask('open-read');row=peer.ask('write 66',ok=False)
                        assert row['rc']==-1 and row['errno']==errno.EBADF,row
                    else:
                        row=peer.ask('open',ok=False);assert row['rc']==-1 and row['errno']==errno.EROFS,row
                        peer.ask('open-read')
                    expect_byte(peer,'read',65);return
                if name=='parallel_creation_masks':peer.ask('parallel-create');return
                if name=='guest_inotify_to_outer_uid1001':
                    with Peer('native',path,uid=1001) as watcher:
                        watcher.ask('watch');peer.ask('open');peer.ask('write 66')
                        c.eventually(lambda: watcher.ask('events')['bytes'] > 0, timeout=3)
                    return
                peer.ask('open')
                if name in ('outer_mmap_window', 'outer_attr_window', 'outer_read_window'):
                    c.write('notification_probe: ' + json.dumps(notification_probe(), sort_keys=True))
                    operation = {'outer_mmap_window': 'mapped', 'outer_attr_window': 'stat',
                                 'outer_read_window': 'read'}[name]
                    if operation == 'mapped':
                        peer.ask('map')
                    field = 'mode' if operation == 'stat' else 'byte'
                    samples = []
                    for i in range(32):
                        peer.ask(operation)
                        expected = (0o640 if i % 2 else 0o600) if operation == 'stat' else 66 + i
                        started = time.time_ns()
                        if operation == 'stat':
                            os.chmod(path, expected)
                        else:
                            with path.open('r+b', buffering=0) as outer:
                                outer.write(bytes([expected]))
                        row = peer.window(operation, {'rc': 0, field: expected}, started)
                        row['mode'] = mode
                        samples.append(row)
                        c.write('coherence_sample: ' + json.dumps(row, sort_keys=True))
                    if operation == 'mapped':
                        peer.ask('unmap')
                    check_windows(c, mode, samples)
                elif name in ('outer_mapped_write', 'outer_mapped_read'):
                    operation = 'mapped' if name == 'outer_mapped_write' else 'read'
                    if operation == 'mapped':
                        peer.ask('map')
                    expect_byte(peer, operation, 65)
                    samples = []
                    with path.open('r+b', buffering=0) as outer:
                        with mmap.mmap(outer.fileno(), 8192, access=mmap.ACCESS_WRITE) as mapped:
                            for i in range(32):
                                # The second half keeps the host page dirty: no msync or new fault.
                                before = path.stat()
                                started = time.time_ns()
                                mapped[0] = 66 + i
                                ended = time.time_ns()
                                after = path.stat()
                                c.write('mmap_store_stamp: ' + json.dumps({'mode': mode, 'round': i,
                                    'flushed': i < 16, 'mtime_changed': before.st_mtime_ns != after.st_mtime_ns,
                                    'ctime_changed': before.st_ctime_ns != after.st_ctime_ns}))
                                samples.append(peer.window(operation, {'rc': 0, 'byte': 66 + i}, started, ended))
                                # Measure visibility before disk writeback, then test a clean page again.
                                if i < 16:
                                    mapped.flush()
                    if operation == 'mapped':
                        peer.ask('unmap')
                    check_windows(c, mode, samples)
                elif name=='read_initial':expect_byte(peer,'read',65)
                elif name in ['outer_write_open_fd','outer_write_reopen']:
                    expect_byte(peer, 'read', 65)
                    samples = []
                    for value in range(66, 98):
                        started = time.time_ns()
                        host_write(path, value)
                        ended = time.time_ns()
                        samples.append(peer.window('reopened' if name == 'outer_write_reopen' else 'read',
                                                   {'rc': 0, 'byte': value}, started, ended))
                    check_windows(c, mode, samples)
                elif name=='outer_truncate_fstat':
                    samples = []
                    for i in range(32):
                        peer.ask('stat')
                        size = 4096 + i
                        started = time.time_ns()
                        os.truncate(path, size)
                        samples.append(peer.window('stat', {'rc': 0, 'size': size}, started))
                    check_windows(c, mode, samples)
                elif name in ('outer_permission_revoked', 'outer_directory_permission'):
                    samples = []
                    target = path if name == 'outer_permission_revoked' else share
                    denied, allowed = (0o600, 0o666) if target == path else (0o700, 0o755)
                    for _ in range(16):
                        peer.ask('reopen')
                        started = time.time_ns()
                        os.chmod(target, denied)
                        ended = time.time_ns()
                        samples.append(peer.window('reopen', {'rc': -1, 'errno': errno.EACCES}, started, ended))
                        expect_byte(peer, 'read', 65)
                        started = time.time_ns()
                        os.chmod(target, allowed)
                        samples.append(peer.window('reopen', {'rc': 0}, started))
                    check_windows(c, mode, samples)
                elif name=='outer_metadata_churn':
                    check_windows(c, mode, metadata_churn(peer, path))
                elif name=='outer_negative_lookup':
                    samples = []
                    for i in range(16):
                        peer.ask('close')
                        started = time.time_ns()
                        path.unlink()
                        ended = time.time_ns()
                        samples.append(peer.window('open', {'rc': -1, 'errno': errno.ENOENT}, started, ended))
                        started = time.time_ns()
                        path.write_bytes(bytes([66 + i]) * 8192)
                        samples.append(peer.window('open', {'rc': 0}, started))
                        expect_byte(peer, 'read', 66 + i)
                    check_windows(c, mode, samples)
                elif name=='outer_chmod_fstat':
                    samples = []
                    for i in range(32):
                        peer.ask('stat')
                        expected = 0o640 if i % 2 else 0o600
                        started = time.time_ns()
                        os.chmod(path, expected)
                        samples.append(peer.window('stat', {'rc': 0, 'mode': expected}, started))
                    check_windows(c, mode, samples)
                elif name=='guest_write_outer_read':
                    peer.ask('write 66');expect_byte(peer, 'read', 66);assert path.read_bytes()[0]==66
                elif name.startswith('mmap_'):
                    peer.ask('map');expect_byte(peer,'mapped',65)
                    if name=='mmap_outer_to_guest':
                        samples = []
                        for value in range(66, 98):
                            started = time.time_ns()
                            host_write(path, value)
                            samples.append(peer.window('mapped', {'rc': 0, 'byte': value}, started))
                        check_windows(c, mode, samples)
                    else:peer.ask('mwrite 66');assert path.read_bytes()[0]==66
                    peer.ask('unmap')
                elif name in ('replace_open_inode', 'unlink_open_inode'):
                    if name == 'replace_open_inode':
                        replacement = path.with_suffix('.new')
                        replacement.write_bytes(b'B' * 8192)
                        started = time.time_ns()
                        replacement.replace(path)
                        ended = time.time_ns()
                        expect_byte(peer, 'read', 65)
                        expected = 66
                    else:
                        path.unlink()
                        peer.ask('write 66');expect_byte(peer, 'read', 66)
                        started = time.time_ns()
                        path.write_bytes(b'C' * 8192)
                        ended = time.time_ns()
                        expected = 67
                    check_windows(c, mode, [peer.window('reopened', {'rc': 0, 'byte': expected}, started, ended)])
                elif name=='xattr_both_directions':
                    peer.ask('setxattr');assert os.getxattr(path,'user.harddind')==b'verified'
                    os.setxattr(path,'user.harddind',b'outer-ok');assert peer.ask('getxattr')['outer_value']
                elif name=='outer_lock_blocks_guest':
                    with path.open('r+b') as f:
                        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        row=peer.ask('lock',ok=False);peer.ask('unlock')
                        assert row['rc']==-1 and row['errno'] in (errno.EAGAIN,errno.EWOULDBLOCK),row
                elif name=='guest_lock_blocks_outer':
                    peer.ask('lock');blocked=False
                    with path.open('r+b') as f:
                        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError:blocked=True
                    peer.ask('unlock');assert blocked,'outer lock ignored guest lock'
                elif name in ['outer_inotify_to_guest','outer_inotify_to_guest_uid1001']:
                    peer.ask('watch')
                    samples = []
                    for value in range(66, 98):
                        peer.ask('events')
                        started = time.time_ns()
                        host_write(path, value)
                        samples.append(peer.window('events', {'rc': 0, 'names': 1}, started))
                    check_windows(c, mode, samples)
        finally:os.chmod(share,0o755)
    if name in ("outer_mmap_window", "outer_attr_window", "outer_read_window", "outer_mapped_write", "outer_mapped_read", "outer_directory_permission",
                "outer_metadata_churn", "outer_replace_churn", "outer_negative_lookup",
                "outer_permission_revoked", "outer_inotify_to_guest", "outer_inotify_to_guest_uid1001",
                "outer_write_open_fd", "outer_write_reopen", "outer_truncate_fstat", "outer_chmod_fstat",
                "mmap_outer_to_guest", "replace_open_inode", "unlink_open_inode"):
        failures = []
        for mode in ("docker", "native"):
            try:
                one(mode, name)
            except AssertionError as error:
                failures.append({'mode': mode, 'error': str(error)})
        assert not failures, failures
    else:
        one("docker", name)
