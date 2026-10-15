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
NAMES = ['read_initial', 'outer_write_open_fd', 'outer_write_reopen', 'outer_truncate_fstat', 'outer_chmod_fstat', 'guest_write_outer_read', 'mmap_outer_to_guest', 'mmap_guest_to_outer', 'replace_open_inode', 'unlink_open_inode', 'xattr_both_directions', 'outer_lock_blocks_guest', 'guest_lock_blocks_outer', 'outer_inotify_to_guest', 'user_permission_denied', 'user_create_owner', 'read_only_bind', 'outer_inotify_to_guest_uid1001', 'guest_inotify_to_outer_uid1001', 'parallel_creation_masks', 'outer_metadata_churn', 'outer_replace_churn', 'outer_negative_lookup', 'outer_mtime', 'outer_permission_revoked', 'outer_mmap_window', 'outer_attr_window', 'outer_read_window', 'outer_mapped_write', 'notification_mmap_latency', 'outer_directory_inotify']


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

def change_window(read, expected, clock=time.monotonic_ns, timeout_ns=3_000_000_000):
    """Bound visibility by RPC observations; preserve the first-access verdict."""
    started, stale, last_stale = clock(), 0, 0
    while True:
        before = clock() - started
        value = read()
        elapsed = clock() - started
        if value == expected:
            return {'first_access': stale == 0, 'stale_reads': stale,
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
                os.pwrite(outer.fileno(), bytes([value]), 0)
                written = time.monotonic_ns()
                receive(value)
                samples.append((time.monotonic_ns() - written) / 1e6)
        assert process.wait(timeout=c.remaining(5)) == 0, process.returncode
        ordered = sorted(samples)
        c.write('mmap_delivery: ' + json.dumps({'mode': mode, 'samples_ms': samples,
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
    if name == 'outer_directory_inotify':
        return directory_events(c, share)
    if name == 'notification_mmap_latency':
        return mmap_delivery(c, share)
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
        uid=1001 if name in ['user_permission_denied','user_create_owner','outer_inotify_to_guest_uid1001','outer_permission_revoked'] else None
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
                        if operation == 'stat':
                            os.chmod(path, expected)
                        else:
                            with path.open('r+b', buffering=0) as outer:
                                outer.write(bytes([expected]))
                        row = change_window(lambda: peer.ask(operation)[field], expected)
                        row['mode'] = mode
                        samples.append(row)
                        c.write('coherence_sample: ' + json.dumps(row, sort_keys=True))
                    windows = sorted(row['visible_by_ms'] for row in samples)
                    misses = sum(not row['first_access'] for row in samples)
                    result = {'mode': mode, 'rounds': len(samples), 'first_access_misses': misses,
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
                            mapped[0] = 66
                            mapped.flush()
                            try:
                                row = change_window(lambda: peer.ask('mapped')['byte'], 66)
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
    if name in ("outer_mmap_window", "outer_attr_window", "outer_read_window", "outer_mapped_write"):
        one("native", name)
    one("docker", name)
