"""The measure lock as a writer-preferring readers-writer lock.

flock(2) alone lets a steady stream of shared holders (functional tests,
interactive guests) starve an exclusive benchmark forever. A writer first takes
the `measure-gate` lock, which new shared holders must pass through, then waits
for the current shared holders to drain.
"""
import contextlib
import contextvars
import fcntl
import sys
import time

from pipeline import config

_held = contextvars.ContextVar('measure_lock', default=None)


def _open(name):
    config.LOCKS.mkdir(parents=True, exist_ok=True)
    return open(config.LOCKS / f'{name}.lock', 'a')


def _take(f, flags, label):
    try:
        fcntl.flock(f, flags | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f'[hd {time.strftime("%H:%M:%S")}] waiting for lock {label}', file=sys.stderr, flush=True)
        fcntl.flock(f, flags)


def acquire_shared():
    """Shared measure lock; returns the open file (the lock lives as long as it does)."""
    with _open('measure-gate') as gate:
        _take(gate, fcntl.LOCK_SH, 'measure-gate')
        measure = _open('measure')
        _take(measure, fcntl.LOCK_SH, 'measure (shared)')
    return measure


@contextlib.contextmanager
def measure(shared=False):
    held = _held.get()
    if held is not None:
        if held == 'shared' and not shared:
            raise RuntimeError('cannot upgrade a shared measure lock')
        yield
        return
    token = _held.set('shared' if shared else 'exclusive')
    try:
        with _measure(shared):
            yield
    finally:
        _held.reset(token)


@contextlib.contextmanager
def _measure(shared):
    if shared:
        f = acquire_shared()
        try:
            yield
        finally:
            f.close()
        return
    with _open('measure-gate') as gate:
        _take(gate, fcntl.LOCK_EX, 'measure-gate')
        with _open('measure') as f:
            _take(f, fcntl.LOCK_EX, 'measure (exclusive)')
            fcntl.flock(gate, fcntl.LOCK_UN)
            yield

