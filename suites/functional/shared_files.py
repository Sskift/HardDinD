"""Shared file effects ported from archive/evidence-20261015."""
import errno
import fcntl
import os

from suites.functional.peer import Peer as ProbePeer

# Guest->outer inotify is checked with an outer UID 1001 watcher only: outer root
# shares its 128 inotify instances with every root process on the Starpub host.
NAMES = ['read_initial', 'outer_write_open_fd', 'outer_write_reopen', 'outer_truncate_fstat', 'outer_chmod_fstat', 'guest_write_outer_read', 'mmap_outer_to_guest', 'mmap_guest_to_outer', 'replace_open_inode', 'unlink_open_inode', 'xattr_both_directions', 'outer_lock_blocks_guest', 'guest_lock_blocks_outer', 'outer_inotify_to_guest', 'user_permission_denied', 'user_create_owner', 'read_only_bind', 'outer_inotify_to_guest_uid1001', 'guest_inotify_to_outer_uid1001', 'parallel_creation_masks']

def run(c, name):
    share = c.ctx.run_dir / "share" / c.id
    share.mkdir(parents=True, exist_ok=True)
    os.chmod(share, 0o755)
    def Peer(mode, path, uid=None, readonly=False):
        return ProbePeer(c, mode, path, uid=uid, readonly=readonly)
    def host_write(path,byte):
        with path.open('r+b',buffering=0) as f:f.write(bytes([byte]));os.fsync(f.fileno())
    def expect_byte(peer,op,byte):
        row=peer.ask(op);assert row.get('byte')==byte,row
    def one(mode,name):
        path=share/(mode+'-'+name);path.write_bytes(b'A'*8192);os.chmod(path,0o666)
        uid=1001 if name in ['user_permission_denied','user_create_owner','outer_inotify_to_guest_uid1001'] else None
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
                if name=='read_initial':expect_byte(peer,'read',65)
                elif name in ['outer_write_open_fd','outer_write_reopen']:
                    expect_byte(peer,'read',65);host_write(path,66)
                    if name=='outer_write_reopen':peer.ask('close');peer.ask('open')
                    expect_byte(peer,'read',66)
                elif name=='outer_truncate_fstat':
                    peer.ask('stat');os.truncate(path,16384);row=peer.ask('stat');assert row['size']==16384,row
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
    one("docker", name)
