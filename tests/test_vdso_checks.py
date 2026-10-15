import io
import json
import pathlib
import tarfile
import tempfile
import unittest
from unittest import mock

from pipeline import functional, probe, rseq
from suites.functional import clocks


class ClockResultTests(unittest.TestCase):
    def test_probe_instances_do_not_share_dirty_revision_suffix(self):
        a = probe.instance_path('20261015-120000-abcdef12-dirty-probe', 'A')
        b = probe.instance_path('20261015-121000-abcdef12-dirty-probe', 'A')
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, probe.instance_path('20261015-120000-abcdef12-dirty-probe', 'B'))
        self.assertLessEqual(len(a.name), 30)

    def test_missing_failed_or_skipped_checker_never_passes(self):
        with tempfile.TemporaryDirectory() as root:
            case = mock.Mock(root=pathlib.Path(root))
            case.object.return_value = 'check:local'
            with mock.patch.object(clocks.probe, 'program'), mock.patch.object(clocks.probe, 'image_tar', return_value=b'tar'):
                for row in ({}, {'check': 'summary', 'pass': 2, 'fail': 1},
                            {'check': 'summary', 'pass': 0, 'fail': 0, 'status': 'pass'}):
                    case.output.return_value = json.dumps(row)
                    with self.assertRaises(AssertionError):
                        clocks.run(case, 'clock_ordinary')
                case.output.return_value = json.dumps({'check': 'summary', 'pass': 2, 'fail': 0,
                                                       'not_tested': 1, 'status': 'not_tested'})
                with self.assertRaises(functional.NotTested):
                    clocks.run(case, 'clock_privileged')
                case.output.return_value = json.dumps({'check': 'summary', 'pass': 2, 'fail': 0,
                                                       'not_tested': 0, 'status': 'pass'})
                clocks.run(case, 'clock_ordinary')
                self.assertEqual(case.output.call_args.kwargs['opts'], ())
                clocks.run(case, 'clock_privileged')
                self.assertEqual(case.output.call_args.kwargs['opts'], ('--privileged',))


class RseqArchiveTests(unittest.TestCase):
    def archive(self, path, names):
        with tarfile.open(path, 'w') as tar:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 4
                tar.addfile(info, io.BytesIO(b'data'))

    def test_extracts_selftests_and_required_headers_only(self):
        names = ['tools/testing/selftests/rseq/rseq.c', 'tools/testing/selftests/kselftest.h',
                 'tools/include/linux/compiler.h', 'tools/arch/x86/include/asm/barrier.h',
                 'include/uapi/linux/auxvec.h']
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            self.archive(root / 'source.tar', ['linux-7.2.10/' + p for p in names + ['kernel/time/ntp.c']])
            rseq.extract_sources(root / 'source.tar', root / 'out')
            found = {p.relative_to(root / 'out').as_posix() for p in (root / 'out').rglob('*') if p.is_file()}
            self.assertEqual(found, set(names))

    def test_rejects_absolute_paths_and_parent_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            for name in ('/tools/include/bad.h', 'linux-7.2.10/tools/include/../../../../escape'):
                self.archive(root / 'source.tar', [name])
                with self.assertRaisesRegex(ValueError, 'invalid kernel archive path'):
                    rseq.extract_sources(root / 'source.tar', root / 'out')
