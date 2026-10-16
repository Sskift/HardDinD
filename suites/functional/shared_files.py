"""Shared file effects ported from archive/evidence-20261015."""
import errno
import fcntl
import os
import json
import mmap
import time
import struct

from suites.functional.peer import Peer as ProbePeer

# Guest->outer inotify is checked with an outer UID 1001 watcher only: outer root
# shares its 128 inotify instances with every root process on the Starpub host.
NAMES = ['read_initial', 'outer_write_open_fd', 'outer_write_reopen', 'outer_truncate_fstat', 'outer_chmod_fstat', 'guest_write_outer_read', 'mmap_outer_to_guest', 'mmap_guest_to_outer', 'replace_open_inode', 'unlink_open_inode', 'xattr_both_directions', 'outer_lock_blocks_guest', 'guest_lock_blocks_outer', 'outer_inotify_to_guest', 'user_permission_denied', 'user_create_owner', 'read_only_bind', 'outer_inotify_to_guest_uid1001', 'guest_inotify_to_outer_uid1001', 'parallel_creation_masks', 'outer_metadata_churn', 'outer_replace_churn', 'outer_negative_lookup', 'outer_mtime', 'outer_permission_revoked', 'outer_mmap_window', 'outer_attr_window', 'outer_read_window', 'outer_mapped_write', 'notification_mmap_latency', 'outer_directory_inotify', 'outer_readdir_preserved_mtime']


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


LOOKUP_RACER = r'''
#define _DEFAULT_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
static double seconds(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) exit(2);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
int main(int argc, char **argv) {
    if (argc != 3) return 3;
    long timeout_ms = strtol(argv[2], NULL, 10);
    if (timeout_ms <= 0 || timeout_ms > 3000) return 4;
    struct stat st;
    struct pollfd control = {0, POLLIN, 0};
    if (write(1, "R", 1) != 1) return 5;
    for (;;) {
        int ready = poll(&control, 1, 0);
        if (ready < 0) return 6;
        if (ready) {
            char command;
            if (read(0, &command, 1) != 1 || command != 'S') return 7;
            break;
        }
        if (stat(argv[1], &st) && errno != ENOENT) return 8;
    }
    double start = seconds();
    unsigned stale = 0;
    bool visible = false;
    do {
        int rc = stat(argv[1], &st);
        if (rc && errno != ENOENT) return 9;
        if (!rc && st.st_size == 8192) { visible = true; break; }
        stale++;
    } while ((seconds() - start) * 1000 < timeout_ms);
    printf("{\"first_access\":%s,\"visible\":%s,\"stale_reads\":%u,\"probe_ms\":%.6f}\n",
           !stale && visible ? "true" : "false", visible ? "true" : "false",
           stale, (seconds() - start) * 1000);
    return visible ? 0 : 10;
}
'''


def lookup_race(c, share):
    import select
    import shutil
    binary = build_probe(c, 'lookup-racer', LOOKUP_RACER)
    shutil.copy2(binary, share / 'lookup-racer')
    for mode in ('native', 'docker'):
        path = share / ('race-' + mode)
        for repeat in range(8):
            if mode == 'native':
                process = c.spawn(command=[str(binary), str(path), '3000'])
            else:
                name = c.object('container', 'lookup-race-' + str(repeat))
                process = c.spawn('run', '--rm', '-i', '--name', name, '--label', c.label,
                                  '--mount', f'type=bind,src={share},dst=/share',
                                  'hd-functional:local', '/share/lookup-racer',
                                  '/share/' + path.name, '3000')
            assert select.select([process.stdout], [], [], c.remaining(20))[0], 'racer startup timeout'
            assert os.read(process.stdout.fileno(), 1) == b'R', 'racer exited before ready'
            for _ in range(512):
                path.write_bytes(b'old')
                path.unlink()
            started = time.monotonic_ns()
            path.write_bytes(b'new-data' * 1024)
            process.stdin.write(b'S')
            process.stdin.flush()
            assert select.select([process.stdout], [], [], c.remaining(20))[0], 'racer reply timeout'
            row = json.loads(process.stdout.readline())
            row.update(mode=mode, repeat=repeat, visible_by_ms=(time.monotonic_ns() - started) / 1e6)
            c.write('lookup_race: ' + json.dumps(row, sort_keys=True))
            assert process.wait(timeout=c.remaining(5)) == 0 and row['visible'], row
            assert row['first_access'], row
            process.stdin.close()
            process.stdout.close()
            path.unlink()


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


