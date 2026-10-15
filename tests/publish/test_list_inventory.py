"""P0: safe complete inventory transport at 4096+ forwards.

Exercises the real network-backend control socket:
  - legacy bare list fail-closes with an explicit pagination error once the
    fixed buffer cannot hold the full array (no truncation, no OOB)
  - paged listing returns every record exactly once with one revision
  - revision mismatch between pages forces an aggregator restart
  - invalid pages are rejected
  - removal frees slots and revisions keep advancing

Designed to run against an ASan/UBSan instrumented backend via
HARDDIND_NETWORK_BACKEND; thousands of listener fds are created, so the test
raises its NOFILE rlimit and skips if the platform refuses.
"""
import json
import os
import resource
import socket
import time
import unittest

from backend import Backend

N_FORWARDS = 4000
PORT_BASE = 12000
# The 32 KiB fixed reply buffer holds at most ~167 maximally-sized records;
# the legacy array must already fail closed well before the table is full.
LEGACY_FAILS_AT = 350


def _raise_nofile(soft):
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, resource.RLIM_INFINITY))
        return True
    except (ValueError, OSError):
        try:
            cur, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, hard), hard))
            return min(soft, hard) >= soft
        except (ValueError, OSError):
            return False


class ListInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Backend process needs ~N_FORWARDS listener fds plus its own pipes.
        if not _raise_nofile(20000):
            raise unittest.SkipTest('cannot raise RLIMIT_NOFILE for inventory test')
        cls.backend = Backend().start()
        cls.addClassCleanup(cls.backend.stop)

    def _key(self, r):
        return (r['family'], r['protocol'], r['address'], r['host_port'],
                r['guest_address'], r['guest_port'])

    def test_01_legacy_list_fails_closed_after_buffer_capacity(self):
        # Fill just past the legacy array capacity using distinct ports.
        n = LEGACY_FAILS_AT
        for i in range(n):
            r = self.backend.command(f'create tcp 127.0.0.1 {PORT_BASE + i} 7777 10.0.2.15')
            self.assertTrue(r.get('ok'), r)
        # Small pages still work.
        page = self.backend.list_page(0, 64)
        self.assertTrue(page['ok'], page)
        self.assertEqual(page['count'], n)
        # Legacy bare list must report an explicit pagination error and never
        # a truncated array.
        legacy = self.backend.command('list')
        self.assertFalse(legacy.get('ok', True), legacy)
        self.assertIn('list', legacy['error'])
        self.assertEqual(legacy['count'], n)

    def test_02_full_inventory_pagination_exact_records(self):
        # Continue from 200 up to N_FORWARDS (4100 total).
        existing = LEGACY_FAILS_AT
        for i in range(existing, N_FORWARDS):
            r = self.backend.command(f'create tcp 127.0.0.1 {PORT_BASE + i} 7777 10.0.2.15')
            self.assertTrue(r.get('ok'), (i, r))
        stats = self.backend.command('stats')
        self.assertEqual(stats['forwards'], N_FORWARDS)

        rows, restarted = self.backend.list_all_paged()
        self.assertFalse(restarted)
        self.assertEqual(len(rows), N_FORWARDS)
        keys = {self._key(r) for r in rows}
        self.assertEqual(len(keys), N_FORWARDS)  # no duplicates
        expected = {
            ('tcp4', 'tcp', '127.0.0.1', PORT_BASE + i, '10.0.2.15', 7777)
            for i in range(N_FORWARDS)
        }
        self.assertEqual(keys, expected)
        # Page boundary: 4100 = 64*64 + 4 -> last page carries 4 rows.
        self.assertEqual(len(self.backend.list_page(N_FORWARDS - 4, 64)['forwards']), 4)
        self.assertEqual(self.backend.list_page(N_FORWARDS, 64)['forwards'], [])

    def test_03_invalid_pages_rejected(self):
        for args in ('list 0 0', 'list 0 65', 'list 99999 64', 'list -1 64', 'list 0 x'):
            r = self.backend.command(args)
            self.assertFalse(r.get('ok', True), args)

    def test_04_revision_invalidation_and_release(self):
        rev0 = self.backend.list_page(0, 1)['revision']
        removed_port = PORT_BASE + 10
        added_port = 49000

        # Mutate exactly between page 0 and page 1: the aggregator must restart
        # and still return a complete consistent snapshot.
        state = {'step': 0}
        def mutate():
            state['step'] += 1
            if state['step'] == 1:
                self.assertTrue(self.backend.command(
                    f'del tcp 127.0.0.1 {removed_port} 7777 10.0.2.15')['ok'])
                self.assertTrue(self.backend.command(
                    f'create tcp 127.0.0.2 {added_port} 7777 10.0.2.15')['ok'])
        rows, restarted = self.backend.list_all_paged(mutate_between=mutate)
        self.assertTrue(restarted)
        self.assertEqual(len(rows), N_FORWARDS)  # one removed, one added
        keys = {self._key(r) for r in rows}
        self.assertNotIn(('tcp4', 'tcp', '127.0.0.1', removed_port, '10.0.2.15', 7777), keys)
        self.assertIn(('tcp4', 'tcp', '127.0.0.2', added_port, '10.0.2.15', 7777), keys)

        rev1 = self.backend.list_page(0, 1)['revision']
        self.assertGreaterEqual(rev1 - rev0, 2)

        # Capacity reclaims: fill back to MAX 4096, then one more must fail
        # with the forward limit (fail-closed, not memory corruption).
        stats = self.backend.command('stats')
        room = 4096 - stats['forwards']
        for i in range(room):
            r = self.backend.command(f'create tcp 127.0.0.3 {20000 + i} 7777 10.0.2.15')
            self.assertTrue(r.get('ok'), r)
        over = self.backend.command('create tcp 127.0.0.3 30000 7777 10.0.2.15')
        self.assertFalse(over.get('ok', True))
        self.assertEqual(over['error'], 'forward limit')
        self.assertEqual(self.backend.command('stats')['forwards'], 4096)


if __name__ == '__main__':
    unittest.main()
