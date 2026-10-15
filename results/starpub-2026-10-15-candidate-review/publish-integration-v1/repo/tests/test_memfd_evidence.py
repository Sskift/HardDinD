import sys
import pathlib
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from result_checks import memfd_backing_evidence

class MemfdEvidenceTests(unittest.TestCase):
    def test_boot_flags_alone_cannot_certify_backend(self):
        for text in ['', 'memfd_ram=1 memfd_stub=1\n', 'HARDDIND_RAM backend=memfd bytes=4096\nHARDDIND_STUB backend=memfd\n']:
            self.assertFalse(memfd_backing_evidence(text, True, True)['verified'])
    def test_mismatched_or_duplicate_backend_is_rejected(self):
        log='HARDDIND_RAM backend=memfd bytes=2147483648\nHARDDIND_STUB backend=memfd\n'
        self.assertTrue(memfd_backing_evidence(log, True, True)['verified'])
        self.assertFalse(memfd_backing_evidence(log, False, True)['verified'])
        self.assertFalse(memfd_backing_evidence(log+'HARDDIND_STUB backend=file\n', True, True)['verified'])
        self.assertFalse(memfd_backing_evidence(log+'HARDDIND_RAM backend=file bytes=4096\n', True, True)['verified'])