def expect_metadata(row, size, mode):
    assert row.get('rc') == 0 and (row.get('size'), row.get('mode')) == (size, mode), row


def metadata_churn(peer, path, rounds=32):
    """Require the first fstat after each outer change to see the new values."""
    for i in range(rounds):
        size, mode = 4096 + i, (0o640 if i % 2 else 0o600)
        peer.ask('stat')
        os.truncate(path, size)
        os.chmod(path, mode)
        expect_metadata(peer.ask('stat'), size, mode)

def change_window(read, expected, clock=time.monotonic_ns, timeout_ns=3_000_000_000, started_ns=None):
    """Bound visibility by RPC observations; preserve the first-access verdict."""
    started, stale, last_stale = clock() if started_ns is None else started_ns, 0, 0
    while True:
        before = clock() - started
        value = read()
        elapsed = clock() - started
        if value == expected:
            return {'first_access': stale == 0, 'stale_reads': stale,
                    'origin': 'outer_change_start' if started_ns is not None else 'first_read_start',
                    'last_stale_ms': last_stale / 1e6, 'visible_by_ms': elapsed / 1e6}
        stale += 1
        last_stale = before
        if elapsed >= timeout_ns:
            raise AssertionError({'expected': expected, 'actual': value,
                                  'stale_reads': stale, 'elapsed_ms': elapsed / 1e6})


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


MMAP_WAITER = r'''
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) return 3;
    volatile unsigned char *map = mmap(0, 4096, PROT_READ, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED || *map != 65) return 4;
    unsigned char ready = 65;
    if (write(1, &ready, 1) != 1) return 5;
    for (unsigned char value = 66; value < 98; value++) {
        alarm(3);
        while (*map != value) {}
        if (write(1, &value, 1) != 1) return 6;
    }
    alarm(0);
    munmap((void *)map, 4096);
    close(fd);
    return 0;
}
'''



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
    """A guest already polling mmap: no post-write request to hide stale time."""
    import select
    import shutil
    import subprocess
    binary = build_probe(c, 'mmap-waiter', MMAP_WAITER)
    shutil.copy2(binary, share / 'waiter')
    for mode in ('native', 'docker'):
        path = share / ('data-' + mode)
        path.write_bytes(b'A' * 4096)
        if mode == 'native':
            process = c.spawn(command=[str(binary), str(path)], stdin=subprocess.DEVNULL)
        else:
            name = c.object('container', 'mmap-latency')
            process = c.spawn('run', '--rm', '--name', name, '--label', c.label,
                              '--mount', f'type=bind,src={share},dst=/share',
                              'hd-functional:local', '/share/waiter', '/share/' + path.name,
                              stdin=subprocess.DEVNULL)
        def receive(expected):
            assert select.select([process.stdout], [], [], c.remaining(20))[0], 'waiter timeout'
            actual = os.read(process.stdout.fileno(), 1)
            assert actual == bytes([expected]), (mode, expected, actual, process.poll())
        receive(65)
        samples = []
        with path.open('r+b', buffering=0) as outer:
            for value in range(66, 98):
                written = time.monotonic_ns()
                os.pwrite(outer.fileno(), bytes([value]), 0)
                receive(value)
                samples.append((time.monotonic_ns() - written) / 1e6)
        assert process.wait(timeout=c.remaining(5)) == 0, process.returncode
        ordered = sorted(samples)
        c.write('mmap_delivery: ' + json.dumps({'mode': mode, 'origin': 'before_pwrite', 'samples_ms': samples,
                 'p50_visible_by_ms': ordered[16], 'p95_visible_by_ms': ordered[30],
                 'max_visible_by_ms': max(ordered)}, sort_keys=True))


