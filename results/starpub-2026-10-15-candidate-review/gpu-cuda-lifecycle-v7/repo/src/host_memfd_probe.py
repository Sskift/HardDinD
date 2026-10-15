#!/usr/bin/env python3
"""Check executable shared memfd support without a mount or new capability."""
import array
import ctypes
import json
import mmap
import os
import platform
import socket
import sys

report = {'kind': 'host_memfd_capability', 'kernel': platform.release(),
          'architecture': platform.machine(), 'checks': {},
          'performance_verified': False, 'uml_backend_verified': False}
fd = -1
region = parent = child = None
pid = None
try:
    if platform.machine() != 'x86_64':
        raise RuntimeError('machine-code fixture is x86_64 only')
    fd = os.memfd_create('harddind-ram-capability', os.MFD_CLOEXEC)
    os.ftruncate(fd, mmap.PAGESIZE)
    region = mmap.mmap(fd, mmap.PAGESIZE, flags=mmap.MAP_SHARED,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
    region[:6] = bytes.fromhex('b82a000000c3')  # mov eax,42; ret
    region[32] = 17
    def execute(mapping):
        address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
        return ctypes.CFUNCTYPE(ctypes.c_int)(address)()
    report['checks']['parent_execute'] = execute(region) == 42
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    pid = os.fork()
    if pid == 0:
        parent.close(); region.close(); os.close(fd)
        code = 1
        try:
            _, messages, flags, _ = child.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
            if flags & socket.MSG_CTRUNC: raise RuntimeError('truncated FD message')
            descriptors = array.array('i')
            for level, kind, data in messages:
                if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS): descriptors.frombytes(data)
            if len(descriptors) != 1: raise RuntimeError('expected one FD')
            with mmap.mmap(descriptors[0], mmap.PAGESIZE, flags=mmap.MAP_SHARED,
                           prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC) as shared:
                ok = execute(shared) == 42 and shared[32] == 17
                shared[32] = 91
            os.close(descriptors[0]); child.send(b'1' if ok else b'0'); code = 0 if ok else 1
        finally:
            child.close(); os._exit(code)
    child.close(); child = None
    parent.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
    parent.settimeout(10)
    report['checks']['received_fd_execute'] = parent.recv(1) == b'1'
    waited, status = os.waitpid(pid, 0)
    report['checks']['child_exit'] = waited == pid and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    pid = None
    report['checks']['shared_data_visible'] = region[32] == 91
    report['backing'] = os.readlink('/proc/self/fd/' + str(fd))
except Exception as error:
    report['error'] = type(error).__name__ + ': ' + str(error)
finally:
    if pid:
        try: os.kill(pid, 9)
        except ProcessLookupError: pass
        os.waitpid(pid, 0)
    if parent: parent.close()
    if child: child.close()
    if region: region.close()
    if fd >= 0: os.close(fd)
report['passed'] = not report.get('error') and len(report['checks']) == 4 and all(report['checks'].values())
print(json.dumps(report, indent=2))
sys.exit(0 if report['passed'] else 1)
