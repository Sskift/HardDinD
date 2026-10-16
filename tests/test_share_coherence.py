"""Exercise bounded metadata checks with fresh and stale peer responses."""
import pathlib
import tempfile
import unittest

from suites.functional.shared_files import expect_metadata, metadata_churn


class MetadataChecks(unittest.TestCase):
    def test_reject_missing_failed_or_stale_stat(self):
        for row in ({}, {'rc': -1, 'size': 4096, 'mode': 0o600},
                    {'rc': 0, 'size': 8192, 'mode': 0o600},
                    {'rc': 0, 'size': 4096, 'mode': 0o640}):
            with self.subTest(row=row), self.assertRaises(AssertionError):
                expect_metadata(row, 4096, 0o600)

    def test_churn_records_each_change_and_rejects_missing_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'file'
            path.write_bytes(b'A' * 8192)

            class Peer:
                calls = 0
                stale = False

                def ask(self, operation):
                    self.calls += 1
                    assert operation == 'stat'
                    st = path.stat()
                    return {'rc': 0, 'size': 8192 if self.stale else st.st_size,
                            'mode': st.st_mode & 0o777}

            peer = Peer()
            samples = metadata_churn(peer, path, rounds=8)
            self.assertEqual(peer.calls, 16)
            peer.calls, peer.stale = 0, True
            with self.assertRaises(AssertionError):
                metadata_churn(peer, path, rounds=8)
            self.assertGreater(peer.calls, 2)