INOTIFY_WAITER = r'''
#define _GNU_SOURCE
#include <grp.h>
#include <poll.h>
#include <sys/inotify.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    if (geteuid() == 0 && (setgroups(0, 0) || setgid(1001) || setuid(1001))) return 3;
    int fd = inotify_init1(IN_CLOEXEC);
    if (fd < 0 || inotify_add_watch(fd, argv[1], IN_CREATE | IN_DELETE | IN_ATTRIB |
                                   IN_MODIFY | IN_MOVED_FROM | IN_MOVED_TO) < 0) return 4;
    if (write(1, "R", 1) != 1) return 5;
    struct pollfd fds[2] = {{fd, POLLIN, 0}, {0, POLLIN, 0}};
    for (;;) {
        if (poll(fds, 2, 20000) <= 0) return 6;
        if (fds[1].revents) return 0;
        if (fds[0].revents) {
            char buffer[4096];
            ssize_t count = read(fd, buffer, sizeof(buffer));
            if (count <= 0) return 7;
            for (ssize_t sent = 0; sent < count;) {
                ssize_t n = write(1, buffer + sent, count - sent);
                if (n <= 0) return 8;
                sent += n;
            }
        }
    }
}
'''


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
    import select
    import shutil
    binary = build_probe(c, 'inotify-waiter', INOTIFY_WAITER)
    shutil.copy2(binary, share / 'waiter')
    for mode in ('native', 'docker'):
        directory = share / mode
        directory.mkdir(mode=0o755)
        if mode == 'native':
            process = c.spawn(command=[str(binary), directory.name], cwd=share)
        else:
            name = c.object('container', 'directory-events')
            process = c.spawn('run', '--rm', '-i', '--name', name, '--label', c.label,
                              '--user', '1001:1001', '--mount', f'type=bind,src={share},dst=/share',
                              'hd-functional:local', '/share/waiter', '/share/docker')
        assert select.select([process.stdout], [], [], c.remaining(20))[0], 'watcher startup timeout'
        assert os.read(process.stdout.fileno(), 1) == b'R', 'watcher failed'
        pending, observed = b'', []
        def await_event(mask, filename, after=0):
            nonlocal pending
            end = time.monotonic() + c.remaining(3)
            while True:
                for row in observed[after:]:
                    if row['mask'] & mask and row['name'] == filename:
                        return row
                left = end - time.monotonic()
                assert left > 0 and select.select([process.stdout], [], [], left)[0], (mode, mask, filename, observed)
                chunk = os.read(process.stdout.fileno(), 4096)
                assert chunk, ('watcher exited', process.poll())
                rows, pending = inotify_records(pending + chunk)
                observed.extend(rows)
                c.write('directory_events: ' + json.dumps({'mode': mode, 'events': rows}, sort_keys=True))
                assert all(not row['mask'] & 0x4000 for row in rows), 'inotify overflow'
        original, renamed = directory / 'original', directory / 'renamed'
        original.write_bytes(b'content')
        await_event(0x100, 'original');await_event(0x2, 'original')
        cursor = len(observed)
        os.chmod(original, 0o640)
        await_event(0x4, 'original', cursor)
        cursor = len(observed)
        original.rename(renamed)
        moved_from = await_event(0x40, 'original', cursor)
        moved_to = await_event(0x80, 'renamed', cursor)
        assert moved_from['cookie'] != 0 and moved_from['cookie'] == moved_to['cookie'], observed
        cursor = len(observed)
        renamed.unlink();await_event(0x200, 'renamed', cursor)
        process.stdin.write(b'q');process.stdin.flush()
        assert process.wait(timeout=c.remaining(5)) == 0, process.returncode


def run(c, name):
    share = c.ctx.share / c.id
    share.mkdir(parents=True, exist_ok=True)
    os.chmod(share, 0o755)
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
    if name == 'outer_readdir_preserved_mtime':
        for mode in ('native', 'docker'):
            directory = share / mode
            directory.mkdir()
            if mode == 'native':
                listing = lambda: sorted(os.listdir(directory))
            else:
                container = c.container('sleep', '120', detached=True,
                                        opts=('--mount', f'type=bind,src={directory},dst=/share'))
                listing = lambda: sorted(c.d('exec', container, 'ls', '-A', '/share').splitlines())
            assert listing() == []
            stamp = directory.stat()
            original, renamed = directory / 'original', directory / 'renamed'
            for change, expected in ((lambda: original.write_bytes(b'content'), ['original']),
                                     (lambda: original.rename(renamed), ['renamed']),
                                     (renamed.unlink, [])):
                change()
                os.utime(directory, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                actual = listing()
                c.write('readdir_visibility: ' + json.dumps({'mode': mode, 'expected': expected, 'actual': actual}))
                assert actual == expected, (mode, expected, actual)
        return
    if name == 'outer_mtime':
        path = share / 'timestamp'
        path.write_bytes(b'timestamp')
        container = c.container('sleep', '120', detached=True,
                                opts=('--mount', f'type=bind,src={share},dst=/share'))
        for seconds in range(1700000000, 1700000008):
            c.d('exec', container, 'stat', '-c', '%Y', '/share/timestamp')
            os.utime(path, ns=(seconds * 1000000000, seconds * 1000000000))
            actual = c.d('exec', container, 'stat', '-c', '%Y', '/share/timestamp').strip()
            assert actual == str(seconds), (seconds, actual)
        return
    def Peer(mode, path, uid=None, readonly=False):
        return ProbePeer(c, mode, path, uid=uid, readonly=readonly)
    def host_write(path,byte):
        with path.open('r+b',buffering=0) as f:f.write(bytes([byte]));os.fsync(f.fileno())
    def expect_byte(peer,op,byte):
        row=peer.ask(op);assert row.get('byte')==byte,row
    def one(mode,name):
        path=share/(mode+'-'+name);path.write_bytes(b'A'*8192);os.chmod(path,0o666)
        uid=1001 if name in ['user_permission_denied','user_create_owner','outer_inotify_to_guest_uid1001','outer_permission_revoked','outer_directory_permission'] else None
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
                        started = time.monotonic_ns()
                        if operation == 'stat':
                            os.chmod(path, expected)
                        else:
                            with path.open('r+b', buffering=0) as outer:
                                outer.write(bytes([expected]))
                        row = change_window(lambda: peer.ask(operation)[field], expected, started_ns=started)
                        row['mode'] = mode
                        samples.append(row)
                        c.write('coherence_sample: ' + json.dumps(row, sort_keys=True))
                    windows = sorted(row['visible_by_ms'] for row in samples)
                    misses = sum(not row['first_access'] for row in samples)
                    result = {'mode': mode, 'origin': 'outer_change_start',
                              'rounds': len(samples), 'first_access_misses': misses,
                              'p50_visible_by_ms': windows[len(windows) // 2],
                              'p95_visible_by_ms': windows[int(len(windows) * .95)],
                              'max_visible_by_ms': max(windows)}
                    c.write('coherence_window: ' + json.dumps(result, sort_keys=True))
                    if operation == 'mapped':
                        peer.ask('unmap')
                    assert misses == 0, result
                elif name == 'outer_mapped_write':
                    peer.ask('map');expect_byte(peer, 'mapped', 65)
                    with path.open('r+b', buffering=0) as outer:
                        with mmap.mmap(outer.fileno(), 8192, access=mmap.ACCESS_WRITE) as mapped:
                            started = time.monotonic_ns()
                            mapped[0] = 66
                            mapped.flush()
                            try:
                                row = change_window(lambda: peer.ask('mapped')['byte'], 66, started_ns=started)
                            except AssertionError as error:
                                c.write('mmap_store_window: ' + json.dumps({'mode': mode, 'error': str(error)}))
                                raise
                            c.write('mmap_store_window: ' + json.dumps({'mode': mode, **row}))
                            assert row['first_access'], row
                    peer.ask('unmap')
                elif name=='read_initial':expect_byte(peer,'read',65)
                elif name in ['outer_write_open_fd','outer_write_reopen']:
                    expect_byte(peer,'read',65);host_write(path,66)
                    if name=='outer_write_reopen':peer.ask('close');peer.ask('open')
                    expect_byte(peer,'read',66)
                elif name=='outer_truncate_fstat':
                    peer.ask('stat');os.truncate(path,16384);row=peer.ask('stat');assert row['size']==16384,row
                elif name=='outer_permission_revoked':
                    for _ in range(16):
                        peer.ask('reopen')
                        os.chmod(path, 0o600)
                        row = peer.ask('reopen', ok=False)
                        assert row['rc'] == -1 and row['errno'] == errno.EACCES, row
                        expect_byte(peer, 'read', 65)
                        os.chmod(path, 0o666)
                        peer.ask('reopen')
                elif name=='outer_directory_permission':
                    for _ in range(16):
                        peer.ask('reopen')
                        os.chmod(share, 0o700)
                        row = peer.ask('reopen', ok=False)
                        assert row['rc'] == -1 and row['errno'] == errno.EACCES, row
                        expect_byte(peer, 'read', 65)
                        os.chmod(share, 0o755)
                        peer.ask('reopen')
                elif name=='outer_metadata_churn':
                    metadata_churn(peer, path)
                elif name=='outer_replace_churn':
                    for i in range(32):
                        byte = 66 + i
                        replacement = path.with_suffix('.new')
                        replacement.write_bytes(bytes([byte]) * (8192 + i))
                        os.chmod(replacement, 0o640)
                        replacement.replace(path)
                        peer.ask('close');peer.ask('open')
                        expect_byte(peer, 'read', byte)
                        expect_metadata(peer.ask('stat'), 8192 + i, 0o640)
                elif name=='outer_negative_lookup':
                    for i in range(16):
                        peer.ask('close');path.unlink()
                        row = peer.ask('open', ok=False)
                        assert row['rc'] == -1 and row['errno'] == errno.ENOENT, row
                        path.write_bytes(bytes([66 + i]) * 8192)
                        peer.ask('open');expect_byte(peer, 'read', 66 + i)
                elif name=='outer_chmod_fstat':
                    peer.ask('stat');os.chmod(path,0o640);row=peer.ask('stat');assert row['mode']==0o640,row
                elif name=='guest_write_outer_read':
                    peer.ask('write 66');assert path.read_bytes()[0]==66
                elif name.startswith('mmap_'):
                    peer.ask('map');expect_byte(peer,'mapped',65)
                    if name=='mmap_outer_to_guest':host_write(path,66);expect_byte(peer,'mapped',66)
                    else:peer.ask('mwrite 66');assert path.read_bytes()[0]==66
                    peer.ask('unmap')
                elif name=='replace_open_inode':
                    replacement=path.with_suffix('.new');replacement.write_bytes(b'B'*8192);replacement.replace(path)
                    expect_byte(peer,'read',65);peer.ask('close');peer.ask('open');expect_byte(peer,'read',66)
                elif name=='unlink_open_inode':
                    path.unlink();peer.ask('write 66');expect_byte(peer,'read',66)
                    path.write_bytes(b'C'*8192);peer.ask('close');peer.ask('open');expect_byte(peer,'read',67)
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
                    peer.ask('watch');host_write(path,66)
                    c.eventually(lambda: peer.ask('events')['bytes'] > 0, timeout=3)
        finally:os.chmod(share,0o755)
    if name in ("outer_mmap_window", "outer_attr_window", "outer_read_window", "outer_mapped_write", "outer_directory_permission"):
        one("native", name)
    one("docker", name)
